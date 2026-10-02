"""Incidents from failed jobs on the mirrored bus, their closing, the "trabajo" kind in the API and the family agenda."""

from __future__ import annotations

import httpx

from cassandra_hoard.audit import BusMirror
from cassandra_hoard.job_incidents import EXPIRE_S
from conftest import Harness, T0
from test_audit import FakeHub


def mirror(h: Harness, hub: FakeHub, emitted: list | None = None) -> BusMirror:
    client = httpx.Client(transport=httpx.MockTransport(hub.handler))
    return BusMirror(h.services.db, "http://hub.test", clock_fn=h.clock, client=client, incidents=h.services.incidents,
                     emit=(lambda t, d: emitted.append((t, d))) if emitted is not None else None, job_incidents=h.services.job_incidents,
                     service_kind=lambda sid: (h.services.registry.get(sid).kind if h.services.registry.get(sid) else None))


def job_events(hub: FakeHub, app: str, job_id: str, *, kind: str = "run", title: str = "Quick run", fail: str = "", t: float | None = None) -> None:
    base = {"job_id": job_id, "title": title, "kind": kind}
    t0 = T0 if t is None else t
    hub.add(f"{app}.job.queued", app, {**base, "progress": 0.0}, t0)
    hub.add(f"{app}.job.started", app, {**base, "progress": 0.0}, t0 + 1)
    hub.add(f"{app}.job.progress", app, {**base, "progress": 0.4, "gpu": 2}, t0 + 2)
    if fail:
        hub.add(f"{app}.job.failed", app, {**base, "error": fail}, t0 + 3)
        hub.add("work.failed", "hub", {"app": app, "job_id": job_id, "title": title, "error": fail}, t0 + 3)   # the hub's own echo
    else:
        hub.add(f"{app}.job.done", app, {**base, "progress": 1.0}, t0 + 3)


def open_jobs(h: Harness):
    return [i for i in h.services.incidents.list(None, None, None, True, 50) if i["kind"] == "job"]


def test_a_failed_job_opens_one_incident_with_the_error_and_the_events_of_the_job(tmp_path):
    h = Harness(tmp_path, {"argus": 5183})
    try:
        hub = FakeHub()
        job_events(hub, "galton", "r_1", fail="llama-server did not start")
        mirror(h, hub).sync_once()
        items = open_jobs(h)
        assert len(items) == 1                          # job.failed and work.failed are one failure
        inc = items[0]
        assert inc["service"] == "galton" and inc["kind"] == "job" and inc["opened_at"] == T0 + 3 and inc["open"]
        assert "llama-server did not start" in inc["probable_cause"] and "Quick run" in inc["probable_cause"]
        job = inc["context"]["job"]
        assert job["job_id"] == "r_1" and job["kind"] == "run" and job["error"] == "llama-server did not start" and job["failures"] == 1
        assert [e["type"].split(".")[-1] for e in inc["context"]["job_events"]] == ["queued", "started", "progress", "failed"]
        assert inc["context"]["causes"][0]["code"] == "job_failed" and inc["context_final"]
    finally:
        h.close()


def test_a_failure_known_only_from_work_failed_still_opens_with_the_kind_from_the_bus(tmp_path):
    h = Harness(tmp_path)
    try:
        hub = FakeHub()
        hub.add("pygmalion.job.queued", "pygmalion", {"job_id": "j9", "title": "Merge", "kind": "merge"}, T0)
        hub.add("work.failed", "hub", {"app": "pygmalion", "job_id": "j9", "title": "Merge", "error": "no space left"}, T0 + 5)
        mirror(h, hub).sync_once()
        (inc,) = open_jobs(h)
        assert inc["service"] == "pygmalion" and inc["context"]["job"]["kind"] == "merge"
    finally:
        h.close()


def test_a_second_failure_of_the_same_app_and_kind_updates_the_open_incident(tmp_path):
    h = Harness(tmp_path)
    try:
        hub = FakeHub()
        job_events(hub, "galton", "r_1", fail="first error")
        job_events(hub, "galton", "r_2", fail="second error", t=T0 + 60)
        job_events(hub, "galton", "r_3", kind="judge", title="Judge", fail="other kind", t=T0 + 120)
        mirror(h, hub).sync_once()
        items = sorted(open_jobs(h), key=lambda i: i["id"])
        assert len(items) == 2
        run, judge = items
        assert run["context"]["job"]["failures"] == 2 and "second error" in run["probable_cause"] and run["context"]["job"]["job_id"] == "r_2"
        assert [a["kind"] for a in run["actions"]] == ["failed_again"]
        assert judge["context"]["job"]["kind"] == "judge"
    finally:
        h.close()


def test_a_later_job_done_of_the_same_app_and_kind_closes_it(tmp_path):
    h = Harness(tmp_path)
    try:
        hub = FakeHub()
        m = mirror(h, hub)
        job_events(hub, "galton", "r_1", fail="boom")
        job_events(hub, "galton", "r_2", kind="judge", title="Judge", fail="boom judge", t=T0 + 10)
        m.sync_once()
        assert len(open_jobs(h)) == 2
        job_events(hub, "lumiere", "x1", kind="run", t=T0 + 20)                      # another app: nothing
        job_events(hub, "galton", "r_5", kind="download", t=T0 + 30)                 # same app, another kind: nothing
        m.sync_once()
        assert len(open_jobs(h)) == 2
        job_events(hub, "galton", "r_6", kind="run", t=T0 + 40)                      # same app and kind: closes the run incident only
        m.sync_once()
        left = open_jobs(h)
        assert [i["context"]["job"]["kind"] for i in left] == ["judge"]
        closed = h.services.incidents.list(None, None, "galton", False, 10)
        run = next(i for i in closed if i["context"]["job"]["kind"] == "run")
        assert run["closed_at"] == T0 + 43 and run["actions"][-1]["kind"] == "job_done"
    finally:
        h.close()


def test_a_done_event_older_than_the_failure_does_not_close_it(tmp_path):
    h = Harness(tmp_path)
    try:
        hub = FakeHub()
        job_events(hub, "galton", "r_1", fail="boom", t=T0 + 100)
        job_events(hub, "galton", "r_0", t=T0)           # finished before the failure (arrives after it in the log)
        mirror(h, hub).sync_once()
        assert len(open_jobs(h)) == 1
    finally:
        h.close()


def test_an_unclosed_job_incident_expires_after_24_hours_without_a_new_failure(tmp_path):
    h = Harness(tmp_path)
    try:
        hub = FakeHub()
        m = mirror(h, hub)
        job_events(hub, "galton", "r_1", fail="boom", t=h.clock.now - 10)
        m.tick()
        assert len(open_jobs(h)) == 1
        h.clock.advance(EXPIRE_S - 100)
        m.tick()
        assert len(open_jobs(h)) == 1
        h.clock.advance(200)
        m.tick()
        assert open_jobs(h) == []
        (inc,) = [i for i in h.services.incidents.list(None, None, "galton", False, 5)]
        assert inc["actions"][-1]["kind"] == "expired" and inc["closed_at"] is not None
    finally:
        h.close()


def test_history_older_than_a_day_opens_nothing(tmp_path):
    h = Harness(tmp_path)
    try:
        hub = FakeHub()
        job_events(hub, "galton", "r_old", fail="ancient", t=h.clock.now - EXPIRE_S - 500)
        mirror(h, hub).sync_once()
        assert h.services.incidents.list(None, None, None, False, 10) == []
    finally:
        h.close()


def test_job_incidents_do_not_mix_with_the_service_going_down_and_up(tmp_path):
    h = Harness(tmp_path, {"argus": 5183})
    try:
        hub = FakeHub()
        m = mirror(h, hub)
        h.tick(); h.tick()
        job_events(hub, "argus", "j1", kind="index", title="Reindex", fail="disk full", t=h.clock.now)
        m.sync_once()
        h.net.apps[5183].mode = "down"
        h.tick(); h.tick()
        assert h.services.incidents.open_for("argus")["kind"] == "down"                  # the service incident, not the job one
        h.net.apps[5183].mode = "up"
        h.tick(); h.tick()
        remaining = open_jobs(h)
        assert len(remaining) == 1 and remaining[0]["service"] == "argus"                  # recovering did not close the job incident
        assert h.services.incidents.open_for("argus") is None
    finally:
        h.close()


def test_the_hub_hears_about_it_as_service_kind_job(tmp_path):
    h = Harness(tmp_path, {"argus": 5183})
    try:
        emitted: list = []
        hub = FakeHub()
        m = mirror(h, hub, emitted)
        job_events(hub, "argus", "j1", kind="index", fail="disk full", t=h.clock.now)
        m.tick()
        opened = [d for t, d in emitted if t == "cassandra.incident.opened"]
        assert len(opened) == 1 and opened[0]["kind"] == "job" and opened[0]["service_kind"] == "job" and opened[0]["app"] == "argus"
        assert opened[0]["to_state"] == "failed"          # not "down": the hub rule that starts an app again must not fire
        m.tick()
        assert len([1 for t, _ in emitted if t == "cassandra.incident.opened"]) == 1
        job_events(hub, "argus", "j2", kind="index", t=h.clock.now + 5)
        m.tick()
        closed = [d for t, d in emitted if t == "cassandra.incident.closed"]
        assert len(closed) == 1 and closed[0]["app"] == "argus"
    finally:
        h.close()


def test_the_feature_can_be_switched_off(tmp_path):
    h = Harness(tmp_path, job_incidents=False)
    try:
        hub = FakeHub()
        client = httpx.Client(transport=httpx.MockTransport(hub.handler))
        job_events(hub, "galton", "r_1", fail="boom")
        m = BusMirror(h.services.db, "http://hub.test", clock_fn=h.clock, client=client,
                      job_incidents=h.services.job_incidents if h.config.job_incidents else None)
        m.sync_once()
        assert open_jobs(h) == []
        assert h.services.bus._job_incidents is None
    finally:
        h.close()


# ------------------------------------------------------------------ API and UI data
def test_the_incident_api_shows_job_incidents_with_an_explanation(client):
    h = client.h
    hub = FakeHub()
    job_events(hub, "galton", "r_1", fail="llama-server did not start", t=h.clock.now - 30)
    m = mirror(h, hub)
    m.sync_once()
    body = client.get("/api/incidents", params={"open_only": "true"}).json()
    (item,) = [i for i in body["incidents"] if i["kind"] == "job"]
    assert item["name"] == "galton" and item["open"] and item["context"]["job"]["title"] == "Quick run"
    text = " ".join(item["explanation"])
    assert "run job" in text and "llama-server did not start" in text and "Still open" in text and "queued → started → progress 40% → failed" in text
    one = client.get(f"/api/incidents/{item['id']}").json()
    assert one["kind"] == "job" and one["explanation"]
    jobs = client.get("/api/incidents", params={"service": "argus"}).json()["incidents"]
    assert all(i["kind"] != "job" for i in jobs)
    job_events(hub, "galton", "r_2", t=h.clock.now + 10)
    h.clock.advance(60)
    m.sync_once()                                   # the done event closes it and the explanation says how
    closed = client.get(f"/api/incidents/{item['id']}").json()
    assert not closed["open"] and "a later job of the same kind finished" in " ".join(closed["explanation"])


# ------------------------------------------------------------------ the agenda
WINDOW = {"from": "2026-09-01", "to": "2026-09-30"}          # around the tests' fixed clock (2026-09-21)


def agenda(client, **params):
    params = {**WINDOW, **params}
    token = (client.h.config.data_dir / "mcp-token").read_text(encoding="utf-8").strip()
    return client.get("/api/family/agenda", params=params, headers={"Authorization": f"Bearer {token}"})


def test_the_agenda_lists_open_incidents_only_and_needs_the_token(client):
    h = client.h
    assert client.get("/api/family/agenda").status_code == 401
    assert agenda(client).json()["items"] == []
    h.tick(); h.tick()
    h.net.apps[5183].mode = "down"
    h.tick(); h.tick()
    hub = FakeHub()
    job_events(hub, "galton", "r_1", fail="boom", t=h.clock.now - 5)
    m = mirror(h, hub)
    m.sync_once()
    body = agenda(client).json()
    assert body["ok"] is True and len(body["items"]) == 2
    by_kind = {("job" if "job" in i["title"] else "down"): i for i in body["items"]}
    down, job = by_kind["down"], by_kind["job"]
    assert down["kind"] == job["kind"] == "incident" and down["priority"] == job["priority"] == "high"
    assert down["title"].lower().startswith("argus") and "is down" in down["title"]
    assert job["title"].startswith("galton: job failed") and "Quick run" in job["title"] and "boom" in job["detail"]
    assert job["url"].endswith("/#/incidents/" + job["id"].rsplit(":", 1)[1]) and job["id"].startswith("cassandra:incident:")
    assert job["start"][:10] and job["end"] and not job["all_day"]
    # a window entirely in the future still shows an open incident? No: it lasts until now, so it does not overlap it
    assert agenda(client, **{"from": "2999-01-01", "to": "2999-01-31"}).json()["items"] == []
    # an incident still open but older than a day is a state, not news: the agenda leaves it to Cassandra's own page
    h.clock.advance(25 * 3600)
    assert agenda(client).json()["items"] == []
    h.clock.advance(-25 * 3600)
    # recovered/closed incidents are history
    h.net.apps[5183].mode = "up"
    h.tick(); h.tick()
    job_events(hub, "galton", "r_2", t=h.clock.now + 1)
    h.clock.advance(5)
    m.sync_once()
    assert agenda(client).json()["items"] == []


def test_the_manifest_declares_the_agenda():
    import json
    from pathlib import Path
    manifest = json.loads((Path(__file__).resolve().parent.parent / "faustus-plugin.json").read_text(encoding="utf-8"))
    assert manifest["x-family"] == {"agenda": True}
