"""faustus_attention: what Faustus is waiting on the person for."""
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