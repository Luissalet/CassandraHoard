"""What the app now takes from the shared Hoard Link commons: stable token, one-instance start, capped agent results,
atomic data files, shared classification and the shared database defaults."""

import os
import subprocess
import sys
import time

import httpx
import pytest
from conftest import ROOT, Harness, make_config
from fastapi.testclient import TestClient

from cassandra_hoard import agent_tools, sites_net
from cassandra_hoard.db import Database
from cassandra_hoard.hoard_link import net
from cassandra_hoard.hoard_link.agentkit import Tool, ann
from cassandra_hoard.logs import slug
from cassandra_hoard.main import create_app
from cassandra_hoard.registry import Registry
from cassandra_hoard.services import Services
from cassandra_hoard.sites import SiteStore


def _bearer(client, scheme="Bearer"):
    return {"Authorization": f"{scheme} {client.h.services.token}"}


def test_token_is_stable_across_restarts_and_replaces_a_short_one(tmp_path):
    config = make_config(tmp_path)
    first = Services(config)
    token = first.token
    first.stop()
    assert len(token) >= 32 and config.token_path.read_text(encoding="utf-8").strip() == token
    second = Services(config)  # a second start (autostart, double click) must not change what the bridge reads
    assert second.token == token
    second.stop()
    config.token_path.write_text("short", encoding="utf-8")
    third = Services(config)
    assert len(third.token) >= 32 and third.token != "short"
    third.stop()
    assert config.url_path.read_text(encoding="utf-8").startswith("http://127.0.0.1:")


def test_agent_call_accepts_any_case_bearer_and_answers_401_with_a_code(client):
    body = {"name": "svc_status"}
    assert client.post("/api/agent/call", json=body, headers=_bearer(client, "bearer")).status_code == 200
    refused = client.post("/api/agent/call", json=body, headers={"Authorization": "Bearer nope"})
    assert refused.status_code == 401 and refused.json()["code"] == "unauthorized" and refused.json()["error"] == "Invalid MCP token."
    assert client.post("/api/agent/call", json=body).status_code == 401


def test_agent_errors_are_json_with_codes_and_issues(client):
    headers = _bearer(client)
    unknown_tool = client.post("/api/agent/call", json={"name": "nope"}, headers=headers)
    assert unknown_tool.status_code == 404 and unknown_tool.json()["code"] == "unknown_tool"
    unknown_service = client.post("/api/agent/call", json={"name": "svc_why_down", "arguments": {"service": "zzz"}}, headers=headers)
    assert unknown_service.status_code == 404 and unknown_service.json()["code"] == "not_found" and "Unknown service" in unknown_service.json()["error"]
    invalid = client.post("/api/agent/call", json={"name": "svc_why_down", "arguments": {}}, headers=headers)
    assert invalid.status_code == 400 and invalid.json()["code"] == "invalid_arguments" and invalid.json()["issues"][0]["loc"] == "service"


def test_unexpected_tool_failure_is_json_not_an_html_500(client, monkeypatch):
    def boom(services, args):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(agent_tools, "TOOLS", [Tool("boom", "x", agent_tools.StatusArgs, ann(True), boom)])
    answer = client.post("/api/agent/call", json={"name": "boom"}, headers=_bearer(client))
    assert answer.status_code == 500 and answer.json()["code"] == "internal" and "kaboom" in answer.json()["error"]


def test_agent_results_are_capped_but_the_web_listing_is_not(client, monkeypatch):
    rows = [{"line": f"row {i} " + "x" * 80} for i in range(1500)]
    monkeypatch.setattr(agent_tools, "TOOLS", [Tool("big", "x", agent_tools.StatusArgs, ann(True), lambda services, args: {"rows": rows})])
    answer = client.post("/api/agent/call", json={"name": "big"}, headers=_bearer(client)).json()
    assert answer["truncated"]["original_lengths"]["rows"] == 1500 and len(answer["rows"]) < 1500
    assert len(str(answer)) < 40_000


@pytest.fixture
def running_app(tmp_path):
    """The real app (``python -m cassandra_hoard``) in a child process on a free port."""
    port = net.free_port()
    env = {**os.environ, "CASSANDRA_DATA_DIR": str(tmp_path / "data"), "CASSANDRA_PORT": str(port), "CASSANDRA_AUTOSTART": "0",
           "CASSANDRA_GPU": "0", "CASSANDRA_HUB_REGISTRY": "0", "CASSANDRA_EXTERNALS": "0", "CASSANDRA_ROOTS": str(tmp_path),
           "CASSANDRA_BUS": "0", "CASSANDRA_SITES": "0", "HOARD_NO_BROWSER": "1", "PYTHONPATH": str(ROOT)}
    command = [sys.executable, "-m", "cassandra_hoard"]
    child = subprocess.Popen(command, cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        assert net.wait_healthy(f"http://127.0.0.1:{port}", "cassandra-hoard", timeout=40), "the app did not start"
        yield {"port": port, "env": env, "command": command, "data": tmp_path / "data"}
    finally:
        child.terminate()
        try:
            child.wait(timeout=15)
        except subprocess.TimeoutExpired:
            child.kill()


def test_second_start_of_the_same_app_exits_cleanly_and_keeps_the_token(running_app):
    token_file = running_app["data"] / "mcp-token"
    token = token_file.read_text(encoding="utf-8")
    second = subprocess.run(running_app["command"], cwd=ROOT, env=running_app["env"], capture_output=True, text=True, timeout=40)
    assert second.returncode == 0 and "already running" in second.stdout
    assert token_file.read_text(encoding="utf-8") == token
    health = httpx.get(f"http://127.0.0.1:{running_app['port']}/api/health", trust_env=False).json()
    assert health["service"] == "cassandra-hoard" and health["dataDirConfigured"] is True and "hoard_link" in health


def test_the_shared_mcp_bridge_lists_and_calls_the_apps_tools(running_app, monkeypatch):
    import asyncio

    from cassandra_hoard.hoard_link.bridge import CatalogBridge

    monkeypatch.setenv("CASSANDRA_DATA_DIR", str(running_app["data"]))
    monkeypatch.setenv("CASSANDRA_URL", f"http://127.0.0.1:{running_app['port']}")
    monkeypatch.setenv("CASSANDRA_BRIDGE_AUTOSTART", "0")
    bridge = CatalogBridge(app="cassandra", service="cassandra-hoard", package="cassandra_hoard", default_port=5190,
                           data_dir_env="CASSANDRA_DATA_DIR", title="Cassandra's Hoard", root=str(ROOT / "mcp_server.py"))

    async def go():
        tools = await bridge.tools()
        called = await bridge.call("svc_status", {})
        missing = await bridge.call("svc_why_down", {"service": "zzz"})
        return tools, called, missing

    tools, called, missing = asyncio.run(go())
    assert {"svc_status", "faustus_farm", "sites_watch"} <= {t["name"] for t in tools}
    assert not called.is_error and "services" in called.body
    assert missing.is_error and missing.body["code"] == "not_found"


def test_data_files_are_written_atomically_with_no_leftover_temp_files(tmp_path):
    config = make_config(tmp_path)
    registry = Registry(config, hub_client=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(599))))
    registry.upsert_user({"id": "mine", "name": "Mine", "url": "http://127.0.0.1:9000", "health_path": "/health"})
    sites = SiteStore(config.sites_path)
    sites.upsert({"url": "https://example.com/"})
    assert sorted(p.name for p in config.data_dir.glob("*.json*")) == ["services.json", "sites.json"]
    assert config.services_path.read_text(encoding="utf-8").endswith("\n") and '"mine"' in config.services_path.read_text(encoding="utf-8")
    assert sites.find("example.com")["url"] == "https://example.com/"


def test_tracked_files_runs_git_through_the_shared_runner(tmp_path):
    from cassandra_hoard.audit import _tracked_files

    repo = tmp_path / "repo"
    repo.mkdir()
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com"}
    (repo / "a.txt").write_text("a", encoding="utf-8")
    (repo / "sub").mkdir()
    (repo / "sub" / "é b.txt").write_text("b", encoding="utf-8")
    for args in (["init", "-q"], ["add", "-A"], ["commit", "-q", "-m", "x"]):
        subprocess.run(["git", "-C", str(repo), *args], check=True, env=env, capture_output=True)
    assert sorted(_tracked_files(repo)) == ["a.txt", "sub/é b.txt"]
    assert _tracked_files(tmp_path) is None  # not a git folder


def test_network_errors_use_the_shared_classification_with_the_monitor_shape():
    request = httpx.Request("GET", "https://example.com")
    dropped = httpx.RemoteProtocolError("server disconnected", request=request)
    assert sites_net.classify_error(dropped, "example.com", 15)["kind"] == "reset"  # the shared fetcher reads a dropped HTTP connection as a reset
    assert sites_net.classify_error(ValueError("odd"), "h", 7) == {"kind": "other", "detail": "ValueError: odd"}


def test_registrable_domain_uses_the_shared_public_suffix_table():
    assert sites_net.registrable_domain("a.b.example.com.br") == "example.com.br"
    assert sites_net.registrable_domain("www.hacienda.gob.es") == "hacienda.gob.es"
    assert sites_net.registrable_domain("[::1]") is None and sites_net.registrable_domain("127.0.0.1") is None


def test_log_names_fold_accents():
    assert slug("Cassandra's Hoard") == "cassandrashoard" and slug("Argüs_Hoard-2") == "argushoard2" and slug(None) == ""


def test_database_has_the_shared_defaults_and_reads_old_plain_text_settings(tmp_path):
    db = Database(tmp_path / "x.db")
    try:
        assert db.one("PRAGMA foreign_keys")[0] == 1 and db.one("PRAGMA busy_timeout")[0] == 15000
        assert db.schema_version == 3
        # rows written by the old class: a repr'd float and the bare JSON text of a dict
        db.execute("INSERT INTO settings(key, value) VALUES ('last_tick', '1790000000.5'), ('rdap_bootstrap', '{\"fetched_at\": 1, \"services\": []}')")
        assert float(db.get_setting("last_tick")) == 1790000000.5
        assert db.get_setting("rdap_bootstrap") == {"fetched_at": 1, "services": []}
        db.set_setting("last_tick", 1790000001.0)
        assert float(db.get_setting("last_tick")) == 1790000001.0
        with pytest.raises(RuntimeError):
            with db.transaction():
                raise RuntimeError("rolled back")
        with db.transaction() as conn:  # the lock was released
            conn.execute("INSERT INTO settings(key, value) VALUES ('k', '1')")
    finally:
        db.close()


def test_rdap_bootstrap_cache_survives_in_both_storage_shapes(tmp_path):
    h = Harness(tmp_path)
    try:
        db = h.services.db
        seen = []

        class Client:  # the cached bootstrap is fresh: the network must not be touched
            def get(self, *a, **k):
                seen.append(a)
                raise AssertionError("network used")

        from cassandra_hoard.sites_net import RdapChecker
        lookup = RdapChecker(db, client=None)
        boot = {"fetched_at": h.clock.now, "services": [[["com"], ["https://rdap.example/"]]]}
        db.set_setting("rdap_bootstrap", boot)
        assert lookup._bases(Client(), "com", h.clock.now) == ["https://rdap.example/"]
        import json as _json
        db.execute("UPDATE settings SET value = ? WHERE key = 'rdap_bootstrap'", (_json.dumps(boot),))  # as the old class stored it
        assert lookup._bases(Client(), "com", h.clock.now) == ["https://rdap.example/"]
        assert not seen
    finally:
        h.close()


def test_config_reads_flags_and_clamps_numbers(monkeypatch, tmp_path):
    from cassandra_hoard.config import Config

    monkeypatch.setenv("CASSANDRA_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("CASSANDRA_EXTERNALS", "false")
    monkeypatch.setenv("CASSANDRA_POLL_S", "1")
    monkeypatch.setenv("CASSANDRA_PORT", "99999")
    monkeypatch.setenv("PORT_STRICT", "yes")
    config = Config.from_env()
    assert config.externals is False and config.poll_s == 2.0 and config.port == 5190 and config.port_strict is True
    assert config.db_path == tmp_path / "cassandra.db" and config.token_path == tmp_path / "mcp-token" and config.logs_dir == tmp_path / "logs"
