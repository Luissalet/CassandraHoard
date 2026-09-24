"""/api/agent/* — the bridge used by mcp_server.py (Bearer token from <DATA_DIR>/mcp-token)."""

from __future__ import annotations

import secrets
import time
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field, ValidationError

from ..agent_tools import AGENT_INSTRUCTIONS, call_tool, tool_catalog
from .deps import services
from ..hoard_link import family

router = APIRouter(prefix="/api/agent")


class CallBody(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    arguments: dict[str, Any] | None = None
    caller: str | None = Field(default=None, max_length=80)  # who asks (the hub's proxy fills it)


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
    t0 = time.monotonic()
    outcome = {"ok": False, "error": ""}
    try:
        result = call_tool(svc, body.name, body.arguments)
        outcome["ok"] = True
        return result
    except KeyError as error:
        outcome["error"] = str(error.args[0])
        raise HTTPException(404, str(error.args[0])) from error
    except ValidationError as error:
        issues = "; ".join(f"{'.'.join(str(p) for p in e['loc']) or 'input'}: {e['msg']}" for e in error.errors())
        outcome["error"] = issues
        raise HTTPException(400, issues) from error
    except (ValueError, LookupError) as error:
        outcome["error"] = str(error)
        raise HTTPException(400 if isinstance(error, ValueError) else 404, str(error)) from error
    except Exception as error:  # noqa: BLE001 — recorded, then re-raised as before
        outcome["error"] = f"{type(error).__name__}: {error}"
        raise
    finally:
        # One agent.call event per call on the family bus (the audit trail).
        family.record_call(body.name, outcome["ok"], int((time.monotonic() - t0) * 1000),
                           caller=body.caller or "", error=outcome["error"])
