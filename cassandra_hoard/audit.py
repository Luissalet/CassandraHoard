"""The audit trail: what the assistant (and the apps) *did*, next to what
went *down*.

Since Hoard Link 0.4 every app posts events to the Hoard Hub's bus: one
``agent.call`` per tool the assistant ran (tool, ok, ms, who asked), the
app's own milestones (``scribe.transcript.done``, ``links.watch.new``…)
and the hub's (``hub.app.started``, ``hub.backup.done``, ``hub.rule.ran``).
The hub keeps a rolling window of them; Cassandra keeps them for good,
here, in ``bus_events`` (its retention rules apply), so "who restarted
Vulcan at 03:12?", "which tool failed most this week?" and "what did the
assistant do right before Borges went down?" have an answer.

``BusMirror`` polls ``GET <hub>/api/events?since_id=`` on a thread (no
token needed: the hub answers loopback reads), stores what is new, and
tells the hub about Cassandra's own incidents (``cassandra.incident.opened``
/ ``closed``) through the vendored ``hoard_link.family``, so a hub rule
can react to a service going down. When the hub is not running nothing
breaks: the mirror retries quietly and the tools say "no events".

``secrets_audit`` is the other half of the watchman: for every app folder
Cassandra knows, does the token file exist, is the data folder
git-ignored, and is any secret-looking file tracked by git.
"""

from __future__ import annotations

import fnmatch
import json
import logging
import os
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional

import httpx

log = logging.getLogger("cassandra.audit")

POLL_S = 10.0
BATCH = 500
SECRET_PATTERNS = ("mcp-token", "*.token", "token", "*.pem", "*.key", "id_rsa*", ".env", ".env.*", "*secret*",
                   "*password*", "credentials*", "*.db", "*.sqlite", "*.sqlite3")
SECRET_FOLDERS = ("data", "data-demo", ".venv", "venv")


class BusMirror:
    def __init__(self, db: Any, hub_url: str, *, clock_fn: Callable[[], float] = time.time, poll_s: float = POLL_S,
                 client: Optional[httpx.Client] = None, incidents: Any = None, emit: Optional[Callable[..., Any]] = None):
        self.db = db
        self.hub_url = hub_url.rstrip("/")
        self.clock = clock_fn
        self.poll_s = poll_s
        self._client = client
        self._incidents = incidents
        self._emit = emit
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.last_hub_id = self._max_hub_id()
        self.last_sync: Optional[float] = None
        self.last_error: Optional[str] = None
        self.synced = 0
        # Incidents that exist when the mirror starts were reported by the
        # previous run (or predate it): only their *closing* is news now.
        self._reported_incidents: set[int] = set()
        self._seen_open: dict[int, bool] = {}
        try:
            for r in self.db.query("SELECT id, closed_at FROM incidents ORDER BY id DESC LIMIT 500"):
                self._reported_incidents.add(int(r["id"]))
                self._seen_open[int(r["id"])] = r["closed_at"] is None
        except Exception:  # noqa: BLE001
            pass

    # ---------- storage ----------
    def _max_hub_id(self) -> int:
        row = self.db.one("SELECT MAX(hub_id) AS m FROM bus_events")
        return int(row["m"] or 0) if row else 0

    def store(self, events: list[dict[str, Any]]) -> int:
        n = 0
        with self.db.lock:
            for ev in events:
                try:
                    hub_id = int(ev.get("id") or 0)
                    ts = float(ev.get("ts") or self.clock())
                except (TypeError, ValueError):
                    continue
                data = ev.get("data") if isinstance(ev.get("data"), dict) else {}
                cur = self.db.execute(
                    "INSERT OR IGNORE INTO bus_events(hub_id, ts, type, source, tool, ok, ms, caller, data) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (hub_id, ts, str(ev.get("type") or ""), str(ev.get("source") or ""),
                     str(data.get("tool")) if data.get("tool") is not None else None,
                     None if data.get("ok") is None else (1 if data.get("ok") else 0),
                     data.get("ms") if isinstance(data.get("ms"), (int, float)) else None,
                     str(data.get("caller")) if data.get("caller") else None,
                     json.dumps(data, ensure_ascii=False, default=str)),
                )
                if cur.rowcount:
                    n += 1
                if hub_id > self.last_hub_id:
                    self.last_hub_id = hub_id
        self.synced += n
        return n

    # ---------- syncing ----------
    def _http(self) -> httpx.Client:
        return self._client or httpx.Client(timeout=5.0, trust_env=False)

    def sync_once(self) -> int:
        """One poll: fetch what the hub has after ``last_hub_id``."""
        total = 0
        client = self._http()
        try:
            while True:
                try:
                    resp = client.get(f"{self.hub_url}/api/events", params={"since_id": self.last_hub_id, "limit": BATCH, "order": "asc"})
                except httpx.HTTPError as exc:
                    self.last_error = f"hub unreachable: {type(exc).__name__}"
                    return total
                if resp.status_code != 200:
                    self.last_error = f"hub answered {resp.status_code}"
                    return total
                body = resp.json()
                events = body.get("events") or []
                if not events:
                    break
                if int(body.get("last_id") or 0) < self.last_hub_id:
                    # The hub's log was reset (new data folder): start over from its ids.
                    self.last_hub_id = 0
                    continue
                total += self.store(events)
                if len(events) < BATCH:
                    break
            self.last_error = None
            self.last_sync = self.clock()
            return total
        finally:
            if self._client is None:
                client.close()

    def report_incidents(self) -> int:
        """Tell the hub about incidents opened/closed since the last look."""
        if self._incidents is None or self._emit is None:
            return 0
        sent = 0
        rows = self.db.query("SELECT id, service, kind, opened_at, closed_at, to_state, detail, probable_cause FROM incidents ORDER BY id DESC LIMIT 50")
        for r in rows:
            iid = int(r["id"])
            is_open = r["closed_at"] is None
            prev = self._seen_open.get(iid)
            if prev is None and iid not in self._reported_incidents:
                if r["kind"] != "restart" and (self.clock() - float(r["opened_at"])) < 3600:
                    self._emit("cassandra.incident.opened", {"incident_id": iid, "app": r["service"], "kind": r["kind"],
                                                            "to_state": r["to_state"], "detail": (r["detail"] or "")[:200],
                                                            "probable_cause": (r["probable_cause"] or "")[:200]})
                    sent += 1
                self._reported_incidents.add(iid)
            elif prev is True and not is_open:
                self._emit("cassandra.incident.closed", {"incident_id": iid, "app": r["service"], "to_state": r["to_state"],
                                                        "duration_s": int(float(r["closed_at"]) - float(r["opened_at"]))})
                sent += 1
            self._seen_open[iid] = is_open
        if len(self._seen_open) > 500:
            for k in sorted(self._seen_open)[:-200]:
                self._seen_open.pop(k, None)
        return sent

    def tick(self) -> None:
        try:
            self.sync_once()
        except Exception as exc:  # noqa: BLE001
            self.last_error = f"{type(exc).__name__}: {exc}"
        try:
            self.report_incidents()
        except Exception as exc:  # noqa: BLE001
            log.debug("incident report failed: %s", exc)

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="cassandra-bus", daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        while not self._stop.is_set():
            self.tick()
            self._stop.wait(self.poll_s)

    def stop(self) -> None:
        self._stop.set()

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def status(self) -> dict[str, Any]:
        row = self.db.one("SELECT COUNT(*) AS n, MIN(ts) AS a, MAX(ts) AS b FROM bus_events")
        return {"hub_url": self.hub_url, "running": self.running, "last_sync": self.last_sync, "last_error": self.last_error,
                "last_hub_id": self.last_hub_id, "stored": int(row["n"] or 0) if row else 0,
                "first_ts": row["a"] if row else None, "last_ts": row["b"] if row else None}

    # ---------- queries ----------
    @staticmethod
    def _row(r: Any) -> dict[str, Any]:
        try:
            data = json.loads(r["data"])
        except (TypeError, ValueError):
            data = {}
        return {"id": r["hub_id"], "ts": r["ts"], "type": r["type"], "source": r["source"], "tool": r["tool"],
                "ok": None if r["ok"] is None else bool(r["ok"]), "ms": r["ms"], "caller": r["caller"], "data": data}

    def search(self, *, query: str = "", type: Optional[str] = None, source: Optional[str] = None, tool: Optional[str] = None,
               ok: Optional[bool] = None, since: Optional[float] = None, until: Optional[float] = None, limit: int = 50) -> list[dict[str, Any]]:
        where, params = ["1=1"], []
        if source:
            where.append("source = ?"); params.append(source)
        if tool:
            where.append("tool = ?"); params.append(tool)
        if ok is not None:
            where.append("ok = ?"); params.append(1 if ok else 0)
        if since is not None:
            where.append("ts >= ?"); params.append(float(since))
        if until is not None:
            where.append("ts <= ?"); params.append(float(until))
        for word in (query or "").split():
            where.append("(type LIKE ? OR data LIKE ? OR source LIKE ?)"); params += [f"%{word}%"] * 3
        limit = max(1, min(int(limit), 500))
        rows = self.db.query(f"SELECT * FROM bus_events WHERE {' AND '.join(where)} ORDER BY ts DESC LIMIT ?",
                             (*params, limit * (4 if type and "*" in type else 1)))
        out = [self._row(r) for r in rows]
        if type:
            pats = [p.strip() for p in type.split("|") if p.strip()]
            out = [e for e in out if any(fnmatch.fnmatchcase(e["type"], p) for p in pats)][:limit]
        return out

    def around(self, ts: float, minutes: float = 3.0, limit: int = 30) -> list[dict[str, Any]]:
        return self.search(since=ts - minutes * 60, until=ts + minutes * 60, limit=limit)

    def stats(self, since: Optional[float] = None, until: Optional[float] = None) -> dict[str, Any]:
        where, params = ["1=1"], []
        if since is not None:
            where.append("ts >= ?"); params.append(float(since))
        if until is not None:
            where.append("ts <= ?"); params.append(float(until))
        w = " AND ".join(where)
        total = self.db.one(f"SELECT COUNT(*) AS n FROM bus_events WHERE {w}", params)
        by_type = self.db.query(f"SELECT type, COUNT(*) AS n FROM bus_events WHERE {w} GROUP BY type ORDER BY n DESC LIMIT 40", params)
        by_source = self.db.query(f"SELECT source, COUNT(*) AS n FROM bus_events WHERE {w} GROUP BY source ORDER BY n DESC", params)
        calls = self.db.query(
            f"SELECT source, tool, COUNT(*) AS n, SUM(CASE WHEN ok = 0 THEN 1 ELSE 0 END) AS failed, AVG(ms) AS avg_ms, MAX(ms) AS max_ms "
            f"FROM bus_events WHERE {w} AND type = 'agent.call' GROUP BY source, tool ORDER BY n DESC LIMIT 60", params)
        callers = self.db.query(f"SELECT caller, COUNT(*) AS n FROM bus_events WHERE {w} AND type = 'agent.call' AND caller IS NOT NULL "
                                f"GROUP BY caller ORDER BY n DESC LIMIT 20", params)
        failures = self.db.query(f"SELECT * FROM bus_events WHERE {w} AND type = 'agent.call' AND ok = 0 ORDER BY ts DESC LIMIT 10", params)
        return {
            "total": int(total["n"] or 0) if total else 0,
            "by_type": [{"type": r["type"], "count": r["n"]} for r in by_type],
            "by_source": [{"source": r["source"], "count": r["n"]} for r in by_source],
            "agent_calls": [{"app": r["source"], "tool": r["tool"], "count": r["n"], "failed": r["failed"],
                             "avg_ms": round(r["avg_ms"]) if r["avg_ms"] is not None else None, "max_ms": r["max_ms"]} for r in calls],
            "callers": [{"caller": r["caller"], "count": r["n"]} for r in callers],
            "recent_failures": [self._row(r) for r in failures],
        }

    def prune(self, before_ts: float) -> int:
        cur = self.db.execute("DELETE FROM bus_events WHERE ts < ?", (float(before_ts),))
        return int(cur.rowcount or 0)


# ---------------------------------------------------------------------------
# secrets
# ---------------------------------------------------------------------------

def _gitignore_covers(folder: Path, rel: str) -> Optional[bool]:
    try:
        lines = [l.strip() for l in (folder / ".gitignore").read_text(encoding="utf-8-sig").splitlines()]
    except OSError:
        return None
    rel = rel.strip("/").replace("\\", "/")
    for l in lines:
        if not l or l.startswith("#"):
            continue
        pat = l.strip("/").rstrip("/")
        if pat in (rel, rel.split("/")[0], "**/" + rel) or fnmatch.fnmatchcase(rel, pat):
            return True
    return False


def _tracked_files(folder: Path, timeout: float = 10.0) -> Optional[list[str]]:
    if not (folder / ".git").exists():
        return None
    try:
        out = subprocess.run(["git", "-C", str(folder), "ls-files", "-z"], capture_output=True, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if out.returncode != 0:
        return None
    return [p for p in out.stdout.decode("utf-8", "replace").split("\0") if p]


def _looks_secret(path: str) -> bool:
    parts = path.replace("\\", "/").split("/")
    name = parts[-1]
    # Only a top-level data/ folder is the app's runtime data (a package's own
    # data/ subfolder holds fixtures and code, not secrets).
    if len(parts) > 1 and parts[0] in SECRET_FOLDERS and not name.endswith((".md", ".json", ".txt", ".example", ".py", ".tsv", ".csv")):
        return True
    return any(fnmatch.fnmatchcase(name.lower(), pat) for pat in SECRET_PATTERNS)


def audit_folder(folder: str, app_id: str, name: str, *, token_file: Optional[str] = None, now: Optional[float] = None) -> dict[str, Any]:
    f = Path(folder)
    now = now or time.time()
    line: dict[str, Any] = {"id": app_id, "name": name, "folder": str(f), "problems": []}
    if not f.is_dir():
        line["problems"].append("folder missing")
        return line
    tf = Path(token_file) if token_file else f / "data" / "mcp-token"
    line["token_file"] = str(tf)
    line["token_present"] = tf.is_file() and bool(tf.read_text(encoding="utf-8-sig", errors="replace").strip()) if tf.is_file() else False
    if tf.is_file():
        try:
            st = tf.stat()
            line["token_age_h"] = round((now - st.st_mtime) / 3600, 1)
            if os.name != "nt":
                line["token_mode"] = oct(st.st_mode & 0o777)
                if st.st_mode & 0o077:
                    line["problems"].append(f"token file readable by others ({line['token_mode']})")
        except OSError:
            pass
    else:
        line["problems"].append("no agent token file (the app has not run, or writes it elsewhere)")
    data_rel = "data"
    line["data_gitignored"] = _gitignore_covers(f, data_rel)
    if line["data_gitignored"] is False:
        line["problems"].append("data/ is not in .gitignore")
    elif line["data_gitignored"] is None:
        line["problems"].append("no .gitignore")
    tracked = _tracked_files(f)
    line["git"] = tracked is not None
    if tracked is not None:
        leaks = [p for p in tracked if _looks_secret(p)]
        line["tracked_secret_like"] = leaks[:20]
        if leaks:
            line["problems"].append(f"{len(leaks)} secret-looking file(s) tracked by git: " + ", ".join(leaks[:5]))
    env_files = [str(p.relative_to(f)) for p in f.glob(".env*") if p.is_file()]
    if env_files:
        line["env_files"] = env_files
        for e in env_files:
            if tracked is not None and e in tracked:
                line["problems"].append(f"{e} is tracked by git")
    line["ok"] = not line["problems"]
    return line


def secrets_audit(services: list[Any], *, now: Optional[float] = None) -> dict[str, Any]:
    lines = []
    for s in services:
        folder = getattr(s, "folder", "") or ""
        if not folder:
            continue
        lines.append(audit_folder(folder, s.id, s.name, now=now))
    problems = [l for l in lines if not l.get("ok")]
    return {"checked": len(lines), "ok": len(lines) - len(problems), "with_problems": len(problems),
            "apps": lines, "summary": [f"{l['name']}: " + "; ".join(l["problems"]) for l in problems]}
