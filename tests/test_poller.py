"""Poller → samples, events and incidents with context, against fake services that flip up and down."""

from conftest import T0, Harness


def states(h):
    return {sid: cur.state for sid, cur in h.services.poller.current.items()}


def test_first_tick_all_up_and_samples(harness):
    result = harness.tick()
    assert result["services"] == 3 and result["changes"] == []  # first observation is not a change
    assert states(harness) == {"argus": "up", "borges": "up", "scribe": "up"}
    rows = harness.services.db.query("SELECT service, state, pid, cmd_hash FROM samples ORDER BY service")
    assert [(r["service"], r["state"], r["pid"]) for r in rows] == [("argus", "up", 6183), ("borges", "up", 6184), ("scribe", "up", 6185)]
    assert all(r["cmd_hash"] for r in rows)
    # Heartbeat: an unchanged up state is stored again only after sample_every_s (60 s).
    harness.tick(20)
    assert harness.services.db.one("SELECT COUNT(*) AS n FROM samples")["n"] == 3
    harness.tick(45)
    assert harness.services.db.one("SELECT COUNT(*) AS n FROM samples")["n"] == 6


def test_down_opens_incident_with_context_and_up_closes_it(harness, tmp_path):
    log_dir = harness.apps_root / "ArgusHoard" / "data" / "logs"
    log_dir.mkdir(parents=True)
    log_file = log_dir / "argus.log"
    harness.tick()
    stamp = lambda ts: __import__("datetime").datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")  # noqa: E731
    log_file.write_text(
        f"{stamp(T0 + 25)} INFO capture ok\n{stamp(T0 + 30)} ERROR capture failed\nTraceback (most recent call last):\n"
        '  File "x.py", line 1, in <module>\nRuntimeError: display lost\n',
        encoding="utf-8",
    )
    harness.alive[6183] = False
    harness.net.apps[5183].mode = "down"
    result = harness.tick()
    assert result["changes"] == [{"service": "argus", "from": "up", "to": "down", "detail": "nothing listens on port 5183"}]
    incident = harness.services.incidents.open_for("argus")
    assert incident and incident["kind"] == "down" and incident["from_state"] == "up"
    ctx = incident["context"]
    assert ctx["process"]["pid"] == 6183 and ctx["process"]["alive"] is False and ctx["process"]["name"] == "python.exe"
    assert [line["line"] for line in ctx["log_tail"]][-1] == "RuntimeError: display lost"
    assert ctx["gpu"] and ctx["gpu"][0]["last"]["mem_used_mb"] == 2000
    assert "Traceback" in incident["probable_cause"] and "RuntimeError: display lost" in incident["probable_cause"]
    assert any(c["code"] == "gone" for c in ctx["causes"])
    # Still down: no second incident.
    harness.tick()
    assert len(harness.services.incidents.list()) == 1
    harness.net.apps[5183].mode = "up"
    harness.tick()
    closed = harness.services.incidents.get(incident["id"])
    assert closed["closed_at"] == harness.clock.now and closed["actions"][-1]["kind"] == "recovered"
    events = [(r["kind"], r["from_state"], r["to_state"]) for r in harness.services.db.query("SELECT * FROM events WHERE service = 'argus' ORDER BY ts")]
    assert events == [("state", "up", "down"), ("state", "down", "up")]


def test_hung_process_and_foreign_and_degraded(harness):
    harness.tick()
    harness.alive[6184] = True
    # Port still listening, but the app raises: the process is alive and not answering.
    harness.net.apps[5184].mode = "down"
    harness.services.poller.listeners_fn = lambda: {5183: 6183, 5184: 6184, 5185: 6185}
    harness.tick()
    incident = harness.services.incidents.open_for("borges")
    assert incident["to_state"] == "down" and "still alive but does not answer" in incident["probable_cause"]
    harness.net.apps[5185].mode = "foreign"
    harness.tick()
    foreign = harness.services.incidents.open_for("scribe")
    assert foreign["to_state"] == "foreign" and "Another program now answers on port 5185" in foreign["probable_cause"]
    harness.net.apps[5183].mode = "degraded"
    harness.tick()
    assert states(harness)["argus"] == "degraded"
    assert harness.services.incidents.open_for("argus") is None  # degraded is not an incident


def test_never_seen_does_not_open_incidents(tmp_path):
    h = Harness(tmp_path, {"argus": 5183, "vulcan": 5186})
    try:
        h.net.apps[5186].mode = "down"
        h.tick()
        h.tick()
        state = h.services.poller.service_state(h.services.registry.get("vulcan"))
        assert state["state"] == "never_seen" and state["note"] == "never seen running"
        assert h.services.incidents.list() == []
        assert h.services.db.one("SELECT COUNT(*) AS n FROM samples WHERE service = 'vulcan'")["n"] == 1  # down is stored on change only
    finally:
        h.close()


def test_pid_change_is_a_restart_incident(harness):
    harness.tick()
    harness.services.poller.listeners_fn = lambda: {5183: 7777, 5184: 6184, 5185: 6185}
    result = harness.tick()
    assert result["changes"][0]["detail"] == "pid 6183 → 7777"
    items = harness.services.incidents.list(service="argus")
    assert len(items) == 1 and items[0]["kind"] == "restart" and items[0]["closed_at"] is not None
    assert "replaced without downtime" in items[0]["probable_cause"]


def test_state_survives_a_restart_of_cassandra(tmp_path):
    h = Harness(tmp_path, {"argus": 5183})
    h.tick()
    h.services.stop()
    h2 = Harness.__new__(Harness)
    h2.__dict__.update(h.__dict__)
    from cassandra_hoard.services import Services

    h2.services = Services(h.config, clock_fn=h.clock, poller_kwargs=dict(transport=h.net, listeners_fn=h.net.listeners, proc_fn=h.proc,
                           alive_fn=lambda pid, started=None: None, boot_fn=lambda: h.boot, gpu_reader=h.gpu, restart_async=False))
    try:
        assert h2.services.poller.current["argus"].state == "up" and h2.services.poller.current["argus"].ever_up
        h.net.apps[5183].mode = "down"
        h2.tick(30)
        assert h2.services.incidents.open_for("argus") is not None
    finally:
        h2.services.stop()


def test_lanes_and_history_views(harness):
    from cassandra_hoard import views

    harness.tick()
    harness.net.apps[5183].mode = "down"
    harness.tick(100)
    harness.net.apps[5183].mode = "up"
    harness.tick(100)
    now = harness.clock.now
    lanes = views.lanes(harness.services.db, ["argus", "borges"], now - 3600, now, {"argus", "borges"})
    assert [seg[2] for seg in lanes["argus"]] == ["up", "down", "up"]
    assert lanes["argus"][1][1] - lanes["argus"][1][0] == 100
    assert [seg[2] for seg in lanes["borges"]] == ["up"]
    pct = views.uptime_pct(lanes["argus"], now - 3600, now)
    assert 40 < pct < 100
    hist = views.history(harness.services.db, "argus", now - 3600, now)
    assert [(c["from"], c["to"]) for c in hist["changes"]] == [("up", "down"), ("down", "up")]
