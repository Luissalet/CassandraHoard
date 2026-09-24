"""The poll loop: every ``CASSANDRA_POLL_S`` seconds check every service in
parallel, sample the GPUs, tail the logs, and turn state changes into events
and incidents.

States: ``up`` (answers as expected), ``degraded`` (answers with an HTTP
error or slower than ``CASSANDRA_SLOW_MS``), ``foreign`` (something else
answers on that port), ``down`` (nothing answers). A service that has never
been up is shown as ``never_seen`` and never opens an incident.

Before any HTTP probe the listening ports are read once (psutil): a closed
loopback port is ``down`` straight away, because on Windows connecting to it
takes ~1.5 s to be refused. The pid behind each port, its start time and a
hash of its command line are kept: the same service answering from a new
pid is a restart event.

Samples are stored on every state change and, while a service is up or
degraded, at most every ``CASSANDRA_SAMPLE_EVERY_S`` seconds (latency
history). Cassandra's own absence (the PC slept, or Cassandra was closed) is
detected from the time of its previous tick and recorded as a ``gap`` event
of the ``system`` pseudo-service, like a change of boot time (``reboot``).
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

import httpx

from . import procs
from .times import clock, duration, iso

log = logging.getLogger("cassandra.poller")

OK_STATES = ("up", "degraded")
BAD_STATES = ("down", "foreign")
RESCAN_S = 300.0
PRUNE_S = 3600.0


@dataclass
class Probe:
    state: str
    latency_ms: Optional[float] = None
    detail: str = ""
    status: Optional[int] = None
    pid: Optional[int] = None
    proc: Optional[procs.ProcInfo] = None

    @property
    def pid_started(self) -> Optional[float]:
        return self.proc.started if self.proc else None


@dataclass
class Current:
    state: str
    since: float
    latency_ms: Optional[float] = None
    detail: str = ""
    pid: Optional[int] = None
    pid_started: Optional[float] = None
    proc_name: str = ""
    cmdline: str = ""
    cmd_hash: str = ""
    last_stored: float = 0.0
    last_seen: float = 0.0
    ever_up: bool = False
    extra: dict[str, Any] = field(default_factory=dict)


def expect_matches(expect: dict[str, Any], body: Any) -> tuple[bool, str]:
    if not expect:
        return True, ""
    if not isinstance(body, dict):
        return False, "the answer is not JSON"
    for key, want in expect.items():
        if key not in body:
            return False, f"no '{key}' in the answer"
        if want == "*":
            continue
        got = body.get(key)
        if str(got).lower() != str(want).lower():
            return False, f"answers as {key}={got!r}, expected {want!r}"
    return True, ""


class Poller:
    def __init__(self, config, db, registry, logs, incidents, restarter, *, clock_fn: Callable[[], float] = time.time,
                 transport: Optional[httpx.AsyncBaseTransport] = None,
                 listeners_fn: Callable[[], Optional[dict[int, int]]] = procs.listening_pids,
                 proc_fn: Callable[[int], Optional[procs.ProcInfo]] = procs.proc_info,
                 alive_fn: Callable[..., Optional[bool]] = procs.pid_alive,
                 boot_fn: Callable[[], Optional[float]] = procs.boot_time,
                 gpu_reader: Optional[Callable[[], list]] = None,
                 restart_async: bool = True):
        self.config = config
        self.db = db
        self.registry = registry
        self.logs = logs
        self.incidents = incidents
        self.restarter = restarter
        self.clock = clock_fn
        self.transport = transport
        self.listeners_fn = listeners_fn
        self.proc_fn = proc_fn
        self.alive_fn = alive_fn
        self.boot_fn = boot_fn
        self.gpu_reader = gpu_reader
        self.restart_async = restart_async
        self.current: dict[str, Current] = {}
        self.gpu_now: list[dict[str, Any]] = []
        self.ticks = 0
        self.last_tick: Optional[float] = None
        self.last_tick_ms: Optional[float] = None
        self.last_error: Optional[str] = None
        self.boot_time: Optional[float] = None
        self._tick_lock = threading.Lock()
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._last_rescan = 0.0
        self._last_prune = 0.0
        self._load_state()

    # ---------- state ----------
    def _load_state(self) -> None:
        rows = self.db.query(
            "SELECT s.* FROM samples s JOIN (SELECT service, MAX(id) AS id FROM samples GROUP BY service) m ON m.id = s.id"
        )
        ever = {r["service"] for r in self.db.query("SELECT DISTINCT service FROM samples WHERE state IN ('up', 'degraded')")}
        for r in rows:
            since_row = self.db.one("SELECT MAX(ts) AS ts FROM events WHERE service = ? AND kind = 'state'", (r["service"],))
            self.current[r["service"]] = Current(
                state=r["state"], since=(since_row["ts"] if since_row and since_row["ts"] else r["ts"]), latency_ms=r["latency_ms"],
                detail=r["detail"], pid=r["pid"], pid_started=r["pid_started"], cmd_hash=r["cmd_hash"] or "",
                last_stored=r["ts"], last_seen=r["ts"], ever_up=r["service"] in ever,
            )

    # ---------- thread ----------
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="cassandra-poller", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread:
            self._thread.join(timeout=15)

    def wake(self) -> None:
        self._wake.set()

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception as error:  # noqa: BLE001  (one bad tick never stops the loop)
                self.last_error = f"{type(error).__name__}: {error}"
                log.exception("poll failed")
            self._wake.wait(self.config.poll_s)
            self._wake.clear()

    # ---------- probing ----------
    async def _probe_all(self, services: list, listeners: Optional[dict[int, int]]) -> list[Probe]:
        timeout = max(1.0, min(5.0, self.config.poll_s / 2))
        kwargs: dict[str, Any] = {"timeout": timeout, "trust_env": False, "follow_redirects": False}
        if self.transport is not None:
            kwargs["transport"] = self.transport
        async with httpx.AsyncClient(**kwargs) as client:
            return list(await asyncio.gather(*(self._probe(client, s, listeners) for s in services)))

    async def _probe(self, client: httpx.AsyncClient, service, listeners: Optional[dict[int, int]]) -> Probe:
        port = service.port
        pid = None
        if listeners is not None and service.is_local:
            if port not in listeners:
                return Probe("down", detail=f"nothing listens on port {port}")
            pid = listeners.get(port) or None
        started = time.perf_counter()
        try:
            response = await client.get(service.health_url(), headers={"Accept": "application/json", "User-Agent": "cassandra-hoard"})
        except httpx.TimeoutException:
            return Probe("down", detail="no answer (timeout)", pid=pid)
        except Exception as error:  # noqa: BLE001  (refused, reset, app crashed mid-request)
            return Probe("down", detail=f"no answer ({type(error).__name__})", pid=pid)
        latency = round((time.perf_counter() - started) * 1000, 1)
        try:
            body = response.json()
        except Exception:  # noqa: BLE001
            body = None
        code = response.status_code
        if code >= 500:
            return Probe("degraded", latency, f"HTTP {code}", code, pid)
        matches, why = expect_matches(service.expect, body)
        if not matches:
            if code >= 400 and not isinstance(body, dict):
                return Probe("foreign", latency, f"HTTP {code}: {why}", code, pid)
            return Probe("foreign", latency, why, code, pid)
        if code >= 400:
            return Probe("degraded", latency, f"HTTP {code}", code, pid)
        if latency > self.config.slow_ms:
            return Probe("degraded", latency, f"slow answer ({latency:.0f} ms)", code, pid)
        return Probe("up", latency, "", code, pid)

    # ---------- the tick ----------
    def tick(self) -> dict[str, Any]:
        with self._tick_lock:
            return self._tick()

    def _tick(self) -> dict[str, Any]:
        started = time.perf_counter()
        now = self.clock()
        system = self._system(now)
        if now - self._last_rescan >= RESCAN_S:
            self.registry.reload()
            self._last_rescan = now
        services = self.registry.list()
        listeners = None
        if self.config.port_check:
            try:
                listeners = self.listeners_fn()
            except Exception:  # noqa: BLE001
                listeners = None
        probes = asyncio.run(self._probe_all(services, listeners))
        for probe in probes:
            if probe.pid and probe.state != "down":
                try:
                    probe.proc = self.proc_fn(probe.pid)
                except Exception:  # noqa: BLE001
                    probe.proc = None
        self._sample_gpu(now)
        try:
            self.logs.tick()
        except Exception as error:  # noqa: BLE001
            self.logs.last_error = str(error)
        changes = []
        for service, probe in zip(services, probes):
            change = self._apply(service, probe, now, system)
            if change:
                changes.append(change)
        self.incidents.refresh_pending()
        if now - self._last_prune >= PRUNE_S:
            self.prune(now)
            self._last_prune = now
        self.db.set_setting("last_tick", repr(now))
        self.ticks += 1
        self.last_tick = now
        self.last_tick_ms = round((time.perf_counter() - started) * 1000, 1)
        self.last_error = None
        return {"ts": now, "services": len(services), "changes": changes, "took_ms": self.last_tick_ms}

    def _system(self, now: float) -> dict[str, Any]:
        system: dict[str, Any] = {"boot_time": None, "previous_boot_time": None, "gap": None}
        last_raw = self.db.get_setting("last_tick")
        last = float(last_raw) if last_raw else None
        limit = max(3 * self.config.poll_s, 90.0)
        if last is not None and now - last > limit:
            system["gap"] = [last, now]
            self.db.execute("INSERT INTO events(service, ts, kind, from_state, to_state, detail, until_ts) VALUES ('system', ?, 'gap', NULL, NULL, ?, ?)",
                            (last, f"Cassandra did not run from {iso(last)} to {iso(now)} ({duration(now - last)}): sleep, shutdown or closed", now))
        boot = None
        try:
            boot = self.boot_fn()
        except Exception:  # noqa: BLE001
            pass
        if boot:
            prev_raw = self.db.get_setting("boot_time")
            prev = float(prev_raw) if prev_raw else None
            system["boot_time"] = boot
            if prev and boot - prev > 30:
                system["previous_boot_time"] = prev
                self.db.execute("INSERT INTO events(service, ts, kind, from_state, to_state, detail) VALUES ('system', ?, 'reboot', NULL, NULL, ?)",
                                (boot, f"the machine booted at {iso(boot)} (previous boot {iso(prev)})"))
            if prev_raw is None or (prev and abs(boot - prev) > 30):
                self.db.set_setting("boot_time", repr(boot))
            self.boot_time = boot
        return system

    def _sample_gpu(self, now: float) -> None:
        if not self.config.gpu or self.gpu_reader is None:
            return
        try:
            samples = self.gpu_reader()
        except Exception:  # noqa: BLE001
            samples = []
        if samples:
            with self.db.transaction() as conn:
                conn.executemany(
                    "INSERT INTO gpu_samples(ts, gpu, mem_used_mb, mem_total_mb, util_pct) VALUES (?, ?, ?, ?, ?)",
                    [(now, s.gpu, s.mem_used_mb, s.mem_total_mb, s.util_pct) for s in samples],
                )
        self.gpu_now = [{**s.to_dict(), "ts": now} for s in samples]

    def _apply(self, service, probe: Probe, now: float, system: dict[str, Any]) -> Optional[dict[str, Any]]:
        prev = self.current.get(service.id)
        prev_state = prev.state if prev else None
        changed = prev_state != probe.state
        proc = probe.proc
        pid_changed = bool(
            prev and prev_state in OK_STATES and probe.state in OK_STATES and prev.pid and probe.pid
            and (prev.pid != probe.pid or (prev.pid_started and proc and proc.started and abs(prev.pid_started - proc.started) > 2))
        )
        ever_up = (prev.ever_up if prev else False) or probe.state in OK_STATES
        heartbeat = probe.state in OK_STATES and (not prev or now - prev.last_stored >= self.config.sample_every_s)
        stored = prev.last_stored if prev else 0.0
        if changed or pid_changed or heartbeat:
            self.db.execute(
                "INSERT INTO samples(service, ts, state, latency_ms, detail, pid, pid_started, cmd_hash) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (service.id, now, probe.state, probe.latency_ms, probe.detail[:500], probe.pid, probe.pid_started, proc.cmd_hash if proc else None),
            )
            stored = now
        change = None
        if changed and prev_state is not None:
            self.db.execute("INSERT INTO events(service, ts, kind, from_state, to_state, detail) VALUES (?, ?, 'state', ?, ?, ?)",
                            (service.id, now, prev_state, probe.state, probe.detail[:500]))
            change = {"service": service.id, "from": prev_state, "to": probe.state, "detail": probe.detail}
        if pid_changed:
            detail = f"pid {prev.pid} → {probe.pid}"
            self.db.execute("INSERT INTO events(service, ts, kind, from_state, to_state, detail) VALUES (?, ?, 'pid', ?, ?, ?)",
                            (service.id, now, prev_state, probe.state, detail))
            self.incidents.open(service.id, "restart", prev_state, probe.state, detail, now,
                                {"process": self._proc_dict(prev, alive=None), "system": system, "port": service.port})
            change = change or {"service": service.id, "from": prev_state, "to": probe.state, "detail": detail}
        if prev_state in OK_STATES and probe.state in BAD_STATES:
            self._open_incident(service, prev, probe, now, system)
        elif prev_state in BAD_STATES and probe.state in OK_STATES:
            self.incidents.close(service.id, now, probe.state)
        self.current[service.id] = Current(
            state=probe.state, since=now if changed or not prev else prev.since, latency_ms=probe.latency_ms, detail=probe.detail,
            pid=probe.pid if probe.state != "down" else None, pid_started=probe.pid_started,
            proc_name=proc.name if proc else "", cmdline=proc.cmdline if proc else "", cmd_hash=proc.cmd_hash if proc else "",
            last_stored=stored, last_seen=now, ever_up=ever_up,
        )
        return change

    def _proc_dict(self, prev: Optional[Current], alive: Optional[bool]) -> Optional[dict[str, Any]]:
        if not prev or not prev.pid:
            return None
        return {"pid": prev.pid, "name": prev.proc_name, "cmdline": prev.cmdline[:500], "started": prev.pid_started,
                "started_at": iso(prev.pid_started) if prev.pid_started else None, "alive": alive}

    def _open_incident(self, service, prev: Current, probe: Probe, now: float, system: dict[str, Any]) -> None:
        alive = None
        if prev.pid:
            try:
                alive = self.alive_fn(prev.pid, prev.pid_started)
            except Exception:  # noqa: BLE001
                alive = None
        detail = probe.detail or "no answer"
        incident_id = self.incidents.open(service.id, "down", prev.state, probe.state, detail, now,
                                          {"process": self._proc_dict(prev, alive), "system": system, "port": service.port})
        allowed, why = self.restarter.should_auto_restart(service, probe.state)
        if allowed:
            if self.restart_async:
                threading.Thread(target=self.restarter.restart, args=(service, "auto", incident_id), name=f"restart-{service.id}", daemon=True).start()
            else:
                self.restarter.restart(service, "auto", incident_id)
        elif service.restart.enabled:
            self.incidents.add_action(incident_id, {"ts": now, "kind": "restart_skipped", "detail": why})

    # ---------- maintenance ----------
    def prune(self, now: Optional[float] = None) -> dict[str, int]:
        now = self.clock() if now is None else now
        horizon = now - self.config.retention_days * 86400
        out = {
            "samples": self.db.execute("DELETE FROM samples WHERE ts < ? AND id NOT IN (SELECT MAX(id) FROM samples GROUP BY service)", (horizon,)).rowcount,
            "events": self.db.execute("DELETE FROM events WHERE ts < ?", (horizon,)).rowcount,
            "gpu_samples": self.db.execute("DELETE FROM gpu_samples WHERE ts < ?", (horizon,)).rowcount,
            "restarts": self.db.execute("DELETE FROM restarts WHERE ts < ?", (horizon,)).rowcount,
            "incidents": self.incidents.prune(self.config.retention_days),
            "log_lines": self.logs.prune(),
        }
        return out

    # ---------- views ----------
    def service_state(self, service, now: Optional[float] = None) -> dict[str, Any]:
        now = self.clock() if now is None else now
        cur = self.current.get(service.id)
        state = cur.state if cur else "unknown"
        shown = state
        if cur and state == "down" and not cur.ever_up:
            shown = "never_seen"
        open_incident = self.incidents.open_for(service.id)
        out = {
            **{k: v for k, v in service.to_dict().items() if k not in ("launch", "expect", "purpose")},
            "state": shown,
            "probe_state": state,
            "since": cur.since if cur else None,
            "since_iso": iso(cur.since) if cur else None,
            "for": duration(now - cur.since) if cur else None,
            "latency_ms": cur.latency_ms if cur else None,
            "detail": cur.detail if cur else "",
            "pid": cur.pid if cur else None,
            "process": cur.proc_name if cur else "",
            "uptime": duration(now - cur.pid_started) if cur and cur.pid_started else None,
            "ever_up": bool(cur and cur.ever_up),
            "last_check": iso(cur.last_seen) if cur else None,
            "open_incident": open_incident["id"] if open_incident else None,
            "can_restart": bool(service.restart.cmd or service.launch or service.kind == "app"),
        }
        if shown == "never_seen":
            out["note"] = "not configured / never seen" if service.kind == "external" else "never seen running"
        return out

    def system_state(self, now: Optional[float] = None) -> dict[str, Any]:
        now = self.clock() if now is None else now
        boot = self.boot_time
        return {
            "id": "system", "name": "System", "kind": "system", "state": "up",
            "boot_time": boot, "boot_iso": iso(boot) if boot else None, "uptime": duration(now - boot) if boot else None,
            "detail": f"up since {clock(boot)} ({duration(now - boot)})" if boot else "boot time unknown",
        }
