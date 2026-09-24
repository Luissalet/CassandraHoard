"""Restart policies: opt-in per service, rate-limited, every attempt recorded.

How a service is (re)started, first match wins:

1. ``restart.cmd`` from its policy (``data/services.json``): a shell string or an argv list, run in ``restart.cwd``;
2. a discovered app while the launcher (Hoard Hub) is up: ``POST <hub>/api/apps/<id>/start``
   (``/restart`` when it is still running), so the launcher keeps owning its apps and its logs;
3. a discovered app's manifest ``launch_hint`` (placeholders resolved like the launcher does).

Automatic restarts happen only when the service's policy is enabled, the
master switch ``CASSANDRA_AUTO_RESTART`` is on, the service really went down
(never when another program holds its port) and fewer than
``max_per_hour`` automatic restarts were made in the last hour. A manual
restart (``svc_restart``, the UI button) is always allowed for a service
that has a way to start. Spawned processes are detached and their output
goes to ``data/logs/<id>.log``, which the log tailer reads.
"""

from __future__ import annotations

import os
import threading
import time
from typing import Any, Callable, Optional

import httpx

from . import procs
from .times import clock

HUB_TIMEOUT_S = 1.5


class Restarter:
    def __init__(self, db, config, registry, incidents, clock_fn: Callable[[], float] = time.time,
                 hub_client: Optional[httpx.Client] = None, spawn: Callable[..., Any] = procs.spawn_detached):
        self.db = db
        self.config = config
        self.registry = registry
        self.incidents = incidents
        self.clock = clock_fn
        self.hub_client = hub_client
        self.spawn = spawn
        self._lock = threading.Lock()
        self._busy: set[str] = set()

    # ---------- hub ----------
    def _client(self, timeout: float) -> httpx.Client:
        return self.hub_client or httpx.Client(timeout=timeout, trust_env=False)

    def hub_up(self) -> bool:
        client = self._client(HUB_TIMEOUT_S)
        try:
            response = client.get(f"{self.config.hub_url}/api/health", timeout=HUB_TIMEOUT_S)
            return response.status_code == 200 and str(response.json().get("service", "")).lower() == "hoard-hub"
        except Exception:  # noqa: BLE001
            return False
        finally:
            if client is not self.hub_client:
                client.close()

    def _hub_call(self, app_id: str, action: str) -> tuple[bool, str]:
        client = self._client(60)
        try:
            response = client.post(f"{self.config.hub_url}/api/apps/{app_id}/{action}", json={"wait": False}, timeout=60)
            try:
                body = response.json()
            except ValueError:
                body = {}
            if response.status_code == 404:
                return False, "the launcher does not know this app"
            ok = response.status_code < 400 and body.get("ok", True) is not False
            detail = body.get("detail") or body.get("error") or ("started" if ok else f"HTTP {response.status_code}")
            return ok, f"launcher: {detail}"
        except Exception as error:  # noqa: BLE001
            return False, f"launcher unreachable: {error}"
        finally:
            if client is not self.hub_client:
                client.close()

    # ---------- policy ----------
    def method_for(self, service, hub_up: Optional[bool] = None) -> Optional[str]:
        if service.restart.cmd:
            return "cmd"
        if service.kind == "app" and (self.hub_up() if hub_up is None else hub_up):
            return "hub"
        if service.launch is not None:
            return "launch"
        return None

    def recent(self, service_id: str, seconds: float = 3600, trigger: Optional[str] = "auto") -> int:
        since = self.clock() - seconds
        if trigger:
            row = self.db.one("SELECT COUNT(*) AS n FROM restarts WHERE service = ? AND ts >= ? AND trigger = ?", (service_id, since, trigger))
        else:
            row = self.db.one("SELECT COUNT(*) AS n FROM restarts WHERE service = ? AND ts >= ?", (service_id, since))
        return int(row["n"])

    def history(self, service_id: Optional[str] = None, limit: int = 50) -> list[dict[str, Any]]:
        if service_id:
            rows = self.db.query("SELECT * FROM restarts WHERE service = ? ORDER BY ts DESC LIMIT ?", (service_id, limit))
        else:
            rows = self.db.query("SELECT * FROM restarts ORDER BY ts DESC LIMIT ?", (limit,))
        return [{**dict(r), "ok": bool(r["ok"])} for r in rows]

    def should_auto_restart(self, service, to_state: str) -> tuple[bool, str]:
        if not service.restart.enabled:
            return False, "policy off"
        if not self.config.auto_restart:
            return False, "automatic restarts are disabled (CASSANDRA_AUTO_RESTART=0)"
        if to_state != "down":
            return False, "another program holds the port" if to_state == "foreign" else f"state {to_state}"
        used = self.recent(service.id)
        if used >= service.restart.max_per_hour:
            return False, f"rate limit: {used} automatic restarts in the last hour (max {service.restart.max_per_hour})"
        return True, ""

    # ---------- action ----------
    def restart(self, service, trigger: str = "manual", incident_id: Optional[int] = None, running: bool = False,
                pid: Optional[int] = None, pid_started: Optional[float] = None) -> dict[str, Any]:
        with self._lock:
            if service.id in self._busy:
                return {"ok": False, "service": service.id, "error": "a restart of this service is already in progress"}
            self._busy.add(service.id)
        try:
            return self._restart(service, trigger, incident_id, running, pid, pid_started)
        finally:
            with self._lock:
                self._busy.discard(service.id)

    def _restart(self, service, trigger, incident_id, running, pid, pid_started) -> dict[str, Any]:
        now = self.clock()
        method = self.method_for(service)
        if method is None:
            reason = service.launch_reason or "no restart command, no launch hint and the launcher is not running"
            return {"ok": False, "service": service.id, "error": f"{service.name} cannot be restarted: {reason}. Set restart.cmd with svc_watch or in the UI."}
        ok, detail = False, ""
        if method == "hub":
            ok, detail = self._hub_call(service.id, "restart" if running else "start")
            if not ok and service.launch is not None and "does not know" in detail:
                method = "launch"
        if method in ("cmd", "launch"):
            if running and pid:
                stopped = self._stop(pid, pid_started)
                if not stopped["ok"]:
                    ok, detail = False, f"could not stop the running process: {stopped['error']}"
                    return self._record(service, now, trigger, method, ok, detail, incident_id)
            log_path = str(self.config.logs_dir / f"{service.id}.log")
            try:
                if method == "cmd":
                    cwd = service.restart.cwd or service.folder or None
                    child = self.spawn(service.restart.cmd, cwd, log_path)
                else:
                    spec = service.launch
                    child = self.spawn([spec.executable, *spec.argv], spec.cwd, log_path, spec.env)
                ok, detail = True, f"started pid {getattr(child, 'pid', '?')} (output in {os.path.basename(log_path)})"
            except Exception as error:  # noqa: BLE001
                ok, detail = False, f"could not start: {error}"
        return self._record(service, now, trigger, method, ok, detail, incident_id)

    def _stop(self, pid: int, started: Optional[float]) -> dict[str, Any]:
        if pid == os.getpid():
            return {"ok": False, "error": "refusing to stop Cassandra itself"}
        ps = procs._psutil()
        if ps is None:
            return {"ok": False, "error": "psutil is not available"}
        try:
            root = ps.Process(pid)
            if started is not None and abs(root.create_time() - started) > 2.0:
                return {"ok": False, "error": "the pid was recycled"}
            family = [root, *root.children(recursive=True)]
        except ps.NoSuchProcess:
            return {"ok": True}
        except Exception as error:  # noqa: BLE001
            return {"ok": False, "error": str(error)}
        for p in family:
            try:
                p.terminate()
            except Exception:  # noqa: BLE001
                pass
        _, alive = ps.wait_procs(family, timeout=5)
        for p in alive:
            try:
                p.kill()
            except Exception:  # noqa: BLE001
                pass
        return {"ok": True}

    def _record(self, service, now, trigger, method, ok, detail, incident_id) -> dict[str, Any]:
        cur = self.db.execute(
            "INSERT INTO restarts(service, ts, trigger, method, ok, detail, incident_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (service.id, now, trigger, method, 1 if ok else 0, detail, incident_id),
        )
        self.db.execute("INSERT INTO events(service, ts, kind, from_state, to_state, detail) VALUES (?, ?, 'restart', NULL, NULL, ?)",
                        (service.id, now, f"{trigger} restart via {method}: {'ok' if ok else 'failed'} — {detail}"))
        target = incident_id
        if target is None:
            open_incident = self.incidents.open_for(service.id)
            target = open_incident["id"] if open_incident else None
        if target is not None:
            self.incidents.add_action(target, {"ts": now, "kind": "restart", "trigger": trigger, "method": method, "ok": ok, "detail": detail})
        return {"ok": ok, "service": service.id, "name": service.name, "trigger": trigger, "method": method, "detail": detail,
                "at": clock(now), "restart_id": int(cur.lastrowid), "incident_id": target}
