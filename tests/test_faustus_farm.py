"""faustus_farm: what Faustus is running right now (runs, sub-agents, jobs, budget)."""
import copy
from types import SimpleNamespace

import httpx
import pytest

from cassandra_hoard import faustus_farm as ff
from cassandra_hoard.agent_tools import TOOLS_BY_NAME, call_tool

NOW = 1_800_000_000.0
URL = "http://127.0.0.1:7000"

STATE = {
    "schema": 1, "generated_at": NOW,
    "counts": {"total": 4, "roots": 2, "by_kind": {"chat_run": 1, "worker": 2, "dispatch_job": 1},
               "by_state": {"running": 3, "stalled": 1}},
    "items": [
        {"id": "run:s1", "kind": "chat_run", "title": "Refactor the importer", "parent": None, "state": "running",
         "started_at": NOW - 600, "last_event_at": NOW - 4, "model": "qwen-9b",
         "progress": {"percent": 40.0, "unit": "plan"}, "link": "/studio?s=s1", "phase": "tool", "tool": "read_file",
         "children": [
             {"id": "worker:c1", "kind": "worker", "title": "reader", "parent": "run:s1", "state": "running",
              "started_at": NOW - 300, "last_event_at": NOW - 9, "model": "w-model", "progress": None,
              "link": "/studio?s=c1", "children": []},
             {"id": "worker:c2", "kind": "worker", "title": "writer", "parent": "run:s1", "state": "stalled",
              "started_at": NOW - 280, "last_event_at": NOW - 200, "model": None, "progress": None,
              "link": None, "children": []}]},
        {"id": "job:j1", "kind": "dispatch_job", "title": "Port the parser", "parent": None, "state": "running",
         "started_at": NOW - 90, "last_event_at": NOW - 2, "model": "local-27b",
         "progress": {"done": 1, "total": 2, "unit": "tasks", "percent": 50.0}, "link": None, "children": []},
    ],
    "budget": {
        "enabled": True, "window": "week", "window_resets_at": NOW + 5000, "interactive_active": True,
        "providers": [{"provider": "hosted", "metric": "usd", "window": "week", "used": 2.5, "target": 10.0,
                       "used_fraction": 0.25, "pace_fraction": 0.3, "pace_allowed": 3.0, "paused": False,
                       "paused_until": None, "reason": "ok", "resets_at": NOW + 5000}],
        "gpu": {"provider": "local", "metric": "gpu_seconds", "window": "day", "used": 120.0, "target": 600,
                "used_fraction": 0.2, "pace_fraction": 0.5, "pace_allowed": 300.0, "paused": False, "paused_until": None},
        "breaker": {"enabled": True, "open": True, "consecutive_failures": 3, "threshold": 3,
                    "reopens_at": NOW + 900, "last_failure_kind": "dispatch"},
        "cooldowns": [{"endpoint": "api.example.com", "status": 429, "until": NOW + 120, "hits": 2}],
    },
}


def _server(status=200, body=None, etag='W/"farm-1"', seen=None):
    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        inm = request.headers.get("if-none-match")
        if status == 200 and inm and inm == etag:
            return httpx.Response(304, headers={"ETag": etag})
        return httpx.Response(status, json=body if body is not None else STATE, headers={"ETag": etag})
    return httpx.MockTransport(handler)


def _reader(transport, interval=0.0):
    return ff.FarmReader(min_interval_s=interval, transport=transport)


# -- reading --------------------------------------------------------------------

def test_reads_the_state_with_the_token_and_the_right_path(tmp_path):
    seen = []
    out = ff.report(tmp_path, url=URL, token="ody_t", reader=_reader(_server(seen=seen)), clock=lambda: NOW)
    assert out["ok"] is True and seen[0].url.path == "/api/farm/state"
    assert seen[0].headers["authorization"] == "Bearer ody_t" and "if-none-match" not in seen[0].headers


def test_a_repeat_sends_the_etag_and_a_304_reuses_the_picture(tmp_path):
    seen = []
    reader = _reader(_server(seen=seen))
    first = reader.read(tmp_path, url=URL, token="t")
    second = reader.read(tmp_path, url=URL, token="t")
    assert first["unchanged"] is False and second["unchanged"] is True and second["state"] is first["state"]
    assert seen[1].headers["if-none-match"] == 'W/"farm-1"'


def test_one_reader_is_one_request_per_interval_however_many_ask(tmp_path):
    seen = []
    reader = _reader(_server(seen=seen), interval=60.0)
    for _ in range(5):
        assert reader.read(tmp_path, url=URL, token="t")["ok"] is True
    assert len(seen) == 1


def test_a_different_token_or_url_starts_clean(tmp_path):
    seen = []
    reader = _reader(_server(seen=seen))
    reader.read(tmp_path, url=URL, token="a")
    reader.read(tmp_path, url=URL, token="b")
    assert "if-none-match" not in seen[1].headers


@pytest.mark.parametrize("status,reason", [(401, "forbidden"), (403, "forbidden"), (404, "old_faustus"), (500, "bad_answer")])
def test_it_says_why_it_cannot_read(tmp_path, status, reason):
    out = ff.report(tmp_path, url=URL, token="t", reader=_reader(_server(status=status, body={"detail": "x"})))
    assert out["ok"] is False and out["reason"] == reason and out["note"]


def test_no_token_unreachable_and_wrong_shape(tmp_path, monkeypatch):
    for name in ("CASSANDRA_FAUSTUS_FARM_TOKEN", "CASSANDRA_FAUSTUS_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    assert ff.report(tmp_path, url=URL, reader=_reader(_server()))["reason"] == "no_token"

    def boom(request):
        raise httpx.ConnectError("refused")
    assert ff.report(tmp_path, url=URL, token="t", reader=_reader(httpx.MockTransport(boom)))["reason"] == "unreachable"
    assert ff.report(tmp_path, url=URL, token="t", reader=_reader(_server(body={"hello": 1})))["reason"] == "bad_answer"


def test_a_failure_forgets_the_old_picture(tmp_path):
    state = {"status": 200}

    def handler(request):
        if state["status"] == 200:
            return httpx.Response(200, json=STATE, headers={"ETag": 'W/"farm-1"'})
        return httpx.Response(state["status"], json={})
    reader = _reader(httpx.MockTransport(handler))
    assert reader.read(tmp_path, url=URL, token="t")["ok"] is True
    state["status"] = 503
    assert reader.read(tmp_path, url=URL, token="t")["ok"] is False
    state["status"] = 200
    assert reader.read(tmp_path, url=URL, token="t")["unchanged"] is False


def test_never_sends_the_token_off_the_machine(tmp_path):
    seen = []
    out = ff.report(tmp_path, url="http://example.com", token="t", reader=_reader(_server(seen=seen)))
    assert out["ok"] is False and out["reason"] == "bad_url" and seen == []


# -- the token -------------------------------------------------------------------

def test_token_order_is_env_then_farm_file_then_the_attention_token(tmp_path, monkeypatch):
    for name in ("CASSANDRA_FAUSTUS_FARM_TOKEN", "CASSANDRA_FAUSTUS_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    assert ff.read_token(tmp_path) == ""
    (tmp_path / "faustus-token").write_text("attention\n", encoding="utf-8")
    assert ff.read_token(tmp_path) == "attention"
    (tmp_path / "faustus-farm-token").write_text("farm\n", encoding="utf-8")
    assert ff.read_token(tmp_path) == "farm"
    monkeypatch.setenv("CASSANDRA_FAUSTUS_FARM_TOKEN", "env")
    assert ff.read_token(tmp_path) == "env"

# -- the rows ----------------------------------------------------------------------

def test_sub_agents_sit_under_their_run_with_age_progress_and_a_full_link():
    out = ff.summarize(copy.deepcopy(STATE), NOW, base=URL)
    run, job = out["runs"]
    assert run["title"] == "Refactor the importer" and run["running_for"] == "10 min" and run["progress"] == "40 %"
    assert run["link"] == "http://127.0.0.1:7000/studio?s=s1" and run["tool"] == "read_file"
    kids = {c["title"]: c for c in run["children"]}
    assert set(kids) == {"reader", "writer"} and kids["writer"]["state"] == "stalled" and kids["writer"]["idle_s"] == 200
    assert "link" not in kids["writer"] and "model" not in kids["writer"]
    assert job["progress"] == "1/2 tasks" and job["model"] == "local-27b" and "children" not in job
    assert out["headline"] == "4 running (1 chat turn, 1 dispatched job, 2 sub-agent): 1 stalled"


def test_an_empty_farm_says_nothing_is_running():
    out = ff.summarize({"items": [], "counts": {"total": 0}, "budget": None}, NOW)
    assert out["runs"] == [] and out["headline"] == "Nothing is running in Faustus." and out["budget"] is None


def test_the_budget_has_window_gpu_breaker_and_cooldowns_with_countdowns():
    b = ff.summarize(copy.deepcopy(STATE), NOW)["budget"]
    assert b["providers"][0]["used_pct"] == 25 and b["providers"][0]["pace_pct"] == 30
    assert b["gpu"]["used_s"] == 120.0 and b["gpu"]["target_s"] == 600 and b["gpu"]["pace_pct"] == 50
    assert b["breaker"]["open"] is True and b["breaker"]["reopens_in_s"] == 900
    assert b["cooldowns"] == [{"endpoint": "api.example.com", "status": 429, "hits": 2, "remaining_s": 120}]
    for part in ("hosted usd/week 25 % used", "pace line 30 %", "GPU today 120 s of 600 s", "breaker OPEN (3/3, reopens in 15 min)",
                 "api.example.com 2 min", "interactive turn is live"):
        assert part in b["line"], part


def test_a_closed_breaker_and_no_cooldowns_read_plainly():
    state = copy.deepcopy(STATE)
    state["budget"]["breaker"] = {"enabled": True, "open": False, "consecutive_failures": 0, "threshold": 3, "reopens_at": None}
    state["budget"]["cooldowns"] = []
    state["budget"]["interactive_active"] = False
    line = ff.summarize(state, NOW)["budget"]["line"]
    assert "failure breaker closed" in line and "no provider cooldowns" in line and "interactive" not in line


def test_the_budget_can_be_left_out_and_faustus_errors_are_passed_on():
    state = copy.deepcopy(STATE)
    state["errors"] = {"workflows": "OperationalError: locked"}
    out = ff.summarize(state, NOW, include_budget=False)
    assert "budget" not in out and out["faustus_errors"] == {"workflows": "OperationalError: locked"}


# -- the tool ----------------------------------------------------------------------

def test_registered_as_a_read_only_bilingual_tool(tmp_path, monkeypatch):
    tool = TOOLS_BY_NAME["faustus_farm"]
    assert tool.annotations["readOnlyHint"] is True
    monkeypatch.setattr(ff, "_READER", _reader(_server()))
    monkeypatch.setenv("CASSANDRA_FAUSTUS_FARM_TOKEN", "t")
    monkeypatch.setenv("CASSANDRA_FAUSTUS_URL", URL)
    services = SimpleNamespace(config=SimpleNamespace(data_dir=tmp_path), clock=lambda: NOW)
    out = call_tool(services, "faustus_farm", {})
    assert out["ok"] is True and out["runs"][0]["children"][0]["title"] == "reader" and "budget" in out
    assert "budget" not in call_tool(services, "faustus_farm", {"include_budget": False})


def test_the_assistant_is_told_when_to_use_it():
    from cassandra_hoard.agent_tools import AGENT_INSTRUCTIONS
    assert "faustus_farm" in AGENT_INSTRUCTIONS and "what is Faustus running right now" in AGENT_INSTRUCTIONS


# -- the Panel's endpoint ------------------------------------------------------------

def test_the_panel_endpoint_serves_the_rows_or_the_reason(client, monkeypatch):
    for name in ("CASSANDRA_FAUSTUS_FARM_TOKEN", "CASSANDRA_FAUSTUS_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(ff, "_READER", _reader(_server()))
    off = client.get("/api/faustus/farm").json()
    assert off["ok"] is False and off["reason"] == "no_token"
    monkeypatch.setenv("CASSANDRA_FAUSTUS_FARM_TOKEN", "t")
    monkeypatch.setenv("CASSANDRA_FAUSTUS_URL", URL)
    on = client.get("/api/faustus/farm").json()
    assert on["ok"] is True and on["runs"][0]["children"][1]["state"] == "stalled" and on["budget"]["breaker"]["open"] is True