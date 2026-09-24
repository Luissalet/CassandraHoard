"""Read-only views over the stored history: lanes, per-service history, GPU timeline."""

from __future__ import annotations

from typing import Any, Optional

from .times import clock, iso


def gaps(db, since: float, until: float) -> list[list[float]]:
    """Periods when Cassandra was not running (sleep, shutdown, closed), clipped to the window."""
    rows = db.query("SELECT ts, until_ts FROM events WHERE service = 'system' AND kind = 'gap' AND until_ts >= ? AND ts <= ? ORDER BY ts", (since, until))
    return [[max(since, r["ts"]), min(until, r["until_ts"])] for r in rows]


def lanes(db, service_ids: list[str], since: float, until: float, ever_up: set[str]) -> dict[str, list[list[Any]]]:
    """``{service: [[start, end, state], ...]}`` — consecutive samples with the same state collapsed."""
    out: dict[str, list[list[Any]]] = {sid: [] for sid in service_ids}
    state: dict[str, Optional[str]] = {}
    start: dict[str, float] = {}
    for r in db.query(
        "SELECT s.service, s.state FROM samples s JOIN (SELECT service, MAX(ts) AS ts FROM samples WHERE ts < ? GROUP BY service) m "
        "ON m.service = s.service AND m.ts = s.ts", (since,)
    ):
        if r["service"] in out:
            state[r["service"]] = r["state"]
            start[r["service"]] = since
    for r in db.query("SELECT service, ts, state FROM samples WHERE ts >= ? AND ts <= ? ORDER BY ts, id", (since, until)):
        sid = r["service"]
        if sid not in out:
            continue
        if state.get(sid) != r["state"]:
            if state.get(sid) is not None:
                out[sid].append([start[sid], r["ts"], state[sid]])
            state[sid] = r["state"]
            start[sid] = r["ts"]
    for sid in service_ids:
        if state.get(sid) is not None:
            out[sid].append([start[sid], until, state[sid]])
        if sid not in ever_up:
            for seg in out[sid]:
                if seg[2] == "down":
                    seg[2] = "never_seen"
    return out


def uptime_pct(segments: list[list[Any]], since: float, until: float) -> Optional[float]:
    known = sum(seg[1] - seg[0] for seg in segments if seg[2] != "never_seen")
    if known <= 0:
        return None
    up = sum(seg[1] - seg[0] for seg in segments if seg[2] in ("up", "degraded"))
    return round(100.0 * up / known, 2)


def history(db, service_id: str, since: float, until: float, limit: int = 200) -> dict[str, Any]:
    first = db.one("SELECT state, ts FROM samples WHERE service = ? AND ts < ? ORDER BY ts DESC LIMIT 1", (service_id, since))
    rows = db.query(
        "SELECT ts, kind, from_state, to_state, detail FROM events WHERE service = ? AND ts BETWEEN ? AND ? ORDER BY ts DESC LIMIT ?",
        (service_id, since, until, int(limit)),
    )
    changes = [
        {"ts": r["ts"], "at": iso(r["ts"]), "kind": r["kind"], "from": r["from_state"], "to": r["to_state"], "detail": r["detail"]}
        for r in reversed(rows)
    ]
    return {"state_at_since": first["state"] if first else None, "changes": changes, "truncated": len(rows) >= limit}


def gpu_timeline(db, since: float, until: float, gpu: Optional[int] = None, points: int = 60) -> dict[str, Any]:
    params: list[Any] = [since, until]
    sql = "SELECT ts, gpu, mem_used_mb, mem_total_mb, util_pct FROM gpu_samples WHERE ts BETWEEN ? AND ?"
    if gpu is not None:
        sql += " AND gpu = ?"
        params.append(gpu)
    rows = db.query(sql + " ORDER BY ts", params)
    points = max(2, min(1000, int(points)))
    step = max(1.0, (until - since) / points)
    per_gpu: dict[int, dict[str, Any]] = {}
    for r in rows:
        g = per_gpu.setdefault(r["gpu"], {"gpu": r["gpu"], "mem_total_mb": r["mem_total_mb"], "buckets": {}, "peak_mem": None, "peak_util": None, "last": None})
        pct = 100.0 * r["mem_used_mb"] / r["mem_total_mb"] if r["mem_total_mb"] else 0.0
        index = int((r["ts"] - since) // step)
        b = g["buckets"].setdefault(index, {"mem_max": 0.0, "mem_sum": 0.0, "util_max": None, "n": 0})
        b["mem_max"] = max(b["mem_max"], pct)
        b["mem_sum"] += pct
        b["n"] += 1
        if r["util_pct"] is not None:
            b["util_max"] = max(b["util_max"] or 0.0, r["util_pct"])
        if g["peak_mem"] is None or r["mem_used_mb"] > g["peak_mem"]["mem_used_mb"]:
            g["peak_mem"] = {"ts": r["ts"], "at": clock(r["ts"]), "mem_used_mb": r["mem_used_mb"], "mem_pct": round(pct, 1)}
        if r["util_pct"] is not None and (g["peak_util"] is None or r["util_pct"] > g["peak_util"]["util_pct"]):
            g["peak_util"] = {"ts": r["ts"], "at": clock(r["ts"]), "util_pct": r["util_pct"]}
        g["last"] = {"ts": r["ts"], "at": clock(r["ts"]), "mem_used_mb": r["mem_used_mb"], "mem_total_mb": r["mem_total_mb"],
                     "mem_free_mb": round(r["mem_total_mb"] - r["mem_used_mb"], 1), "mem_pct": round(pct, 1), "util_pct": r["util_pct"]}
    gpus = []
    for g in sorted(per_gpu.values(), key=lambda x: x["gpu"]):
        series = [
            [round(since + i * step), round(b["mem_max"], 1), round(b["mem_sum"] / b["n"], 1), None if b["util_max"] is None else round(b["util_max"], 1)]
            for i, b in sorted(g["buckets"].items())
        ]
        gpus.append({"gpu": g["gpu"], "mem_total_mb": g["mem_total_mb"], "now": g["last"], "peak_mem": g["peak_mem"], "peak_util": g["peak_util"],
                     "series": series})
    return {"since": iso(since), "until": iso(until), "step_s": round(step), "series_columns": ["ts", "mem_pct_max", "mem_pct_avg", "util_pct_max"],
            "gpus": gpus, "samples": len(rows)}
