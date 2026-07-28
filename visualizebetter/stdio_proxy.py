"""mcp-stdio — the [8-D] thin proxy for stdio MCP clients (Claude Desktop).

★ [8-D] load-bearing rule: exactly one process owns the Graph Core — ``visualizebetter
serve``. A stdio client such as Claude Desktop spawns its MCP server as a separate
OS process; if THAT process kept its own in-memory graph it would split-brain, and
a node the AI pushed would never reach the browser attached to serve.

So this module owns no graph. It is a bidirectional message pump: it discovers (or
auto-launches) a local ``serve`` and forwards MCP JSON-RPC between its own
stdin/stdout and serve's MCP HTTP endpoint verbatim — which is exactly what carries
initialize's ``clientInfo`` through to serve for [10-A]/[23-E] layer tagging. It
imports no Graph, no create_server, no WSHub: there is nothing here to split-brain
with.

Security ([11]): loopback only. When the discovered serve advertises a token (a
non-loopback serve), the proxy attaches it as a Bearer header on every relayed
request. The token is read from serve's own 0600 discovery file.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import anyio
import httpx
from mcp.client.streamable_http import streamablehttp_client
from mcp.server.stdio import stdio_server

from visualizebetter.graph.snapshots import default_data_dir
from visualizebetter.server import port_file_path

HEALTH_TIMEOUT_S = 2.0
LAUNCH_TIMEOUT_S = 25.0
_POLL_INTERVAL_S = 0.25


# --- discovery ([8-D]) ---


def read_port_file(data_dir: Path | str) -> dict | None:
    """The serve advertised in the [8-D] discovery file, or None if absent/garbled."""
    try:
        return json.loads(port_file_path(data_dir).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def is_serve_healthy(info: dict) -> bool:
    """True if the advertised serve actually answers on loopback right now — so a
    stale port-file (a serve that died without cleanup) is not mistaken for live."""
    headers = {}
    if info.get("token"):
        headers["Authorization"] = f"Bearer {info['token']}"
    try:
        response = httpx.get(
            f"{info['url']}/graph.json", headers=headers, timeout=HEALTH_TIMEOUT_S
        )
        return response.status_code == 200
    except httpx.HTTPError:
        return False


def discover_serve(data_dir: Path | str) -> dict | None:
    """A live local serve, or None. Never returns a stale (dead) advertisement."""
    info = read_port_file(data_dir)
    if info and is_serve_healthy(info):
        return info
    return None


# --- auto-launch ([8-D]) ---


def spawn_serve(data_dir: Path | str, port: int | None = None) -> subprocess.Popen:
    """Background-launch a headless ``visualizebetter serve`` that outlives this proxy.

    Detached (own session / no window, no stdio tie), so Claude Desktop killing the
    proxy does not take serve — the graph and its browser survive. Loopback + no
    token: an auto-launched serve is local-only, so [11] requires none.
    """
    cmd = [sys.executable, "-m", "visualizebetter.cli", "serve", "--no-open",
           "--data-dir", str(data_dir)]
    if port is not None:
        cmd += ["--port", str(port)]
    kwargs: dict = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
    }
    if os.name == "posix":
        kwargs["start_new_session"] = True
    else:  # Windows: detach into its own process group, no console window
        kwargs["creationflags"] = (
            subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
        )
    return subprocess.Popen(cmd, **kwargs)


def ensure_serve(
    data_dir: Path | str, port: int | None = None, timeout_s: float = LAUNCH_TIMEOUT_S
) -> dict:
    """Discover a live serve, or auto-launch one and wait until it is healthy ([8-D])."""
    info = discover_serve(data_dir)
    if info is not None:
        return info

    spawn_serve(data_dir, port=port)
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        info = discover_serve(data_dir)
        if info is not None:
            return info
        time.sleep(_POLL_INTERVAL_S)
    raise RuntimeError(
        "could not discover or auto-launch visualizebetter serve within "
        f"{timeout_s:.0f}s ([8-D])"
    )


# --- the bridge (owns no graph) ---


def _auth_headers(token: str | None) -> dict[str, str] | None:
    """[11] The Bearer header the proxy attaches to every relayed request when the
    discovered serve advertised a token, or None for a loopback serve with none.

    An empty token is treated as no token — a loopback serve writes ``token: null``,
    never an empty string, so this only ever attaches a real credential."""
    return {"Authorization": f"Bearer {token}"} if token else None


async def _bridge(mcp_url: str, token: str | None) -> None:
    """Pump MCP JSON-RPC both ways between this process's stdin/stdout and serve's
    MCP HTTP endpoint. Messages pass through untouched, so initialize's clientInfo
    reaches serve verbatim ([23-E]); the HTTP transport handles the mcp-session-id."""
    headers = _auth_headers(token)
    async with stdio_server() as (stdin_read, stdout_write):
        async with streamablehttp_client(mcp_url, headers=headers) as (
            http_read,
            http_write,
            _get_session_id,
        ):
            async with anyio.create_task_group() as tg:

                async def client_to_serve() -> None:
                    async for message in stdin_read:
                        if isinstance(message, Exception):
                            break
                        await http_write.send(message)
                    tg.cancel_scope.cancel()  # client (stdin) gone → shut down

                async def serve_to_client() -> None:
                    async for message in http_read:
                        if isinstance(message, Exception):
                            break
                        await stdout_write.send(message)
                    tg.cancel_scope.cancel()  # serve gone → shut down

                tg.start_soon(client_to_serve)
                tg.start_soon(serve_to_client)


def run_stdio_proxy(data_dir: Path | str | None = None, port: int | None = None) -> None:
    """[8-D] entry point: ensure a local serve exists, then bridge stdin/stdout to it."""
    dd = Path(data_dir) if data_dir is not None else default_data_dir()
    info = ensure_serve(dd, port=port)
    anyio.run(_bridge, info["mcp"], info.get("token"))
