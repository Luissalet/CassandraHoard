"""The audit trail: the hub's event bus mirrored into bus_events, searched
and summarised by the tools, incidents reported back to the bus, and the
secrets audit over app folders."""

from __future__ import annotations

import json
import subprocess

import httpx

from cassandra_hoard.audit import BusMirror, audit_folder, secrets_audit
from conftest import Harness, T0


class FakeHub:
    """Answers GET /api/events?since_id= like the Hoard Hub."""

    def __init__(self):
        self.events: list[dict] = []
        self.down = False
        self.reset_ids = False

    def add(self, type_: str, source: str, data: dict, ts: float | None = None) -> dict:
        ev = {"id": len(self.events) + 1, "ts": ts if ts is not None else T0 + len(self.events), "type": type_, "source": source, "data": data}
        self.events.append(ev)
        return ev

    def handler(self, request: httpx.Request) -> httpx.Response:
        if self.down:
            raise httpx.ConnectError("refused", request=request)
        since = int(request.url.params.get("since_id", 0))
        limit = int(request.url.params.get("limit", 100))
        batch = [e for e in self.events if e["id"] > since][:limit]
        return httpx.Response(200, json={"ok": True, "last_id": self.events[-1]["id"] if self.events else 0, "events": batch})


def make_mirror(harness: Harness, hub: FakeHub, emitted: list | None = None) -> BusMirror:
    client = httpx.Client(transport=httpx.MockTransport(hub.handler))
    return BusMirror(harness.services.db, "http://hub.test", clock_fn=harness.clock, client=client,
                     incidents=harness.services.incidents, emit=(lambda t, d: emitted.append((t, d))) if emitted is not None else None)


def test_mirror_stores_and_resumes(tmp_path):
    h = Harness(tmp_path, {"argus": 5183})
    try:
        hub = FakeHub()
        hub.add("agent.call", "vulcan", {"tool": "models_search", "ok": True, "ms": 120, "caller": "hub"})
        hub.add("agent.call", "vulcan", {"tool": "model_listing_set", "ok": False, "ms": 30, "error": "no such model"})
        hub.add("scribe.transcript.done", "scribe", {"session_id": 7})
        m = make_mirror(h, hub)
        assert m.sync_once() == 3 and m.last_hub_id == 3 and m.synced == 3
        assert m.sync_once() == 0  # nothing new
        hub.add("hub.backup.done", "hub", {"snapshot": "x"})
        assert m.sync_once() == 1
        # a new mirror over the same db resumes from the last hub id
        m2 = make_mirror(h, hub)
        assert m2.last_hub_id == 4 and m2.sync_once() == 0
        st = m2.status()
        assert st["stored"] == 4 and st["last_error"] is None
        # search
        calls = m2.search(type="agent.call")
        assert [c["tool"] for c in calls] == ["model_listing_set", "models_search"]
        assert m2.search(type="agent.call", ok=False)[0]["data"]["error"] == "no such model"
        assert m2.search(source="scribe")[0]["type"] == "scribe.transcript.done"
        assert m2.search(query="snapshot")[0]["type"] == "hub.backup.done"
        assert m2.search(type="scribe.*|hub.*") and len(m2.search(type="scribe.*|hub.*")) == 2
        assert [e["type"] for e in m2.around(T0 + 2, minutes=0.005)] == ["scribe.transcript.done"]
        stats = m2.stats()
        assert stats["total"] == 4 and stats["agent_calls"][0]["app"] == "vulcan"
        failed = next(c for c in stats["agent_calls"] if c["tool"] == "model_listing_set")
        assert failed["failed"] == 1 and stats["recent_failures"][0]["tool"] == "model_listing_set"
        assert stats["callers"] == [{"caller": "hub", "count": 1}]
        # hub down: quiet
        hub.down = True
        assert m2.sync_once() == 0 and "unreachable" in m2.status()["last_error"]
        hub.down = False
        assert m2.sync_once() == 0 and m2.status()["last_error"] is None
        assert m2.prune(T0 + 1.5) == 2 and m2.status()["stored"] == 2
    finally:
        h.close()


def test_incidents_are_reported_to_the_bus(tmp_path):
    h = Harness(tmp_path, {"argus": 5183})
    try:
        emitted: list = []
        hub = FakeHub()
        m = make_mirror(h, hub, emitted)
        h.tick(); h.tick()
        m.tick()
        assert emitted == []
        h.net.apps[5183].mode = "down"
        h.tick(); h.tick()
        m.tick()
        opened = [e for e in emitted if e[0] == "cassandra.incident.opened"]
        assert len(opened) == 1 and opened[0][1]["app"] == "argus" and opened[0][1]["to_state"] == "down"
        m.tick()
        assert len([e for e in emitted if e[0] == "cassandra.incident.opened"]) == 1  # not twice
        h.net.apps[5183].mode = "up"
        h.tick(); h.tick()
        m.tick()
        closed = [e for e in emitted if e[0] == "cassandra.incident.closed"]
        assert len(closed) == 1 and closed[0][1]["app"] == "argus" and closed[0][1]["duration_s"] >= 20
    finally:
        h.close()


def test_tools_and_why_down_include_bus_events(tmp_path):
    h = Harness(tmp_path, {"argus": 5183})
    try:
        from cassandra_hoard.agent_tools import call_tool
        hub = FakeHub()
        h.services.bus = make_mirror(h, hub)
        h.tick(); h.tick()
        hub.add("agent.call", "argus", {"tool": "screen_timeline", "ok": True, "ms": 900}, ts=h.clock.now + 5)
        h.net.apps[5183].mode = "down"
        h.tick(); h.tick()
        h.services.bus.sync_once()
        why = call_tool(h.services, "svc_why_down", {"service": "argus"})
        assert why["bus_events_around"] and why["bus_events_around"][0]["tool"] == "screen_timeline"
        found = call_tool(h.services, "audit_search", {"query": "screen", "since": "1h"})
        assert found["count"] == 1 and found["events"][0]["source"] == "argus" and found["bus"]["stored"] == 1
        assert call_tool(h.services, "audit_search", {"failed": True, "since": "1h"})["count"] == 0
        stats = call_tool(h.services, "audit_stats", {"since": "1h"})
        assert stats["total"] == 1 and stats["agent_calls"][0]["tool"] == "screen_timeline"
        empty = call_tool(h.services, "audit_search", {"query": "nothing-like-this", "since": "1h"})
        assert empty["note"].startswith("No event")
    finally:
        h.close()


def test_secrets_audit_over_app_folders(tmp_path):
    app = tmp_path / "Leaky's Hoard"
    (app / "data").mkdir(parents=True)
    (app / "data" / "mcp-token").write_text("tok", encoding="utf-8")
    (app / ".env").write_text("SECRET=1", encoding="utf-8")
    (app / ".gitignore").write_text("node_modules/\n", encoding="utf-8")  # data/ NOT ignored
    subprocess.run(["git", "init", "-q", str(app)], check=True)
    subprocess.run(["git", "-C", str(app), "add", "-A"], check=True)
    line = audit_folder(str(app), "leaky", "Leaky's Hoard", now=T0)
    assert line["token_present"] and line["data_gitignored"] is False and line["git"]
    assert "data/mcp-token" in line["tracked_secret_like"] and ".env" in line["tracked_secret_like"]
    assert any("not in .gitignore" in p for p in line["problems"]) and any("tracked by git" in p for p in line["problems"])
    assert not line["ok"]
    good = tmp_path / "Tidy's Hoard"
    (good / "data").mkdir(parents=True)
    (good / "data" / "mcp-token").write_text("tok", encoding="utf-8")
    (good / "data" / "mcp-token").chmod(0o600)
    (good / ".gitignore").write_text("data/\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q", str(good)], check=True)
    subprocess.run(["git", "-C", str(good), "add", "-A"], check=True)
    tidy = audit_folder(str(good), "tidy", "Tidy's Hoard", now=T0)
    assert tidy["ok"] and tidy["tracked_secret_like"] == [] and tidy["data_gitignored"] is True

    class S:  # what secrets_audit needs from a registry entry
        def __init__(self, id_, name, folder):
            self.id, self.name, self.folder = id_, name, folder

    report = secrets_audit([S("leaky", "Leaky's Hoard", str(app)), S("tidy", "Tidy's Hoard", str(good)), S("ext", "External", "")])
    assert report["checked"] == 2 and report["with_problems"] == 1 and report["summary"][0].startswith("Leaky's Hoard:")
    missing = audit_folder(str(tmp_path / "nope"), "x", "X")
    assert missing["problems"] == ["folder missing"]


def test_audit_routes(client):
    r = client.get("/api/audit", params={"since": "1h"})
    assert r.status_code == 200 and r.json()["events"] == [] and "hub_url" in r.json()["bus"]
    r = client.get("/api/audit/stats")
    assert r.status_code == 200 and r.json()["total"] == 0
    r = client.post("/api/audit/sync")
    assert r.status_code == 200 and "synced" in r.json()
    r = client.get("/api/secrets")
    assert r.status_code == 200 and r.json()["checked"] == 2
    r = client.get("/api/secrets", params={"service": "argus"})
    assert r.status_code == 200 and r.json()["apps"][0]["id"] == "argus"
