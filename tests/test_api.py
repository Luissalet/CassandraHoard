"""HTTP API and the agent tool catalogue through TestClient (fake network and clock)."""

import re


def auth(client):
    return {"Authorization": f"Bearer {client.h.services.token}"}


def call(client, name, **arguments):
    response = client.post("/api/agent/call", json={"name": name, "arguments": arguments}, headers=auth(client))
    assert response.status_code == 200, response.text
    return response.json()


def test_health_status_and_local_only(client):
    assert client.get("/api/health").json() == {"service": "cassandra-hoard", "version": "0.1.0", "dataDirConfigured": True}
    assert client.get("/api/health", headers={"host": "evil.example"}).status_code == 403
    assert client.get("/api/nope").status_code == 404
    client.h.tick()
    status = client.get("/api/status").json()
    assert status["service"] == "cassandra-hoard" and status["counts"] == {"up": 2} and status["services_total"] == 2
    assert status["poller"]["ticks"] == 1 and status["gpu"]["available"] and status["system"]["boot_time"]
    assert (client.h.config.data_dir / "url").read_text() == "http://127.0.0.1:5190"
    assert (client.h.config.data_dir / "mcp-token").read_text() == client.h.services.token


def test_services_lanes_incidents_logs_gpu(client):
    h = client.h
    h.tick()
    h.net.apps[5183].mode = "down"
    h.tick()
    h.net.apps[5183].mode = "up"
    h.tick(120)
    services = client.get("/api/services").json()
    assert [s["id"] for s in services["services"]] == ["argus", "borges"] and services["system"]["id"] == "system"
    one = client.get("/api/services/argus").json()
    assert one["state"] == "up" and one["restart_method"] == "launch" and one["launch"]["argv"] == ["-m", "argus"]
    assert client.get("/api/services/nope").status_code == 404
    lanes = client.get("/api/lanes", params={"hours": 1}).json()
    argus = next(lane for lane in lanes["lanes"] if lane["id"] == "argus")
    assert [seg[2] for seg in argus["segments"]] == ["up", "down", "up"] and argus["uptime_pct"] < 100
    incidents = client.get("/api/incidents").json()["incidents"]
    assert len(incidents) == 1 and incidents[0]["name"] == "Argus's Hoard" and incidents[0]["explanation"][0].startswith("Argus's Hoard went down")
    detail = client.get(f"/api/incidents/{incidents[0]['id']}").json()
    assert "(port 5183)" in detail["explanation"][0] and detail["context"]["gpu"]
    assert client.get("/api/incidents/999").status_code == 404
    assert client.get("/api/incidents", params={"since": "garbage"}).status_code == 400
    history = client.get("/api/services/argus/history", params={"since": "1h"}).json()
    assert [c["to"] for c in history["changes"]] == ["down", "up"]
    gpu = client.get("/api/gpu", params={"since": "1h"}).json()
    assert [g["gpu"] for g in gpu["gpus"]] == [0, 1]
    assert client.get("/api/logs", params={"q": "x"}).json()["lines"] == []
    assert client.get("/api/logs", params={"level": "fatal"}).status_code == 400
    assert "sources" in client.get("/api/logs/sources").json()
    polled = client.post("/api/poll").json()
    assert polled["ok"] is True


def test_settings_watch_policy_restart(client):
    h = client.h
    created = client.post("/api/services", json={"id": "whisper", "url": "http://127.0.0.1:9000", "health_path": "/health"})
    assert created.status_code == 201 and created.json()["kind"] == "user"
    assert client.post("/api/services", json={"id": "Bad Id!"}).status_code == 400
    settings = client.get("/api/settings").json()
    assert settings["user_services"][0]["id"] == "whisper" and settings["config"]["poll_s"] == 20.0
    policy = client.put("/api/services/argus/policy", json={"enabled": True, "max_per_hour": 1}).json()
    assert policy["restart"]["enabled"] is True and policy["method"] == "launch"
    cleared = client.put("/api/services/argus/policy", json={"cmd": ""}).json()
    assert cleared["restart"]["cmd"] is None and cleared["restart"]["enabled"] is True
    h.tick()
    restarted = client.post("/api/services/argus/restart").json()
    assert restarted["ok"] and restarted["method"] == "launch"
    assert client.post("/api/services/whisper/restart").status_code == 409  # no way to start it
    assert client.delete("/api/services/whisper").json() == {"ok": True}
    assert client.delete("/api/services/whisper").status_code == 404


def test_agent_catalogue_schema(client):
    catalog = client.get("/api/agent/tools").json()
    names = [t["name"] for t in catalog["tools"]]
    assert names == ["svc_status", "svc_incidents", "svc_why_down", "logs_search", "gpu_timeline", "svc_history", "svc_restart", "svc_watch"]
    assert "never restart" in catalog["instructions"] and "04:00" in catalog["instructions"]
    for tool in catalog["tools"]:
        first = tool["description"].split("\n", 1)[0]
        assert len(first) <= 110, (tool["name"], len(first))
        assert " / " in first and re.search(r"[áéíóúñ¿]", first + tool["description"]), tool["name"]  # English / Spanish
        assert "Sinónimos:" in tool["description"] and tool["inputSchema"]["type"] == "object"
        read_only = tool["name"] not in ("svc_restart", "svc_watch")
        assert tool["annotations"]["readOnlyHint"] is read_only
    assert client.post("/api/agent/call", json={"name": "svc_status"}).status_code == 401
    assert client.post("/api/agent/call", json={"name": "svc_status"}, headers={"Authorization": "Bearer nope"}).status_code == 401
    assert client.post("/api/agent/call", json={"name": "nope"}, headers=auth(client)).status_code == 404
    assert client.post("/api/agent/call", json={"name": "svc_why_down", "arguments": {}}, headers=auth(client)).status_code == 400
    assert client.post("/api/agent/call", json={"name": "svc_why_down", "arguments": {"service": "zzz"}}, headers=auth(client)).status_code == 404
    assert client.post("/api/agent/call", json={"name": "svc_incidents", "arguments": {"at": "99:99"}}, headers=auth(client)).status_code == 400


def test_agent_tools_answer_the_questions(client):
    h = client.h
    h.tick()
    before = call(client, "svc_status")
    assert before["summary"] == {"up": 2} and before["down"] == []
    h.net.apps[5184].mode = "down"
    h.tick()
    down_at = h.clock.now
    h.tick()
    status = call(client, "svc_status", service="Borges")
    assert status["state"] == "down" and status["latest_incident"]["duration"].endswith("(still open)")
    assert status["restart"]["method"] == "launch" and status["restart"]["enabled"] is False
    incidents = call(client, "svc_incidents", at=str(int(down_at)), window_min=5)
    assert incidents["count"] == 1 and incidents["incidents"][0]["service"] == "borges"
    assert call(client, "svc_incidents", open_only=True)["count"] == 1
    assert call(client, "svc_incidents", service="argus")["count"] == 0
    why = call(client, "svc_why_down", service="5184")
    assert why["explanation"][0].startswith("Borges's Hoard (port 5184) went down at") and why["causes"]
    assert any(s.startswith("It is still down") for s in why["explanation"])
    history = call(client, "svc_history", service="borges", since="1h")
    assert [c["to"] for c in history["changes"]] == ["down"] and history["segments"][-1]["state"] == "down"
    gpu = call(client, "gpu_timeline", since="1h")
    assert gpu["freest_gpu"]["gpu"] == 0 and gpu["freest_gpu"]["mem_free_mb"] == 22000
    logs = call(client, "logs_search", query="anything")
    assert logs["count"] == 0 and logs["note"]
    none = call(client, "svc_why_down", service="argus")
    assert none["incident"] is None and "no recorded incident" in none["explanation"][0]
    restarted = call(client, "svc_restart", service="borges")
    assert restarted["ok"] and restarted["trigger"] == "manual" and restarted["incident_id"] == status["latest_incident"]["id"]
    watched = call(client, "svc_watch", id="whisper", url="http://127.0.0.1:9000", restart={"enabled": True, "max_per_hour": 2})
    assert watched["ok"] and watched["service"]["restart"]["max_per_hour"] == 2
    refused = client.post("/api/agent/call", json={"name": "svc_watch", "arguments": {"id": "whisper", "restart": {"cmd": "evil.bat"}}}, headers=auth(client))
    assert refused.status_code == 400 and "UI" in refused.json()["error"]
    h.services.config.agent_commands = True
    allowed = call(client, "svc_watch", id="whisper", restart={"cmd": ["python", "serve.py"]})
    assert allowed["service"]["restart"]["cmd"] == ["python", "serve.py"] and allowed["service"]["restart"]["max_per_hour"] == 2
    h.net.apps[5183].mode = "foreign"
    h.tick()
    foreign = client.post("/api/agent/call", json={"name": "svc_restart", "arguments": {"service": "argus"}}, headers=auth(client))
    assert foreign.status_code == 400 and "Another program" in foreign.json()["error"]
