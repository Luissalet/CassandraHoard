"""Process-level configuration read from the environment (never from the DB)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from .hoard_link.appconfig import AppPaths, env_flag, env_float, env_int, env_str
from .hoard_link.guard import parse_allowed_hosts

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PORT = 5190
DEFAULT_HUB_URL = "http://127.0.0.1:8810"


def split_paths(raw: str) -> list[str]:
    """`a;b` or `a<os.pathsep>b` → ["a", "b"] (a Windows drive colon is never a separator)."""
    parts: list[str] = []
    for chunk in (raw or "").split(";"):
        if os.pathsep != ";":
            parts.extend(chunk.split(os.pathsep))
        else:
            parts.append(chunk)
    return [p.strip() for p in parts if p.strip()]


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
    job_incidents: bool = True  # open an incident when an app's job fails on the mirrored bus (needs the bus)
    hub_registry: bool = True  # take the app list from the hub when it answers (scan manifests otherwise)
    allowed_hosts: tuple[str, ...] = ()
    data_dir_configured: bool = False
    boop_enabled: bool = False
    boop_url: str = ""
    boop_api_key: str = field(default="", repr=False)
    public_url: str = ""  # user-configured URL reachable from the notification device
    faustus_wait_min: float = 15.0  # announce a Faustus approval/question waiting this long (0 = off)
    sites: bool = True  # watch the public sites listed in data/sites.json (none are built in)
    sites_tick_s: float = 15.0  # how often the site watcher looks for sites whose check is due

    @property
    def paths(self) -> AppPaths:
        """The shared data-folder layout (``cassandra.db``, ``mcp-token``, ``url``, ``logs/``)."""
        return AppPaths("cassandra", REPO_ROOT, self.data_dir, self.data_dir_configured)

    @property
    def db_path(self) -> Path:
        return self.paths.db_path

    @property
    def token_path(self) -> Path:
        return self.paths.token_path

    @property
    def url_path(self) -> Path:
        return self.paths.url_path

    @property
    def services_path(self) -> Path:
        return self.data_dir / "services.json"

    @property
    def sites_path(self) -> Path:
        return self.data_dir / "sites.json"

    @property
    def logs_dir(self) -> Path:
        return self.paths.logs_dir

    @classmethod
    def from_env(cls) -> "Config":
        raw_dir = env_str("CASSANDRA_DATA_DIR") or ""
        port = env_int("CASSANDRA_PORT", "PORT", default=DEFAULT_PORT)
        if not 1 <= port <= 65535:
            port = DEFAULT_PORT
        roots = split_paths(env_str("CASSANDRA_ROOTS") or "") or [str(REPO_ROOT.parent)]
        return cls(
            data_dir=Path(raw_dir).expanduser() if raw_dir else REPO_ROOT / "data",
            port=port,
            port_strict=env_flag("PORT_STRICT"),
            poll_s=env_float("CASSANDRA_POLL_S", default=20.0, minimum=2.0, maximum=3600.0),
            roots=roots,
            externals=env_flag("CASSANDRA_EXTERNALS", True),
            log_globs=split_paths(env_str("CASSANDRA_LOG_GLOBS") or ""),
            default_logs=env_flag("CASSANDRA_DEFAULT_LOGS", True),
            retention_days=env_int("CASSANDRA_RETENTION_DAYS", default=14, minimum=1, maximum=3650),
            log_max_lines=env_int("CASSANDRA_LOG_MAX_LINES", default=500_000, minimum=1000, maximum=50_000_000),
            sample_every_s=env_float("CASSANDRA_SAMPLE_EVERY_S", default=60.0, minimum=0.0, maximum=86400.0),
            slow_ms=env_float("CASSANDRA_SLOW_MS", default=3000.0, minimum=50.0, maximum=600_000.0),
            auto_restart=env_flag("CASSANDRA_AUTO_RESTART", True),
            agent_commands=env_flag("CASSANDRA_AGENT_COMMANDS"),
            hub_url=(env_str("CASSANDRA_HUB_URL") or DEFAULT_HUB_URL).rstrip("/"),
            faustus_python=env_str("CASSANDRA_FAUSTUS_PYTHON") or "",
            gpu=env_flag("CASSANDRA_GPU", True),
            autostart=env_flag("CASSANDRA_AUTOSTART", True),
            port_check=env_flag("CASSANDRA_PORT_CHECK", True),
            bus=env_flag("CASSANDRA_BUS", True),
            job_incidents=env_flag("CASSANDRA_JOB_INCIDENTS", True),
            hub_registry=env_flag("CASSANDRA_HUB_REGISTRY", True),
            allowed_hosts=parse_allowed_hosts(env_str("CASSANDRA_ALLOWED_HOSTS")),
            data_dir_configured=bool(raw_dir),
            boop_enabled=env_flag("CASSANDRA_BOOP_ENABLED"),
            boop_url=env_str("CASSANDRA_BOOP_URL") or "",
            boop_api_key=env_str("CASSANDRA_BOOP_API_KEY") or "",
            faustus_wait_min=env_float("CASSANDRA_FAUSTUS_WAIT_MIN", default=15.0, minimum=0.0, maximum=1440.0),
            public_url=env_str("CASSANDRA_PUBLIC_URL") or "",
            sites=env_flag("CASSANDRA_SITES", True),
            sites_tick_s=env_float("CASSANDRA_SITES_TICK_S", default=15.0, minimum=1.0, maximum=3600.0),
        )
