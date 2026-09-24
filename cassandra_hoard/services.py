"""Wiring of database, registry, logs, incidents, restarts and the poller."""

from __future__ import annotations

import logging
import secrets
import time
from typing import Any, Callable, Optional

from . import SERVICE, __version__
from .audit import BusMirror
from .config import Config
from .db import Database
from .gpu import GpuReader
from .incidents import Incidents
from .logs import LogStore
from .poller import Poller
from .registry import Registry
from .restart import Restarter
from .times import iso

log = logging.getLogger("cassandra")


def write_token(config: Config) -> str:
    config.data_dir.mkdir(parents=True, exist_ok=True)
    token = secrets.token_hex(32)
    config.token_path.write_text(token, encoding="utf-8")
    try:
        config.token_path.chmod(0o600)
    except OSError:
        pass
    return token


def write_url(config: Config) -> None:
    try:
        config.url_path.write_text(f"http://127.0.0.1:{config.port}", encoding="utf-8")
    except OSError:
        pass


class Services:
    def __init__(self, config: Config, *, clock_fn: Callable[[], float] = time.time, poller_kwargs: Optional[dict[str, Any]] = None,
                 restarter_kwargs: Optional[dict[str, Any]] = None, bus_kwargs: Optional[dict[str, Any]] = None,
                 registry_kwargs: Optional[dict[str, Any]] = None):
        self.config = config
        self.clock = clock_fn
        self.started_at = time.time()
        config.data_dir.mkdir(parents=True, exist_ok=True)
        config.logs_dir.mkdir(parents=True, exist_ok=True)
        self.token = write_token(config)
        write_url(config)
        self.db = Database(config.db_path)
        self.registry = Registry(config, **(registry_kwargs or {}))
        self.logs = LogStore(self.db, config, self.registry.list, clock_fn)
        self.incidents = Incidents(self.db, self.logs, self.name_of, clock_fn)
        self.restarter = Restarter(self.db, config, self.registry, self.incidents, clock_fn, **(restarter_kwargs or {}))
        kwargs = dict(poller_kwargs or {})
        if "gpu_reader" not in kwargs and config.gpu:
            kwargs["gpu_reader"] = GpuReader()
        self.poller = Poller(config, self.db, self.registry, self.logs, self.incidents, self.restarter, clock_fn=clock_fn, **kwargs)
        # The family bus, mirrored for good: what the assistant and the apps did.
        self.bus = BusMirror(self.db, config.hub_url, clock_fn=clock_fn, incidents=self.incidents, emit=self._emit_event,
                             **(bus_kwargs or {}))

    def _emit_event(self, type_: str, data: dict[str, Any]) -> None:
        try:
            from .hoard_link import family
            family.emit(type_, data)
        except Exception:  # noqa: BLE001
            pass

    def name_of(self, service_id: str) -> str:
        if service_id == "system":
            return "System"
        service = self.registry.get(service_id)
        return service.name if service else service_id

    # ---------- lifecycle ----------
    def start(self) -> None:
        if self.config.autostart:
            self.poller.start()
            if self.config.bus:
                self.bus.start()

    def stop(self) -> None:
        self.bus.stop()
        self.poller.stop()
        self.db.close()

    # ---------- lookups ----------
    def resolve(self, text: str):
        service = self.registry.find(text)
        if service is None:
            known = ", ".join(s.id for s in self.registry.list()[:40])
            raise LookupError(f"Unknown service '{text}'. Known ids: {known}")
        return service

    def service_states(self) -> list[dict[str, Any]]:
        now = self.clock()
        return [self.poller.service_state(s, now) for s in self.registry.list()]

    # ---------- status ----------
    def status(self) -> dict[str, Any]:
        now = self.clock()
        states = self.service_states()
        counts: dict[str, int] = {}
        for s in states:
            counts[s["state"]] = counts.get(s["state"], 0) + 1
        try:
            db_bytes = self.config.db_path.stat().st_size
        except OSError:
            db_bytes = 0
        gpu_reader = self.poller.gpu_reader
        return {
            "service": SERVICE,
            "version": __version__,
            "data_dir": str(self.config.data_dir),
            "db_bytes": db_bytes,
            "started_at": self.started_at,
            "now": now,
            "poller": {
                "running": self.poller.running, "interval_s": self.config.poll_s, "ticks": self.poller.ticks,
                "last_tick": iso(self.poller.last_tick) if self.poller.last_tick else None, "last_tick_ms": self.poller.last_tick_ms,
                "error": self.poller.last_error,
            },
            "counts": counts,
            "services_total": len(states),
            "incidents": self.incidents.counts(),
            "system": self.poller.system_state(now),
            "gpu": {"available": bool(self.poller.gpu_now), "now": self.poller.gpu_now,
                    "error": getattr(gpu_reader, "error", None) if self.config.gpu else "disabled (CASSANDRA_GPU=0)"},
            "logs": self.logs.counts(),
            "bus": self.bus.status(),
            "auto_restart": self.config.auto_restart,
            "registry_error": self.registry.load_error,
            "registry_source": self.registry.source,
            "roots": list(self.config.roots),
        }
