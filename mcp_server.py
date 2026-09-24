"""Stdio MCP bridge for Cassandra's Hoard.

It never opens the database: every tool call is proxied to the running app
(`POST /api/agent/call`) with the Bearer token from `<DATA_DIR>/mcp-token`.
The tool list is fetched from `GET /api/agent/tools` at start, so the bridge
and the app can never disagree. When nothing answers, the bridge starts the
app itself (`python -m cassandra_hoard`, detached, on the port of
CASSANDRA_URL) and waits for it; CASSANDRA_BRIDGE_AUTOSTART=0 turns that off.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Sequence
from urllib.parse import urlparse

import httpx
from mcp.server.fastmcp import FastMCP
from mcp.types import TextContent, Tool as MCPTool, ToolAnnotations

ROOT = Path(__file__).resolve().parent
BASE_URL = os.environ.get("CASSANDRA_URL", "http://127.0.0.1:5190").rstrip("/")
TOKEN_FILE = Path(
    os.environ.get("CASSANDRA_TOKEN_FILE")
    or Path(os.environ.get("CASSANDRA_DATA_DIR") or ROOT / "data") / "mcp-token"
)
NOT_RUNNING = "Open Cassandra's Hoard (python -m cassandra_hoard) so the assistant can see your local services."


def _check_local(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme != "http" or parsed.hostname not in ("127.0.0.1", "localhost", "::1"):
        raise SystemExit("The MCP bridge only connects to the local server.")


def _token() -> str:
    env = os.environ.get("CASSANDRA_TOKEN")
    if env:
        return env.strip()
    return TOKEN_FILE.read_text(encoding="utf-8").strip()


class CassandraBridge(FastMCP):
    """FastMCP whose tools come from the app's catalog instead of local functions."""

    def __init__(self, catalog: list[dict], instructions: str):
        super().__init__(name="cassandra-hoard", instructions=instructions)
        self._catalog = catalog

    async def list_tools(self) -> list[MCPTool]:
        return [
            MCPTool(
                name=t["name"],
                description=t["description"],
                inputSchema=t["inputSchema"],
                annotations=ToolAnnotations(**t.get("annotations", {})),
            )
            for t in self._catalog
        ]

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Sequence[TextContent]:
        return await self._call(name, arguments, retry=True)

    async def _call(self, name: str, arguments: dict[str, Any], retry: bool) -> Sequence[TextContent]:
        try:
            async with httpx.AsyncClient(timeout=90, trust_env=False) as client:
                response = await client.post(
                    f"{BASE_URL}/api/agent/call",
                    json={"name": name, "arguments": arguments or {}},
                    headers={"Authorization": f"Bearer {_token()}"},
                )
            body = response.json()
            if response.status_code >= 400:
                return [TextContent(type="text", text=json.dumps({"error": body.get("error", f"Error {response.status_code}")}, ensure_ascii=False))]
            return [TextContent(type="text", text=json.dumps(body, ensure_ascii=False))]
        except (httpx.ConnectError, FileNotFoundError):
            if retry and ensure_running():
                return await self._call(name, arguments, retry=False)
            return [TextContent(type="text", text=json.dumps({"error": NOT_RUNNING}))]
        except Exception as error:  # keep the bridge alive on any failure
            return [TextContent(type="text", text=json.dumps({"error": str(error)}))]


def _healthy() -> bool:
    try:
        response = httpx.get(f"{BASE_URL}/api/health", timeout=2, trust_env=False)
        return response.status_code == 200 and response.json().get("service") == "cassandra-hoard"
    except Exception:
        return False


def ensure_running(timeout_s: float = 40.0) -> bool:
    """Start the app detached when it is not answering (unless disabled); True once healthy."""
    if _healthy():
        return True
    if os.environ.get("CASSANDRA_BRIDGE_AUTOSTART", "1") == "0":
        return False
    port = urlparse(BASE_URL).port or 5190
    env = {**os.environ, "CASSANDRA_PORT": str(port), "PORT_STRICT": "1", "PYTHONUNBUFFERED": "1"}
    kwargs: dict[str, Any] = {"start_new_session": True}
    if sys.platform.startswith("win"):
        kwargs = {"creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(subprocess, "CREATE_NO_WINDOW", 0)}
    logs = Path(os.environ.get("CASSANDRA_DATA_DIR") or ROOT / "data") / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    with open(logs / "cassandra-app.log", "ab") as out:
        subprocess.Popen([sys.executable, "-m", "cassandra_hoard"], cwd=ROOT, env=env, stdin=subprocess.DEVNULL,
                         stdout=out, stderr=subprocess.STDOUT, close_fds=True, **kwargs)
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if _healthy():
            return True
        time.sleep(0.5)
    return False


def fetch_catalog() -> tuple[list[dict], str]:
    ensure_running()
    try:
        response = httpx.get(f"{BASE_URL}/api/agent/tools", timeout=10, trust_env=False)
        response.raise_for_status()
    except Exception as error:
        raise SystemExit(f"{NOT_RUNNING} ({error})") from error
    data = response.json()
    return data["tools"], data.get("instructions", "")


def main() -> None:
    logging.basicConfig(level=logging.WARNING)  # stderr only; stdout belongs to the protocol
    logging.getLogger("httpx").setLevel(logging.WARNING)
    _check_local(BASE_URL)
    catalog, instructions = fetch_catalog()
    CassandraBridge(catalog, instructions).run(transport="stdio")


if __name__ == "__main__":
    sys.exit(main())
