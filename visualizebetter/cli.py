"""Command line entry point ([14]: typer entry — serve, mcp-stdio, import).

mcp-stdio and import land with their own tasks ([8-D] proxy, [5-E] import).
"""

import asyncio
import json
import threading
import webbrowser
from pathlib import Path

import typer

from visualizebetter import __version__
from visualizebetter.graph.core import Graph
from visualizebetter.graph.snapshots import SnapshotStore
from visualizebetter.mcp_server import export_graph_to_dir, import_payload
from visualizebetter.server import DEFAULT_BIND, DEFAULT_PORT, is_loopback, run

app = typer.Typer(help="VisualizeBetter — MCP-native realtime graph workspace.")


@app.callback()
def _root() -> None:
    """Root callback — subcommands: serve, version, import, export, mcp-stdio ([13])."""


@app.command()
def version() -> None:
    """Print the visualizebetter version."""
    typer.echo(__version__)


@app.command()
def serve(
    port: int = typer.Option(DEFAULT_PORT, help="Port to listen on."),
    bind: str = typer.Option(DEFAULT_BIND, help="Address to bind. Default is loopback only."),
    auth_token: str = typer.Option(
        None,
        "--auth-token",
        help="Required when binding beyond loopback ([11]).",
    ),
    open_browser: bool = typer.Option(
        True, "--open/--no-open", help="Open the workspace in a browser once serving."
    ),
    data_dir: str = typer.Option(
        None, "--data-dir", help="Server data directory (default: platform data dir)."
    ),
) -> None:
    """Run the graph workspace: MCP endpoint + WebSocket + UI ([8-D])."""
    if not is_loopback(bind) and not auth_token:
        # [11]: a non-loopback bind without a token refuses to start. There is no
        # TLS here, so exposing the graph to the LAN unauthenticated is not a
        # default anyone should be able to reach by accident.
        raise typer.BadParameter(
            f"--auth-token is required when binding to {bind} (non-loopback). "
            "Without it the graph would be readable by anyone on the network.",
            param_hint="--auth-token",
        )

    url = f"http://{'localhost' if is_loopback(bind) else bind}:{port}/"
    typer.echo(f"visualizebetter serving on {url}  (MCP: {url}mcp)")
    if open_browser:
        threading.Timer(1.0, webbrowser.open, args=[url]).start()

    run(port=port, bind=bind, auth_token=auth_token, data_dir=data_dir)


@app.command(name="mcp-stdio")
def mcp_stdio(
    data_dir: str = typer.Option(
        None, "--data-dir", help="Server data directory (default: platform data dir)."
    ),
) -> None:
    """Bridge a stdio MCP client (e.g. Claude Desktop) to a local serve ([8-D]).

    A thin proxy: it owns no graph. It discovers a running `visualizebetter serve` (or
    auto-launches one) and relays MCP JSON-RPC between stdin/stdout and serve's MCP
    HTTP endpoint — so what the AI pushes reaches the same graph the browser sees.
    Register `visualizebetter mcp-stdio` as the command in your Claude Desktop config.
    """
    from visualizebetter.stdio_proxy import run_stdio_proxy

    run_stdio_proxy(data_dir=data_dir)


async def _latest_graph(store: SnapshotStore) -> Graph:
    """The most recent saved graph, or an empty one if nothing is stored yet."""
    rows = await store.list_snapshots()
    if not rows:
        return Graph(name="visualizebetter")
    return await store.load_snapshot(rows[0]["id"])  # list_snapshots is newest-first


@app.command(name="export")
def export_cmd(
    fmt: str = typer.Option("json", "--format", help="Export format (M1: json)."),
    filter: str = typer.Option(None, "--filter", help="[6] filter DSL — export a subgraph."),
    data_dir: str = typer.Option(None, "--data-dir", help="Server data directory."),
) -> None:
    """Export the latest saved graph to a JSON file in the data directory ([5-E])."""

    async def go() -> dict:
        store = SnapshotStore(data_dir) if data_dir else SnapshotStore()
        graph = await _latest_graph(store)
        node_ids = None
        if filter:
            from visualizebetter.filter import compile_filter

            node_ids = compile_filter(filter).evaluate_nodes(graph)
        return export_graph_to_dir(graph, store.data_dir, fmt, node_ids)

    try:
        result = asyncio.run(go())
    except Exception as exc:  # a clean CLI error, not a traceback
        raise typer.BadParameter(str(exc)) from None
    typer.echo(f"exported {result['size']} bytes -> {result['path']}")


@app.command(name="import")
def import_cmd(
    path: str = typer.Argument(..., help="JSON file to import."),
    fmt: str = typer.Option("json", "--format", help="Import format (M1: json)."),
    merge: bool = typer.Option(
        True, "--merge/--replace", help="Merge into (default) or replace the saved graph."
    ),
    data_dir: str = typer.Option(None, "--data-dir", help="Server data directory."),
) -> None:
    """Import a JSON graph file and save the result as a snapshot ([5-E]).

    Unlike the import_from_file MCP tool (which restricts untrusted callers to the
    data directory), the CLI is run by the local user and reads the given path
    directly. The import still goes through the WRITE validation, so a file cannot
    forge reserved '_' keys ([11]/[23-B]).
    """

    async def go() -> dict:
        if fmt != "json":
            raise ValueError(f"import format {fmt!r} is not supported — use 'json' ([5-E]).")
        store = SnapshotStore(data_dir) if data_dir else SnapshotStore()
        graph = await _latest_graph(store) if merge else Graph(name="visualizebetter")
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        result = import_payload(graph, payload, merge=True)
        await store.save_snapshot(graph, name=f"import-{Path(path).name}", description="CLI import ([5-E])")
        return result

    try:
        result = asyncio.run(go())
    except FileNotFoundError:
        raise typer.BadParameter(f"file not found: {path}") from None
    except Exception as exc:
        raise typer.BadParameter(str(exc)) from None
    typer.echo(f"imported {result['added_nodes']} nodes, {result['added_edges']} edges")


def main() -> None:
    app()


if __name__ == "__main__":
    main()
