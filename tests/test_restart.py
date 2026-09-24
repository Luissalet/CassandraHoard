"""Restart policies: opt-in, rate limit per hour, manual always allowed, hub route, foreign never restarted."""

import json

import httpx
import pytest


def fall_and_recover(h, port=5183, down_s=20.0, up_s=20.0):
    h.net.apps[port].mode = "down"
    h.tick(down_s)
    h.net.apps[port].mode = "up"
    h.tick(up_s)


def test_no_policy_no_restart(harness):
    harness.tick()
    fall_and_recover(harness)
    assert harness.spawned == [] and harness.services.restarter.history() == []


def test_auto_restart_rate_limited_per_hour(harness):
    registry = harness.services.registry
    registry.set_policy("argus", {"enabled": True, "max_per_hour": 2, "cmd": ["python", "-m", "argus"], "cwd": str(harness.tmp)})
    saved = json.loads(harness.config.services_path.read_text(encoding="utf-8"))
    assert saved["policies"]["argus"]["enabled"] is True and saved["policies"]["argus"]["max_per_hour"] == 2
    harness.tick()
    for _ in range(3):
        fall_and_recover(harness)
    auto = [r for r in harness.services.restarter.history("argus") if r["trigger"] == "auto"]
    assert len(auto) == 2 and all(r["ok"] and r["method"] == "cmd" for r in auto)
    assert len(harness.spawned) == 2 and harness.spawned[0][0] == ["python", "-m", "argus"]
    assert harness.spawned[0][2].endswith("argus.log")
    third = harness.services.incidents.latest("argus")
    assert third["actions"][0]["kind"] == "restart_skipped" and "rate limit: 2 automatic restarts" in third["actions"][0]["detail"]
    first = harness.services.incidents.list(service="argus")[-1]
    assert first["actions"][0]["kind"] == "restart" and first["actions"][0]["trigger"] == "auto"
    # Manual restarts are always allowed, and do not count against the automatic limit.
    service = registry.get("argus")
    result = harness.services.restarter.restart(service, "manual")
    assert result["ok"] and result["method"] == "cmd"
    # An hour later the budget is back.
    harness.tick(3600)
    fall_and_recover(harness)
    assert len([r for r in harness.services.restarter.history("argus") if r["trigger"] == "auto"]) == 3


def test_master_switch_and_foreign(harness):
    harness.services.registry.set_policy("borges", {"enabled": True, "cmd": "start-borges"})
    harness.services.config.auto_restart = False
    harness.tick()
    fall_and_recover(harness, 5184)
    assert harness.spawned == []
    assert "CASSANDRA_AUTO_RESTART=0" in harness.services.incidents.latest("borges")["actions"][0]["detail"]
    harness.services.config.auto_restart = True
    harness.net.apps[5184].mode = "foreign"
    harness.tick()
    assert harness.spawned == []
    assert "another program holds the port" in harness.services.incidents.latest("borges")["actions"][0]["detail"]


def test_hub_route_and_launch_hint(harness):
    calls = []

    def hub(request: httpx.Request):
        calls.append((request.method, request.url.path))
        if request.url.path == "/api/health":
            return httpx.Response(200, json={"service": "hoard-hub"})
        return httpx.Response(200, json={"ok": True, "detail": "started"})

    restarter = harness.services.restarter
    restarter.hub_client = httpx.Client(transport=httpx.MockTransport(hub))
    service = harness.services.registry.get("scribe")
    assert restarter.method_for(service) == "hub"
    result = restarter.restart(service, "manual", running=False)
    assert result["ok"] and result["method"] == "hub" and ("POST", "/api/apps/scribe/start") in calls
    restarter.restart(service, "manual", running=True)
    assert ("POST", "/api/apps/scribe/restart") in calls
    # Without the hub, the manifest's launch hint ({FAUSTUS_PYTHON} -m scribe in the app folder).
    restarter.hub_client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(404)))
    assert restarter.method_for(service) == "launch"
    result = restarter.restart(service, "manual")
    assert result["ok"] and harness.spawned[-1][0][1:] == ["-m", "scribe"] and harness.spawned[-1][1].endswith("ScribeHoard")


def test_cannot_restart_without_a_way(tmp_path):
    from conftest import Harness

    h = Harness(tmp_path, {})
    try:
        entry = h.services.registry.upsert_user({"id": "custom", "url": "http://127.0.0.1:9999", "health_path": "/ping"})
        result = h.services.restarter.restart(entry, "manual")
        assert result["ok"] is False and "cannot be restarted" in result["error"]
    finally:
        h.close()


def test_policy_validation(harness):
    with pytest.raises(LookupError):
        harness.services.registry.set_policy("nope", {"enabled": True})
    updated = harness.services.registry.set_policy("argus", {"max_per_hour": 500})
    assert updated.restart.max_per_hour == 60  # clamped to the allowed range
