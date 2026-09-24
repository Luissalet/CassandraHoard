"""Process-level configuration read from the environment (never from the DB)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from .guard import parse_allowed_hosts

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PORT = 5190
DEFAULT_HUB_URL = "http://127.0.0.1:8810"


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def split_paths(raw: str) -> list[str]:
    """`a;b` or `a<os.pathsep>b` → ["a", "b"] (a Windows drive colon is never a separator)."""
    parts: list[str] = []
    for chunk in (raw or "").split(";"):
        if os.pathsep != ";":
            parts.extend(chunk.split(os.pathsep))
        else:
            parts.append(chunk)
    return [p.strip() for p in parts if p.strip()]


def _int(raw: str, default: int, low: int, high: int) -> int:
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    return value if low <= value <= high else default


def _float(raw: str, default: float, low: float, high: float) -> float:
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return default
    return value if low <= value <= high else default


@dataclass
class Config:
    """Everything the process needs before the database exists."""

    data_dir: Path = field(default_factory=lambda: REPO_ROOT / "data")
    port: int = DEFAULT_PORT
    port_strict: bool = False
    poll_s: float = 20.0
    roots: list[str] = field(default_factory=lambda: [str(REPO_ROOT.parent)])
    externals: bool = True  # built-in externals: Faustus, llama-server, Ollama, ComfyUI, Hoard Hub
    log_globs: list[str] = field(default_factory=list)  # extra "glob" or "service=glob" entries
    default_logs: bool = True  # tail each app's data/logs/*.log, the hub's logs and %LOCALAPPDATA%/Hoards/*.log
    retention_days: int = 14
    log_max_lines: int = 500_000
    sample_every_s: float = 60.0  # an unchanged state is stored at most this often
    slow_ms: float = 3000.0  # a health answer slower than this is "degraded"
    auto_restart: bool = True  # master switch for the per-service opt-in restart policies
    agent_commands: bool = False  # may the assistant set restart commands through svc_watch?
    hub_url: str = DEFAULT_HUB_URL
    faustus_python: str = ""  # fills {FAUSTUS_PYTHON} in launch hints (defaults to this interpreter)
    gpu: bool = True  # sample nvidia-smi when present
    autostart: bool = True  # start the poller with the app
    port_check: bool = True  # look at listening ports (psutil) before probing
    bus: bool = True  # mirror the Hoard Hub's event bus (the audit trail)
    hub_registry: bool = True  # take the app list from the hub when it answers (scan manifests otherwise)
    allowed_hosts: tuple[str, ...] = ()
    data_dir_configured: bool = False

    @property
    def db_path(self) -> Path:
        return self.data_dir / "cassandra.db"

    @property
    def token_path(self) -> Path:
        return self.data_dir / "mcp-token"

    @property
    def url_path(self) -> Path:
        return self.data_dir / "url"

    @property
    def services_path(self) -> Path:
        return self.data_dir / "services.json"

    @property
    def logs_dir(self) -> Path:
        return self.data_dir / "logs"

    @classmethod
    def from_env(cls) -> "Config":
        raw_dir = _env("CASSANDRA_DATA_DIR")
        port = _int(_env("CASSANDRA_PORT") or _env("PORT") or str(DEFAULT_PORT), DEFAULT_PORT, 1, 65535)
        roots = split_paths(_env("CASSANDRA_ROOTS")) or [str(REPO_ROOT.parent)]
        return cls(
            data_dir=Path(raw_dir).expanduser() if raw_dir else REPO_ROOT / "data",
            port=port,
            port_strict=_env("PORT_STRICT") == "1",
            poll_s=_float(_env("CASSANDRA_POLL_S"), 20.0, 2.0, 3600.0),
            roots=roots,
            externals=_env("CASSANDRA_EXTERNALS", "1") != "0",
            log_globs=split_paths(_env("CASSANDRA_LOG_GLOBS")),
            default_logs=_env("CASSANDRA_DEFAULT_LOGS", "1") != "0",
            retention_days=_int(_env("CASSANDRA_RETENTION_DAYS"), 14, 1, 3650),
            log_max_lines=_int(_env("CASSANDRA_LOG_MAX_LINES"), 500_000, 1000, 50_000_000),
            sample_every_s=_float(_env("CASSANDRA_SAMPLE_EVERY_S"), 60.0, 0.0, 86400.0),
            slow_ms=_float(_env("CASSANDRA_SLOW_MS"), 3000.0, 50.0, 600_000.0),
            auto_restart=_env("CASSANDRA_AUTO_RESTART", "1") != "0",
            agent_commands=_env("CASSANDRA_AGENT_COMMANDS") == "1",
            hub_url=(_env("CASSANDRA_HUB_URL") or DEFAULT_HUB_URL).rstrip("/"),
            faustus_python=_env("CASSANDRA_FAUSTUS_PYTHON"),
            gpu=_env("CASSANDRA_GPU", "1") != "0",
            autostart=_env("CASSANDRA_AUTOSTART", "1") != "0",
            port_check=_env("CASSANDRA_PORT_CHECK", "1") != "0",
            bus=_env("CASSANDRA_BUS", "1") != "0",
            hub_registry=_env("CASSANDRA_HUB_REGISTRY", "1") != "0",
            allowed_hosts=parse_allowed_hosts(_env("CASSANDRA_ALLOWED_HOSTS")),
            data_dir_configured=bool(raw_dir),
        )
