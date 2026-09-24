"""Processes behind the ports: who listens where, since when, and the machine's boot time.

psutil is a hard dependency, but every function degrades to "unknown"
instead of raising (psutil can be denied access to other users' processes,
and on macOS ``net_connections`` needs root).
"""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from typing import Any, Optional


def _psutil():
    try:
        import psutil  # type: ignore

        return psutil
    except Exception:  # noqa: BLE001
        return None


def listening_pids() -> Optional[dict[int, int]]:
    """``{port: pid}`` for every TCP listener, or None when it cannot be known."""
    ps = _psutil()
    if ps is not None:
        try:
            out: dict[int, int] = {}
            for conn in ps.net_connections(kind="tcp"):
                if conn.status == ps.CONN_LISTEN and conn.laddr:
                    out.setdefault(conn.laddr.port, conn.pid or 0)
            return out
        except Exception:  # noqa: BLE001  (AccessDenied on macOS)
            pass
    return _listening_pids_cli()


def _listening_pids_cli() -> Optional[dict[int, int]]:
    out: dict[int, int] = {}
    try:
        if sys.platform.startswith("win"):
            text = subprocess.run(["netstat", "-ano", "-p", "tcp"], capture_output=True, text=True, timeout=10).stdout
            for line in text.splitlines():
                parts = line.split()
                if len(parts) >= 5 and parts[0].upper() == "TCP" and parts[3].upper() == "LISTENING":
                    m = re.search(r":(\d+)$", parts[1])
                    if m:
                        out.setdefault(int(m.group(1)), int(parts[4]))
        else:
            text = subprocess.run(["ss", "-ltnp"], capture_output=True, text=True, timeout=10).stdout
            for line in text.splitlines()[1:]:
                m = re.search(r":(\d+)\s", line)
                if m:
                    pid = re.search(r"pid=(\d+)", line)
                    out.setdefault(int(m.group(1)), int(pid.group(1)) if pid else 0)
    except Exception:  # noqa: BLE001
        return None
    return out if out else None


@dataclass
class ProcInfo:
    pid: int
    name: str = ""
    cmdline: str = ""
    started: Optional[float] = None
    rss_mb: Optional[float] = None

    @property
    def cmd_hash(self) -> str:
        return hashlib.sha1(self.cmdline.encode("utf-8", "replace")).hexdigest()[:12] if self.cmdline else ""

    def to_dict(self) -> dict[str, Any]:
        return {"pid": self.pid, "name": self.name, "cmdline": self.cmdline[:500], "started": self.started, "rss_mb": self.rss_mb, "cmd_hash": self.cmd_hash}


def proc_info(pid: int) -> Optional[ProcInfo]:
    if not pid:
        return None
    ps = _psutil()
    if ps is None:
        return ProcInfo(pid)
    info = ProcInfo(pid)
    try:
        p = ps.Process(pid)
        with p.oneshot():
            info.name = p.name()
            try:
                info.cmdline = " ".join(p.cmdline())
            except Exception:  # noqa: BLE001
                pass
            info.started = p.create_time()
            try:
                info.rss_mb = round(p.memory_info().rss / (1024 * 1024), 1)
            except Exception:  # noqa: BLE001
                pass
    except Exception:  # noqa: BLE001
        return None
    return info


def pid_alive(pid: Optional[int], started: Optional[float] = None) -> Optional[bool]:
    """Is that process (same pid AND same start time) still alive? None when unknown."""
    if not pid:
        return None
    ps = _psutil()
    if ps is None:
        return None
    try:
        p = ps.Process(pid)
        if p.status() == getattr(ps, "STATUS_ZOMBIE", "zombie"):
            return False
        if started is not None and abs(p.create_time() - started) > 2.0:
            return False  # the pid was recycled
        return True
    except Exception:  # noqa: BLE001  (NoSuchProcess, AccessDenied)
        try:
            return bool(ps.pid_exists(pid)) and started is None
        except Exception:  # noqa: BLE001
            return None


def boot_time() -> Optional[float]:
    ps = _psutil()
    if ps is None:
        return None
    try:
        return float(ps.boot_time())
    except Exception:  # noqa: BLE001
        return None


def detached_kwargs() -> dict[str, Any]:
    if sys.platform.startswith("win"):
        flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(subprocess, "CREATE_NO_WINDOW", 0)
        return {"creationflags": flags}
    return {"start_new_session": True}


def spawn_detached(cmd: Any, cwd: Optional[str], log_path: str, env: Optional[dict[str, str]] = None) -> subprocess.Popen:
    """Start ``cmd`` (list = argv, str = shell command) detached from us, output appended to ``log_path``."""
    os.makedirs(os.path.dirname(log_path), exist_ok=True)
    full_env = dict(os.environ)
    full_env.update(env or {})
    full_env.setdefault("PYTHONUNBUFFERED", "1")
    log = open(log_path, "ab")
    try:
        shown = cmd if isinstance(cmd, str) else " ".join(cmd)
        log.write(f"\n--- cassandra restart {time.strftime('%Y-%m-%d %H:%M:%S')}: {shown}\n".encode("utf-8"))
        log.flush()
        return subprocess.Popen(
            cmd, cwd=cwd or None, env=full_env, shell=isinstance(cmd, str), stdin=subprocess.DEVNULL,
            stdout=log, stderr=subprocess.STDOUT, close_fds=True, **detached_kwargs(),
        )
    finally:
        log.close()
