"""Request guard shared by every Hoard app: which Host values are accepted, which
Origins may call the API and which Fetch Metadata combinations are let through.

Pure functions over plain header mappings so the rules are unit-testable;
install_guard() wraps them as a FastAPI/Starlette HTTP middleware.

Rules (identical in every app):
1. Host: localhost / 127.0.0.1 / [::1] plus the comma-separated env list
   (exact hostnames or "*.suffix"), compared without port, case-insensitive.
2. Origin (when present): its host must pass the host rule (any scheme or
   port), or be one of the Vite dev origins.
3. Fetch Metadata: a cross-site request is only accepted when it is a
   top-level navigation (mode "navigate" and not embedded in a frame).
   Requests without Sec-Fetch-* headers (curl, the MCP bridge) pass.
4. State-changing methods are never accepted as navigations (an HTML form
   post from another page): the API takes JSON from fetch() only.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

LOCAL_HOSTS = ("localhost", "127.0.0.1", "[::1]")
DEV_ORIGINS = ("http://localhost:5173", "http://127.0.0.1:5173", "http://localhost:5174", "http://127.0.0.1:5174")
FRAME_DESTS = frozenset({"iframe", "frame", "embed", "object"})
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def host_of(value: str | None) -> str:
    """Host part of a Host header, Origin or URL: no scheme, no path, no port, lowercase."""
    host = (value or "").strip().lower()
    scheme = host.find("://")
    if scheme != -1:
        host = host[scheme + 3 :]
    host = host.split("/", 1)[0]
    if host.startswith("["):
        end = host.find("]")
        return host if end == -1 else host[: end + 1]
    return host.split(":", 1)[0]


def parse_allowed_hosts(raw: str | None) -> tuple[str, ...]:
    """Parse "a.example, *.ts.net" into a clean tuple of patterns."""
    patterns = []
    for entry in (raw or "").split(","):
        entry = entry.strip()
        pattern = "*." + host_of(entry[2:]) if entry.startswith("*.") else host_of(entry)
        if pattern and pattern != "*.":
            patterns.append(pattern)
    return tuple(patterns)


def is_allowed_host(host: str | None, allowed_hosts: Iterable[str] = ()) -> bool:
    if not host:
        return False
    for pattern in (*LOCAL_HOSTS, *allowed_hosts):
        if pattern.startswith("*."):
            if host.endswith(pattern[1:]) and len(host) > len(pattern) - 1:
                return True
        elif host == pattern:
            return True
    return False


def check_request(method: str, headers: Mapping[str, str], allowed_hosts: Iterable[str] = ()) -> str | None:
    """Return None when the request may proceed, otherwise the error message."""
    allowed = tuple(allowed_hosts)
    if not is_allowed_host(host_of(headers.get("host")), allowed):
        return "Only local access is allowed."
    origin = headers.get("origin")
    if origin and origin not in DEV_ORIGINS and not is_allowed_host(host_of(origin), allowed):
        return "Origin not allowed."
    site = headers.get("sec-fetch-site")
    mode = headers.get("sec-fetch-mode")
    dest = headers.get("sec-fetch-dest")
    if site == "cross-site" and (mode != "navigate" or dest in FRAME_DESTS):
        return "Cross-site requests are not allowed."
    if mode == "navigate" and method.upper() not in SAFE_METHODS:
        return "Form submissions are not allowed."
    return None


def install_guard(app: FastAPI, allowed_hosts: Iterable[str] = ()) -> None:
    """Add the guard as an HTTP middleware: 403 {"error": ...} when check_request() rejects."""
    allowed = tuple(allowed_hosts)

    @app.middleware("http")
    async def guard(request: Request, call_next):
        error = check_request(request.method, request.headers, allowed)
        if error:
            return JSONResponse({"error": error}, status_code=403)
        return await call_next(request)
