"""Incidents, logs and GPU timeline."""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, HTTPException, Query, Request

from .. import views
from ..audit import secrets_audit
from ..incidents import explain
from ..times import iso
from .deps import services
from .status import _service, _window

router = APIRouter(prefix="/api")


@router.get("/incidents")
def incidents(request: Request, since: Optional[str] = None, until: Optional[str] = None, at: Optional[str] = None,
              window_min: float = Query(15, ge=1, le=1440), service: Optional[str] = None, open_only: bool = False,
              limit: int = Query(100, ge=1, le=1000)):
    svc = services(request)
    start, end = _window(since, until, at, window_min, 24 * 7, svc.clock())
    target = _service(svc, service).id if service else None
    items = svc.incidents.list(None if open_only else start, end, target, open_only, limit)
    now = svc.clock()
    for item in items:
        item["name"] = svc.name_of(item["service"])
        item["explanation"] = explain(item, item["name"], None, now)
    return {"since": iso(start), "until": iso(end), "incidents": items}


@router.get("/incidents/{incident_id}")
def incident(request: Request, incident_id: int):
    svc = services(request)
    item = svc.incidents.get(incident_id)
    if item is None:
        raise HTTPException(404, "Incident not found.")
    service = svc.registry.get(item["service"])
    item["name"] = svc.name_of(item["service"])
    item["explanation"] = explain(item, item["name"], service.port if service else None, svc.clock())
    return item


@router.get("/logs")
def logs(request: Request, q: str = Query("", max_length=300), service: Optional[str] = None, since: Optional[str] = None,
         until: Optional[str] = None, at: Optional[str] = None, window_min: float = Query(15, ge=1, le=1440),
         level: Optional[str] = Query(None, pattern="^(error|warning|info|debug)$"), limit: int = Query(200, ge=1, le=2000)):
    svc = services(request)
    start, end = _window(since, until, at, window_min, 24, svc.clock())
    target = _service(svc, service).id if service else None
    rows = svc.logs.search(q, target, start, end, limit, level)
    return {"since": iso(start), "until": iso(end), "lines": rows, "truncated": len(rows) >= limit}


@router.get("/logs/sources")
def log_sources(request: Request):
    svc = services(request)
    files = svc.logs.files()
    return {"sources": [s.to_dict() for s in svc.logs.sources()], "files": [{"path": p, "service": s} for p, s in files]}


@router.get("/gpu")
def gpu(request: Request, since: Optional[str] = None, until: Optional[str] = None, at: Optional[str] = None,
        window_min: float = Query(15, ge=1, le=1440), gpu: Optional[int] = Query(None, ge=0, le=64), points: int = Query(240, ge=4, le=1000)):
    svc = services(request)
    start, end = _window(since, until, at, window_min, 24, svc.clock())
    return views.gpu_timeline(svc.db, start, end, gpu, points)


@router.get("/audit")
def audit(request: Request, q: str = "", type: Optional[str] = None, source: Optional[str] = None, tool: Optional[str] = None,
          failed: bool = False, since: Optional[str] = None, until: Optional[str] = None, at: Optional[str] = None,
          window_min: float = Query(15, ge=1, le=1440), limit: int = Query(200, ge=1, le=500)):
    """The family bus mirrored from the hub (agent calls, app events, hub actions)."""
    svc = services(request)
    start, end = _window(since, until, at, window_min, 24, svc.clock())
    kwargs = {"query": q, "type": type, "source": source, "tool": tool, "since": start, "until": end, "limit": limit}
    if failed:
        kwargs["ok"] = False
        kwargs["type"] = type or "agent.call"
    return {"since": iso(start), "until": iso(end), "events": svc.bus.search(**kwargs), "bus": svc.bus.status()}


@router.get("/audit/stats")
def audit_stats(request: Request, since: Optional[str] = None, until: Optional[str] = None, at: Optional[str] = None,
                window_min: float = Query(15, ge=1, le=1440)):
    svc = services(request)
    start, end = _window(since, until, at, window_min, 24 * 7, svc.clock())
    return {"since": iso(start), "until": iso(end), **svc.bus.stats(start, end), "bus": svc.bus.status()}


@router.post("/audit/sync")
def audit_sync(request: Request):
    svc = services(request)
    return {"synced": svc.bus.sync_once(), "bus": svc.bus.status()}


@router.get("/secrets")
def secrets(request: Request, service: Optional[str] = None):
    svc = services(request)
    targets = [_service(svc, service)] if service else svc.registry.list()
    return secrets_audit(targets, now=svc.clock())
