"""Completion verification for TASK W — [5-A] WRITE tools.

This is the loop the README is built on: an AI draws what it finds, and the
browser sees it. Until these tools existed only findings and citations could be
recorded — there was no way to push a graph.
"""

import asyncio
import json

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

from visualizebetter.graph.core import Graph
from visualizebetter.graph.snapshots import AutoSnapshotter, SnapshotStore
from visualizebetter.mcp_server import MAX_BATCH_ITEMS, create_server
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
    tool = run(mcp.get_tool(tool_name))
    result = tool.fn(**kwargs)
    return run(result) if asyncio.iscoroutine(result) else result


class FakeConn:
    def __init__(self):
        self.sent: list[str] = []

    async def send(self, text: str) -> None:
        self.sent.append(text)

    @property
    def messages(self):
        return [json.loads(t) for t in self.sent]


# --- registration ---


def test_all_write_tools_are_exposed(mcp):
    async def go():
        async with Client(mcp) as c:
            return {t.name for t in await c.list_tools()}

    assert {
        "push_node",
        "push_edge",
        "push_batch",
        "update_node",
        "update_edge",
        "delete_node",
        "delete_edge",
        "clear_layer",
        "clear_all",
    } <= run(go())


def test_destructive_tools_carry_the_hint(mcp):
    """[5-A]: destructiveHint annotation 명시 (클라이언트 승인 UX 유도)."""

    async def go():
        async with Client(mcp) as c:
            return {t.name: t.annotations for t in await c.list_tools()}

    annotations = run(go())
    for name in ("delete_node", "delete_edge", "clear_layer", "clear_all"):
        assert annotations[name] is not None, name
        assert annotations[name].destructiveHint is True, name
    # push_node is additive; it carries no destructive annotation at all.
    assert annotations["push_node"] is None


# --- push_node / push_edge ([5-A]) ---


def test_push_node_draws_a_node(mcp, graph):
    result = call(mcp, "push_node", id="app.OrderService", label="OrderService", type="class")

    assert result["ok"] is True
    assert result["node"]["id"] == "app.OrderService"
    assert graph.get_node("app.OrderService").label == "OrderService"


def test_push_node_is_idempotent_and_merges_properties(mcp, graph):
    call(mcp, "push_node", id="a", label="A", type="class", properties={"ns": "MOD"})
    call(mcp, "push_node", id="a", label="A2", type="struct", properties={"size": 8})

    node = graph.get_node("a")
    assert len(graph.nodes) == 1
    assert node.label == "A2"
    assert node.properties == {"ns": "MOD", "size": 8}


def test_push_edge_draws_a_relation(mcp, graph):
    call(mcp, "push_node", id="a", label="A", type="class")
    call(mcp, "push_node", id="b", label="B", type="class")

    result = call(mcp, "push_edge", source="a", target="b", relation="field", key="m_user")

    assert result["ok"] is True
    assert result["edge"]["key"] == "m_user"
    assert graph.get_edge("a", "b", "field", "m_user") is not None


def test_push_edge_creates_placeholders_for_unknown_endpoints(mcp, graph):
    """[5-A]/[3-A]: AI 는 엣지를 노드보다 먼저 push 하는 경우가 반드시 생긴다."""
    call(mcp, "push_edge", source="ghost", target="other", relation="ref")

    placeholder = graph.get_node("ghost")
    assert placeholder.type == "unresolved"
    assert placeholder.properties == {"placeholder": True}


def test_a_later_push_node_resolves_the_placeholder(mcp, graph):
    call(mcp, "push_edge", source="a", target="late", relation="ref")

    call(mcp, "push_node", id="late", label="LateClass", type="class")

    node = graph.get_node("late")
    assert "placeholder" not in node.properties
    assert node.type == "class"


def test_push_edge_is_idempotent_on_the_4_tuple(mcp, graph):
    call(mcp, "push_edge", source="a", target="b", relation="field", weight=0.2)
    call(mcp, "push_edge", source="a", target="b", relation="field", weight=0.9)

    assert len(graph.edges) == 1
    assert graph.get_edge("a", "b", "field").weight == 0.9


# --- [23-B] 예약키 쓰기보호 (TASK 3 Q2=C 이관분) ---


def test_push_node_rejects_reserved_property_keys(mcp, graph):
    with pytest.raises(ToolError, match="reserved"):
        call(
            mcp,
            "push_node",
            id="a",
            label="A",
            type="class",
            properties={"_citations": [{"url": "evil", "title": "forged", "ts": "t"}]},
        )

    assert graph.get_node("a") is None, "the node is not created either"


def test_push_edge_rejects_reserved_property_keys(mcp):
    with pytest.raises(ToolError, match="reserved"):
        call(mcp, "push_edge", source="a", target="b", relation="r", properties={"_x": 1})


def test_update_node_rejects_reserved_property_keys(mcp, graph):
    call(mcp, "push_node", id="a", label="A", type="class")

    with pytest.raises(ToolError, match="reserved"):
        call(mcp, "update_node", id="a", patch={"set": {"properties": {"_citations": []}}})


def test_push_batch_rejects_reserved_property_keys(mcp):
    result = call(mcp, "push_batch", nodes=[{"id": "a", "label": "A", "type": "t", "properties": {"_x": 1}}])

    assert result["added_nodes"] == 0
    assert "reserved" in result["errors"][0]["error"]


def test_ordinary_properties_still_pass(mcp, graph):
    call(mcp, "push_node", id="a", label="A", type="class", properties={"placeholder": True, "ns": "x"})

    assert graph.get_node("a").properties == {"placeholder": True, "ns": "x"}


def test_cite_still_writes_the_reserved_array(mcp, graph):
    """The guard blocks callers, not the server's own evidence path ([5-F])."""
    call(mcp, "push_node", id="a", label="A", type="class")

    call(mcp, "cite", node_id="a", source_url="trace://0x1400", source_title="IDA")

    assert len(graph.get_node("a").properties["_citations"]) == 1


# --- push_batch ([5-A]) ---


def test_push_batch_applies_nodes_then_edges(mcp, graph):
    """[5-A] 트랜잭션 처리: nodes 먼저 → edges (배치 내 상호참조 허용)."""
    result = call(
        mcp,
        "push_batch",
        nodes=[
            {"id": "a", "label": "A", "type": "class"},
            {"id": "b", "label": "B", "type": "class"},
        ],
        edges=[{"source": "a", "target": "b", "relation": "field"}],
    )

    assert result == {"added_nodes": 2, "added_edges": 1, "errors": []}
    assert graph.get_node("a").type == "class", "not a placeholder — the node came first"


def test_push_batch_reports_per_item_errors(mcp, graph):
    result = call(
        mcp,
        "push_batch",
        nodes=[{"id": "ok", "label": "OK", "type": "t"}, {"id": "bad"}],
    )

    assert result["added_nodes"] == 1
    assert result["errors"][0]["index"] == 1
    assert graph.get_node("ok") is not None


def test_push_batch_accepts_the_limit(mcp, graph):
    nodes = [{"id": f"n{i}", "label": "N", "type": "t"} for i in range(MAX_BATCH_ITEMS)]

    result = call(mcp, "push_batch", nodes=nodes)

    assert result["added_nodes"] == MAX_BATCH_ITEMS


def test_push_batch_over_the_item_limit_points_at_import(mcp, graph):
    nodes = [{"id": f"n{i}", "label": "N", "type": "t"} for i in range(MAX_BATCH_ITEMS + 1)]

    with pytest.raises(ToolError, match="import_from_file"):
        call(mcp, "push_batch", nodes=nodes)

    assert graph.nodes == {}, "nothing is applied when the batch is refused"


def test_push_batch_counts_nodes_and_edges_together(mcp):
    half = MAX_BATCH_ITEMS // 2 + 1
    nodes = [{"id": f"n{i}", "label": "N", "type": "t"} for i in range(half)]
    edges = [{"source": "n0", "target": "n1", "relation": "r", "key": str(i)} for i in range(half)]

    with pytest.raises(ToolError, match="exceeds"):
        call(mcp, "push_batch", nodes=nodes, edges=edges)


def test_push_batch_over_the_payload_limit(mcp):
    # Korean is 3 bytes per char in UTF-8, so this clears 1MB on its own.
    heavy = [{"id": "n", "label": "L", "type": "t", "properties": {"blob": "글" * 400_000}}]

    with pytest.raises(ToolError, match="payload"):
        call(mcp, "push_batch", nodes=heavy)


def test_push_batch_empty(mcp):
    assert call(mcp, "push_batch") == {"added_nodes": 0, "added_edges": 0, "errors": []}


# --- update / delete ([5-A]) ---


def test_update_node_applies_a_patch(mcp, graph):
    call(mcp, "push_node", id="a", label="A", type="class", properties={"old": 1, "keep": 2})

    result = call(mcp, "update_node", id="a", patch={"set": {"label": "A2"}, "remove": ["old"]})

    assert result["node"]["label"] == "A2"
    assert graph.get_node("a").properties == {"keep": 2}


def test_update_edge_applies_a_patch(mcp, graph):
    call(mcp, "push_edge", source="a", target="b", relation="field")

    result = call(mcp, "update_edge", source="a", target="b", relation="field", key="", patch={"set": {"weight": 0.4}})

    assert result["edge"]["weight"] == 0.4


def test_delete_node_refuses_to_orphan_edges(mcp, graph):
    """[5-A]: dangling edge 는 불변식으로 금지 ([4])."""
    call(mcp, "push_edge", source="a", target="b", relation="field")

    result = call(mcp, "delete_node", id="a")

    assert result == {"ok": False, "error": "has_edges", "edge_count": 1}
    assert graph.get_node("a") is not None


def test_delete_node_cascade(mcp, graph):
    call(mcp, "push_edge", source="a", target="b", relation="field")

    assert call(mcp, "delete_node", id="a", cascade=True) == {"ok": True}
    assert graph.edges == {}


def test_delete_edge(mcp, graph):
    call(mcp, "push_edge", source="a", target="b", relation="field", key="k")

    assert call(mcp, "delete_edge", source="a", target="b", relation="field", key="k") == {"ok": True}


def test_delete_unknown_raises(mcp):
    with pytest.raises(ToolError):
        call(mcp, "delete_node", id="ghost")


# --- clear_layer / clear_all ([5-A]) ---


def test_clear_layer_removes_only_that_layer(mcp, graph):
    call(mcp, "push_node", id="a", label="A", type="class", layer="l1")
    call(mcp, "push_node", id="b", label="B", type="class", layer="l2")

    result = call(mcp, "clear_layer", layer="l1")

    assert result["ok"] is True
    assert graph.get_node("a") is None
    assert graph.get_node("b") is not None


def test_clear_layer_cascades_cross_layer_edges(mcp, graph):
    """[4] forbids dangling edges, so an edge losing an endpoint goes too."""
    call(mcp, "push_node", id="a", label="A", type="class", layer="l1")
    call(mcp, "push_node", id="b", label="B", type="class", layer="l2")
    call(mcp, "push_edge", source="a", target="b", relation="field", layer="l2")

    call(mcp, "clear_layer", layer="l1")

    assert graph.edges == {}, "the l2 edge cannot survive without node a"
    assert graph.get_node("b") is not None


def test_clear_layer_keeps_findings(mcp, graph):
    """★ clear wipes the graph, not the gold ([23-B])."""
    call(mcp, "push_node", id="a", label="A", type="class", layer="l1")
    recorded = call(mcp, "record_finding", title="결제 실패의 핵심 경로", node_ids=["a"], layer="l1")

    call(mcp, "clear_layer", layer="l1")

    finding = graph.get_finding(recorded["finding_id"])
    assert finding is not None
    assert finding.title == "결제 실패의 핵심 경로"
    assert finding.node_ids == [], "the anchor is gone, the finding is not"


def test_clear_all_wipes_the_graph_but_not_the_gold(mcp, graph):
    call(mcp, "push_edge", source="a", target="b", relation="field")
    recorded = call(mcp, "record_finding", title="gold", node_ids=["a"])

    result = call(mcp, "clear_all")

    assert result["ok"] is True
    assert graph.nodes == {}
    assert graph.edges == {}
    assert graph.get_finding(recorded["finding_id"]).title == "gold"


def test_clear_all_keeps_snapshots(mcp, graph):
    """[5-A]: 전체 삭제 (스냅샷은 유지)."""
    call(mcp, "push_node", id="a", label="A", type="class")
    call(mcp, "save_snapshot", name="v1")

    call(mcp, "clear_all")

    names = [r["name"] for r in call(mcp, "list_snapshots")["snapshots"]]
    assert "v1" in names


def test_clear_saves_a_recovery_point_first(mcp, graph):
    """[5-A] 안전장치: confirm 파라미터 대신 실행 직전 자동 스냅샷."""
    call(mcp, "push_node", id="doomed", label="Doomed", type="class")

    result = call(mcp, "clear_all")

    assert result["snapshot_id"]
    rows = {r["id"]: r for r in call(mcp, "list_snapshots")["snapshots"]}
    assert rows[result["snapshot_id"]]["kind"] == "auto"
    assert rows[result["snapshot_id"]]["name"].startswith("pre-clear_all-")


def test_the_pre_clear_snapshot_actually_recovers(mcp, graph):
    call(mcp, "push_node", id="precious", label="Precious", type="class")

    result = call(mcp, "clear_all")
    assert graph.nodes == {}

    call(mcp, "load_snapshot", snapshot_id=result["snapshot_id"])

    assert graph.get_node("precious") is not None, "a mistaken clear_all is undoable"


def test_clear_layer_recovery_point(mcp, graph):
    call(mcp, "push_node", id="a", label="A", type="class", layer="l1")

    result = call(mcp, "clear_layer", layer="l1")
    call(mcp, "load_snapshot", snapshot_id=result["snapshot_id"])

    assert graph.get_node("a") is not None


# --- ★ WS: the loop AI draws → human sees ([8-C]) ---


def test_pushes_reach_the_browser_as_one_graph_batch(mcp, graph):
    hub = WSHub(graph)
    hub.subscribe()
    conn = FakeConn()
    hub.register(conn)

    for i in range(5):
        call(mcp, "push_node", id=f"n{i}", label="N", type="class")
    call(mcp, "push_edge", source="n0", target="n1", relation="field")
    run(hub.flush())

    (message,) = conn.messages
    assert message["op"] == "graph.batch"
    assert len(message["data"]["nodes_added"]) == 5
    assert len(message["data"]["edges_added"]) == 1


def test_clear_broadcasts_one_clear_op_not_thousands_of_deletes(mcp, graph):
    """[8-C] forbids sending a wipe as N individual delete messages."""
    for i in range(50):
        call(mcp, "push_node", id=f"n{i}", label="N", type="class", layer="l1")
    hub = WSHub(graph)
    hub.subscribe()
    conn = FakeConn()
    hub.register(conn)

    call(mcp, "clear_layer", layer="l1")
    run(hub.flush())

    ops = [m["op"] for m in conn.messages]
    assert ops == ["clear"]
    assert conn.messages[0]["data"] == {"layer": "l1"}


def test_clear_all_broadcasts_clear_with_no_layer(mcp, graph):
    call(mcp, "push_node", id="a", label="A", type="class")
    hub = WSHub(graph)
    hub.subscribe()
    conn = FakeConn()
    hub.register(conn)

    call(mcp, "clear_all")
    run(hub.flush())

    assert conn.messages[0]["op"] == "clear"
    assert conn.messages[0]["data"] == {"layer": None}


def test_clear_tells_the_browser_about_orphaned_anchors(mcp, graph):
    call(mcp, "push_node", id="a", label="A", type="class", layer="l1")
    call(mcp, "record_finding", title="gold", node_ids=["a"])
    hub = WSHub(graph)
    hub.subscribe()
    conn = FakeConn()
    hub.register(conn)

    call(mcp, "clear_layer", layer="l1")
    run(hub.flush())

    ops = [m["op"] for m in conn.messages]
    assert ops == ["clear", "finding.update"]
    assert conn.messages[1]["data"]["patch"] == {"set": {"node_ids": []}}


def test_clear_sets_the_dirty_flag(mcp, graph):
    call(mcp, "push_node", id="a", label="A", type="class")
    graph.clear_dirty()

    call(mcp, "clear_all")

    assert graph.dirty is True


# --- [audit #16] ttl has a Pydantic lower bound, like apply_style/add_annotation ---


def test_push_node_rejects_negative_ttl(mcp):
    async def go():
        async with Client(mcp) as c:
            return await c.call_tool(
                "push_node", {"id": "x", "label": "X", "type": "class", "ttl": -1}
            )

    with pytest.raises(ToolError):
        run(go())


def test_push_edge_rejects_negative_ttl(mcp):
    async def go():
        async with Client(mcp) as c:
            return await c.call_tool(
                "push_edge", {"source": "a", "target": "b", "relation": "r", "ttl": -1}
            )

    with pytest.raises(ToolError):
        run(go())


def test_push_node_accepts_zero_and_positive_ttl(mcp):
    # The bound is ge=0, so 0 (permanent) and positive values still pass.
    call(mcp, "push_node", id="a", label="A", type="class", ttl=0)
    call(mcp, "push_node", id="b", label="B", type="class", ttl=60)

# --- [M2g #3] push_edge weight has a Pydantic lower bound; push_batch validates too ---


def test_push_edge_rejects_negative_weight(mcp):
    async def go():
        async with Client(mcp) as c:
            return await c.call_tool(
                "push_edge", {"source": "a", "target": "b", "relation": "r", "weight": -1.0}
            )

    with pytest.raises(ToolError):
        run(go())


def test_push_edge_accepts_zero_and_positive_weight(mcp):
    # ge=0: 0 and values above 1 are legal (weight is a magnitude used unbounded
    # by update_edge/filters — the floor only rejects nonsensical negatives).
    call(mcp, "push_edge", source="a", target="b", relation="r", weight=0.0)
    call(mcp, "push_edge", source="a", target="c", relation="r", weight=2.0)


def test_push_batch_rejects_a_negative_weight_edge(mcp, graph):
    result = call(
        mcp,
        "push_batch",
        nodes=[{"id": "a", "label": "A", "type": "t"}, {"id": "b", "label": "B", "type": "t"}],
        edges=[{"source": "a", "target": "b", "relation": "r", "weight": -3.0}],
    )
    assert result["added_edges"] == 0
    assert result["errors"] and result["errors"][0]["kind"] == "edge"
    assert graph.get_edge("a", "b", "r", "") is None  # not written


def test_push_batch_rejects_a_negative_ttl_node(mcp, graph):
    result = call(
        mcp,
        "push_batch",
        nodes=[{"id": "a", "label": "A", "type": "t", "ttl": -5}],
    )
    assert result["added_nodes"] == 0
    assert result["errors"] and result["errors"][0]["kind"] == "node"
    assert graph.get_node("a") is None


def test_push_batch_accepts_valid_weight_and_ttl(mcp, graph):
    result = call(
        mcp,
        "push_batch",
        nodes=[{"id": "a", "label": "A", "type": "t", "ttl": 60}, {"id": "b", "label": "B", "type": "t"}],
        edges=[{"source": "a", "target": "b", "relation": "r", "weight": 2.5}],
    )
    assert result["added_nodes"] == 2 and result["added_edges"] == 1 and not result["errors"]


# --- [23-C] RN4 AA — push_batch 의 항목별 errors[] 계약이 깨지지 않는다 ---


def test_push_batch_reports_bad_properties_per_item_without_dropping_the_rest(mcp, graph):
    """비-dict properties 항목은 그 항목만 errors[] 로 보고되고, 뒤 항목은
    정상 처리돼야 한다. 비문자열 키가 startswith 에서 AttributeError 를 내던
    시절엔 그 예외가 항목 루프를 탈출해 뒤 항목이 조용히 유실됐다."""
    result = call(
        mcp,
        "push_batch",
        nodes=[
            {"id": "good1", "label": "G1", "type": "class"},
            {"id": "bad_list", "label": "B", "type": "class",
             "properties": [["_citations", "FORGED"]]},
            {"id": "bad_key", "label": "B", "type": "class", "properties": {1: "x"}},
            {"id": "good2", "label": "G2", "type": "class"},
        ],
    )

    assert result["added_nodes"] == 2                 # ★ 뒤 항목이 살아있다
    assert "good1" in graph.nodes and "good2" in graph.nodes
    assert "bad_list" not in graph.nodes and "bad_key" not in graph.nodes
    indexes = {e["index"] for e in result["errors"]}
    assert indexes == {1, 2}                          # 실패한 항목만 보고된다
    assert all(e["kind"] == "node" for e in result["errors"])


def test_push_node_rejects_pair_list_properties(mcp):
    with pytest.raises(ToolError, match="must be an object"):
        call(mcp, "push_node", id="x", label="X", type="class",
             properties=[["_citations", "FORGED"]])


@pytest.mark.parametrize(
    "patch",
    [{"set": {1: "x"}}, {"remove": [1]}, {"set": ["label"]}, {"remove": "label"}],
)
def test_malformed_patch_is_a_tool_error_not_a_crash(mcp, graph, patch):
    """[23-C] RN5 JJ — MCP 층에서도 ValueError→ToolError 로 수렴한다. 이전에는
    AttributeError 가 그대로 새어나가거나(체이닝) 조용히 무동작이었다."""
    call(mcp, "push_node", id="a", label="A", type="class")
    before = graph.get_node("a").to_dict()
    with pytest.raises(ToolError):
        call(mcp, "update_node", id="a", patch=patch)
    assert graph.get_node("a").to_dict() == before
