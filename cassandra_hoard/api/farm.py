"""GET /api/faustus/farm - what Faustus is running right now, for the Panel card."""

from __future__ import annotations

from fastapi import APIRouter, Request

from .. import faustus_farm
from .deps import services

router = APIRouter(prefix="/api/faustus")


@router.get("/farm")
def farm(request: Request):
    """The runs, their sub-agents and the budget; or why Faustus cannot be read."""
    svc = services(request)
    return faustus_farm.report(svc.config.data_dir, clock=svc.clock)