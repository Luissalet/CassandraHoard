"""Incidents from failed jobs, read off the mirrored family bus.

Every Hoard app reports its long work to the hub as ``<app>.job.queued|started|progress|done|failed`` (and the hub adds
``work.failed`` when one fails). The bus mirror hands every new event to :meth:`JobIncidents.process`:

* ``<app>.job.failed`` (or ``work.failed``, whose ``app`` field names the app) opens an incident of kind ``job``: service = the
  app, probable cause = the error, context = the job and its own events from the bus. One incident per app and job kind stays
  open at a time: another failure of the same app and kind while it is open is added to it as an action (the newest error
  becomes the cause) instead of opening a second card;
* ``<app>.job.done`` with the same job kind closes it (the work is running again);
* an incident nobody closed is closed after ``EXPIRE_S`` (24 h) without a new failure.

A failure older than ``EXPIRE_S`` (the first sync of a long bus history) opens nothing. Job incidents never take part in the
down/up bookkeeping of the poller (:meth:`Incidents.open_for` and :meth:`Incidents.close` leave them alone).
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Callable, Optional

from .incidents import Incidents
from .times import clock

log = logging.getLogger("cassandra.jobs")

EXPIRE_S = 24 * 3600.0
EVENT_CONTEXT_LIMIT = 40


def _text(value: Any, n: int) -> str:
    return " ".join(str(value if value is not None else "").split())[:n]


class JobIncidents:
    def __init__(self, db: Any, incidents: Incidents, *, clock_fn: Callable[[], float] = time.time, expire_s: float = EXPIRE_S):
        self.db, self.incidents, self.clock, self.expire_s = db, incidents, clock_fn, float(expire_s)

    # ------------------------------------------------------------------ reading events
    @staticmethod
    def _split(event: dict[str, Any]) -> tuple[str, str, dict[str, Any]]:
        """``(verb, app, data)`` for the events that matter, else ``("", "", {})``. ``verb`` is ``failed`` or ``done``."""
        etype = str(event.get("type") or "")
        data = event.get("data") if isinstance(event.get("data"), dict) else {}
        if etype == "work.failed":
            return "failed", str(data.get("app") or ""), data
        parts = etype.split(".")
        if len(parts) == 3 and parts[1] == "job" and parts[2] in ("failed", "done"):
            return parts[2], parts[0], data
        return "", "", {}

    def _events_of(self, app: str, job_id: str, since: float) -> list[dict[str, Any]]:
        """The bus events of one job, oldest first."""
        if not job_id:
            return []
        out = []
        for row in self.db.query("SELECT hub_id, ts, type, data FROM bus_events WHERE source = ? AND type LIKE ? AND ts >= ? ORDER BY ts, hub_id LIMIT 500",
                                 (app, f"{app}.job.%", since)):
            try:
                data = json.loads(row["data"])
            except (TypeError, ValueError):
                continue
            if isinstance(data, dict) and str(data.get("job_id") or "") == job_id:
                out.append({"ts": row["ts"], "at": clock(row["ts"]), "type": row["type"], "progress": data.get("progress"),
                            "error": _text(data.get("error"), 200) or None, "gpu": data.get("gpu")})
        return out[-EVENT_CONTEXT_LIMIT:]

    def _kind_from_bus(self, app: str, job_id: str) -> str:
        if not job_id:
            return ""
        for row in self.db.query("SELECT data FROM bus_events WHERE source = ? AND type LIKE ? ORDER BY ts DESC LIMIT 200", (app, f"{app}.job.%")):
            try:
                data = json.loads(row["data"])
            except (TypeError, ValueError):
                continue
            if isinstance(data, dict) and str(data.get("job_id") or "") == job_id and data.get("kind"):
                return str(data["kind"])
        return ""

    # ------------------------------------------------------------------ open incidents
    def _open_of(self, app: str, kind: Optional[str] = None) -> list[dict[str, Any]]:
        rows = self.db.query("SELECT id FROM incidents WHERE service = ? AND kind = 'job' AND closed_at IS NULL ORDER BY opened_at DESC", (app,))
        items = [self.incidents.get(int(r["id"])) for r in rows]
        items = [i for i in items if i]
        if kind is not None:
            items = [i for i in items if str(((i["context"] or {}).get("job") or {}).get("kind") or "") == kind]
        return items

    # ------------------------------------------------------------------ the entry point
    def process(self, events: list[dict[str, Any]]) -> dict[str, int]:
        """Look at newly stored bus events (oldest first). Returns ``{opened, updated, closed}`` for tests and logs."""
        counts = {"opened": 0, "updated": 0, "closed": 0}
        for event in events:
            try:
                verb, app, data = self._split(event)
                if not verb or not app:
                    continue
                ts = float(event.get("ts") or self.clock())
                if verb == "failed":
                    result = self._failed(app, data, ts)
                    if result in counts:
                        counts[result] += 1
                else:
                    counts["closed"] += self._done(app, data, ts)
            except Exception:  # noqa: BLE001 — the mirror must keep going
                log.exception("job incident handling failed for %s", event.get("type"))
        return counts

    def _failed(self, app: str, data: dict[str, Any], ts: float) -> str:
        """Open or update the incident of this failure. Returns ``opened``, ``updated`` or ``skipped``."""
        job_id = _text(data.get("job_id"), 80)
        kind = _text(data.get("kind"), 40) or self._kind_from_bus(app, job_id)
        title = _text(data.get("title"), 160) or job_id or kind or app
        error = _text(data.get("error"), 300)
        if self.clock() - ts > self.expire_s:
            return "skipped"
        existing = self._open_of(app)
        for item in existing:                     # the same job reported twice (<app>.job.failed, then work.failed)
            if job_id and str(((item["context"] or {}).get("job") or {}).get("job_id") or "") == job_id:
                return "skipped"
        same_kind = [i for i in existing if str(((i["context"] or {}).get("job") or {}).get("kind") or "") == kind]
        since = ts - self.expire_s
        job = {"job_id": job_id, "title": title, "kind": kind, "error": error, "url": _text(data.get("url"), 300), "ts": ts}
        events = self._events_of(app, job_id, since)
        name = self.incidents.name_of(app)
        what = f"{kind} " if kind else ""
        cause = f"The {what}job “{title}” of {name} failed" + (f": {error}" if error else " (no error text was reported).")
        if same_kind:
            item = same_kind[0]
            context = dict(item["context"])
            context["job"] = {**job, "failures": int((context.get("job") or {}).get("failures") or 1) + 1}
            context["job_events"] = events
            context["causes"] = [{"code": "job_failed", "weight": 100, "text": cause}]
            self.db.execute("UPDATE incidents SET detail = ?, probable_cause = ?, context = ? WHERE id = ?",
                            (f"{title}: {error}"[:500] if error else title[:500], cause[:500], json.dumps(context, ensure_ascii=False), item["id"]))
            self.incidents.add_action(item["id"], {"ts": ts, "kind": "failed_again", "detail": f"{title}: {error}"[:300]})
            return "updated"
        context = {"correlated": [], "gpu": [], "log_tail": [], "process": None, "system": None, "port": None,
                   "job": {**job, "failures": 1}, "job_events": events,
                   "causes": [{"code": "job_failed", "weight": 100, "text": cause}]}
        self.db.execute(
            "INSERT INTO incidents(service, kind, opened_at, from_state, to_state, detail, probable_cause, context, context_final) VALUES (?, 'job', ?, 'running', 'failed', ?, ?, ?, 1)",
            (app, ts, (f"{title}: {error}" if error else title)[:500], cause[:500], json.dumps(context, ensure_ascii=False)))
        return "opened"

    def _done(self, app: str, data: dict[str, Any], ts: float) -> int:
        kind = _text(data.get("kind"), 40)
        closed = 0
        for item in self._open_of(app, kind):
            if ts < float(item["opened_at"]):      # a "done" older than the failure cannot close it
                continue
            self.incidents.close(item["service"], ts, "up", incident_id=item["id"])
            self.incidents.add_action(item["id"], {"ts": ts, "kind": "job_done", "detail": _text(data.get("title"), 160) or "a later job of the same kind finished"})
            closed += 1
        return closed

    # ------------------------------------------------------------------ expiry
    def expire(self, now: Optional[float] = None) -> int:
        """Close the job incidents without a new failure for ``expire_s``."""
        now = self.clock() if now is None else now
        closed = 0
        for row in self.db.query("SELECT id FROM incidents WHERE kind = 'job' AND closed_at IS NULL"):
            item = self.incidents.get(int(row["id"]))
            if not item:
                continue
            last = float(((item["context"] or {}).get("job") or {}).get("ts") or item["opened_at"])
            if now - last >= self.expire_s:
                self.incidents.close(item["service"], now, "closed", incident_id=item["id"])
                self.incidents.add_action(item["id"], {"ts": now, "kind": "expired", "detail": f"no new failure for {int(self.expire_s // 3600)} h"})
                closed += 1
        return closed
