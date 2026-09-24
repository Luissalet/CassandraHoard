"""Which services Cassandra watches.

Three sources, merged by id:

* **discovered apps** — every ``<root>/*/faustus-plugin.json`` (the same
  manifest the launcher reads: ``app.url_default``, ``app.health``,
  ``app.launch_hint``). When the app's folder has ``data/url`` pointing at a
  loopback URL, that wins over the manifest default (the app may have moved
  to another port).
* **built-in externals** — the model servers and the workspace that have no
  manifest: Faustus (7000-7003), llama-server (8081/8082), Ollama (11434),
  ComfyUI (8188-8191) and Hoard Hub (8810). One is skipped when a
  discovered app already declares its port. An external that never answered
  is reported as "never seen", never as an incident.
* **user services** — ``data/services.json``: ``{"services": [...],
  "policies": {...}}``. A user entry with the id of a discovered app or an
  external overrides the fields it sets. ``policies`` holds the restart
  policy of services the user did not define (discovered apps, externals).

Nothing here touches the network: this is pure reading, like the hub's own
registry (placeholders are resolved the same way: ``{X_DIR}`` is the app's
folder, ``{FAUSTUS_PYTHON}`` the configured interpreter or this one).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional
from urllib.parse import urlsplit

MANIFEST_NAME = "faustus-plugin.json"
SELF_ID = "cassandra"
ID_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,63}$")
_PLACEHOLDER = re.compile(r"\{([A-Z][A-Z0-9_]*)\}")
_MAX_DEPTH = 8
LOOPBACK = ("127.0.0.1", "localhost", "::1")


@dataclass
class LaunchSpec:
    executable: str
    argv: list[str]
    cwd: str
    env: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"executable": self.executable, "argv": list(self.argv), "cwd": self.cwd}


@dataclass
class RestartPolicy:
    enabled: bool = False  # automatic restart when the service goes down (opt-in)
    cmd: Any = None  # str (shell) or list[str]; None = use the hub or the manifest launch hint
    cwd: Optional[str] = None
    max_per_hour: int = 3

    def to_dict(self) -> dict[str, Any]:
        return {"enabled": self.enabled, "cmd": self.cmd, "cwd": self.cwd, "max_per_hour": self.max_per_hour}

    @classmethod
    def from_dict(cls, raw: Any) -> "RestartPolicy":
        if not isinstance(raw, dict):
            return cls()
        cmd = raw.get("cmd")
        if isinstance(cmd, list):
            cmd = [str(c) for c in cmd if str(c).strip()] or None
        elif cmd is not None:
            cmd = str(cmd).strip() or None
        try:
            max_per_hour = max(0, min(60, int(raw.get("max_per_hour", 3))))
        except (TypeError, ValueError):
            max_per_hour = 3
        cwd = raw.get("cwd")
        return cls(enabled=bool(raw.get("enabled", False)), cmd=cmd, cwd=str(cwd) if cwd else None, max_per_hour=max_per_hour)


@dataclass
class Service:
    id: str
    name: str
    kind: str  # app | external | user
    url: str
    health_path: str = "/api/health"
    expect: dict[str, Any] = field(default_factory=dict)
    group: str = "apps"  # apps | faustus | llm | comfyui | hub | custom
    log_globs: list[str] = field(default_factory=list)
    restart: RestartPolicy = field(default_factory=RestartPolicy)
    launch: Optional[LaunchSpec] = None
    launch_reason: str = ""
    folder: str = ""
    purpose: str = ""

    @property
    def port(self) -> Optional[int]:
        try:
            parsed = urlsplit(self.url)
            return parsed.port or (443 if parsed.scheme == "https" else 80)
        except ValueError:
            return None

    @property
    def host(self) -> str:
        return urlsplit(self.url).hostname or "127.0.0.1"

    @property
    def is_local(self) -> bool:
        return self.host in LOOPBACK

    def health_url(self) -> str:
        return self.url.rstrip("/") + self.health_path

    def can_restart(self, hub_known: bool = False) -> bool:
        return bool(self.restart.cmd) or self.launch is not None or (self.kind == "app" and hub_known)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "name": self.name, "kind": self.kind, "group": self.group, "url": self.url, "port": self.port,
            "health_path": self.health_path, "health_url": self.health_url(), "expect": dict(self.expect),
            "log_globs": list(self.log_globs), "restart": self.restart.to_dict(),
            "launch": self.launch.to_dict() if self.launch else None, "launch_reason": self.launch_reason,
            "folder": self.folder, "purpose": self.purpose,
        }


# ---------------------------------------------------------------------------
# built-in externals
# ---------------------------------------------------------------------------

def builtin_externals() -> list[Service]:
    out: list[Service] = [
        Service("faustus", "Faustus", "external", "http://127.0.0.1:7000", "/api/health", {"service": "faustus"}, "faustus"),
    ]
    for port in (7001, 7002, 7003):
        out.append(Service(f"faustus-{port}", f"Faustus (test {port})", "external", f"http://127.0.0.1:{port}", "/api/health", {"service": "faustus"}, "faustus"))
    out.append(Service("llama-server", "llama-server", "external", "http://127.0.0.1:8081", "/health", {}, "llm"))
    out.append(Service("llama-helper", "llama-server (helper 8082)", "external", "http://127.0.0.1:8082", "/health", {}, "llm"))
    out.append(Service("ollama", "Ollama", "external", "http://127.0.0.1:11434", "/api/tags", {"models": "*"}, "llm"))
    for index, port in enumerate((8188, 8189, 8190, 8191)):
        name = "ComfyUI" if index == 0 else f"ComfyUI ({port})"
        out.append(Service("comfyui" if index == 0 else f"comfyui-{port}", name, "external", f"http://127.0.0.1:{port}", "/system_stats", {"system": "*"}, "comfyui"))
    out.append(Service("hoardhub", "Hoard Hub", "external", "http://127.0.0.1:8810", "/api/health", {"service": "hoard-hub"}, "hub"))
    return out


# ---------------------------------------------------------------------------
# placeholders and manifests (same rules as the launcher)
# ---------------------------------------------------------------------------

def resolve_placeholders(template: str, *, folder: str, defaults: dict[str, str], extra: dict[str, str], missing: set[str], _depth: int = 0) -> str:
    if _depth > _MAX_DEPTH:
        return template

    def one(match: "re.Match[str]") -> str:
        name = match.group(1)
        if name in extra:
            return extra[name]
        if name.endswith("_DIR") and name != "FAUSTUS_DIR":
            return folder
        if name in defaults:
            return resolve_placeholders(str(defaults[name]), folder=folder, defaults=defaults, extra=extra, missing=missing, _depth=_depth + 1)
        missing.add(name)
        return match.group(0)

    return _PLACEHOLDER.sub(one, template)


def _executable_exists(executable: str, cwd: str) -> bool:
    if not executable:
        return False
    if os.sep in executable or "/" in executable:
        return os.path.isfile(executable) or os.path.isfile(os.path.join(cwd, executable))
    return shutil.which(executable) is not None


def _read_url_file(folder: str) -> Optional[str]:
    try:
        text = Path(folder, "data", "url").read_text(encoding="utf-8-sig").strip()
    except OSError:
        return None
    parsed = urlsplit(text)
    if parsed.scheme == "http" and parsed.hostname in LOOPBACK and parsed.port:
        return text.rstrip("/")
    return None


def _group_for(app_id: str) -> str:
    return "hub" if app_id == "hoardhub" else "apps"


def read_manifest(path: str | Path, *, faustus_python: Optional[str] = None) -> Optional[Service]:
    """One ``faustus-plugin.json`` → a :class:`Service`, or None when unusable. Never raises."""
    path = str(path)
    folder = os.path.dirname(os.path.abspath(path))
    try:
        with open(path, "r", encoding="utf-8-sig") as fh:
            raw = json.load(fh)
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict):
        return None
    app_id = str(raw.get("id") or "").strip()
    app_block = raw.get("app")
    if not app_id or not isinstance(app_block, dict):
        return None
    defaults = {str(k): str(v) for k, v in (raw.get("defaults") or {}).items()}
    extra = {"FAUSTUS_PYTHON": faustus_python or sys.executable}
    missing: set[str] = set()

    def fill(value: Any) -> str:
        return resolve_placeholders(str(value), folder=folder, defaults=defaults, extra=extra, missing=missing)

    url_default = str(app_block.get("url_default") or defaults.get("APP_URL") or "").strip()
    url = _read_url_file(folder) or (fill(url_default) if url_default else "")
    if not url:
        return None
    extra["APP_URL"] = url.rstrip("/")
    health = app_block.get("health") or {}
    health_path = str(health.get("path") or "/api/health")
    if not health_path.startswith("/"):
        health_path = "/" + health_path
    expect = health.get("expect") if isinstance(health.get("expect"), dict) else {}
    service = Service(
        id=app_id.lower(), name=str(raw.get("name") or app_id), kind="app", url=url.rstrip("/"), health_path=health_path,
        expect={str(k): v for k, v in expect.items()}, group=_group_for(app_id.lower()),
        log_globs=[os.path.join(folder, "data", "logs", "*.log")], folder=folder, purpose=str(raw.get("purpose") or ""),
    )
    hint = app_block.get("launch_hint")
    if not isinstance(hint, dict) or hint.get("kind", "process") != "process":
        service.launch_reason = "the manifest has no process launch hint"
        return service
    missing.clear()
    executable = fill(hint.get("executable") or "")
    argv = [fill(a) for a in (hint.get("argv") or [])]
    cwd = fill(hint.get("cwd") or folder) or folder
    env = {str(k): fill(v) for k, v in (hint.get("env") or {}).items()}
    if missing:
        service.launch_reason = "unresolved placeholders: " + ", ".join(sorted(missing))
    elif not _executable_exists(executable, cwd):
        service.launch_reason = f"executable not found: {executable}"
    elif not os.path.isdir(cwd):
        service.launch_reason = f"working directory not found: {cwd}"
    else:
        service.launch = LaunchSpec(executable, argv, cwd, env)
    return service


def scan(roots: Iterable[str], *, faustus_python: Optional[str] = None, exclude_ids: Iterable[str] = (SELF_ID,)) -> list[Service]:
    """Every ``<root>/*/faustus-plugin.json`` (one level deep) plus any root that is itself an app folder."""
    seen: dict[str, Service] = {}
    excluded = set(exclude_ids)
    for root in roots:
        root = os.path.abspath(os.path.expanduser(str(root)))
        candidates: list[str] = []
        direct = os.path.join(root, MANIFEST_NAME)
        if os.path.isfile(direct):
            candidates.append(direct)
        elif os.path.isdir(root):
            try:
                entries = sorted(os.listdir(root))
            except OSError:
                entries = []
            for entry in entries:
                p = os.path.join(root, entry, MANIFEST_NAME)
                if os.path.isfile(p):
                    candidates.append(p)
        for path in candidates:
            service = read_manifest(path, faustus_python=faustus_python)
            if service is None or service.id in excluded or service.id in seen:
                continue
            seen[service.id] = service
    return sorted(seen.values(), key=lambda s: s.name.lower())


# ---------------------------------------------------------------------------
# user services (data/services.json)
# ---------------------------------------------------------------------------

def validate_user_entry(raw: dict[str, Any]) -> dict[str, Any]:
    """Normalise one user entry; raises ValueError with a readable message."""
    sid = str(raw.get("id") or "").strip().lower()
    if not ID_RE.match(sid):
        raise ValueError("id must be 1-64 characters: lowercase letters, digits, '.', '_' or '-'.")
    if sid in ("system", SELF_ID):
        raise ValueError(f"'{sid}' is reserved.")
    out: dict[str, Any] = {"id": sid}
    if raw.get("name") is not None:
        out["name"] = str(raw["name"]).strip()[:120]
    if raw.get("url") is not None:
        url = str(raw["url"]).strip().rstrip("/")
        parsed = urlsplit(url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise ValueError("url must be an http:// or https:// address.")
        out["url"] = url
    if raw.get("health_path") is not None:
        hp = str(raw["health_path"]).strip() or "/"
        out["health_path"] = hp if hp.startswith("/") else "/" + hp
    if raw.get("expect") is not None:
        if not isinstance(raw["expect"], dict):
            raise ValueError("expect must be an object of JSON key/value pairs.")
        out["expect"] = {str(k): v for k, v in raw["expect"].items()}
    if raw.get("log_paths") is not None:
        paths = raw["log_paths"]
        if isinstance(paths, str):
            paths = [paths]
        if not isinstance(paths, list):
            raise ValueError("log_paths must be a list of paths or globs.")
        out["log_paths"] = [str(p).strip() for p in paths if str(p).strip()][:50]
    if raw.get("restart") is not None:
        out["restart"] = RestartPolicy.from_dict(raw["restart"]).to_dict()
    return out


class Registry:
    """The merged, thread-safe list of services; reloaded on demand."""

    def __init__(self, config):
        self.config = config
        self._lock = threading.RLock()
        self._services: dict[str, Service] = {}
        self._file: dict[str, Any] = {"services": [], "policies": {}}
        self.load_error: str | None = None
        self.reload()

    # ---------- file ----------
    def _read_file(self) -> dict[str, Any]:
        path = self.config.services_path
        self.load_error = None
        try:
            raw = json.loads(path.read_text(encoding="utf-8-sig"))
        except FileNotFoundError:
            return {"services": [], "policies": {}}
        except (OSError, ValueError) as error:
            self.load_error = f"{path.name}: {error}"
            return {"services": [], "policies": {}}
        if isinstance(raw, list):
            raw = {"services": raw, "policies": {}}
        if not isinstance(raw, dict):
            return {"services": [], "policies": {}}
        services = []
        for entry in raw.get("services") or []:
            if isinstance(entry, dict):
                try:
                    services.append(validate_user_entry(entry))
                except ValueError as error:
                    self.load_error = f"{path.name}: {entry.get('id')}: {error}"
        policies = {str(k).lower(): RestartPolicy.from_dict(v).to_dict() for k, v in (raw.get("policies") or {}).items() if isinstance(v, dict)}
        return {"services": services, "policies": policies}

    def _write_file(self) -> None:
        path = self.config.services_path
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self._file, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)

    # ---------- merge ----------
    def reload(self) -> list[Service]:
        with self._lock:
            self._file = self._read_file()
            merged: dict[str, Service] = {}
            discovered = scan(self.config.roots, faustus_python=self.config.faustus_python or None)
            ports = {s.port for s in discovered if s.is_local}
            for s in discovered:
                merged[s.id] = s
            if self.config.externals:
                for s in builtin_externals():
                    if s.id not in merged and s.port not in ports:
                        merged[s.id] = s
            for entry in self._file["services"]:
                base = merged.get(entry["id"])
                if base is None:
                    if "url" not in entry:
                        continue  # a partial entry for something that is not there any more
                    base = Service(entry["id"], entry.get("name") or entry["id"], "user", entry["url"], "/", {}, "custom")
                else:
                    base = Service(**{**base.__dict__})  # never mutate the discovered copy
                for key in ("name", "url", "health_path", "expect"):
                    if key in entry and entry[key] not in (None, ""):
                        setattr(base, key, entry[key])
                if "log_paths" in entry:
                    base.log_globs = list(dict.fromkeys([*base.log_globs, *entry["log_paths"]]))
                if "restart" in entry:
                    base.restart = RestartPolicy.from_dict(entry["restart"])
                merged[base.id] = base
            for sid, policy in self._file["policies"].items():
                if sid in merged and not any(e["id"] == sid and "restart" in e for e in self._file["services"]):
                    merged[sid].restart = RestartPolicy.from_dict(policy)
            self._services = merged
            return self.list()

    def list(self) -> list[Service]:
        with self._lock:
            order = {"faustus": 0, "llm": 1, "comfyui": 2, "hub": 3, "apps": 4, "custom": 5}
            return sorted(self._services.values(), key=lambda s: (order.get(s.group, 9), s.port or 0, s.name.lower()))

    def get(self, service_id: str) -> Optional[Service]:
        with self._lock:
            return self._services.get((service_id or "").strip().lower())

    def find(self, text: str) -> Optional[Service]:
        """By id, then by name (case-insensitive, also without "'s Hoard"), then by port."""
        text = (text or "").strip()
        if not text:
            return None
        hit = self.get(text)
        if hit:
            return hit
        low = text.lower()
        for s in self.list():
            names = {s.name.lower(), s.name.lower().replace("'s hoard", "").strip(), s.name.lower().replace("'s", "")}
            if low in names:
                return s
        if low.isdigit():
            for s in self.list():
                if s.port == int(low):
                    return s
        for s in self.list():
            if low in s.name.lower() or low in s.id:
                return s
        return None

    def user_entries(self) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(e) for e in self._file["services"]]

    def policies(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._file["policies"])

    # ---------- writes ----------
    def upsert_user(self, raw: dict[str, Any]) -> Service:
        entry = validate_user_entry(raw)
        with self._lock:
            existing = next((e for e in self._file["services"] if e["id"] == entry["id"]), None)
            if existing is None and "url" not in entry and entry["id"] not in self._services:
                raise ValueError("A new service needs a url.")
            if existing is None:
                self._file["services"].append(entry)
            else:
                existing.update(entry)
            self._write_file()
            self.reload()
            return self._services[entry["id"]]

    def remove_user(self, service_id: str) -> bool:
        sid = (service_id or "").strip().lower()
        with self._lock:
            before = len(self._file["services"])
            self._file["services"] = [e for e in self._file["services"] if e["id"] != sid]
            removed = len(self._file["services"]) != before
            if removed:
                self._write_file()
                self.reload()
            return removed

    def set_policy(self, service_id: str, patch: dict[str, Any]) -> Service:
        service = self.get(service_id)
        if service is None:
            raise LookupError(f"Unknown service: {service_id}")
        with self._lock:
            current = service.restart.to_dict()
            current.update({k: v for k, v in patch.items() if k in ("enabled", "cmd", "cwd", "max_per_hour")})
            policy = RestartPolicy.from_dict(current).to_dict()
            user = next((e for e in self._file["services"] if e["id"] == service.id), None)
            if user is not None and (service.kind == "user" or "restart" in user):
                user["restart"] = policy
            else:
                self._file["policies"][service.id] = policy
            self._write_file()
            self.reload()
            return self._services[service.id]


def service_summary(s: Service) -> dict[str, Any]:
    return {"id": s.id, "name": s.name, "kind": s.kind, "group": s.group, "url": s.url, "port": s.port}


__all__ = ["Service", "RestartPolicy", "LaunchSpec", "Registry", "scan", "read_manifest", "builtin_externals", "validate_user_entry"]
