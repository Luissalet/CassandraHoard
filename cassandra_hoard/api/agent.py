"""/api/agent/* — the bridge used by mcp_server.py (Bearer token from <DATA_DIR>/mcp-token)."""

from __future__ import annotations

from typing import Any

from fastapi import Request

from ..agent_tools import AGENT_INSTRUCTIONS, call_tool, tool_catalog
from ..hoard_link.agentkit import AppError, make_agent_router
from .deps import services


def _call(name: str, arguments: dict[str, Any], request: Request) -> Any:
    try:
        return call_tool(services(request), name, arguments)
    except KeyError:
        raise  # unknown tool: the shared router answers 404 unknown_tool
    except LookupError as error:  # "Unknown service 'x'": a missing thing, not a server fault
        raise AppError("not_found", str(error)) from error


router = make_agent_router(
    tools_fn=tool_catalog,
    call_fn=_call,
    token_fn=lambda request: services(request).token,
    instructions=AGENT_INSTRUCTIONS,
    app_name="cassandra",
)
