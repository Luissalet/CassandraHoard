"""Health, status, services, lanes, history, poll-now."""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, HTTPException, Query, Request

from .. import SERVICE, __version__, views
from ..times import iso, window
from .deps import services

router = APIRouter(prefix="/api")


def _window(since, until, at, window_min, default_hours, now):
    try:
        return window(since, until, at, window_min, default_hours, now)
    except ValueError as error:
        raise HTTPException(400, str(error)) from error


def _service(svc, text: str):
    try:
        return svc.resolve(text)
    except LookupError as error:
        raise HTTPException(404, str(error)) from error


@router.get("/health")
def health(request: Request):
    return {"service": SERVICE, "version": __version__, "dataDirConfigured": request.app.state.config.data_dir_configured}


@router.get("/status")
def status(request: Request):
    return services(request).status()


@router.get("/services")
def list_services(request: Request):
    svc = services(request)
    return {"services": svc.service_states(), "system": svc.poller.system_state()}


@router.get("/services/{service_id}")
def one_service(request: Request, service_id: str):
    svc = services(request)
    service = _service(svc, service_id)
    state = svc.poller.service_state(service)
    return {**state, "expect": service.expect, "launch": service.launch.to_dict() if service.launch else None,
            "restart_method": svc.restarter.method_for(service), "restarts": svc.restarter.history(service.id, 20),
            "auto_restarts_last_hour": svc.restarter.recent(service.id)}


@router.get("/services/{service_id}/history")
def service_history(request: Request, service_id: str, since: Optional[str] = None, until: Optional[str] = None,
                    at: Optional[str] = None, window_min: float = Query(15, ge=1, le=1440)):
    svc = services(request)
    service = _service(svc, service_id)
    start, end = _window(since, until, at, window_min, 24, svc.clock())
    return {"service": service.id, "since": iso(start), "until": iso(end), **views.history(svc.db, service.id, start, end)}


@router.get("/lanes")
def lanes(request: Request, hours: float = Query(24, gt=0, le=24 * 30)):
    svc = services(request)
    now = svc.clock()
    since = now - hours * 3600
    states = svc.service_states()
    ever = {s["id"] for s in states if s["ever_up"]}
    ids = [s["id"] for s in states]
    data = views.lanes(svc.db, ids, since, now, ever)
    return {
        "since": since, "until": now,
        "lanes": [{"id": s["id"], "name": s["name"], "group": s["group"], "state": s["state"], "segments": data[s["id"]],
                   "uptime_pct": views.uptime_pct(data[s["id"]], since, now)} for s in states],
        "gaps": views.gaps(svc.db, since, now),
        "reboots": [r["ts"] for r in svc.db.query("SELECT ts FROM events WHERE service = 'system' AND kind = 'reboot' AND ts >= ?", (since,))],
    }


@router.post("/poll")
def poll_now(request: Request):
    svc = services(request)
    result = svc.poller.tick()
    return {"ok": True, "took_ms": result["took_ms"], "changes": result["changes"]}
