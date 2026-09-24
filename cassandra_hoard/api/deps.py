"""Shared helpers for the API routers."""

from __future__ import annotations

from fastapi import Request

from ..services import Services


def services(request: Request) -> Services:
    return request.app.state.services
