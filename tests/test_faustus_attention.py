"""faustus_attention: what Faustus is waiting on the person for."""
import json
from types import SimpleNamespace

import httpx

from cassandra_hoard import faustus_attention as fa
from cassandra_hoard.agent_tools import TOOLS_BY_NAME, call_tool

NOW = 1_800_000_000.0
RUNS = [
    {"session_id": "s1", "kind": "approval", "label": "Fix the build", "since": NOW - 45 * 60, "next_action": "approve", "unread": True},
    {"session_id": "s2", "kind": "question", "label": "Plan trip", "since": NOW - 3 * 60, "next_action": "answer", "unread": True},
    {"session_id": "s3", "kind": "disconnected", "label": "Long research", "since": NOW - 20 * 60, "next_action": "reconnect", "detail": "20 min"},
    {"session_id": "s4", "kind": "finished_unreviewed", "label": "Done", "since": NOW - 90 * 60, "next_action": "open"},
    "junk",
]


def _transport(status=200, body=None, seen=None):
    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        return httpx.Response(status, json=body if body is not None else {"runs": RUNS, "unread_count": 2})
    return httpx.MockTransport(handler)


def test_summarize_counts_and_long_waits():
    out = fa.summarize(RUNS, NOW, wait_min=10)
    assert out["waiting_on_you"] == 2
    assert out["stalled"] == 1
    assert out["counts"]["finished_unreviewed"] == 1
    assert [i["session_id"] for i in out["long_waits"]] == ["s1", "s3"]
    assert out["oldest_wait_min"] == 45.0
    assert out["items"][0]["what"] == "waiting for an approval"


def test_report_reads_with_the_token(tmp_path):
    seen = []
    (tmp_path / "faustus-token").write_text("ody_abc\n", encoding="utf-8")
    out = fa.report(tmp_path, url="http://127.0.0.1:7000", transport=_transport(seen=seen), clock=lambda: NOW)
    assert out["ok"] is True and out["waiting_on_you"] == 2 and out["unread"] == 2
    assert seen[0].headers["authorization"] == "Bearer ody_abc"
    assert seen[0].url.path == "/api/attention"


def test_report_explains_why_it_cannot_read(tmp_path, monkeypatch):
    monkeypatch.delenv("CASSANDRA_FAUSTUS_TOKEN", raising=False)
    assert fa.report(tmp_path, url="http://127.0.0.1:7000")["reason"] == "no_token"
    out = fa.report(tmp_path, url="http://127.0.0.1:7000", token="t", transport=_transport(status=403, body={"detail": "no"}))
    assert out["reason"] == "forbidden" and "attention:read" in out["note"]

    def boom(request):
        raise httpx.ConnectError("refused")
    out = fa.report(tmp_path, url="http://127.0.0.1:7000", token="t", transport=httpx.MockTransport(boom))
    assert out["reason"] == "unreachable"


def test_never_sends_the_token_off_the_machine(tmp_path):
    seen = []
    out = fa.report(tmp_path, url="http://example.com", token="t", transport=_transport(seen=seen))
    assert out["ok"] is False and seen == []


def test_env_token_wins(tmp_path, monkeypatch):
    (tmp_path / "faustus-token").write_text("file", encoding="utf-8")
    monkeypatch.setenv("CASSANDRA_FAUSTUS_TOKEN", "env")
    assert fa.read_token(tmp_path) == "env"


def test_registered_as_a_read_only_tool(tmp_path, monkeypatch):
    tool = TOOLS_BY_NAME["faustus_attention"]
    assert tool.annotations["readOnlyHint"] is True
    monkeypatch.setattr(fa, "fetch", lambda url, token, **kw: (200, {"runs": RUNS, "unread_count": 2}))
    services = SimpleNamespace(config=SimpleNamespace(data_dir=tmp_path), clock=lambda: NOW)
    monkeypatch.setenv("CASSANDRA_FAUSTUS_TOKEN", "t")
    out = call_tool(services, "faustus_attention", {"wait_min": 30})
    assert out["ok"] is True and [i["session_id"] for i in out["long_waits"]] == ["s1"]

def test_unknown_wait_is_not_zero():
    out = fa.summarize([{"session_id": "q", "kind": "question"}], NOW)
    assert out["waiting_on_you"] == 1 and out["oldest_wait_min"] is None
    assert out["items"][0]["waited_min"] is None and out["long_waits"] == []


# -- the watcher -------------------------------------------------------------

def _watcher(tmp_path, runs, events, wait_min=10):
    (tmp_path / "faustus-token").write_text("t", encoding="utf-8")
    box = {"runs": runs}

    def handler(request):
        return httpx.Response(200, json={"runs": box["runs"], "unread_count": 0})
    w = fa.Watcher(tmp_path, wait_min=wait_min, interval_s=60, emit=lambda ty, d: events.append((ty, d)),
                   clock=lambda: NOW, transport=httpx.MockTransport(handler))
    return w, box


def test_watcher_announces_each_long_wait_once(tmp_path, monkeypatch):
    monkeypatch.setenv("CASSANDRA_FAUSTUS_URL", "http://127.0.0.1:7000")
    monkeypatch.delenv("CASSANDRA_FAUSTUS_TOKEN", raising=False)
    events = []
    w, box = _watcher(tmp_path, RUNS, events)
    w.tick()
    assert [(ty, d["session_id"], d["kind"]) for ty, d in events] == [(fa.EVENT, "s1", "approval")]
    w.tick()
    assert len(events) == 1  # still the same wait: no second alert
    box["runs"] = [r for r in RUNS if isinstance(r, dict) and r["session_id"] != "s1"]
    w.tick()
    box["runs"] = RUNS
    w.tick()
    assert len(events) == 2  # answered, then a new wait: a new alert
    st = w.status()
    assert st["ok"] is True and st["sent"] == 2 and st["waiting_on_you"] == 2


def test_watcher_off_and_without_token(tmp_path, monkeypatch):
    monkeypatch.delenv("CASSANDRA_FAUSTUS_TOKEN", raising=False)
    events = []
    off = fa.Watcher(tmp_path, wait_min=0, interval_s=60, emit=lambda *a: events.append(a))
    assert off.enabled is False
    off.start()
    assert off.status()["running"] is False
    w = fa.Watcher(tmp_path, wait_min=10, interval_s=60, emit=lambda *a: events.append(a), clock=lambda: NOW)
    assert w.tick()["reason"] == "no_token" and events == []


def test_boop_waiting_payload_carries_no_chat_content():
    from cassandra_hoard.notifications import BoopNotifier
    cfg = SimpleNamespace(boop_url="https://boop.example", boop_api_key="k", public_url="https://cass.example",
                          boop_enabled=True, bus=True)
    sent = []

    class Client:
        def post(self, url, json, headers, timeout, follow_redirects):
            sent.append(json)
            return SimpleNamespace(status_code=202)

    n = BoopNotifier(cfg, client=Client())
    assert n.send(fa.EVENT, {"session_id": "s1", "kind": "approval", "waited_min": 45.2, "label": "Secret plan"}) is True
    body = json.dumps(sent[0])
    assert "Secret plan" not in body and "45 min" in sent[0]["body"]
    assert sent[0]["fingerprint"] == "cassandra:faustus:s1:approval"
    assert n.send(fa.EVENT, {"session_id": "s1", "kind": "finished_unreviewed"}) is False