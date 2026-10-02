"""SQLite connection (WAL) and ordered schema migrations."""

from __future__ import annotations

import functools

from .hoard_link import sqlkit

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
    # 2: the family bus mirrored from the Hoard Hub (agent calls, app milestones, hub actions)
    """
    CREATE TABLE bus_events (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      hub_id INTEGER NOT NULL UNIQUE,
      ts REAL NOT NULL,
      type TEXT NOT NULL,
      source TEXT NOT NULL,
      tool TEXT,
      ok INTEGER,
      ms REAL,
      caller TEXT,
      data TEXT NOT NULL DEFAULT '{}'
    );
    CREATE INDEX bus_events_ts ON bus_events(ts);
    CREATE INDEX bus_events_type_ts ON bus_events(type, ts);
    CREATE INDEX bus_events_source_ts ON bus_events(source, ts);
    """,
    # 3: public sites - the last state of each watched site and the cached RDAP answers (domain registration expiry)
    """
    CREATE TABLE site_state (
      site TEXT PRIMARY KEY,
      data TEXT NOT NULL DEFAULT '{}',
      updated_at REAL NOT NULL
    );
    CREATE TABLE rdap_cache (
      domain TEXT PRIMARY KEY,
      checked_at REAL NOT NULL,
      status TEXT NOT NULL,
      expires_at REAL,
      registrar TEXT NOT NULL DEFAULT '',
      detail TEXT NOT NULL DEFAULT '',
      source TEXT NOT NULL DEFAULT '',
      warned TEXT NOT NULL DEFAULT '{}'
    );
    """,
]


# One shared connection and lock, a re-entrant ``transaction()`` and an explicit busy timeout come from the shared
# SQLite helper; the schema above is this app's own. The app is the only writer; the MCP bridge never opens this file.
Database = functools.partial(sqlkit.Database, migrations=MIGRATIONS)
