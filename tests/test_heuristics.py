"""Probable-cause heuristics: machine-wide events, VRAM contention, reboots, Cassandra's own absence, logs."""

from conftest import Harness

from cassandra_hoard.gpu import GpuSample
from cassandra_hoard.incidents import Incidents, explain


def test_three_services_in_the_same_minute_is_machine_wide(harness):
    harness.tick()
    for port in (5183, 5184, 5185):
        harness.net.apps[port].mode = "down"
    harness.tick()
    items = harness.services.incidents.list()
    assert len(items) == 3
    for item in items:
        assert item["probable_cause"].startswith("3 services fell in the same minute")
        assert "machine-wide event" in item["probable_cause"]
        assert len(item["context"]["correlated"]) == 2


def test_correlation_window_is_completed_by_later_polls(harness):
    harness.tick()
    harness.net.apps[5183].mode = "down"
    harness.tick()
    first = harness.services.incidents.open_for("argus")
    assert first["context"]["correlated"] == [] and not first["context_final"]
    harness.net.apps[5184].mode = "down"
    harness.tick(40)
    again = harness.services.incidents.get(first["id"])
    assert [c["service"] for c in again["context"]["correlated"]] == ["borges"]
    assert "Also changed within 3 min: Borges's Hoard up→down" in " ".join(c["text"] for c in again["context"]["causes"])
    harness.tick(200)
    assert harness.services.incidents.get(first["id"])["context_final"] is True


def test_gpu_memory_jump_before_the_fall(harness):
    harness.tick()
    harness.gpu.samples = [GpuSample(0, 2000, 24000, 5.0), GpuSample(1, 11800, 12000, 99.0)]
    harness.tick(20)  # GPU 1 jumps to 98 %
    harness.net.apps[5184].mode = "down"
    harness.tick(40)  # borges falls 40 s later
    incident = harness.services.incidents.open_for("borges")
    assert "GPU 1 memory jumped to 98% 40 s before → VRAM contention" in incident["probable_cause"]
    assert all("GPU 0" not in c["text"] for c in incident["context"]["causes"])


def test_reboot_and_gap_explain_everything(tmp_path):
    h = Harness(tmp_path, {"argus": 5183})
    try:
        h.tick()
        # The PC slept / shut down for two hours and booted again.
        h.boot = h.clock.now + 3000
        h.net.apps[5183].mode = "down"
        h.tick(7200)
        incident = h.services.incidents.open_for("argus")
        codes = [c["code"] for c in incident["context"]["causes"]]
        assert codes[:2] == ["reboot", "gap"]
        assert incident["probable_cause"].startswith("The machine restarted")
        kinds = {r["kind"] for r in h.services.db.query("SELECT kind FROM events WHERE service = 'system'")}
        assert kinds == {"reboot", "gap"}
        from cassandra_hoard import views

        assert views.gaps(h.services.db, h.clock.now - 86400, h.clock.now) == [[h.clock.now - 7200, h.clock.now]]
    finally:
        h.close()


class _NoLogs:
    def tail_for(self, *a):
        return []


def _causes(tail, kind="down", to_state="down", process=None):
    inc = Incidents(db=None, logs=_NoLogs(), name_of=lambda s: s)
    item = {"opened_at": 1000.0, "service": "x", "kind": kind, "to_state": to_state, "detail": "d"}
    return inc.causes(item, {"correlated": [], "gpu": [], "log_tail": tail, "process": process, "system": {}, "port": 1})


def test_log_heuristics():
    line = lambda text, level="info", ts=990.0: {"ts": ts, "level": level, "line": text}  # noqa: E731
    oom = _causes([line("loading"), line("torch.OutOfMemoryError: CUDA out of memory. Tried to allocate 2.00 GiB", "error")])
    assert oom[0]["code"] == "log_oom" and "CUDA out of memory" in oom[0]["text"]
    tb = _causes([line("Traceback (most recent call last):", "error"), line('  File "a.py", line 3'), line("KeyError: 'model'", "error")])
    assert tb[0]["code"] == "log_traceback" and "KeyError: 'model'" in tb[0]["text"]
    killed = _causes([line("working"), line("Killed", "error")])
    assert killed[0]["code"] == "log_killed"
    err = _causes([line("llama_model_load: error: failed to open model", "error")])
    assert err[0]["code"] == "log_error"
    old = _causes([line("Traceback (most recent call last):", "error", ts=0.0)])  # too old to matter
    assert old[0]["code"] == "unknown"
    gone = _causes([], process={"pid": 5, "name": "llama-server", "alive": False})
    assert gone[0]["text"].startswith("The process (pid 5 llama-server) is gone")


def test_explain_reads_like_sentences(harness):
    harness.tick()
    harness.net.apps[5183].mode = "down"
    harness.tick()
    harness.net.apps[5183].mode = "up"
    harness.tick(300)
    item = harness.services.incidents.latest("argus")
    text = explain(item, "Argus's Hoard", 5183, harness.clock.now)
    assert text[0].startswith("Argus's Hoard (port 5183) went down at")
    assert text[1].startswith("It came back at") and "5 min" in text[1]
    assert any(t.startswith("Probable cause:") for t in text)
    assert any(t.startswith("GPUs just before: GPU 0 2.0/23.4 GB") for t in text)
