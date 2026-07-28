"""Completion verification for TASK M2a — [8-D] mcp-stdio thin proxy.

The load-bearing property is ★no split-brain: a node pushed through mcp-stdio must
land in the one graph `serve` owns (the graph the browser sees), never a private
proxy graph. That is proved end-to-end here by driving the real `visualizebetter mcp-stdio`
subprocess with a real MCP stdio client and reading the node back from serve's
/graph.json. The rest covers discovery (live vs stale), auto-launch, and token
attachment.

These spin real subprocesses, so they are slower than the unit suite but are the
only honest way to test a process-model proxy.
"""

import asyncio
import json
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.types import Implementation

from visualizebetter.server import create_app, port_file_path
from visualizebetter.stdio_proxy import (
    discover_serve,
    ensure_serve,
    is_serve_healthy,
    read_port_file,
)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_healthy(url: str, token: str | None = None, timeout: float = 30.0) -> bool:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if httpx.get(f"{url}/graph.json", headers=headers, timeout=2.0).status_code == 200:
                return True
        except httpx.HTTPError:
            pass
        time.sleep(0.25)
    return False


def _kill(pid: int) -> None:
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)], capture_output=True)
        else:
            os.kill(pid, signal.SIGTERM)
    except (ProcessLookupError, OSError):
        pass


def _spawn_serve(data_dir: Path, port: int, token: str | None = None) -> subprocess.Popen:
    cmd = [sys.executable, "-m", "visualizebetter.cli", "serve", "--no-open",
           "--data-dir", str(data_dir), "--port", str(port)]
    if token:
        cmd += ["--auth-token", token]
    return subprocess.Popen(
        cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )


@pytest.fixture
def serve_proc(tmp_path):
    port = _free_port()
    data_dir = tmp_path / "data"
    proc = _spawn_serve(data_dir, port)
    url = f"http://127.0.0.1:{port}"
    assert _wait_healthy(url), "serve did not become healthy"
    yield {"data_dir": data_dir, "port": port, "url": url}
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()


# --- discovery ([8-D]) ---


def test_discovery_finds_a_running_serve(serve_proc):
    info = discover_serve(serve_proc["data_dir"])
    assert info is not None
    assert info["port"] == serve_proc["port"]
    assert info["mcp"] == f"{serve_proc['url']}/mcp/"
    assert info["token"] is None  # loopback serve carries no token


def test_serve_writes_and_removes_its_port_file(tmp_path):
    """[8-D] serve writes the discovery file while up and removes it on shutdown.

    Driven in-process through the ASGI lifespan (TestClient enter/exit) rather than
    a subprocess: a subprocess terminate() is a hard kill on Windows and would skip
    the lifespan's cleanup, so a removal assertion there would be a platform lie.
    """
    from fastapi.testclient import TestClient

    data_dir = tmp_path / "pf"
    data_dir.mkdir(parents=True)
    port = _free_port()
    app = create_app(port=port, data_dir=data_dir, serve_static=False, port_file=True)
    pf = port_file_path(data_dir)

    assert not pf.exists()  # nothing advertised before serving
    with TestClient(app, base_url="http://127.0.0.1"):
        assert pf.is_file()  # written on startup
        assert read_port_file(data_dir)["port"] == port
    # ★ shutdown removed it. Mutation guard: delete the lifespan's pf.unlink() and
    # this final assertion fails.
    assert not pf.exists()


def test_discovery_ignores_a_stale_port_file(tmp_path):
    data_dir = tmp_path / "stale"
    data_dir.mkdir(parents=True)
    # A port-file pointing at a port nobody is serving = a serve that died.
    dead = _free_port()
    port_file_path(data_dir).write_text(
        json.dumps({"port": dead, "url": f"http://127.0.0.1:{dead}",
                    "mcp": f"http://127.0.0.1:{dead}/mcp/", "token": None}),
        encoding="utf-8",
    )
    assert discover_serve(data_dir) is None  # health check fails → not "found"


# --- auto-launch ([8-D]) ---


def test_ensure_serve_auto_launches_when_none_is_running(tmp_path):
    data_dir = tmp_path / "auto"
    data_dir.mkdir(parents=True)
    assert discover_serve(data_dir) is None

    info = ensure_serve(data_dir, port=_free_port())
    try:
        assert is_serve_healthy(info)
        assert info["token"] is None
    finally:
        _kill(info["pid"])


# --- token ([11]) ---


def test_token_is_recorded_and_required(tmp_path):
    port = _free_port()
    data_dir = tmp_path / "tok"
    token = "s3cr3t"
    proc = _spawn_serve(data_dir, port, token=token)
    url = f"http://127.0.0.1:{port}"
    try:
        assert _wait_healthy(url, token=token), "token serve did not start"
        info = read_port_file(data_dir)
        assert info["token"] == token  # advertised for the proxy to attach
        # is_serve_healthy attaches the token; without it serve refuses (403).
        assert is_serve_healthy(info) is True
        assert is_serve_healthy({**info, "token": None}) is False
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


# --- ★ split-brain: a push through the proxy lands in serve's one graph ---


async def _push_through_proxy(data_dir: Path, node_id: str, client_name: str) -> object:
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "visualizebetter.cli", "mcp-stdio", "--data-dir", str(data_dir)],
        env=dict(os.environ),
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(
            read, write, client_info=Implementation(name=client_name, version="1.0")
        ) as session:
            await session.initialize()  # carries clientInfo through to serve ([23-E])
            return await session.call_tool(
                "push_node", {"id": node_id, "label": "Proxied", "type": "class"}
            )


def test_push_via_mcp_stdio_reaches_serve_not_a_split_brain(serve_proc):
    result = asyncio.run(
        _push_through_proxy(serve_proc["data_dir"], "PROXY.node", "claude-desktop-test")
    )
    assert not result.isError, result

    # ★ The node the AI pushed through mcp-stdio is in the graph serve owns — the
    # same graph the browser sees. A proxy with its own graph would leave this empty.
    snapshot = httpx.get(f"{serve_proc['url']}/graph.json", timeout=5.0).json()
    assert "PROXY.node" in {n["id"] for n in snapshot["nodes"]}


def test_stdio_client_does_not_start_a_second_serve(serve_proc):
    # The proxy must reuse the discovered serve, never launch a competitor: the
    # existing serve's pid is still the one owning the port after a proxied push.
    before = read_port_file(serve_proc["data_dir"])["pid"]
    asyncio.run(
        _push_through_proxy(serve_proc["data_dir"], "REUSE.node", "claude-desktop-test")
    )
    after = read_port_file(serve_proc["data_dir"])["pid"]
    assert before == after  # same serve process — no auto-launched competitor


# --- [M2g #4] token relay: _bridge attaches the Bearer, distinguishing set/unset ---


def test_auth_headers_attach_bearer_only_when_a_token_is_set():
    from visualizebetter.stdio_proxy import _auth_headers

    assert _auth_headers("s3cr3t") == {"Authorization": "Bearer s3cr3t"}
    assert _auth_headers(None) is None  # loopback serve → no header
    assert _auth_headers("") is None    # empty is treated as no token


def test_push_via_proxy_relays_the_bearer_token_to_a_token_serve(tmp_path):
    # ★ end to end: the proxy discovers the token from serve's port file and _bridge
    # attaches it to every relayed MCP request. serve's [11] guard rejects any
    # request without the Bearer (403), so a successful proxied push proves the
    # token was relayed — the not-attached case would fail the call.
    port = _free_port()
    data_dir = tmp_path / "toktok"
    token = "relay-me"
    proc = _spawn_serve(data_dir, port, token=token)
    url = f"http://127.0.0.1:{port}"
    try:
        assert _wait_healthy(url, token=token), "token serve did not start"
        result = asyncio.run(
            _push_through_proxy(data_dir, "TOKEN.node", "claude-desktop-test")
        )
        assert not result.isError, result
        snap = httpx.get(
            f"{url}/graph.json", headers={"Authorization": f"Bearer {token}"}, timeout=5.0
        ).json()
        assert "TOKEN.node" in {n["id"] for n in snap["nodes"]}
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


# --- [M2g #5] proxy error paths ---


def test_read_port_file_returns_none_on_malformed_json(tmp_path):
    data_dir = tmp_path / "bad"
    data_dir.mkdir(parents=True)
    port_file_path(data_dir).write_text("{ this is not json", encoding="utf-8")
    assert read_port_file(data_dir) is None


def test_read_port_file_returns_none_when_absent(tmp_path):
    assert read_port_file(tmp_path / "does-not-exist") is None


def test_ensure_serve_raises_when_an_auto_launch_never_becomes_healthy(tmp_path, monkeypatch):
    import visualizebetter.stdio_proxy as sp

    monkeypatch.setattr(sp, "discover_serve", lambda _dd: None)  # never healthy
    launched: list[bool] = []
    monkeypatch.setattr(sp, "spawn_serve", lambda _dd, port=None: launched.append(True))
    with pytest.raises(RuntimeError, match="could not discover or auto-launch"):
        sp.ensure_serve(tmp_path, timeout_s=0.3)
    assert launched  # it attempted an auto-launch before giving up ([8-D])
