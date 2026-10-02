"""What is running in Faustus right now: runs, their sub-agents, jobs and the budget.

``faustus_attention`` answers "is something waiting for me?". This answers the
other half of "what is Faustus doing?": every chat turn in flight, the
sub-agents each one started (grouped under it), dispatched jobs and their
workers, a night shift, workflow runs, scheduled tasks and the period budget
(window used against pace, GPU seconds today, the failure breaker, provider
cooldowns). Faustus builds one document for it (``GET /api/farm/state``, at
most once a second, with an ETag); this module reads it with a read-only token
(scope ``farm:read``) and turns it into rows a person and the assistant can read.

Token: ``CASSANDRA_FAUSTUS_FARM_TOKEN`` or the file ``<data>/faustus-farm-token``
(mint it in Faustus, Settings, API tokens, scope "Farm"). Without either, the
attention token is tried (it works when it also carries ``farm:read``).
Address: ``CASSANDRA_FAUSTUS_URL`` (loopback only). Nothing is written to
Faustus and nothing leaves this machine. The document carries chat and task
*names*, never what anyone typed.
"""

from __future__ import annotations

import hashlib
import os
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional

import httpx

from . import faustus_attention
from .times import clock as clock_text, duration

TIMEOUT_S = 3.0
MIN_INTERVAL_S = 1.0
STATE_PATH = "/api/farm/state"
TOKEN_FILE = "faustus-farm-token"

NOTES = {
    "no_token": "Set CASSANDRA_FAUSTUS_FARM_TOKEN or write a Faustus API token with scope farm:read to data/faustus-farm-token (Settings, API tokens, Farm).",
    "unreachable": "Faustus did not answer (is it running?).",
    "forbidden": "Faustus refused the token: it needs the farm:read (or sessions) scope.",
    "old_faustus": "This Faustus has no /api/farm/state yet: restart it on the current code.",
    "bad_answer": "Faustus answered something that is not the farm state.",
    "bad_url": "Faustus URL must be a loopback address.",
}


def read_token(data_dir: Path) -> str:
    """The farm token, else the attention token (one token may carry both scopes)."""
    env = (os.environ.get("CASSANDRA_FAUSTUS_FARM_TOKEN") or "").strip()
    if env:
        return env
    try:
        own = (Path(data_dir) / TOKEN_FILE).read_text(encoding="utf-8").strip()
    except OSError:
        own = ""
    return own or faustus_attention.read_token(data_dir)


def fetch(url: str, token: str, *, etag: Optional[str] = None,
          transport: Optional[httpx.BaseTransport] = None) -> tuple[Optional[int], Any, Optional[str]]:
    """``(status, json, etag)``; status None = could not connect. Never raises."""
    if not faustus_attention._is_loopback(url):
        return -1, {"error": NOTES["bad_url"]}, None
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    if etag:
        headers["If-None-Match"] = etag
    try:
        with httpx.Client(timeout=TIMEOUT_S, transport=transport) as client:
            resp = client.get(f"{url}{STATE_PATH}", headers=headers)
    except Exception:  # noqa: BLE001 - absent Faustus is an answer, not an error
        return None, None, None
    try:
        data = resp.json() if resp.status_code == 200 else None
    except ValueError:
        data = None
    return resp.status_code, data, resp.headers.get("etag")


class FarmReader:
    """Reads Faustus's document, at most once per ``min_interval_s`` and with
    ``If-None-Match`` so an unchanged picture costs a 304. Every watcher of the
    panel and the assistant share one reader, so ten open tabs are one request.
    """

    def __init__(self, *, min_interval_s: float = MIN_INTERVAL_S,
                 transport: Optional[httpx.BaseTransport] = None,
                 monotonic: Callable[[], float] = time.monotonic):
        self.min_interval_s = float(min_interval_s)
        self.transport = transport
        self._mono = monotonic
        self._lock = threading.Lock()
        self._key: Optional[str] = None
        self._at = 0.0
        self._etag: Optional[str] = None
        self._state: Optional[dict[str, Any]] = None
        self._result: Optional[dict[str, Any]] = None

    def read(self, data_dir: Path, *, url: Optional[str] = None, token: Optional[str] = None) -> dict[str, Any]:
        url = (url or faustus_attention.faustus_url()).rstrip("/")
        token = read_token(data_dir) if token is None else token
        if not token:
            return {"ok": False, "url": url, "reason": "no_token", "note": NOTES["no_token"]}
        key = url + "|" + hashlib.sha256(token.encode("utf-8")).hexdigest()[:16]
        with self._lock:
            if key != self._key:
                self._key, self._etag, self._state, self._result, self._at = key, None, None, None, 0.0
            if self._result is not None and self._mono() - self._at < self.min_interval_s:
                return self._result
            status, data, etag = fetch(url, token, etag=self._etag if self._state else None, transport=self.transport)
            result = self._interpret(url, status, data, etag)
            self._result, self._at = result, self._mono()
            return result

    def _interpret(self, url: str, status: Optional[int], data: Any, etag: Optional[str]) -> dict[str, Any]:
        def fail(reason: str, **extra: Any) -> dict[str, Any]:
            self._etag, self._state = None, None
            return {"ok": False, "url": url, "reason": reason, "note": NOTES[reason], **extra}

        if status is None:
            return fail("unreachable")
        if status == -1:
            return fail("bad_url")
        if status in (401, 403):
            return fail("forbidden", status=status)
        if status == 404:
            return fail("old_faustus", status=status)
        if status == 304 and self._state is not None:
            return {"ok": True, "url": url, "state": self._state, "etag": self._etag, "unchanged": True}
        if status != 200 or not isinstance(data, dict) or not isinstance(data.get("items"), list):
            return fail("bad_answer", status=status)
        self._state, self._etag = data, etag
        return {"ok": True, "url": url, "state": data, "etag": etag, "unchanged": False}


_READER = FarmReader()

# ---------------------------------------------------------------------------
# Rows a person can read
# ---------------------------------------------------------------------------

KIND_LABEL = {
    "chat_run": "chat turn",
    "worker": "sub-agent",
    "dispatch_job": "dispatched job",
    "night_shift": "night shift",
    "workflow_run": "workflow run",
    "scheduled_task": "scheduled task",
}
_EXTRA = ("phase", "tool", "role", "round", "waiting_for", "unattended", "queue_position",
          "tool_calls", "task_type", "trigger")
MAX_DEPTH = 6


def _num(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if out == out else None


def _progress(p: Any) -> tuple[Optional[str], Optional[float]]:
    if not isinstance(p, dict):
        return None, None
    pct = _num(p.get("percent"))
    if p.get("total"):
        return f"{p.get('done', 0)}/{p['total']} {p.get('unit') or ''}".strip(), pct
    if pct is not None:
        return f"{round(pct)} %", pct
    return None, None


def _row(item: dict[str, Any], now: float, base: str, depth: int = 0) -> dict[str, Any]:
    started, last = _num(item.get("started_at")), _num(item.get("last_event_at"))
    text, pct = _progress(item.get("progress"))
    link = item.get("link")
    row: dict[str, Any] = {
        "id": item.get("id"), "kind": item.get("kind"), "title": item.get("title") or "",
        "state": item.get("state"), "model": item.get("model"),
        "running_s": int(max(0, now - started)) if started else None,
        "running_for": duration(now - started) if started else None,
        "idle_s": int(max(0, now - last)) if last else None,
        "progress": text, "progress_pct": pct,
        "link": f"{base}{link}" if isinstance(link, str) and link.startswith("/") else None,
    }
    for key in _EXTRA:
        if item.get(key) not in (None, ""):
            row[key] = item[key]
    kids = item.get("children") if depth < MAX_DEPTH else None
    if isinstance(kids, list) and kids:
        row["children"] = [_row(c, now, base, depth + 1) for c in kids if isinstance(c, dict)]
    return {k: v for k, v in row.items() if v is not None}


def _pct(fraction: Any) -> Optional[int]:
    f = _num(fraction)
    return None if f is None else round(100 * f)


def _budget_view(block: Any, now: float) -> Optional[dict[str, Any]]:
    """The budget block with the percentages and countdowns a card shows, and
    one English line for the assistant."""
    if not isinstance(block, dict):
        return None
    providers = []
    for r in block.get("providers") or []:
        if not isinstance(r, dict):
            continue
        providers.append({
            "label": f"{r.get('provider')} {r.get('metric')}", "window": r.get("window"),
            "metric": r.get("metric"), "used": r.get("used"), "target": r.get("target"),
            "used_pct": _pct(r.get("used_fraction")), "pace_pct": _pct(r.get("pace_fraction")),
            "paused": bool(r.get("paused")), "paused_until": r.get("paused_until"), "reason": r.get("reason"),
        })
    g = block.get("gpu") if isinstance(block.get("gpu"), dict) else None
    gpu = None
    if g:
        gpu = {"used_s": g.get("used"), "target_s": g.get("target"), "used_pct": _pct(g.get("used_fraction")),
               "pace_pct": _pct(g.get("pace_fraction")), "paused": bool(g.get("paused")),
               "paused_until": g.get("paused_until")}
    b = block.get("breaker") if isinstance(block.get("breaker"), dict) else {}
    until = _num(b.get("reopens_at"))
    breaker = {"enabled": bool(b.get("enabled")), "open": bool(b.get("open")),
               "consecutive_failures": b.get("consecutive_failures") or 0, "threshold": b.get("threshold") or 0,
               "reopens_in_s": int(max(0, until - now)) if b.get("open") and until else None}
    cooldowns = []
    for c in block.get("cooldowns") or []:
        if isinstance(c, dict):
            end = _num(c.get("until"))
            cooldowns.append({"endpoint": c.get("endpoint"), "status": c.get("status"), "hits": c.get("hits"),
                              "remaining_s": int(max(0, end - now)) if end else None})
    out = {"enabled": bool(block.get("enabled")), "window": block.get("window"), "providers": providers,
           "gpu": gpu, "breaker": breaker, "cooldowns": cooldowns,
           "interactive_active": bool(block.get("interactive_active"))}
    out["line"] = _budget_line(out, now)
    return out


def _budget_line(b: dict[str, Any], now: float) -> str:
    parts: list[str] = []
    if not b["enabled"] and not b["gpu"]:
        parts.append("no budget target set")
    for r in b["providers"]:
        text = f"{r['label']}/{r['window']} {r['used_pct']} % used" if r["used_pct"] is not None else f"{r['label']} {r['used']}"
        if r["pace_pct"] is not None:
            text += f" (pace line {r['pace_pct']} %)"
        if r["paused"]:
            text += " PAUSED" + (f" until {clock_text(r['paused_until'])}" if r.get("paused_until") else "")
        parts.append(text)
    if b["gpu"]:
        g = b["gpu"]
        text = f"GPU today {round(g['used_s'] or 0)} s"
        if g.get("target_s"):
            text += f" of {round(g['target_s'])} s"
        if g["pace_pct"] is not None:
            text += f" (pace line {g['pace_pct']} %)"
        if g["paused"]:
            text += " PAUSED"
        parts.append(text)
    br = b["breaker"]
    if br["open"]:
        wait = f", reopens in {duration(br['reopens_in_s'])}" if br["reopens_in_s"] is not None else ""
        parts.append(f"failure breaker OPEN ({br['consecutive_failures']}/{br['threshold']}{wait})")
    else:
        parts.append("failure breaker closed")
    if b["cooldowns"]:
        parts.append("cooldowns: " + ", ".join(
            f"{c['endpoint']} {duration(c['remaining_s'])}" for c in b["cooldowns"]))
    else:
        parts.append("no provider cooldowns")
    if b["interactive_active"]:
        parts.append("an interactive turn is live (unattended work yields)")
    return "; ".join(parts)


def _headline(counts: dict[str, Any]) -> str:
    total = int(counts.get("total") or 0)
    if not total:
        return "Nothing is running in Faustus."
    kinds = ", ".join(f"{n} {KIND_LABEL.get(k, k)}" for k, n in sorted((counts.get("by_kind") or {}).items()))
    states = counts.get("by_state") or {}
    extra = [f"{states[s]} {s}" for s in ("stalled", "waiting", "queued", "paused") if states.get(s)]
    return f"{total} running ({kinds})" + (": " + ", ".join(extra) if extra else "")


def summarize(state: dict[str, Any], now: float, *, include_budget: bool = True, base: str = "") -> dict[str, Any]:
    """Faustus's document as nested rows (a sub-agent under the run that started
    it), the counts, a one-line headline and, if asked, the budget."""
    base = base.rstrip("/")
    counts = state.get("counts") if isinstance(state.get("counts"), dict) else {}
    out: dict[str, Any] = {
        "headline": _headline(counts),
        "counts": counts,
        "runs": [_row(i, now, base) for i in state.get("items") or [] if isinstance(i, dict)],
        "generated_at": state.get("generated_at"),
    }
    if include_budget:
        out["budget"] = _budget_view(state.get("budget"), now)
    if state.get("errors"):
        out["faustus_errors"] = state["errors"]
    return out


def report(data_dir: Path, *, include_budget: bool = True, url: Optional[str] = None,
           token: Optional[str] = None, reader: Optional[FarmReader] = None,
           clock: Callable[[], float] = time.time) -> dict[str, Any]:
    """What the assistant and the panel get: ``ok`` and the rows, or ``ok`` false
    with the reason Faustus could not be read."""
    res = (reader or _READER).read(data_dir, url=url, token=token)
    if not res.get("ok"):
        return {k: res[k] for k in ("ok", "url", "reason", "note", "status") if k in res}
    out = summarize(res["state"], clock(), include_budget=include_budget, base=res["url"])
    out.update({"ok": True, "url": res["url"]})
    return out