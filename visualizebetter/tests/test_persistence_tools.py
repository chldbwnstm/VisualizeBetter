"""Completion verification for TASK P — [5-E] tools + [23-D] handoff.

The point: an AI can save its session's gold and a later session can load it back.
Until these tools existed the only path to SQLite was the 300s auto-snapshot, so
the [23-D] handoff had no mechanism.
"""

import asyncio

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

from visualizebetter.graph.core import Graph
from visualizebetter.graph.snapshots import AutoSnapshotter, SnapshotStore
from visualizebetter.mcp_server import create_server
from visualizebetter.ws.hub import WSHub


@pytest.fixture
def graph():
    return Graph(name="test")


@pytest.fixture
def store(tmp_path):
    return SnapshotStore(tmp_path / "data")


@pytest.fixture
def snapshotter(graph, store):
    return AutoSnapshotter(graph, store)


@pytest.fixture
def mcp(graph, store, snapshotter):
    return create_server(graph, store=store, snapshotter=snapshotter)


def run(coro):
    return asyncio.run(coro)


def call(mcp, tool_name, /, **kwargs):
    """Positional-only so a tool's own `name` argument cannot collide."""
    tool = run(mcp.get_tool(tool_name))
    result = tool.fn(**kwargs)
    return run(result) if asyncio.iscoroutine(result) else result


# --- registration ---


def tool_names(server) -> set[str]:
    async def go():
        async with Client(server) as c:
            return {t.name for t in await c.list_tools()}

    return run(go())


def test_persistence_tools_are_exposed(mcp):
    """[23-D] needs all three: list to find, load to resume, save to hand off."""
    assert {"save_snapshot", "list_snapshots", "load_snapshot"} <= tool_names(mcp)


def test_without_a_store_the_persistence_tools_are_absent(graph):
    """No store, nowhere to persist — the tools do not pretend otherwise."""
    names = tool_names(create_server(graph))

    assert "record_finding" in names
    assert {"save_snapshot", "list_snapshots", "load_snapshot"}.isdisjoint(names)


# --- save_snapshot ([5-E]) ---


def test_save_snapshot_returns_id_and_size(mcp, graph):
    graph.add_node(id="a", label="A", type="class")

    result = call(mcp, "save_snapshot", name="프로젝트-구조-v1", description="첫 저장")

    assert result["ok"] is True
    assert result["snapshot_id"]
    assert result["size"] > 0


def test_save_snapshot_clears_dirty(mcp, graph):
    """[23-C] dirty flag는 스냅샷 저장 시 clear."""
    graph.add_node(id="a", label="A", type="class")
    assert graph.dirty is True

    call(mcp, "save_snapshot", name="v1")

    assert graph.dirty is False


def test_save_snapshot_is_manual_kind(mcp, graph):
    call(mcp, "save_snapshot", name="v1")

    (row,) = call(mcp, "list_snapshots")["snapshots"]
    assert row["kind"] == "manual"


def test_a_hostile_name_is_stored_as_data_only(mcp, graph, store, tmp_path):
    """[5-E]/[11]: name is a DB key, never a path."""
    call(mcp, "save_snapshot", name="../../etc/passwd")

    (row,) = call(mcp, "list_snapshots")["snapshots"]
    assert row["name"] == "../../etc/passwd"
    written = {p for p in tmp_path.rglob("*") if p.is_file()}
    assert written <= {store.db_path}


# --- list_snapshots ([5-E]) ---


def test_list_snapshots_reports_spec_fields(mcp, graph):
    graph.add_node(id="a", label="A", type="class")
    graph.add_edge(source="a", target="b", relation="field")
    call(mcp, "save_snapshot", name="v1", description="설명")

    (row,) = call(mcp, "list_snapshots")["snapshots"]

    assert set(row) == {
        "id",
        "name",
        "description",
        "created_at",
        "node_count",
        "edge_count",
        "kind",
    }
    assert row["node_count"] == 2
    assert row["edge_count"] == 1


def test_list_snapshots_distinguishes_manual_and_auto(mcp, graph, snapshotter):
    call(mcp, "save_snapshot", name="사람이-저장")
    graph.add_node(id="a", label="A", type="class")
    run(snapshotter.snapshot_if_dirty())

    kinds = {r["name"]: r["kind"] for r in call(mcp, "list_snapshots")["snapshots"]}

    assert kinds["사람이-저장"] == "manual"
    assert any(k == "auto" for k in kinds.values())


def test_list_snapshots_is_empty_initially(mcp):
    assert call(mcp, "list_snapshots") == {"snapshots": []}


def test_list_snapshots_truncates_an_oversized_response(mcp, graph):
    """[5] 공통 규칙 — 50KB 초과 시 절단 + truncated + total."""
    heavy = "굵" * 1000
    for i in range(60):
        call(mcp, "save_snapshot", name=f"{heavy}-{i}")

    result = call(mcp, "list_snapshots")

    assert result["truncated"] is True
    assert result["total"] == 60
    assert len(result["snapshots"]) < 60


# --- ★ [23-D] handoff: save → change → load restores ---


def test_load_snapshot_restores_the_session(mcp, graph):
    """세션 A save → 세션 B load — the handoff this task exists for."""
    graph.add_node(id="app.OrderService", label="OrderService", type="class")
    graph.cite("app.OrderService", "trace://0x1400", "IDA")
    recorded = call(mcp, "record_finding", title="결제 실패의 핵심 경로", confidence=0.95)
    saved = call(mcp, "save_snapshot", name="프로젝트-구조-v1")

    # The session moves on and loses the thread.
    graph.delete_finding(recorded["finding_id"])
    graph.add_node(id="junk", label="Junk", type="noise")
    assert len(graph.findings) == 0

    result = call(mcp, "load_snapshot", snapshot_id=saved["snapshot_id"])

    assert result["ok"] is True
    assert result["findings"] == 1
    assert graph.get_finding(recorded["finding_id"]).title == "결제 실패의 핵심 경로"
    assert graph.get_node("junk") is None
    assert graph.get_node("app.OrderService").properties["_citations"][0]["url"] == "trace://0x1400"


def test_loaded_findings_are_queryable(mcp, graph):
    """[23-D] step 4: list_findings after the load reads the previous gold."""
    graph.add_node(id="a", label="A", type="class")
    call(mcp, "record_finding", title="gold", node_ids=["a"], confidence=0.9)
    saved = call(mcp, "save_snapshot", name="v1")
    graph.findings.clear()

    call(mcp, "load_snapshot", snapshot_id=saved["snapshot_id"])
    listed = call(mcp, "list_findings", min_confidence=0.5)

    assert [f["title"] for f in listed["findings"]] == ["gold"]


def test_load_snapshot_rebuilds_indices(mcp, graph):
    graph.add_node(id="a", label="A", type="class")
    graph.add_node(id="b", label="B", type="class")
    graph.add_edge(source="a", target="b", relation="field")
    saved = call(mcp, "save_snapshot", name="v1")
    graph.reload_from(Graph())

    call(mcp, "load_snapshot", snapshot_id=saved["snapshot_id"])

    assert graph.indices.nodes_of_type("class") == {"a", "b"}
    assert graph.delete_node("a") == {"ok": False, "error": "has_edges", "edge_count": 1}


def test_load_snapshot_saves_the_current_state_first(mcp, graph):
    """[23-C] 파괴적 작업 직전 훅 — the pre-load state stays recoverable."""
    graph.add_node(id="doomed", label="Doomed", type="class")
    saved = call(mcp, "save_snapshot", name="v1")
    graph.add_node(id="about-to-be-lost", label="X", type="class")

    result = call(mcp, "load_snapshot", snapshot_id=saved["snapshot_id"])

    assert graph.get_node("about-to-be-lost") is None
    assert result["pre_load_snapshot_id"]
    rows = {r["id"]: r for r in call(mcp, "list_snapshots")["snapshots"]}
    assert rows[result["pre_load_snapshot_id"]]["kind"] == "auto"
    assert rows[result["pre_load_snapshot_id"]]["name"].startswith("pre-load_snapshot-")


def test_the_pre_load_snapshot_actually_recovers(mcp, graph, store):
    graph.add_node(id="keep", label="Keep", type="class")
    saved = call(mcp, "save_snapshot", name="v1")
    graph.add_node(id="lost", label="Lost", type="class")

    result = call(mcp, "load_snapshot", snapshot_id=saved["snapshot_id"])
    call(mcp, "load_snapshot", snapshot_id=result["pre_load_snapshot_id"])

    assert graph.get_node("lost") is not None, "the mistaken load is undoable"


def test_load_snapshot_unknown_id(mcp):
    with pytest.raises(ToolError):
        call(mcp, "load_snapshot", snapshot_id="does-not-exist")


# --- reload_from ([8-D]): subscribers survive the swap ---


def test_reload_from_keeps_event_bus_subscribers(graph):
    """[8-D]: the hub subscribed to *this* Graph — a reload must not orphan it."""
    captured = []
    graph.events.subscribe(captured.append)

    other = Graph()
    other.add_node(id="fresh", label="Fresh", type="class")
    graph.reload_from(other)
    captured.clear()

    graph.add_node(id="after", label="After", type="class")

    assert [e.op for e in captured] == ["node.add"]


def test_reload_from_keeps_seq_monotonic(graph):
    """[8-C]: a client that saw seq N must never be sent a lower one."""
    graph.add_node(id="a", label="A", type="class")
    graph.add_node(id="b", label="B", type="class")
    before = graph.events.seq

    graph.reload_from(Graph())
    graph.add_node(id="c", label="C", type="class")

    assert graph.events.seq == before + 1


def test_reload_from_replaces_contents(graph):
    graph.add_node(id="old", label="Old", type="class")
    graph.add_finding(title="old gold")

    other = Graph(name="other")
    other.add_node(id="new", label="New", type="class")
    graph.reload_from(other)

    assert set(graph.nodes) == {"new"}
    assert graph.findings == {}
    assert graph.metadata["name"] == "other"


def test_reload_from_is_not_dirty(graph):
    """The incoming state came from disk; re-saving it would only duplicate it."""
    graph.add_node(id="a", label="A", type="class")

    graph.reload_from(Graph())

    assert graph.dirty is False


def test_load_snapshot_reaches_a_subscribed_hub(mcp, graph):
    """★ WSHub keeps working across the swap and sees snapshot.load ([8-C])."""
    hub = WSHub(graph)
    hub.subscribe()
    graph.add_node(id="a", label="A", type="class")
    saved = call(mcp, "save_snapshot", name="v1")

    class FakeConn:
        def __init__(self):
            self.sent = []

        async def send(self, text: str) -> None:
            self.sent.append(text)

    conn = FakeConn()
    hub.register(conn)
    call(mcp, "load_snapshot", snapshot_id=saved["snapshot_id"])
    run(hub.flush())

    import json

    ops = [json.loads(t)["op"] for t in conn.sent]
    assert "snapshot.load" in ops
    payload = next(json.loads(t) for t in conn.sent if json.loads(t)["op"] == "snapshot.load")
    assert payload["data"] == {"snapshot_id": saved["snapshot_id"]}


def test_hub_still_receives_events_after_a_load(mcp, graph):
    hub = WSHub(graph)
    hub.subscribe()
    saved = call(mcp, "save_snapshot", name="v1")

    class FakeConn:
        def __init__(self):
            self.sent = []

        async def send(self, text: str) -> None:
            self.sent.append(text)

    conn = FakeConn()
    hub.register(conn)
    call(mcp, "load_snapshot", snapshot_id=saved["snapshot_id"])
    run(hub.flush())
    conn.sent.clear()

    graph.add_finding(title="post-load gold")
    run(hub.flush())

    import json

    assert [json.loads(t)["op"] for t in conn.sent] == ["finding.add"]
