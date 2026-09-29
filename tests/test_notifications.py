"""Boop contract and incident lifecycle; all HTTP stays inside MockTransport."""
import json

import httpx
import pytest

from cassandra_hoard.config import Config
from cassandra_hoard.notifications import BoopNotifier
from conftest import Harness


def config(**overrides):
    return Config(**{
        "boop_enabled": True, "boop_url": "https://boop.example.test",
        "boop_api_key": "test-key-only", "public_url": "https://cassandra.example.test",
        **overrides,
    })


def test_disabled_by_default_and_secrets_not_in_repr(monkeypatch):
    for key in ("CASSANDRA_BOOP_ENABLED", "CASSANDRA_BOOP_URL", "CASSANDRA_BOOP_API_KEY", "CASSANDRA_PUBLIC_URL"):
        monkeypatch.delenv(key, raising=False)
    assert not Config.from_env().boop_enabled
    assert "test-key-only" not in repr(config())
    with httpx.Client(transport=httpx.MockTransport(lambda _: pytest.fail("network while disabled"))) as client:
        notifier = BoopNotifier(config(boop_enabled=False), client=client)
        assert not notifier.send("cassandra.incident.opened", {"incident_id": 1})


@pytest.mark.parametrize("override", [
    {"boop_api_key": ""}, {"boop_api_key": "bad\nkey"}, {"boop_api_key": "ñ"},
    {"boop_url": "http://remote.test"}, {"boop_url": "https://user:secret@remote.test"},
    {"public_url": ""}, {"public_url": "javascript:bad"}, {"public_url": "https://a.test?token=secret"},
    {"bus": False},
])
def test_invalid_configuration_is_disabled(override):
    notifier = BoopNotifier(config(**override))
    assert not notifier.enabled
    assert notifier.status()["last_error"]
    assert "secret" not in json.dumps(notifier.status())


def test_environment_configuration(monkeypatch):
    monkeypatch.setenv("CASSANDRA_BOOP_ENABLED", "1")
    monkeypatch.setenv("CASSANDRA_BOOP_URL", "http://127.0.0.1:8080")
    monkeypatch.setenv("CASSANDRA_BOOP_API_KEY", "fake-key")
    monkeypatch.setenv("CASSANDRA_PUBLIC_URL", "https://cassandra.example.test")
    assert BoopNotifier(Config.from_env()).enabled


def test_incident_open_close_grouping_and_no_repeated_polls(tmp_path, monkeypatch):
    h = Harness(tmp_path, {"argus": 5183}, boop_enabled=True,
                boop_url="https://boop.example.test", boop_api_key="test-key-only",
                public_url="https://cassandra.example.test")
    requests = []
    monkeypatch.setattr("cassandra_hoard.hoard_link.family.emit", lambda *args: None)
    with httpx.Client(transport=httpx.MockTransport(lambda r: requests.append(r) or httpx.Response(201))) as client:
        h.services.notifications._client = client
        try:
            h.tick()
            h.net.apps[5183].mode = "down"
            h.tick()
            assert h.services.bus.report_incidents() == 1
            h.services.bus.report_incidents()
            h.net.apps[5183].mode = "up"
            h.tick()
            assert h.services.bus.report_incidents() == 1
            h.services.bus.report_incidents()
            assert len(requests) == 2
            opened, closed = [json.loads(r.content) for r in requests]
            assert opened["fingerprint"] == closed["fingerprint"] == "cassandra:incident:1"
            assert opened["external_id"] != closed["external_id"]
            assert opened["actions"][0]["url"] == "https://cassandra.example.test/#/incidents/1"
            assert opened["level"] == "warning" and closed["level"] == "info"
            assert all(r.url.path == "/api/v1/events" for r in requests)
            assert all(r.headers["Authorization"] == "Bearer test-key-only" for r in requests)
            assert h.services.notifications.status()["accepted"] == 2
        finally:
            h.close()


@pytest.mark.parametrize("outcome", [401, 500, 307, "timeout"])
def test_failure_is_bounded_sanitized_and_never_follows_redirect(outcome):
    requests = []

    def handler(request):
        requests.append(request)
        if outcome == "timeout":
            raise httpx.ReadTimeout("test-key-only private log", request=request)
        return httpx.Response(outcome, text="test-key-only private log", headers={"Location": "https://elsewhere.test"})

    with httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True) as client:
        notifier = BoopNotifier(config(), client=client)
        assert not notifier.send("cassandra.incident.opened", {"incident_id": 4, "detail": "private log"})
        assert len(requests) == 1
        assert notifier.status()["failed"] == 1
        assert "test-key-only" not in json.dumps(notifier.status())
        assert "private log" not in requests[0].content.decode()


def test_unrelated_events_and_invalid_incidents_are_not_forwarded():
    with httpx.Client(transport=httpx.MockTransport(lambda _: pytest.fail("unexpected request"))) as client:
        notifier = BoopNotifier(config(), client=client)
        assert not notifier.send("agent.call", {"incident_id": 1})
        for iid in (None, True, "1", 0, -1):
            assert not notifier.send("cassandra.incident.opened", {"incident_id": iid})
