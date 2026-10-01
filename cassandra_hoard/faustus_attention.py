"""What is waiting for the person in Faustus: approvals, questions, stalled runs.

Cassandra watches whether Faustus is *up*. That says nothing about a turn
that has been sitting on an approval card for forty minutes while the person
is somewhere else, or a run that stopped sending events. Faustus already
answers that question for its own Activity screen (``GET /api/attention``);
this module reads it with a read-only token (scope ``attention:read``) and
turns it into a short report the assistant and the panel can quote.

Token: ``CASSANDRA_FAUSTUS_TOKEN`` or the file ``<data>/faustus-token``
(mint one in Faustus, Settings, API tokens, scope "Attention"). Address:
``CASSANDRA_FAUSTUS_URL`` (default ``http://127.0.0.1:7000``). Nothing is
written to Faustus; nothing is sent anywhere but that loopback address.
"""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import urlsplit

import httpx

DEFAULT_URL = "http://127.0.0.1:7000"
TIMEOUT_S = 3.0
LOOPBACK = {"127.0.0.1", "localhost", "::1", "[::1]"}

KIND_LABEL = {
    "approval": "waiting for an approval",
    "question": "waiting for an answer",
    "disconnected": "no events for a while (stalled?)",
    "queued_model": "queued for the model",
    "dependency": "waiting for a delegated worker",
    "finished_unreviewed": "finished, not reviewed",
}
WAITING_ON_PERSON = ("approval", "question")


def faustus_url() -> str:
    return (os.environ.get("CASSANDRA_FAUSTUS_URL") or DEFAULT_URL).strip().rstrip("/")


def read_token(data_dir: Path) -> str:
    env = (os.environ.get("CASSANDRA_FAUSTUS_TOKEN") or "").strip()
    if env:
        return env
    try:
        return (Path(data_dir) / "faustus-token").read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _is_loopback(url: str) -> bool:
    return (urlsplit(url).hostname or "").lower() in LOOPBACK


def fetch(url: str, token: str, *, transport: Optional[httpx.BaseTransport] = None,
          limit: int = 50) -> tuple[Optional[int], Any]:
    """``(status, json)``; status None = could not connect. Never raises."""
    if not _is_loopback(url):
        return -1, {"error": "Faustus URL must be a loopback address"}
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    try:
        with httpx.Client(timeout=TIMEOUT_S, transport=transport) as client:
            resp = client.get(f"{url}/api/attention", params={"limit": limit}, headers=headers)
    except Exception:  # noqa: BLE001 - absent Faustus is an answer, not an error
        return None, None
    try:
        data = resp.json()
    except ValueError:
        data = None
    return resp.status_code, data


def summarize(runs: Any, now: float, *, wait_min: float = 10.0) -> dict[str, Any]:
    """Counts by kind, the items waiting on the person, and the long waits."""
    rows = [r for r in (runs or []) if isinstance(r, dict)]
    counts: dict[str, int] = {}
    items: list[dict[str, Any]] = []
    long_waits: list[dict[str, Any]] = []
    for r in rows:
        kind = str(r.get("kind") or "")
        counts[kind] = counts.get(kind, 0) + 1
        since = r.get("since")
        try:
            waited_min = max(0.0, (now - float(since)) / 60.0) if since else None
        except (TypeError, ValueError):
            waited_min = None
        item = {
            "session_id": r.get("session_id"),
            "label": r.get("label") or "",
            "kind": kind,
            "what": KIND_LABEL.get(kind, kind),
            "next_action": r.get("next_action"),
            "waited_min": round(waited_min, 1) if waited_min is not None else None,
        }
        if r.get("detail"):
            item["detail"] = r.get("detail")
        items.append(item)
        if kind in WAITING_ON_PERSON + ("disconnected",) and waited_min is not None and waited_min >= wait_min:
            long_waits.append(item)
    waiting = sum(counts.get(k, 0) for k in WAITING_ON_PERSON)
    long_waits.sort(key=lambda i: -(i["waited_min"] or 0))
    # Unknown ages stay unknown: a question with no timestamp is not "0 minutes".
    oldest = max((i["waited_min"] for i in items
                  if i["kind"] in WAITING_ON_PERSON and i["waited_min"] is not None), default=None)
    return {
        "waiting_on_you": waiting,
        "stalled": counts.get("disconnected", 0),
        "counts": counts,
        "oldest_wait_min": oldest,
        "long_waits": long_waits,
        "wait_min": wait_min,
        "items": items,
    }


def report(data_dir: Path, *, wait_min: float = 10.0, url: Optional[str] = None,
           token: Optional[str] = None, transport: Optional[httpx.BaseTransport] = None,
           clock: Callable[[], float] = time.time) -> dict[str, Any]:
    url = (url or faustus_url()).rstrip("/")
    token = read_token(data_dir) if token is None else token
    if not token:
        return {"ok": False, "url": url, "reason": "no_token",
                "note": "Set CASSANDRA_FAUSTUS_TOKEN or write a Faustus API token with scope attention:read to data/faustus-token."}
    status, data = fetch(url, token, transport=transport)
    if status is None:
        return {"ok": False, "url": url, "reason": "unreachable", "note": "Faustus did not answer (is it running?)."}
    if status in (401, 403):
        return {"ok": False, "url": url, "reason": "forbidden", "status": status,
                "note": "Faustus refused the token: it needs the attention:read (or sessions) scope."}
    if status != 200 or not isinstance(data, dict):
        return {"ok": False, "url": url, "reason": "bad_answer", "status": status}
    out = summarize(data.get("runs"), clock(), wait_min=wait_min)
    out.update({"ok": True, "url": url, "unread": data.get("unread_count")})
    return out

# ---------------------------------------------------------------------------
# Watching: tell the person once when something has waited too long
# ---------------------------------------------------------------------------

EVENT = "cassandra.faustus.waiting"


class Watcher:
    """Reads Faustus's attention list every ``interval_s`` and emits
    :data:`EVENT` once per wait that passes ``wait_min`` minutes (an approval
    or a question). A wait that is answered and comes back later is a new
    wait. Off when ``wait_min`` is 0 or there is no token. Never raises.

    After a restart a wait that is still open is announced again once:
    nothing about Faustus's chats is stored on disk.
    """

    def __init__(self, data_dir: Path, *, wait_min: float, interval_s: float,
                 emit: Callable[[str, dict[str, Any]], None],
                 clock: Callable[[], float] = time.time,
                 transport: Optional[httpx.BaseTransport] = None):
        self.data_dir = Path(data_dir)
        self.wait_min = float(wait_min)
        self.interval_s = max(5.0, float(interval_s))
        self.emit = emit
        self.clock = clock
        self.transport = transport
        self.last: Optional[dict[str, Any]] = None
        self.last_at: Optional[float] = None
        self.notified: dict[str, float] = {}
        self.sent = 0
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    @property
    def enabled(self) -> bool:
        return self.wait_min > 0

    def tick(self) -> dict[str, Any]:
        out = report(self.data_dir, wait_min=self.wait_min, transport=self.transport, clock=self.clock)
        self.last, self.last_at = out, self.clock()
        if not out.get("ok") or not self.enabled:
            return out
        open_keys = set()
        for item in out.get("items") or []:
            if item.get("kind") not in WAITING_ON_PERSON:
                continue
            key = f"{item.get('session_id')}:{item.get('kind')}"
            open_keys.add(key)
            waited = item.get("waited_min")
            if waited is None or waited < self.wait_min or key in self.notified:
                continue
            self.notified[key] = self.clock()
            data = {"session_id": item.get("session_id"), "kind": item.get("kind"),
                    "waited_min": waited, "label": item.get("label") or ""}
            try:
                self.emit(EVENT, data)
                self.sent += 1
            except Exception:  # noqa: BLE001 - a broken transport never stops the watch
                pass
        for key in list(self.notified):
            if key not in open_keys:
                del self.notified[key]
        return out

    def start(self) -> None:
        if not self.enabled or (self._thread and self._thread.is_alive()):
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="cassandra-faustus-watch", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception:  # noqa: BLE001
                pass
            self._stop.wait(self.interval_s)

    def status(self) -> dict[str, Any]:
        last = self.last or {}
        return {"enabled": self.enabled, "wait_min": self.wait_min, "interval_s": self.interval_s,
                "running": bool(self._thread and self._thread.is_alive()),
                "ok": last.get("ok"), "reason": last.get("reason"), "url": last.get("url"),
                "waiting_on_you": last.get("waiting_on_you"), "stalled": last.get("stalled"),
                "long_waits": last.get("long_waits") or [], "checked_at": self.last_at, "sent": self.sent}