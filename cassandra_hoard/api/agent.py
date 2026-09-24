"""/api/agent/* — the bridge used by mcp_server.py (Bearer token from <DATA_DIR>/mcp-token)."""

from __future__ import annotations

import secrets
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field, ValidationError

from ..agent_tools import AGENT_INSTRUCTIONS, call_tool, tool_catalog
from .deps import services

router = APIRouter(prefix="/api/agent")


class CallBody(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    arguments: dict[str, Any] | None = None


@router.get("/tools")
def tools():
    return {"instructions": AGENT_INSTRUCTIONS, "tools": tool_catalog()}


@router.post("/call")
def call(request: Request, body: CallBody):
    svc = services(request)
    header = request.headers.get("authorization", "")
    given = header[7:].strip() if header.startswith("Bearer ") else ""
    if not given or not secrets.compare_digest(given, svc.token):
        raise HTTPException(401, "Invalid MCP token.")
    try:
        return call_tool(svc, body.name, body.arguments)
    except KeyError as error:
        raise HTTPException(404, str(error.args[0])) from error
    except ValidationError as error:
        issues = "; ".join(f"{'.'.join(str(p) for p in e['loc']) or 'input'}: {e['msg']}" for e in error.errors())
        raise HTTPException(400, issues) from error
    except (ValueError, LookupError) as error:
        raise HTTPException(400 if isinstance(error, ValueError) else 404, str(error)) from error
