"""The public websites: status, history, the list (data/sites.json) and a manual check."""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from ..times import iso
from .deps import services
from .status import _window

router = APIRouter(prefix="/api")


class SiteBody(BaseModel):
    id: Optional[str] = Field(None, max_length=64)
    url: Optional[str] = Field(None, max_length=500)
    name: Optional[str] = Field(None, max_length=120)
    expect_status: Optional[list[str]] = Field(None, max_length=20)
    keyword: Optional[str] = Field(None, max_length=200)  # "" removes it
    interval_min: Optional[float] = Field(None, ge=1, le=1440)
    enabled: Optional[bool] = None
    domain: Optional[str] = Field(None, max_length=253)


class CheckBody(BaseModel):
    site: Optional[str] = Field(None, max_length=120)


def _site(svc, text: str) -> dict[str, Any]:
    site = svc.sites.store.find(text)
    if site is None:
        raise HTTPException(404, f"Unknown site '{text}'.")
    return site


@router.get("/sites")
def sites(request: Request, hours: float = Query(24, gt=0, le=24 * 30)):
    svc = services(request)
    now = svc.clock()
    since = now - hours * 3600
    lanes = svc.sites.lanes(since, now)
    items = svc.sites.status_all(now)
    for item in items:
        item["lane"] = lanes.get(item["id"])
    return {"sites": items, "since": since, "until": now, "summary": svc.sites.summary(), "sites_file": str(svc.config.sites_path),
            "error": svc.sites.store.load_error}


@router.get("/sites/{site_id}/history")
def site_history(request: Request, site_id: str, since: Optional[str] = None, until: Optional[str] = None, at: Optional[str] = None,
                 window_min: float = Query(15, ge=1, le=1440)):
    svc = services(request)
    site = _site(svc, site_id)
    start, end = _window(since, until, at, window_min, 24, svc.clock())
    return svc.sites.history(site, start, end)


@router.post("/sites", status_code=201)
def watch_site(request: Request, body: SiteBody):
    svc = services(request)
    raw = body.model_dump(exclude_none=True)
    try:
        site, created = svc.sites.store.upsert(raw)
    except ValueError as error:
        raise HTTPException(400, str(error)) from error
    svc.sites.wake()
    return {"created": created, "site": svc.sites.status_one(site)}


@router.delete("/sites/{site_id}")
def unwatch_site(request: Request, site_id: str):
    svc = services(request)
    site = svc.sites.store.remove(site_id)
    if site is None:
        raise HTTPException(404, f"Unknown site '{site_id}'.")
    svc.sites.forget(site)
    return {"ok": True}


@router.post("/sites/check")
def check_sites(request: Request, body: CheckBody | None = None):
    """Check now (every site, or one). A site checked less than a minute ago is left alone."""
    svc = services(request)
    try:
        results = svc.sites.check_now(body.site if body else None)
    except LookupError as error:
        raise HTTPException(404, str(error)) from error
    return {"ok": True, "results": results, "checked_at": iso(svc.clock())}
