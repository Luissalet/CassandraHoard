"""Incremental log tailing and search.

Sources (each a glob tagged with a service, or tagged by file name):

* every service's ``log_globs`` (a discovered app: ``<app>/data/logs/*.log``;
  a user service: its ``log_paths``);
* the launcher's own ``data/logs/<id>.log`` (the output of the apps it
  started) and Cassandra's ``data/logs/<id>.log`` (the apps it restarted):
  tagged by file name;
* ``%LOCALAPPDATA%\\Hoards\\*.log`` on Windows, tagged by file name;
* ``CASSANDRA_LOG_GLOBS``: ``glob`` (tagged by file name) or ``service=glob``.

A file seen for the first time is read from its last 64 KB only; after that
each tick reads what was appended (a shrunk file is a rotation: start again
from 0). Only complete lines are stored; a partial last line waits for the
next tick. Each line gets a time (parsed from the line when it carries one,
otherwise the previous timestamp in the same file, otherwise the file's
modification time) and a level from a heuristic.
"""

from __future__ import annotations

import glob
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Iterable, Optional

FIRST_READ_BYTES = 64 * 1024
MAX_READ_BYTES = 1024 * 1024
MAX_FILES = 400
MAX_LINE = 2000

_TS_PATTERNS = [
    re.compile(r"(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2}):(\d{2})(?:[.,](\d{1,6}))?"),
    re.compile(r"(\d{4})/(\d{2})/(\d{2})[T ](\d{2}):(\d{2}):(\d{2})(?:[.,](\d{1,6}))?"),
]
_ERROR_CI = re.compile(r"\berror\s*[:\]]|\bfailed to\b|\bout of memory\b|\bpanic:", re.I)
_ERROR_CS = re.compile(r"\b(ERROR|CRITICAL|FATAL)\b|Traceback \(most recent call last\)|^\w*(Error|Exception)\b|CUDA error|out of memory|MemoryError|Segmentation fault|^Killed")
_WARN = re.compile(r"\b(WARN|WARNING)\b|\[warn(ing)?\]", re.I)
_DEBUG = re.compile(r"\b(DEBUG|TRACE)\b")


def parse_line_ts(line: str) -> Optional[float]:
    for pattern in _TS_PATTERNS:
        m = pattern.search(line[:80])
        if m:
            y, mo, d, h, mi, s, frac = m.groups()
            try:
                moment = datetime(int(y), int(mo), int(d), int(h), int(mi), int(s), int((frac or "0").ljust(6, "0")[:6]))
            except ValueError:
                return None
            return moment.timestamp()
    return None


def level_of(line: str) -> str:
    if _ERROR_CS.search(line):
        return "error"
    if _WARN.search(line):
        return "warning"
    if _DEBUG.search(line[:120]):
        return "debug"
    if _ERROR_CI.search(line):
        return "error"
    return "info"


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (text or "").lower())


@dataclass
class Source:
    pattern: str
    service: Optional[str]  # fixed tag, or None = by file name
    fallback: str = "logs"  # tag when the file name matches no service

    def to_dict(self) -> dict[str, Any]:
        return {"pattern": self.pattern, "service": self.service, "fallback": self.fallback}


class LogStore:
    def __init__(self, db, config, services_fn: Callable[[], list], clock: Callable[[], float] = time.time):
        self.db = db
        self.config = config
        self.services_fn = services_fn
        self.clock = clock
        self.last_error: Optional[str] = None
        self.files_watched = 0

    # ---------- sources ----------
    def resolve_name(self, stem: str, fallback: str) -> str:
        key = slug(stem)
        for suffix in ("hoard", "server", "log"):
            if key.endswith(suffix) and len(key) > len(suffix):
                candidates = (key, key[: -len(suffix)])
                break
        else:
            candidates = (key,)
        services = self.services_fn()
        for candidate in candidates:
            for s in services:
                if candidate in (slug(s.id), slug(s.name), slug(s.name.replace("'s Hoard", ""))):
                    return s.id
        return fallback

    def sources(self) -> list[Source]:
        out: list[Source] = []
        for s in self.services_fn():
            for pattern in s.log_globs:
                by_name = s.id == "hoardhub" and s.kind == "app"
                out.append(Source(pattern, None if by_name else s.id, s.id))
        if self.config.default_logs:
            out.append(Source(str(self.config.logs_dir / "*.log"), None, "cassandra"))
            local = os.environ.get("LOCALAPPDATA")
            if local:
                out.append(Source(os.path.join(local, "Hoards", "*.log"), None, "hoards"))
        for entry in self.config.log_globs:
            name, sep, pattern = entry.partition("=")
            if sep and re.fullmatch(r"[a-z0-9][a-z0-9_.-]{0,63}", name.strip().lower()) and not re.fullmatch(r"[A-Za-z]", name.strip()):
                out.append(Source(pattern.strip(), name.strip().lower(), name.strip().lower()))
            else:
                out.append(Source(entry, None, "logs"))
        return out

    def files(self) -> list[tuple[str, str]]:
        """``[(path, service)]`` for every existing file the sources match (first source wins)."""
        seen: dict[str, str] = {}
        horizon = self.clock() - self.config.retention_days * 86400
        for source in self.sources():
            pattern = os.path.expandvars(os.path.expanduser(source.pattern))
            try:
                matches = glob.glob(pattern, recursive=True) if glob.has_magic(pattern) else ([pattern] if os.path.isfile(pattern) else [])
            except Exception:  # noqa: BLE001
                continue
            for path in matches:
                path = os.path.abspath(path)
                if path in seen or not os.path.isfile(path):
                    continue
                try:
                    if os.path.getmtime(path) < horizon:
                        continue
                except OSError:
                    continue
                stem = os.path.splitext(os.path.basename(path))[0]
                seen[path] = source.service or self.resolve_name(stem, source.fallback)
                if len(seen) >= MAX_FILES:
                    return list(seen.items())
        return list(seen.items())

    # ---------- tailing ----------
    def tick(self, only_service: Optional[str] = None) -> int:
        """Read what was appended to every file; returns the number of new lines."""
        total = 0
        files = self.files()
        self.files_watched = len(files)
        for path, service in files:
            if only_service and service != only_service:
                continue
            try:
                total += self._tail_file(path, service)
            except Exception as error:  # noqa: BLE001  (a locked or vanished file never stops the others)
                self.last_error = f"{path}: {error}"
        return total

    def _tail_file(self, path: str, service: str) -> int:
        st = os.stat(path)
        row = self.db.one("SELECT offset, size, mtime FROM log_files WHERE path = ?", (path,))
        first = row is None
        offset = 0 if first else int(row["offset"])
        if not first and st.st_size == row["size"] and st.st_mtime == row["mtime"] and offset + MAX_READ_BYTES > st.st_size:
            return 0
        if not first and st.st_size < offset:
            offset = 0  # rotated or truncated
        start = max(0, st.st_size - FIRST_READ_BYTES) if first else offset
        with open(path, "rb") as fh:
            fh.seek(start)
            data = fh.read(MAX_READ_BYTES)
        skip = 0
        if first and start > 0:  # started mid-file: drop the partial first line
            cut = data.find(b"\n")
            skip = cut + 1 if cut != -1 else len(data)
        end = data.rfind(b"\n")
        if end + 1 <= skip:
            complete, consumed = b"", skip
        else:
            complete, consumed = data[skip : end + 1], end + 1
        new_offset = start + consumed
        rows = self._rows(service, path, complete.decode("utf-8", "replace"), st.st_mtime)
        with self.db.transaction() as conn:
            if rows:
                conn.executemany("INSERT INTO log_lines(service, path, ts, ts_parsed, level, line) VALUES (?, ?, ?, ?, ?, ?)", rows)
            conn.execute(
                "INSERT INTO log_files(path, service, offset, size, mtime, updated_at) VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(path) DO UPDATE SET service = excluded.service, offset = excluded.offset, size = excluded.size, mtime = excluded.mtime, updated_at = excluded.updated_at",
                (path, service, new_offset, st.st_size, st.st_mtime, self.clock()),
            )
        return len(rows)

    def _rows(self, service: str, path: str, text: str, mtime: float) -> list[tuple]:
        now = self.clock()
        fallback = min(now, mtime)
        last_ts: Optional[float] = None
        rows = []
        for raw in text.splitlines():
            line = raw.rstrip()
            if not line.strip():
                continue
            ts = parse_line_ts(line)
            if ts is not None and (ts > now + 86400 or ts < now - 400 * 86400):
                ts = None
            parsed = ts is not None
            if ts is None:
                ts = last_ts if last_ts is not None else fallback
            else:
                last_ts = ts
            rows.append((service, path, ts, 1 if parsed else 0, level_of(line), line[:MAX_LINE]))
        return rows

    # ---------- queries ----------
    def search(self, query: str = "", service: Optional[str] = None, since: Optional[float] = None, until: Optional[float] = None,
               limit: int = 50, level: Optional[str] = None) -> list[dict[str, Any]]:
        where, params = [], []
        for word in [w for w in re.split(r"\s+", (query or "").strip()) if w][:8]:
            where.append("line LIKE ? ESCAPE '\\'")
            params.append("%" + word.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%")
        if service:
            where.append("service = ?")
            params.append(service)
        if since is not None:
            where.append("ts >= ?")
            params.append(since)
        if until is not None:
            where.append("ts <= ?")
            params.append(until)
        if level:
            levels = {"error": ("error",), "warning": ("error", "warning")}.get(level, (level,))
            where.append(f"level IN ({','.join('?' * len(levels))})")
            params.extend(levels)
        sql = "SELECT id, service, path, ts, ts_parsed, level, line FROM log_lines"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY ts DESC, id DESC LIMIT ?"
        params.append(int(limit))
        return [dict(r) for r in self.db.query(sql, params)]

    def tail_for(self, service: str, before: float, n: int = 20) -> list[dict[str, Any]]:
        rows = self.db.query(
            "SELECT id, ts, level, line, path FROM log_lines WHERE service = ? AND ts <= ? ORDER BY ts DESC, id DESC LIMIT ?",
            (service, before, int(n)),
        )
        return [dict(r) for r in reversed(rows)]

    def counts(self) -> dict[str, Any]:
        row = self.db.one("SELECT COUNT(*) AS n, MAX(ts) AS last FROM log_lines")
        files = self.db.one("SELECT COUNT(*) AS n FROM log_files")
        return {"lines": row["n"], "last_ts": row["last"], "files_known": files["n"], "files_watched": self.files_watched, "error": self.last_error}

    def prune(self) -> int:
        horizon = self.clock() - self.config.retention_days * 86400
        removed = self.db.execute("DELETE FROM log_lines WHERE ts < ?", (horizon,)).rowcount
        row = self.db.one("SELECT COUNT(*) AS n FROM log_lines")
        extra = row["n"] - self.config.log_max_lines
        if extra > 0:
            removed += self.db.execute(
                "DELETE FROM log_lines WHERE id IN (SELECT id FROM log_lines ORDER BY ts ASC, id ASC LIMIT ?)", (extra,)
            ).rowcount
        return removed


def summarize_services(rows: Iterable[dict[str, Any]]) -> dict[str, int]:
    out: dict[str, int] = {}
    for row in rows:
        out[row["service"]] = out.get(row["service"], 0) + 1
    return out
