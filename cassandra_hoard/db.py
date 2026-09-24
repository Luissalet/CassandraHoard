"""SQLite connection (WAL) and ordered schema migrations."""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

MIGRATIONS: list[str] = [
    # 1: samples, state-change events, incidents, GPU samples, log lines, tail offsets, restarts, settings
    """
    CREATE TABLE samples (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      service TEXT NOT NULL,
      ts REAL NOT NULL,
      state TEXT NOT NULL,
      latency_ms REAL,
      detail TEXT NOT NULL DEFAULT '',
      pid INTEGER,
      pid_started REAL,
      cmd_hash TEXT
    );
    CREATE INDEX samples_service_ts ON samples(service, ts);
    CREATE INDEX samples_ts ON samples(ts);
    CREATE TABLE events (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      service TEXT NOT NULL,
      ts REAL NOT NULL,
      kind TEXT NOT NULL,
      from_state TEXT,
      to_state TEXT,
      detail TEXT NOT NULL DEFAULT '',
      until_ts REAL
    );
    CREATE INDEX events_ts ON events(ts);
    CREATE INDEX events_service_ts ON events(service, ts);
    CREATE TABLE incidents (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      service TEXT NOT NULL,
      kind TEXT NOT NULL,
      opened_at REAL NOT NULL,
      closed_at REAL,
      from_state TEXT,
      to_state TEXT,
      detail TEXT NOT NULL DEFAULT '',
      probable_cause TEXT NOT NULL DEFAULT '',
      context TEXT NOT NULL DEFAULT '{}',
      actions TEXT NOT NULL DEFAULT '[]',
      context_final INTEGER NOT NULL DEFAULT 0
    );
    CREATE INDEX incidents_opened ON incidents(opened_at);
    CREATE INDEX incidents_service ON incidents(service, opened_at);
    CREATE TABLE gpu_samples (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      ts REAL NOT NULL,
      gpu INTEGER NOT NULL,
      mem_used_mb REAL NOT NULL,
      mem_total_mb REAL NOT NULL,
      util_pct REAL
    );
    CREATE INDEX gpu_samples_ts ON gpu_samples(ts, gpu);
    CREATE TABLE log_lines (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      service TEXT NOT NULL,
      path TEXT NOT NULL,
      ts REAL NOT NULL,
      ts_parsed INTEGER NOT NULL DEFAULT 0,
      level TEXT NOT NULL DEFAULT 'info',
      line TEXT NOT NULL
    );
    CREATE INDEX log_lines_service_ts ON log_lines(service, ts);
    CREATE INDEX log_lines_ts ON log_lines(ts);
    CREATE TABLE log_files (
      path TEXT PRIMARY KEY,
      service TEXT NOT NULL,
      offset INTEGER NOT NULL DEFAULT 0,
      size INTEGER NOT NULL DEFAULT 0,
      mtime REAL NOT NULL DEFAULT 0,
      updated_at REAL NOT NULL DEFAULT 0
    );
    CREATE TABLE restarts (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      service TEXT NOT NULL,
      ts REAL NOT NULL,
      trigger TEXT NOT NULL,
      method TEXT NOT NULL DEFAULT '',
      ok INTEGER NOT NULL DEFAULT 0,
      detail TEXT NOT NULL DEFAULT '',
      incident_id INTEGER
    );
    CREATE INDEX restarts_service_ts ON restarts(service, ts);
    CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
    """,
]


class Database:
    """One connection shared by every thread, guarded by a re-entrant lock.

    The app is the only writer; the MCP bridge never opens this file.
    """

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.lock = threading.RLock()
        self.conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.migrate()

    def migrate(self) -> None:
        with self.lock:
            self.conn.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)")
            row = self.conn.execute("SELECT MAX(version) AS v FROM schema_version").fetchone()
            current = row["v"] or 0
            for index, sql in enumerate(MIGRATIONS, start=1):
                if index <= current:
                    continue
                script = f"BEGIN;\n{sql}\nINSERT INTO schema_version(version) VALUES ({index});\nCOMMIT;"
                try:
                    self.conn.executescript(script)
                except Exception:
                    if self.conn.in_transaction:
                        self.conn.execute("ROLLBACK")
                    raise

    def query(self, sql: str, params: tuple | list = ()) -> list[sqlite3.Row]:
        with self.lock:
            return self.conn.execute(sql, params).fetchall()

    def one(self, sql: str, params: tuple | list = ()) -> sqlite3.Row | None:
        with self.lock:
            return self.conn.execute(sql, params).fetchone()

    def execute(self, sql: str, params: tuple | list = ()) -> sqlite3.Cursor:
        with self.lock:
            return self.conn.execute(sql, params)

    def get_setting(self, key: str, default: str | None = None) -> str | None:
        row = self.one("SELECT value FROM settings WHERE key = ?", (key,))
        return row["value"] if row else default

    def set_setting(self, key: str, value: str) -> None:
        self.execute("INSERT INTO settings(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value))

    def transaction(self):
        """`with db.transaction():` — BEGIN IMMEDIATE / COMMIT (ROLLBACK on error) under the lock."""
        return _Transaction(self)

    def close(self) -> None:
        with self.lock:
            try:
                self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except sqlite3.Error:
                pass
            self.conn.close()


class _Transaction:
    def __init__(self, db: Database):
        self.db = db

    def __enter__(self):
        self.db.lock.acquire()
        self.db.conn.execute("BEGIN IMMEDIATE")
        return self.db.conn

    def __exit__(self, exc_type, exc, tb):
        try:
            if exc_type is None:
                self.db.conn.execute("COMMIT")
            else:
                self.db.conn.execute("ROLLBACK")
        finally:
            self.db.lock.release()
        return False
