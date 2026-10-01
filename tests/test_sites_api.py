"""Public sites through the HTTP API, the agent tools and the notification channel."""

from types import SimpleNamespace

import pytest

from cassandra_hoard import sites as S
from cassandra_hoard.agent_tools import AGENT_INSTRUCTIONS
from cassandra_hoard.hoard_link import family
from cassandra_hoard.notifications import BoopNotifier


@pytest.fixture
def bus(monkeypatch):
    """The family bus is captured, never sent: the real hub may be running on this machine."""
    sent = []
    monkeypatch.setattr(family, "emit", lambda type_, data=None, **kw: sent.append((type_, data)) or True)
    return sent


def auth(client):
    return {"Authorization": f"Bearer {client.h.services.token}"}


def call(client, tool, /, **arguments):
    response = client.post("/api/agent/call", json={"name": tool, "arguments": arguments}, headers=auth(client))
    assert response.status_code == 200, response.text
    return response.json()


def site_events(bus):
    return [(t, d) for t, d in bus if t.startswith("cassandra.site.")]


def fail_twice(client, kind="timeout", detail="Timeout: no answer within 15 s"):
    h = client.h
    h.sitenet.fail(kind, detail)
    for _ in range(2):
        h.clock.advance(300)
        client.post("/api/sites/check")


def test_the_list_through_the_api(client, bus):
    assert client.get("/api/sites").json()["sites"] == []
    created = client.post("/api/sites", json={"url": "https://example.com/", "name": "Shop", "keyword": "Welcome"})
    assert created.status_code == 201 and created.json()["created"] is True and created.json()["site"]["state"] == "unknown"
    assert client.h.config.sites_path.exists()
    assert client.post("/api/sites", json={"url": "ftp://x"}).status_code == 400
    assert client.post("/api/sites", json={"url": "https://x.example/", "interval_min": 0}).status_code == 400
    edited = client.post("/api/sites", json={"id": "example.com", "interval_min": 10}).json()
    assert edited["created"] is False and edited["site"]["interval_min"] == 10 and edited["site"]["keyword"] == "Welcome"
    checked = client.post("/api/sites/check").json()
    assert checked["results"] == [{"id": "example.com", "checked": True}]
    again = client.post("/api/sites/check", json={"site": "example.com"}).json()
    assert again["results"][0]["checked"] is False  # not more than once a minute
    assert client.post("/api/sites/check", json={"site": "nope"}).status_code == 404
    body = client.get("/api/sites", params={"hours": 1}).json()
    site = body["sites"][0]
    assert site["state"] == "up" and site["status"] == 200 and site["latency_ms"] == 120.0 and site["tls"]["days_left"] == 80
    assert site["lane"]["segments"][0][2] == "up" and body["summary"]["counts"] == {"up": 1}
    assert client.get("/api/status").json()["sites"]["total"] == 1
    assert client.get("/api/sites/example.com/history", params={"since": "1h"}).json()["state_now"] == "up"
    assert client.get("/api/sites/nope/history").status_code == 404
    assert client.delete("/api/sites/example.com").json() == {"ok": True}
    assert client.delete("/api/sites/example.com").status_code == 404 and client.get("/api/sites").json()["sites"] == []
    assert client.get("/api/sites", headers={"host": "evil.example"}).status_code == 403


def test_a_down_site_is_an_incident_with_its_cause_and_a_bus_event(client, bus):
    client.post("/api/sites", json={"url": "https://example.com/", "name": "Shop"})
    client.post("/api/sites/check")
    fail_twice(client)
    incidents = client.get("/api/incidents").json()["incidents"]
    assert len(incidents) == 1 and incidents[0]["kind"] == "site" and incidents[0]["name"] == "Shop" and incidents[0]["service"] == "site:example.com"
    assert incidents[0]["probable_cause"].startswith("Timeout") and incidents[0]["explanation"][0].startswith("Shop")
    assert client.get("/api/status").json()["incidents"]["open"] == 1
    assert [t for t, _ in site_events(bus)] == [S.EV_DOWN]
    assert site_events(bus)[0][1]["cause_code"] == "timeout" and site_events(bus)[0][1]["name"] == "Shop"
    assert call(client, "svc_incidents", open_only=True)["incidents"][0]["name"] == "Shop"
    client.h.sitenet.respond()
    client.h.clock.advance(300)
    client.post("/api/sites/check")
    assert [t for t, _ in site_events(bus)] == [S.EV_DOWN, S.EV_UP] and client.get("/api/status").json()["incidents"]["open"] == 0
    # the hub is told about it like any other incident, with a kind that no restart rule matches
    client.h.services.bus.report_incidents()
    opened = [d for t, d in bus if t == "cassandra.incident.opened"]
    assert opened and opened[0]["service_kind"] == "site" and opened[0]["app"] == "site:example.com"


def test_tools_sites_watch_status_history(client, bus):
    empty = call(client, "sites_status")
    assert empty["sites"] == [] and "sites_watch" in empty["note"]
    added = call(client, "sites_watch", action="add", url="https://example.com/", name="Shop", keyword="Welcome", interval_min=10)
    assert added["created"] is True and added["site"]["id"] == "example.com" and added["site"]["interval_min"] == 10
    for bad in ({"action": "add"}, {"action": "add", "url": "https://example.com/"}, {"action": "edit", "id": "nope"}, {"action": "remove"},
                {"action": "remove", "id": "nope"}, {"action": "add", "url": "https://x.example/", "interval_min": 0}):
        response = client.post("/api/agent/call", json={"name": "sites_watch", "arguments": bad}, headers=auth(client))
        assert response.status_code in (400, 404), bad
    client.post("/api/sites/check")
    status = call(client, "sites_status")
    assert status["summary"] == {"up": 1} and status["down"] == [] and status["sites"][0]["tls"]["days_left"] == 80
    assert status["sites"][0]["domain"]["name"] == "example.com" and status["sites"][0]["keyword"] == "Welcome"
    assert call(client, "sites_status", site="Shop")["sites"][0]["id"] == "example.com"
    assert client.post("/api/agent/call", json={"name": "sites_status", "arguments": {"site": "zzz"}}, headers=auth(client)).status_code == 404
    edited = call(client, "sites_watch", action="edit", id="example.com", keyword="", enabled=False)
    assert edited["created"] is False and edited["site"]["state"] == "disabled" and edited["site"]["keyword"] is None
    fail_twice(client)  # a paused site is not checked
    assert site_events(bus) == []
    call(client, "sites_watch", action="edit", id="example.com", enabled=True)
    client.h.clock.advance(61)
    fail_twice(client)
    history = call(client, "site_history", site="example.com", since="2h")
    assert history["state_now"] == "down" and history["incidents"][0]["duration"].endswith("(still open)")
    assert [c["to"] for c in history["changes"] if c["kind"] == "state"] == ["down"]
    removed = call(client, "sites_watch", action="remove", id="example.com")
    assert removed["removed"] == "example.com" and call(client, "sites_status")["sites"] == []
    assert call(client, "svc_incidents", since="2h")["count"] == 1  # the history stays


def test_instructions_and_descriptions_say_when_to_use_the_tools(client):
    assert "sites_status" in AGENT_INSTRUCTIONS and "site_history" in AGENT_INSTRUCTIONS and "sites_watch" in AGENT_INSTRUCTIONS
    catalog = {t["name"]: t for t in client.get("/api/agent/tools").json()["tools"]}
    assert "Only when" in catalog["sites_watch"]["description"] or "only when the user asks" in catalog["sites_watch"]["description"]
    assert catalog["sites_watch"]["annotations"]["readOnlyHint"] is False and catalog["sites_status"]["annotations"]["readOnlyHint"] is True
    assert catalog["sites_watch"]["inputSchema"]["properties"]["action"]["enum"] == ["add", "edit", "remove"]
    assert "never restart a service or edit the watch list" in AGENT_INSTRUCTIONS and "sites_watch change" in AGENT_INSTRUCTIONS.replace("svc_restart, svc_watch and ", "")


def test_only_the_expiry_warnings_are_notified_and_they_carry_no_names():
    cfg = SimpleNamespace(boop_url="https://boop.example", boop_api_key="k", public_url="https://cass.example", boop_enabled=True, bus=True)
    sent = []

    class Client:
        def post(self, url, json, headers, timeout, follow_redirects):
            sent.append(json)
            return SimpleNamespace(status_code=202)

    n = BoopNotifier(cfg, client=Client())
    assert n.send(S.EV_DOWN, {"site": "example.com", "name": "Shop", "incident_id": 4}) is False  # the incident notification covers it
    assert n.send(S.EV_UP, {"site": "example.com"}) is False
    assert n.send(S.EV_CERT, {"site": "example.com", "name": "Shop", "url": "https://example.com/", "days_left": 6, "threshold": 7}) is True
    assert n.send(S.EV_DOMAIN, {"domain": "example.com", "sites": ["example.com"], "days_left": 25, "threshold": 30}) is True
    assert n.send(S.EV_CERT, {"site": "example.com", "days_left": "x", "threshold": 7}) is False
    text = str(sent)
    assert "example.com" not in text and "Shop" not in text and "6 day(s)" in sent[0]["body"] and "25 day(s)" in sent[1]["body"]
    assert sent[0]["fingerprint"] != sent[1]["fingerprint"] and sent[0]["external_id"].endswith(":7") and sent[1]["external_id"].endswith(":30")
    assert sent[0]["actions"][0]["url"] == "https://cass.example/#/"
