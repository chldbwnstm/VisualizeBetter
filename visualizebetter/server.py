"""serve — the one long-running process ([8-B], [8-D]).

[8-D] is the load-bearing rule: exactly one process owns the Graph Core. Graph
Core, the WS Hub and the MCP HTTP endpoint all live here. A stdio MCP server with
its own in-memory graph would split-brain — nodes an AI pushed would never reach
the browser attached to serve.

Assembled from parts that already exist and were reviewed on their own:
Graph ([4]), create_server ([5-G]/[5-F]), WSHub ([8-C]), AutoSnapshotter ([23-C]).

Out of scope by dispatch: snapshot.load replacing the live graph (in-place reload
is a Graph structure change needing its own approval) and the stdio proxy ([8-D]).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError

from visualizebetter.graph.core import Graph
from visualizebetter.graph.snapshots import AutoSnapshotter, SnapshotStore
from visualizebetter.mcp_server import create_server
from visualizebetter.ws.hub import (
    RateLimitExceeded,
    WSHub,
    allowed_origins,
    validate_origin,
)

logger = logging.getLogger("visualizebetter.serve")

DEFAULT_PORT = 8765
DEFAULT_BIND = "127.0.0.1"
MCP_PATH = "/mcp"
FLUSH_INTERVAL_SECONDS = 0.05
"""[8-C] 16~50ms 코얼레싱 윈도우 — the hub buffers, this timer drains it."""

LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}

PORT_FILE_NAME = "serve.json"
"""[8-D] discovery — a running serve advertises its port (and token) here so the
mcp-stdio proxy can find or auto-launch it."""


def port_file_path(data_dir: Path | str) -> Path:
    """[8-D] the serve discovery file inside the (user-private) data directory."""
    return Path(data_dir) / PORT_FILE_NAME


def _write_port_file(path: Path, *, port: int, bind: str, auth_token: str | None) -> None:
    """[8-D]/[11] Advertise this serve for the stdio proxy. The proxy always
    connects over loopback, so the url is 127.0.0.1 regardless of the bind. The
    token (present only for a non-loopback serve) is written 0600 — the data dir
    is already user-private, and this narrows it to owner-only on POSIX."""
    info = {
        "pid": os.getpid(),
        "port": port,
        "bind": bind,
        "url": f"http://127.0.0.1:{port}",
        "mcp": f"http://127.0.0.1:{port}{MCP_PATH}/",
        "token": auth_token,
    }
    path.write_text(json.dumps(info), encoding="utf-8")
    if os.name == "posix":
        with contextlib.suppress(OSError):
            path.chmod(0o600)


def frontend_dist() -> Path:
    """[9-A] the built SPA.

    In a PyInstaller sidecar ([8-F] Tauri shell) the SPA is bundled under
    ``sys._MEIPASS/frontend/dist``; in the repo / PyPI layout it is
    ``<root>/frontend/dist``. Serving it from either lets the desktop app run with
    no browser and no separate static host.
    """
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS", ".")) / "frontend" / "dist"
    return Path(__file__).resolve().parent.parent / "frontend" / "dist"


def allowed_hosts(port: int) -> set[str]:
    """[11] DNS rebinding 방어 — Host 허용 목록."""
    return {
        f"localhost:{port}",
        f"127.0.0.1:{port}",
        f"[::1]:{port}",
    }


def is_loopback(bind: str) -> bool:
    return bind in LOOPBACK_HOSTS


def _ws_token_ok(socket: WebSocket, auth_token: str) -> bool:
    """[11] WS auth — the same Bearer token the HTTP ``guard`` requires.

    Accepted as an ``Authorization: Bearer <token>`` header (identical to the HTTP
    path) or a ``?token=<token>`` query param, because a browser's WebSocket API
    cannot set handshake headers. Reuses the existing token; no new mechanism.
    """
    if socket.headers.get("authorization") == f"Bearer {auth_token}":
        return True
    return socket.query_params.get("token") == auth_token


class WebSocketConnection:
    """Adapts a FastAPI WebSocket to the hub's Connection protocol ([8-C])."""

    def __init__(self, socket: WebSocket) -> None:
        self.socket = socket

    async def send(self, text: str) -> None:
        await self.socket.send_text(text)


def create_app(
    *,
    port: int = DEFAULT_PORT,
    bind: str = DEFAULT_BIND,
    auth_token: str | None = None,
    data_dir: Path | str | None = None,
    graph: Graph | None = None,
    serve_static: bool = True,
    flush_interval: float = FLUSH_INTERVAL_SECONDS,
    port_file: bool = False,
) -> FastAPI:
    """Build the serve ASGI app around a single Graph ([8-D]).

    ``port_file`` writes the [8-D] discovery file while serving (run() sets it);
    it defaults off so a TestClient app never touches the shared data directory.
    """
    core = graph if graph is not None else Graph(name="visualizebetter")
    store = SnapshotStore(data_dir)
    hub = WSHub(core)
    hub.subscribe()
    snapshotter = AutoSnapshotter(core, store)
    # store/snapshotter injected so the [5-E] tools exist: without them an AI
    # cannot save or load, and the [23-D] handoff has no mechanism.
    mcp = create_server(core, store=store, snapshotter=snapshotter, session=hub)

    hosts = allowed_hosts(port)
    origins = allowed_origins(port)

    # [11]: enable the SDK's own transport security, and apply the same rules to
    # our /graph.json and /live below.
    # path="/" because the app is mounted at MCP_PATH — setting both would nest
    # the endpoint at /mcp/mcp.
    mcp_app = mcp.http_app(
        path="/",
        allowed_hosts=sorted(hosts),
        allowed_origins=sorted(origins),
    )

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        # The MCP app carries its own lifespan (session manager); mounting without
        # chaining it leaves the endpoint dead.
        async with mcp_app.router.lifespan_context(app):
            await store.initialize()
            snapshotter.start()
            flusher = asyncio.create_task(_flush_loop(hub, flush_interval))
            pf = port_file_path(store.data_dir) if port_file else None
            if pf is not None:
                _write_port_file(pf, port=port, bind=bind, auth_token=auth_token)
            try:
                yield
            finally:
                if pf is not None:
                    with contextlib.suppress(OSError):
                        pf.unlink()
                flusher.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await flusher
                await snapshotter.stop()
                # [13-B] CH1(3) — stop() only cancels the ticker. Without a
                # final save, every edit since the last tick died with the
                # process on an *orderly* shutdown, which is the one case the
                # user expects to be safe. Best-effort: a failure here must not
                # replace the real shutdown path with an exception.
                try:
                    await snapshotter.snapshot_if_dirty()
                except Exception as exc:  # noqa: BLE001
                    logger.warning('final snapshot on shutdown failed: %s', exc)
                hub.unsubscribe()

    app = FastAPI(title="visualizebetter", lifespan=lifespan)
    app.state.graph = core
    app.state.hub = hub
    app.state.store = store
    app.state.snapshotter = snapshotter
    # SessionState seam: the hub holds layer visibility and the last view state.
    # [5-C]'s USER STATE tools (get_view_state and friends) read it from here when
    # that task exposes them — no tool is registered now.
    app.state.session_state = hub

    @app.middleware("http")
    async def guard(request: Request, call_next):
        """[11] Host 검증 (DNS rebinding) + 비루프백 토큰."""
        host = request.headers.get("host")
        if host is not None and host not in hosts:
            return JSONResponse({"error": "host not allowed"}, status_code=403)
        if auth_token is not None:
            provided = request.headers.get("authorization")
            if provided != f"Bearer {auth_token}":
                return JSONResponse({"error": "unauthorized"}, status_code=403)
        return await call_next(request)

    @app.api_route(
        MCP_PATH, methods=["GET", "POST", "DELETE"], include_in_schema=False
    )
    async def mcp_redirect() -> RedirectResponse:
        """Send the bare /mcp to the mounted /mcp/.

        Starlette's Mount("/mcp") only matches "/mcp/...", so the bare path would
        otherwise fall through to the SPA's StaticFiles mount and 404 — and /mcp
        is exactly the URL a user pastes into their MCP client. 307 keeps the
        method and body intact for POSTed JSON-RPC.
        """
        return RedirectResponse(url=f"{MCP_PATH}/", status_code=307)

    @app.get("/graph.json")
    async def graph_json() -> Response:
        """Initial load / resync snapshot, tagged with the current seq ([8-C])."""
        payload = {
            "seq": core.events.seq,
            "metadata": core.metadata,
            "layers": core.layers,
            "focus": core.focus,
            "active_filter": core.active_filter,
            "nodes": [n.to_dict() for n in core.nodes.values()],
            "edges": [e.to_dict() for e in core.edges.values()],
            "findings": [f.to_dict() for f in core.findings.values()],
        }
        return Response(
            content=json.dumps(payload, ensure_ascii=False),
            media_type="application/json",
        )

    @app.websocket("/live")
    async def live(socket: WebSocket) -> None:
        origin = socket.headers.get("origin")
        if not validate_origin(origin, port):
            # [11]: CORS does not cover WS; a foreign page must not read the graph.
            await socket.close(code=4403)
            return
        host = socket.headers.get("host")
        if host is not None and host not in hosts:
            await socket.close(code=4403)
            return
        if auth_token is not None and not _ws_token_ok(socket, auth_token):
            # [11]: the HTTP `guard` middleware never runs for a WebSocket, so a
            # token-protected serve would otherwise stream the whole graph to any
            # remote client that opened /live on a non-loopback bind. Gate the
            # connection on the same token before accepting — this is purely a
            # connect-time check and does not touch the [8-C] resync/handshake.
            await socket.close(code=4401)
            return

        await socket.accept()
        conn = WebSocketConnection(socket)
        hub.register(conn)
        try:
            while True:
                raw = await socket.receive_text()
                try:
                    await hub.handle_client_event(conn, raw)
                except RateLimitExceeded:
                    await socket.send_text(
                        json.dumps({"op": "error", "data": {"reason": "rate_limited"}})
                    )
                except json.JSONDecodeError:
                    # [11]: a non-JSON text frame is bad input, not a reason to drop
                    # the connection — answer with an error and keep listening.
                    await socket.send_text(
                        json.dumps({"op": "error", "data": {"reason": "malformed"}})
                    )
                except ValidationError as exc:
                    await socket.send_text(
                        json.dumps(
                            {"op": "error", "data": {"reason": "invalid", "detail": str(exc)[:200]}}
                        )
                    )
        except WebSocketDisconnect:
            pass
        finally:
            hub.unregister(conn)

    app.mount(MCP_PATH, mcp_app)

    dist = frontend_dist()
    if serve_static and dist.is_dir():
        # Mounted last: / would otherwise shadow the routes above.
        app.mount("/", StaticFiles(directory=str(dist), html=True), name="spa")

    return app


async def _flush_loop(hub: WSHub, interval: float) -> None:
    """Drive the hub's [8-C] coalescing window.

    A raised exception here would silently stop every client from seeing updates,
    so each turn catches and continues — the same policy [5-E] sets for the
    auto-snapshot loop.
    """
    while True:
        await asyncio.sleep(interval)
        try:
            await hub.flush()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("WS flush failed; continuing")


def run(
    *,
    port: int = DEFAULT_PORT,
    bind: str = DEFAULT_BIND,
    auth_token: str | None = None,
    data_dir: Path | str | None = None,
) -> None:
    import uvicorn

    app = create_app(
        port=port, bind=bind, auth_token=auth_token, data_dir=data_dir, port_file=True
    )
    uvicorn.run(app, host=bind, port=port, log_level="info")


__all__ = [
    "DEFAULT_BIND",
    "DEFAULT_PORT",
    "MCP_PATH",
    "PORT_FILE_NAME",
    "create_app",
    "is_loopback",
    "port_file_path",
    "run",
]
