"""Incidents: opened when a service that was up goes down (or its process is
replaced), closed when it answers again, with a context captured at open time.

The context is what an operator would look at first:

* ``correlated`` — the other services whose state changed within ±3 min
  (the part after the incident is filled in by later polls; the context is
  final three minutes after opening);
* ``gpu`` — memory and utilisation of every GPU just before, and the peak in
  the three minutes before;
* ``log_tail`` — the last lines of that service's logs;
* ``process`` — the pid that was listening, its command line, whether it is
  still alive;
* ``system`` — boot time (a reboot explains everything) and whether
  Cassandra itself was not running for a while before (sleep or shutdown).

``causes`` turns that into plain sentences ordered by how much they explain;
``probable_cause`` is the first one or two.
"""

from __future__ import annotations

import json
import re
import time
from typing import Any, Callable, Optional

from .times import clock, duration, iso

CORRELATION_S = 180.0
SAME_MINUTE_S = 60.0
GPU_LOOKBACK_S = 180.0
GPU_HIGH_PCT = 95.0
GPU_JUMP_PTS = 25.0
LOG_RELEVANT_S = 600.0
DOWN_STATES = ("down", "foreign")


def _row(row) -> dict[str, Any]:
    out = dict(row)
    out["context"] = json.loads(out.get("context") or "{}")
    out["actions"] = json.loads(out.get("actions") or "[]")
    out["open"] = out.get("closed_at") is None
    out["context_final"] = bool(out.get("context_final"))
    return out


class Incidents:
    def __init__(self, db, logs, name_of: Callable[[str], str], clock_fn: Callable[[], float] = time.time):
        self.db = db
        self.logs = logs
        self.name_of = name_of
        self.clock = clock_fn

    # ---------- lifecycle ----------
    def open(self, service: str, kind: str, from_state: str, to_state: str, detail: str, ts: float, extra: Optional[dict] = None) -> int:
        extra = extra or {}
        cur = self.db.execute(
            "INSERT INTO incidents(service, kind, opened_at, from_state, to_state, detail) VALUES (?, ?, ?, ?, ?, ?)",
            (service, kind, ts, from_state, to_state, detail),
        )
        incident_id = int(cur.lastrowid)
        base = {"process": extra.get("process"), "system": extra.get("system"), "port": extra.get("port"), "log_tail": None}
        self._refresh(incident_id, base, now=ts)
        if kind == "restart":
            self.close(service, ts, to_state, incident_id=incident_id)
        return incident_id

    def close(self, service: str, ts: float, to_state: str, incident_id: Optional[int] = None) -> Optional[int]:
        if incident_id is None:
            row = self.db.one("SELECT id FROM incidents WHERE service = ? AND closed_at IS NULL ORDER BY opened_at DESC LIMIT 1", (service,))
            if row is None:
                return None
            incident_id = row["id"]
        self.db.execute("UPDATE incidents SET closed_at = ? WHERE id = ? AND closed_at IS NULL", (ts, incident_id))
        self.add_action(incident_id, {"ts": ts, "kind": "recovered" if to_state == "up" else "closed", "detail": f"state {to_state}"})
        return incident_id

    def open_for(self, service: str) -> Optional[dict[str, Any]]:
        row = self.db.one("SELECT * FROM incidents WHERE service = ? AND closed_at IS NULL ORDER BY opened_at DESC LIMIT 1", (service,))
        return _row(row) if row else None

    def add_action(self, incident_id: int, action: dict[str, Any]) -> None:
        with self.db.transaction() as conn:
            row = conn.execute("SELECT actions FROM incidents WHERE id = ?", (incident_id,)).fetchone()
            if row is None:
                return
            actions = json.loads(row["actions"] or "[]")
            actions.append(action)
            conn.execute("UPDATE incidents SET actions = ? WHERE id = ?", (json.dumps(actions, ensure_ascii=False), incident_id))

    def refresh_pending(self) -> None:
        """Re-capture the context of incidents younger than the correlation window (the "+3 min" half)."""
        now = self.clock()
        for row in self.db.query("SELECT * FROM incidents WHERE context_final = 0"):
            item = _row(row)
            self._refresh(item["id"], item["context"], now=now)

    def _refresh(self, incident_id: int, base: dict[str, Any], now: float) -> None:
        row = self.db.one("SELECT * FROM incidents WHERE id = ?", (incident_id,))
        if row is None:
            return
        item = _row(row)
        context = self.capture(item, base)
        causes = self.causes(item, context)
        context["causes"] = causes
        probable = " ".join(c["text"] for c in causes[:2] if c["weight"] > 0) or (causes[0]["text"] if causes else "")
        final = 1 if now >= item["opened_at"] + CORRELATION_S else 0
        self.db.execute(
            "UPDATE incidents SET context = ?, probable_cause = ?, context_final = ? WHERE id = ?",
            (json.dumps(context, ensure_ascii=False), probable, final, incident_id),
        )

    # ---------- context ----------
    def capture(self, item: dict[str, Any], base: dict[str, Any]) -> dict[str, Any]:
        ts = item["opened_at"]
        service = item["service"]
        correlated = [
            {"service": r["service"], "name": self.name_of(r["service"]), "ts": r["ts"], "at": iso(r["ts"]), "kind": r["kind"],
             "from": r["from_state"], "to": r["to_state"], "delta_s": round(r["ts"] - ts, 1), "detail": r["detail"]}
            for r in self.db.query(
                "SELECT * FROM events WHERE ts BETWEEN ? AND ? AND service != ? AND kind IN ('state', 'pid') ORDER BY ts",
                (ts - CORRELATION_S, ts + CORRELATION_S, service),
            )
        ]
        gpu_rows = self.db.query("SELECT * FROM gpu_samples WHERE ts BETWEEN ? AND ? ORDER BY ts", (ts - GPU_LOOKBACK_S, ts + 1))
        gpus: dict[int, dict[str, Any]] = {}
        for r in gpu_rows:
            pct = round(100.0 * r["mem_used_mb"] / r["mem_total_mb"], 1) if r["mem_total_mb"] else 0.0
            g = gpus.setdefault(r["gpu"], {"gpu": r["gpu"], "min_pct": pct, "peak_pct": pct, "peak_ts": r["ts"], "min_ts": r["ts"], "last": None})
            if pct > g["peak_pct"]:
                g["peak_pct"], g["peak_ts"] = pct, r["ts"]
            if pct < g["min_pct"]:
                g["min_pct"], g["min_ts"] = pct, r["ts"]
            g["last"] = {"ts": r["ts"], "mem_used_mb": r["mem_used_mb"], "mem_total_mb": r["mem_total_mb"], "mem_pct": pct, "util_pct": r["util_pct"]}
        log_tail = base.get("log_tail")
        if not log_tail:
            log_tail = [
                {"ts": r["ts"], "at": iso(r["ts"]), "level": r["level"], "line": r["line"]}
                for r in self.logs.tail_for(service, ts + 5, 20)
            ]
        return {
            "correlated": correlated,
            "gpu": sorted(gpus.values(), key=lambda g: g["gpu"]),
            "log_tail": log_tail,
            "process": base.get("process"),
            "system": base.get("system"),
            "port": base.get("port"),
        }

    # ---------- heuristics ----------
    def causes(self, item: dict[str, Any], context: dict[str, Any]) -> list[dict[str, Any]]:
        ts = item["opened_at"]
        out: list[dict[str, Any]] = []
        system = context.get("system") or {}
        boot, prev_boot = system.get("boot_time"), system.get("previous_boot_time")
        if boot and prev_boot and boot - prev_boot > 30:
            out.append({"code": "reboot", "weight": 100, "text": f"The machine restarted (boot at {clock(boot)}, previous boot {clock(prev_boot)}): a reboot stops every service."})
        gap = system.get("gap")
        if gap:
            out.append({"code": "gap", "weight": 90, "text": f"Cassandra itself was not running between {clock(gap[0])} and {clock(gap[1])} (sleep, hibernation or shutdown?), so it happened somewhere in that gap."})
        fell = [c for c in context.get("correlated", []) if c["kind"] == "state" and c["to"] in DOWN_STATES and abs(c["delta_s"]) <= SAME_MINUTE_S]
        names = list(dict.fromkeys(c["name"] for c in fell))
        if len(names) + 1 >= 3:
            shown = ", ".join([self.name_of(item["service"]), *names[:5]]) + ("…" if len(names) > 5 else "")
            out.append({"code": "machine_wide", "weight": 80, "text": f"{len(names) + 1} services fell in the same minute ({shown}) → machine-wide event (sleep, shutdown, GPU driver reset?)."})
        for g in context.get("gpu", []):
            before = ts - g["peak_ts"]
            if g["peak_pct"] >= GPU_HIGH_PCT and before >= 0:
                verb = "jumped" if g["peak_pct"] - g["min_pct"] >= GPU_JUMP_PTS and g["min_ts"] < g["peak_ts"] else "was"
                out.append({"code": "vram", "weight": 70, "text": f"GPU {g['gpu']} memory {verb} {'to' if verb == 'jumped' else 'at'} {g['peak_pct']:.0f}% {int(before)} s before → VRAM contention (another model loading?)."})
            elif g["peak_pct"] - g["min_pct"] >= GPU_JUMP_PTS and g["min_ts"] < g["peak_ts"] <= ts:
                out.append({"code": "vram_jump", "weight": 40, "text": f"GPU {g['gpu']} memory jumped from {g['min_pct']:.0f}% to {g['peak_pct']:.0f}% {int(ts - g['peak_ts'])} s before."})
        log_cause = self._log_cause(context.get("log_tail") or [], ts)
        if log_cause:
            out.append(log_cause)
        if item["kind"] == "restart":
            out.append({"code": "replaced", "weight": 60, "text": f"The process was replaced without downtime ({item['detail']}): something restarted it (a reload, the launcher or a person)."})
        if item["to_state"] == "foreign":
            out.append({"code": "foreign", "weight": 65, "text": f"Another program now answers on port {context.get('port') or '?'} ({item['detail']}): the service is not running there any more."})
        proc = context.get("process") or {}
        if proc.get("pid") and item["kind"] != "restart":
            label = f"pid {proc['pid']}{' ' + proc['name'] if proc.get('name') else ''}"
            if proc.get("alive") is True:
                out.append({"code": "hung", "weight": 50, "text": f"The process ({label}) is still alive but does not answer: hung, overloaded or still loading."})
            elif proc.get("alive") is False:
                out.append({"code": "gone", "weight": 30, "text": f"The process ({label}) is gone: it exited, crashed or was closed."})
        machine_wide = any(c["code"] == "machine_wide" for c in out)
        others = [c for c in context.get("correlated", []) if not (machine_wide and c in fell)]
        if others and not machine_wide:
            shown = "; ".join(f"{c['name']} {c['from'] or '?'}→{c['to']} at {clock(c['ts'])}" for c in others[:4])
            out.append({"code": "nearby", "weight": 20, "text": f"Also changed within 3 min: {shown}."})
        if not out:
            out.append({"code": "unknown", "weight": 0, "text": "No clear cause in what Cassandra recorded (no reboot, no other service down, no GPU spike, no error in the logs)."})
        out.sort(key=lambda c: -c["weight"])
        return out

    @staticmethod
    def _log_cause(tail: list[dict[str, Any]], ts: float) -> Optional[dict[str, Any]]:
        recent = [line for line in tail if line["ts"] >= ts - LOG_RELEVANT_S]
        if not recent:
            return None
        text = [line["line"] for line in recent]
        joined = "\n".join(text)
        if re.search(r"out of memory|CUDA error|OutOfMemory|cudaMalloc failed|failed to allocate", joined, re.I):
            last = next(l for l in reversed(text) if re.search(r"out of memory|CUDA error|OutOfMemory|cudaMalloc|allocate", l, re.I))
            return {"code": "log_oom", "weight": 75, "text": f"The log reports running out of memory: “{last.strip()[:200]}”."}
        if any("Traceback (most recent call last)" in l for l in text):
            start = max(i for i, l in enumerate(text) if "Traceback (most recent call last)" in l)
            after = [l for l in text[start + 1 :] if re.match(r"^\s*[\w.]*(Error|Exception|Interrupt|Exit)\b", l)]
            last = (after[-1] if after else text[-1]).strip()
            return {"code": "log_traceback", "weight": 60, "text": f"The log ends with a Traceback: “{last[:200]}”."}
        if any(re.match(r"^\s*Killed\b", l) for l in text[-3:]):
            return {"code": "log_killed", "weight": 60, "text": "The log ends with “Killed”: the system or someone ended the process (out of RAM?)."}
        errors = [line for line in recent if line["level"] == "error"]
        if errors:
            return {"code": "log_error", "weight": 45, "text": f"Last error in the log at {clock(errors[-1]['ts'])}: “{errors[-1]['line'].strip()[:200]}”."}
        return None

    # ---------- queries ----------
    def list(self, since: Optional[float] = None, until: Optional[float] = None, service: Optional[str] = None,
             open_only: bool = False, limit: int = 50) -> list[dict[str, Any]]:
        where, params = [], []
        if since is not None:
            # An incident overlaps the window when it opened before its end and closed after its start (or is open).
            where.append("(closed_at IS NULL OR closed_at >= ?)")
            params.append(since)
        if until is not None:
            where.append("opened_at <= ?")
            params.append(until)
        if service:
            where.append("service = ?")
            params.append(service)
        if open_only:
            where.append("closed_at IS NULL")
        sql = "SELECT * FROM incidents" + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY opened_at DESC LIMIT ?"
        params.append(int(limit))
        return [_row(r) for r in self.db.query(sql, params)]

    def get(self, incident_id: int) -> Optional[dict[str, Any]]:
        row = self.db.one("SELECT * FROM incidents WHERE id = ?", (incident_id,))
        return _row(row) if row else None

    def latest(self, service: str) -> Optional[dict[str, Any]]:
        row = self.db.one("SELECT * FROM incidents WHERE service = ? ORDER BY opened_at DESC LIMIT 1", (service,))
        return _row(row) if row else None

    def counts(self) -> dict[str, int]:
        now = self.clock()
        open_n = self.db.one("SELECT COUNT(*) AS n FROM incidents WHERE closed_at IS NULL")["n"]
        day = self.db.one("SELECT COUNT(*) AS n FROM incidents WHERE opened_at >= ?", (now - 86400,))["n"]
        return {"open": open_n, "last_24h": day}

    def prune(self, retention_days: int) -> int:
        horizon = self.clock() - retention_days * 86400
        return self.db.execute("DELETE FROM incidents WHERE closed_at IS NOT NULL AND closed_at < ?", (horizon,)).rowcount


def explain(item: dict[str, Any], name: str, port: Optional[int], now: float) -> list[str]:
    """The incident as plain sentences (what svc_why_down returns)."""
    ctx = item.get("context") or {}
    where = f" (port {port})" if port else ""
    sentences = []
    if item["kind"] == "restart":
        sentences.append(f"{name}{where} was restarted at {clock(item['opened_at'])}: {item['detail']}.")
    else:
        sentences.append(f"{name}{where} went {item['to_state']} at {clock(item['opened_at'])} (was {item['from_state']}; {item['detail'] or 'no answer'}).")
        if item.get("closed_at"):
            sentences.append(f"It came back at {clock(item['closed_at'])}, after {duration(item['closed_at'] - item['opened_at'])}.")
        else:
            sentences.append(f"It is still down ({duration(now - item['opened_at'])} so far).")
    if item.get("probable_cause"):
        sentences.append(f"Probable cause: {item['probable_cause']}")
    for cause in (ctx.get("causes") or [])[2:5]:
        if cause["weight"] > 0:
            sentences.append(cause["text"])
    gpus = ctx.get("gpu") or []
    if gpus:
        parts = []
        for g in gpus:
            last = g.get("last") or {}
            if last:
                parts.append(f"GPU {g['gpu']} {last['mem_used_mb'] / 1024:.1f}/{last['mem_total_mb'] / 1024:.1f} GB ({last['mem_pct']:.0f}%, util {last.get('util_pct') if last.get('util_pct') is not None else '?'}%)")
        if parts:
            sentences.append("GPUs just before: " + "; ".join(parts) + ".")
    tail = ctx.get("log_tail") or []
    if tail:
        sentences.append("Last log lines: " + " | ".join(line["line"].strip()[:160] for line in tail[-3:]))
    restarts = [a for a in item.get("actions") or [] if a.get("kind") == "restart"]
    for action in restarts[-3:]:
        sentences.append(f"Restart attempt at {clock(action['ts'])} ({action.get('trigger', 'manual')}, {action.get('method', '?')}): {'ok' if action.get('ok') else 'failed'} — {action.get('detail', '')}".rstrip(" —"))
    return sentences
