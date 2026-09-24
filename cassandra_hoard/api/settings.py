"""The watch list: user services (data/services.json), restart policies, manual restart."""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from ..poller import OK_STATES
from .deps import services
from .status import _service

router = APIRouter(prefix="/api")


class WatchBody(BaseModel):
    id: str = Field(..., min_length=1, max_length=64)
    name: Optional[str] = Field(None, max_length=120)
    url: Optional[str] = Field(None, max_length=500)
    health_path: Optional[str] = Field(None, max_length=300)
    expect: Optional[dict[str, Any]] = None
    log_paths: Optional[list[str]] = Field(None, max_length=50)
    restart: Optional[dict[str, Any]] = None


class PolicyBody(BaseModel):
    enabled: Optional[bool] = None
    cmd: Optional[str | list[str]] = None
    cwd: Optional[str] = Field(None, max_length=2000)
    max_per_hour: Optional[int] = Field(None, ge=0, le=60)


@router.get("/settings")
def settings(request: Request):
    svc = services(request)
    config = svc.config
    return {
        "user_services": svc.registry.user_entries(),
        "policies": svc.registry.policies(),
        "services_file": str(config.services_path),
        "registry_error": svc.registry.load_error,
        "config": {
            "poll_s": config.poll_s, "roots": config.roots, "externals": config.externals, "log_globs": config.log_globs,
            "retention_days": config.retention_days, "auto_restart": config.auto_restart, "hub_url": config.hub_url,
            "gpu": config.gpu, "slow_ms": config.slow_ms, "sample_every_s": config.sample_every_s,
        },
    }


@router.post("/services", status_code=201)
def watch(request: Request, body: WatchBody):
    svc = services(request)
    raw = body.model_dump(exclude_none=True)
    try:
        service = svc.registry.upsert_user(raw)
    except ValueError as error:
        raise HTTPException(400, str(error)) from error
    svc.poller.wake()
    return service.to_dict()


@router.delete("/services/{service_id}")
def unwatch(request: Request, service_id: str):
    svc = services(request)
    if not svc.registry.remove_user(service_id):
        raise HTTPException(404, "Only services you added (or edited) can be removed.")
    return {"ok": True}


@router.put("/services/{service_id}/policy")
def policy(request: Request, service_id: str, body: PolicyBody):
    svc = services(request)
    service = _service(svc, service_id)
    patch = body.model_dump(exclude_unset=True)
    if "cmd" in patch and isinstance(patch["cmd"], str) and not patch["cmd"].strip():
        patch["cmd"] = None
    try:
        updated = svc.registry.set_policy(service.id, patch)
    except (LookupError, ValueError) as error:
        raise HTTPException(400, str(error)) from error
    return {"ok": True, "restart": updated.restart.to_dict(), "method": svc.restarter.method_for(updated)}


@router.post("/services/{service_id}/restart")
def restart(request: Request, service_id: str):
    svc = services(request)
    service = _service(svc, service_id)
    cur = svc.poller.current.get(service.id)
    if cur and cur.state == "foreign":
        raise HTTPException(409, f"Another program answers on port {service.port}; not restarting over it.")
    running = bool(cur and cur.state in OK_STATES)
    result = svc.restarter.restart(service, "manual", None, running, cur.pid if cur else None, cur.pid_started if cur else None)
    svc.poller.wake()
    if not result.get("ok") and "method" not in result:
        raise HTTPException(409, result.get("error", "Cannot restart."))
    return result
