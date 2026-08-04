"""Completion verification for TASK T — [5-B] READ tools + filter DSL wiring.

Covers each tool's happy path, the [6] filter/edge_filter wiring (valid filter →
result, bad filter → ToolError, group func in an edge filter → ToolError), the
list boundaries, get_graph_summary accuracy, find_paths correctness, reserved-key
exposure, and — the README's core promise — a fresh Client session reading a
prior session's graph back through get_graph_summary → list_nodes → get_neighbors.

READ must never mutate: a dirty-flag / event assertion guards that.
"""

import asyncio

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

from visualizebetter.graph.core import Graph
from visualizebetter.mcp_server import MAX_LIST_LIMIT, create_server


@pytest.fixture
def graph():
    g = Graph(name="test")
    # A small, known shape: Order → View → (User ↔ Auth), plus an island.
    g.add_node(id="app.Order", label="OrderContext", type="class",
               properties={"field_count": 12})
    # 예약키는 cite() 로만 심는다 — core 가 생성 시 직접 주입을 거부한다([23-B], RN3).
    g.cite("app.Order", "trace://0x1", "t")
    g.add_node(id="UI.View", label="MainView", type="component", properties={"field_count": 3})
    g.add_node(id="Svc.User", label="PaymentService", type="service")
    g.add_node(id="Svc.Auth", label="AuthService", type="service")
    g.add_node(id="Island", label="Lonely", type="module")
    g.add_edge(source="app.Order", target="UI.View", relation="owns", weight=2.0)
    g.add_edge(source="UI.View", target="Svc.User", relation="uses")
    g.add_edge(source="Svc.User", target="Svc.Auth", relation="calls")
    return g


@pytest.fixture
def mcp(graph):
    return create_server(graph)


def call(mcp, name, **kwargs):
    tool = asyncio.run(mcp.get_tool(name))
    result = tool.fn(**kwargs)
    return asyncio.run(result) if asyncio.iscoroutine(result) else result


def client_call(mcp, name, args):
    async def go():
        async with Client(mcp) as c:
            return await c.call_tool(name, args)

    return asyncio.run(go())


# --- get_graph_summary ([5-B], MVP) ---


def test_summary_counts(mcp):
    s = call(mcp, "get_graph_summary")
    assert s["nodes"] == 5
    assert s["edges"] == 3


def test_summary_type_distribution(mcp):
    s = call(mcp, "get_graph_summary")
    counts = {row["type"]: row["count"] for row in s["types"]}
    assert counts == {"class": 1, "component": 1, "service": 2, "module": 1}


def test_summary_top_hubs_by_degree(mcp):
    s = call(mcp, "get_graph_summary")
    # UI.View and Svc.User each have degree 2; they lead.
    top = {h["id"]: h["degree"] for h in s["top_hubs"]}
    assert top["UI.View"] == 2
    assert top["Svc.User"] == 2
    assert top["Island"] == 0


# --- [13-B] CH2(6d) — 요약의 결정성은 정렬 계약이다 ---
#
# 이전 판은 `call(...) == call(...)` — 같은 프로세스에서 같은 함수를 연속 두 번
# 부르고 서로 비교했다. dict/set 의 반복 순서는 한 프로세스 안에서 안정적이므로 그
# 단언은 원리적으로 실패할 수 없었고, 실제로 tie-break 를 둘 다 지워도 통과했다.
# 결정성이 깨지는 진짜 모습은 "같은 그래프인데 만들어진 순서가 다르면 요약이
# 다르다" 이고, 그것은 (1) 동률이 있는 픽스처에서 정렬 계약을 직접 단언하거나
# (2) 삽입 순서를 뒤바꾼 그래프와 비교해야만 보인다. 아래 셋이 그 둘이다.
#
# 이 요약은 인수인계 때 AI 가 **가장 먼저** 부르는 도구다([5-B] MVP 3대). 순서가
# 흔들리면 두 세션이 같은 그래프를 두고 서로 다른 "주요 허브"를 읽는다.


def _built_graph(order: str) -> Graph:
    """상단 `graph` 픽스처와 같은 그래프를, 지정한 삽입 순서로."""
    nodes = [
        dict(id="app.Order", label="OrderContext", type="class", properties={"field_count": 12}),
        dict(id="UI.View", label="MainView", type="component", properties={"field_count": 3}),
        dict(id="Svc.User", label="PaymentService", type="service"),
        dict(id="Svc.Auth", label="AuthService", type="service"),
        dict(id="Island", label="Lonely", type="module"),
    ]
    edges = [
        dict(source="app.Order", target="UI.View", relation="owns", weight=2.0),
        dict(source="UI.View", target="Svc.User", relation="uses"),
        dict(source="Svc.User", target="Svc.Auth", relation="calls"),
    ]
    if order == "reversed":
        nodes, edges = list(reversed(nodes)), list(reversed(edges))
    g = Graph(name="test")
    for spec in nodes:
        g.add_node(**spec)
    for spec in edges:
        g.add_edge(**spec)
    return g


# ★ 두 순서 **모두** 로 건다. 한쪽만으로는 이빨이 없을 수 있다: forward 순서에서는
# type 의 삽입 순서(class, component, service, module)가 우연히 알파벳 tie-break 와
# 같은 결과를 내서, tie-break 를 지운 구현도 기대 리스트를 그대로 만족한다(실측 —
# 그 뮤턴트는 아래 두 테스트 중 insertion-order 만 죽였다). 뒤집은 순서에서는
# 갈라진다. 계약이 순서와 무관하다는 주장이니 두 순서로 묻는 게 맞기도 하다.
@pytest.mark.parametrize("order", ["forward", "reversed"])
def test_summary_sorts_types_by_count_then_name(order):
    """[5-B] 계약: (count desc, type asc). count==1 인 type 이 셋 있어 tie-break 가
    없으면 그 셋의 순서가 곧 삽입 순서가 된다."""
    s = call(create_server(_built_graph(order)), "get_graph_summary")

    assert [row["count"] for row in s["types"]].count(1) == 3, "동률이 없으면 이 테스트는 아무것도 안 본다"
    assert [row["type"] for row in s["types"]] == ["service", "class", "component", "module"]


@pytest.mark.parametrize("order", ["forward", "reversed"])
def test_summary_sorts_top_hubs_by_degree_then_id(order):
    """[5-B] 계약: (degree desc, id asc). 동률이 두 쌍(2/2, 1/1) 있다."""
    s = call(create_server(_built_graph(order)), "get_graph_summary")

    assert [h["degree"] for h in s["top_hubs"]] == [2, 2, 1, 1, 0], "동률 쌍이 사라졌다"
    assert [h["id"] for h in s["top_hubs"]] == [
        "Svc.User", "UI.View", "Svc.Auth", "app.Order", "Island",
    ]


def test_summary_does_not_depend_on_insertion_order():
    """같은 그래프를 반대 순서로 만들면 같은 요약이 나와야 한다 — 프로세스 안에서
    두 번 부르는 것으로는 절대 볼 수 없는 축."""
    forward = call(create_server(_built_graph("forward")), "get_graph_summary")
    backward = call(create_server(_built_graph("reversed")), "get_graph_summary")

    assert forward == backward


# --- get_node ([5-B]) ---


def test_get_node_returns_whole_node(mcp):
    r = call(mcp, "get_node", id="app.Order")
    assert r["node"]["label"] == "OrderContext"
    assert r["node"]["properties"]["field_count"] == 12


def test_get_node_exposes_reserved_keys(mcp):
    # [23-B] reserved keys are shown as-is on read (evidence/history), not hidden.
    r = call(mcp, "get_node", id="app.Order")
    assert r["node"]["properties"]["_citations"][0]["url"] == "trace://0x1"


def test_get_node_missing_is_tool_error(mcp):
    with pytest.raises(ToolError, match="not found"):
        call(mcp, "get_node", id="nope")


def test_get_node_neighbors(mcp):
    r = call(mcp, "get_node", id="UI.View", include_neighbors=True)
    ids = {n["id"] for n in r["neighbors"]}
    assert ids == {"app.Order", "Svc.User"}


def test_get_node_without_neighbors_omits_them(mcp):
    r = call(mcp, "get_node", id="UI.View")
    assert "neighbors" not in r


# --- list_nodes + filter DSL ([5-B], [6]) ---


def test_list_nodes_all(mcp):
    r = call(mcp, "list_nodes")
    assert r["total"] == 5
    assert len(r["nodes"]) == 5


def test_list_nodes_with_filter(mcp):
    r = call(mcp, "list_nodes", filter='type == "service"')
    ids = {n["id"] for n in r["nodes"]}
    assert ids == {"Svc.User", "Svc.Auth"}
    assert r["total"] == 2


def test_list_nodes_group_func_filter(mcp):
    r = call(mcp, "list_nodes", filter="degree(node) > 1")
    ids = {n["id"] for n in r["nodes"]}
    assert ids == {"UI.View", "Svc.User"}


def test_list_nodes_bad_filter_is_tool_error(mcp):
    with pytest.raises(ToolError, match="invalid filter"):
        call(mcp, "list_nodes", filter="type === bogus")


def test_list_nodes_filter_limit_breach_is_tool_error(mcp):
    # within over the [6] cap of 5 must be refused at the tool boundary.
    with pytest.raises(ToolError, match="invalid filter"):
        call(mcp, "list_nodes", filter='connected_to("app.Order", within=99)')


def test_list_nodes_pagination_is_deterministic(mcp):
    first = call(mcp, "list_nodes", limit=2, offset=0, sort_by="id", order="asc")
    second = call(mcp, "list_nodes", limit=2, offset=2, sort_by="id", order="asc")
    # Ascending id order is bytewise, so the lowercase-prefixed id sorts last.
    assert [n["id"] for n in first["nodes"]] == ["Island", "Svc.Auth"]
    assert [n["id"] for n in second["nodes"]] == ["Svc.User", "UI.View"]
    assert first["total"] == 5


def test_list_nodes_rejects_oversize_limit(mcp):
    with pytest.raises(ToolError):
        client_call(mcp, "list_nodes", {"limit": MAX_LIST_LIMIT + 1})


def test_list_nodes_rejects_negative_offset(mcp):
    with pytest.raises(ToolError):
        client_call(mcp, "list_nodes", {"offset": -1})


# --- list_edges + edge filter ([5-B], [6]) ---


def test_list_edges_all(mcp):
    r = call(mcp, "list_edges")
    assert r["total"] == 3


def test_list_edges_with_filter(mcp):
    r = call(mcp, "list_edges", filter='relation == "owns"')
    assert r["total"] == 1
    assert r["edges"][0]["source"] == "app.Order"


def test_list_edges_weight_filter(mcp):
    r = call(mcp, "list_edges", filter="weight > 1")
    assert {e["source"] for e in r["edges"]} == {"app.Order"}


def test_list_edges_group_func_is_tool_error(mcp):
    # A node group function has no meaning over edges — rejected, not silently 0.
    with pytest.raises(ToolError, match="not available in an edge filter"):
        call(mcp, "list_edges", filter="degree(node) > 1")


# --- get_neighbors ([5-B], undirected Q1) ---


def test_get_neighbors_one_hop(mcp):
    r = call(mcp, "get_neighbors", id="UI.View", depth=1)
    assert {n["id"] for n in r["neighbors"]} == {"app.Order", "Svc.User"}
    assert r["center"]["id"] == "UI.View"
    assert r["truncated"] is False


def test_get_neighbors_is_undirected(mcp):
    # Svc.Auth only has the incoming calls edge; undirected still reaches Svc.User.
    r = call(mcp, "get_neighbors", id="Svc.Auth", depth=1)
    assert {n["id"] for n in r["neighbors"]} == {"Svc.User"}


def test_get_neighbors_depth_2(mcp):
    r = call(mcp, "get_neighbors", id="app.Order", depth=2)
    assert {n["id"] for n in r["neighbors"]} == {"UI.View", "Svc.User"}


def test_get_neighbors_max_nodes_truncates(mcp):
    r = call(mcp, "get_neighbors", id="app.Order", depth=3, max_nodes=1)
    assert r["truncated"] is True
    assert len(r["neighbors"]) <= 1


def test_get_neighbors_edge_filter(mcp):
    # only follow "owns" edges: from Order that reaches UI.View and stops.
    r = call(mcp, "get_neighbors", id="app.Order", depth=3, edge_filter='relation == "owns"')
    assert {n["id"] for n in r["neighbors"]} == {"UI.View"}


def test_get_neighbors_depth_cap_rejected(mcp):
    with pytest.raises(ToolError):
        client_call(mcp, "get_neighbors", {"id": "app.Order", "depth": 4})


def test_get_neighbors_missing_node(mcp):
    with pytest.raises(ToolError, match="not found"):
        call(mcp, "get_neighbors", id="nope")


# --- get_neighbors direction ([5-B], [4] source/target) ---


def test_direction_in_out_both_differ(mcp):
    # UI.View has an in-edge (app.Order→UI.View) and an out-edge (UI.View→Svc.User).
    out = {n["id"] for n in call(mcp, "get_neighbors", id="UI.View", direction="out")["neighbors"]}
    inc = {n["id"] for n in call(mcp, "get_neighbors", id="UI.View", direction="in")["neighbors"]}
    both = {n["id"] for n in call(mcp, "get_neighbors", id="UI.View", direction="both")["neighbors"]}
    assert out == {"Svc.User"}
    assert inc == {"app.Order"}
    assert both == {"app.Order", "Svc.User"}
    assert out != inc and out != both and inc != both


def test_direction_default_is_both(mcp):
    # Regression: the default must keep TASK T's undirected behaviour.
    default = call(mcp, "get_neighbors", id="UI.View")
    both = call(mcp, "get_neighbors", id="UI.View", direction="both")
    assert default["neighbors"] == both["neighbors"]


def test_direction_follows_each_hop(mcp):
    # out from app.Order, depth 2: Order→View→User, all following out-edges.
    r = call(mcp, "get_neighbors", id="app.Order", depth=2, direction="out")
    assert {n["id"] for n in r["neighbors"]} == {"UI.View", "Svc.User"}
    # in from app.Order: nothing points into Order.
    r = call(mcp, "get_neighbors", id="app.Order", depth=2, direction="in")
    assert r["neighbors"] == []


def test_undirected_edge_counts_for_both_in_and_out():
    # ★ An edge with directed=False has no direction, so a directional query must
    # still follow it ([4] Edge.directed).
    g = Graph(name="u")
    g.add_node(id="a", label="a", type="x")
    g.add_node(id="b", label="b", type="x")
    g.add_edge(source="a", target="b", relation="peer", directed=False)
    m = create_server(g)

    for direction in ("in", "out", "both"):
        r = call(m, "get_neighbors", id="a", direction=direction)
        assert {n["id"] for n in r["neighbors"]} == {"b"}, direction
        # and from b's side too — undirected is symmetric.
        r = call(m, "get_neighbors", id="b", direction=direction)
        assert {n["id"] for n in r["neighbors"]} == {"a"}, direction


def test_directed_edge_excluded_from_wrong_direction():
    # The complement of the undirected case: a directed a→b is out for a, in for b,
    # and absent from the opposite mode.
    g = Graph(name="d")
    g.add_node(id="a", label="a", type="x")
    g.add_node(id="b", label="b", type="x")
    g.add_edge(source="a", target="b", relation="calls")  # directed by default
    m = create_server(g)

    assert {n["id"] for n in call(m, "get_neighbors", id="a", direction="out")["neighbors"]} == {"b"}
    assert call(m, "get_neighbors", id="a", direction="in")["neighbors"] == []
    assert {n["id"] for n in call(m, "get_neighbors", id="b", direction="in")["neighbors"]} == {"a"}
    assert call(m, "get_neighbors", id="b", direction="out")["neighbors"] == []


def test_direction_combines_with_edge_filter(mcp):
    # direction and edge_filter stack: out-only AND only "uses" edges.
    r = call(mcp, "get_neighbors", id="UI.View", direction="out", edge_filter='relation == "uses"')
    assert {n["id"] for n in r["neighbors"]} == {"Svc.User"}
    # out-only but restricted to "owns" (which enters UI.View, not leaves) → none.
    r = call(mcp, "get_neighbors", id="UI.View", direction="out", edge_filter='relation == "owns"')
    assert r["neighbors"] == []


def test_direction_rejects_unknown_value(mcp):
    with pytest.raises(ToolError):
        client_call(mcp, "get_neighbors", {"id": "UI.View", "direction": "sideways"})


# --- find_paths ([5-B]; path_to's real-path counterpart) ---


def test_find_paths_finds_the_chain(mcp):
    r = call(mcp, "find_paths", source="app.Order", target="Svc.Auth")
    assert ["app.Order", "UI.View", "Svc.User", "Svc.Auth"] in r["paths"]


def test_find_paths_shortest_first(mcp):
    r = call(mcp, "find_paths", source="app.Order", target="Svc.User")
    assert r["paths"][0] == ["app.Order", "UI.View", "Svc.User"]


def test_find_paths_none_to_island(mcp):
    r = call(mcp, "find_paths", source="app.Order", target="Island")
    assert r["paths"] == []


def test_find_paths_max_length_bounds(mcp):
    # The only path is length 3; a max_length of 2 finds nothing.
    r = call(mcp, "find_paths", source="app.Order", target="Svc.Auth", max_length=2)
    assert r["paths"] == []


@pytest.mark.parametrize("length", [9, 10])
def test_find_paths_max_length_up_to_10_is_allowed(mcp, length):
    # [5-B] cap is 10; 9 and 10 must pass schema validation.
    client_call(mcp, "find_paths", {"source": "app.Order", "target": "Svc.Auth", "max_length": length})


def test_find_paths_length_over_cap_rejected(mcp):
    with pytest.raises(ToolError):
        client_call(mcp, "find_paths", {"source": "app.Order", "target": "Svc.Auth", "max_length": 11})


def test_find_paths_edge_filter(mcp):
    r = call(mcp, "find_paths", source="app.Order", target="Svc.Auth", edge_filter='relation == "owns"')
    assert r["paths"] == []  # owns alone cannot reach Auth


def test_find_paths_missing_endpoint(mcp):
    with pytest.raises(ToolError, match="not found"):
        call(mcp, "find_paths", source="app.Order", target="nope")


# --- READ must not mutate ([5-B]) ---


def test_reads_do_not_dirty_the_graph(graph, mcp):
    graph.clear_dirty()
    call(mcp, "get_graph_summary")
    call(mcp, "get_node", id="app.Order", include_neighbors=True)
    call(mcp, "list_nodes", filter="degree(node) > 0")
    call(mcp, "list_edges", filter='relation == "owns"')
    call(mcp, "get_neighbors", id="app.Order", depth=2)
    call(mcp, "find_paths", source="app.Order", target="Svc.Auth")
    assert graph.dirty is False


def test_reads_publish_no_events(graph, mcp):
    events = []
    graph.events.subscribe(events.append)
    call(mcp, "get_graph_summary")
    call(mcp, "list_nodes")
    call(mcp, "get_neighbors", id="app.Order", depth=2)
    assert events == []


# --- ★ 핸드오프 read 흐름 (README 핵심 약속) ---


def test_a_fresh_session_reads_a_prior_session_graph(mcp):
    """A new Client — a new AI session — inspects a graph it did not build,
    exactly the [23-D] handoff: summary → filtered list → neighborhood."""

    async def go():
        async with Client(mcp) as c:
            summary = _payload(await c.call_tool("get_graph_summary", {}))
            assert summary["nodes"] == 5

            services = _payload(
                await c.call_tool("list_nodes", {"filter": 'type == "service"'})
            )
            service_ids = {n["id"] for n in services["nodes"]}
            assert service_ids == {"Svc.User", "Svc.Auth"}

            around = _payload(
                await c.call_tool("get_neighbors", {"id": "Svc.User", "depth": 1})
            )
            assert {n["id"] for n in around["neighbors"]} == {"UI.View", "Svc.Auth"}

    asyncio.run(go())


def _payload(result):
    """Unwrap a FastMCP tool result to its structured dict."""
    if getattr(result, "structured_content", None):
        return result.structured_content
    if getattr(result, "data", None) is not None:
        return result.data
    import json

    return json.loads(result.content[0].text)


# --- search ([5-B]) — case-insensitive substring over an allowlisted field set ---


def test_search_partial_substring_match(mcp):
    # "Service" is a substring of both service labels (default in_fields = label,id).
    r = call(mcp, "search", query="Service")
    ids = {n["id"] for n in r["nodes"]}
    assert ids == {"Svc.User", "Svc.Auth"}
    assert r["total"] == 2


def test_search_is_case_insensitive(mcp):
    # Lower-cased query still matches the mixed-case label "MainView".
    r = call(mcp, "search", query="mainview")
    assert [n["id"] for n in r["nodes"]] == ["UI.View"]


def test_search_in_fields_targets_only_named_fields(mcp):
    # "Svc" appears in the ids but not in any label.
    by_id = call(mcp, "search", query="Svc", in_fields=["id"])
    assert {n["id"] for n in by_id["nodes"]} == {"Svc.User", "Svc.Auth"}

    by_label = call(mcp, "search", query="Svc", in_fields=["label"])
    assert by_label["nodes"] == []
    assert by_label["total"] == 0


def test_search_matches_type_field(mcp):
    r = call(mcp, "search", query="SERVICE", in_fields=["type"])
    assert {n["id"] for n in r["nodes"]} == {"Svc.User", "Svc.Auth"}


def test_search_matches_non_reserved_property(mcp):
    # 'properties.<name>' is allowed for a non-reserved key; 12 is field_count of Order.
    r = call(mcp, "search", query="12", in_fields=["properties.field_count"])
    assert [n["id"] for n in r["nodes"]] == ["app.Order"]


def test_search_deterministic_id_ascending_order(mcp):
    r = call(mcp, "search", query="Service")
    # Two matches, always returned id-ascending regardless of insertion order.
    assert [n["id"] for n in r["nodes"]] == ["Svc.Auth", "Svc.User"]


def test_search_no_match_is_empty(mcp):
    r = call(mcp, "search", query="zzz-nonexistent")
    assert r["nodes"] == []
    assert r["total"] == 0


def test_search_limit_caps_returned_but_total_is_full_count(mcp):
    r = call(mcp, "search", query="Service", limit=1)
    assert len(r["nodes"]) == 1          # only one row carried
    assert r["total"] == 2               # ...but the AI learns two matched
    assert r["nodes"][0]["id"] == "Svc.Auth"  # id-ascending → first page


def test_search_limit_over_cap_is_rejected(mcp):
    with pytest.raises(ToolError):
        client_call(mcp, "search", {"query": "a", "limit": MAX_LIST_LIMIT + 1})


def test_search_limit_below_one_is_rejected(mcp):
    with pytest.raises(ToolError):
        client_call(mcp, "search", {"query": "a", "limit": 0})


# --- ★ [11] the in_fields allowlist: a bare field list, never a hole to reserved keys ---


def test_search_reserved_property_field_is_refused(mcp):
    # 'properties._citations' targets a reserved ('_') key — fail-closed.
    with pytest.raises(ToolError, match="reserved"):
        call(mcp, "search", query="trace", in_fields=["properties._citations"])


def test_search_unknown_field_is_refused(mcp):
    with pytest.raises(ToolError, match="unknown search field"):
        call(mcp, "search", query="x", in_fields=["definitely_not_a_field"])


def test_search_malformed_property_field_is_refused(mcp):
    with pytest.raises(ToolError, match="malformed"):
        call(mcp, "search", query="x", in_fields=["properties."])


def test_search_cannot_reach_reserved_key_content_via_allowed_fields(mcp):
    # "ida://0x1" lives only inside Order's reserved _citations. It must not be
    # findable through any allowlisted field — reserved content is not searchable.
    r = call(
        mcp,
        "search",
        query="ida://0x1",
        in_fields=["label", "id", "type", "layer", "properties.field_count"],
    )
    assert r["nodes"] == []
    assert r["total"] == 0


def test_search_tags_field_matches_any_tag():
    g = Graph(name="tagged")
    g.add_node(id="n1", label="Alpha", type="class", tags=["security", "auth"])
    g.add_node(id="n2", label="Beta", type="class", tags=["ui"])
    m = create_server(g)

    r = call(m, "search", query="sec", in_fields=["tags"])
    assert [n["id"] for n in r["nodes"]] == ["n1"]


def test_search_empty_in_fields_matches_nothing():
    g = Graph(name="empty-fields")
    g.add_node(id="n1", label="Alpha", type="class")
    m = create_server(g)

    r = call(m, "search", query="Alpha", in_fields=[])
    assert r["nodes"] == []
    assert r["total"] == 0


def test_search_does_not_mutate_the_graph(graph, mcp):
    graph.clear_dirty()
    events: list = []
    graph.events.subscribe(events.append)

    call(mcp, "search", query="Service")
    call(mcp, "search", query="12", in_fields=["properties.field_count"])

    assert graph.dirty is False
    assert events == []


def test_search_roundtrips_through_an_in_memory_client(mcp):
    """A fresh Client session (a new AI) searches a graph it did not build."""

    async def go():
        async with Client(mcp) as c:
            result = _payload(
                await c.call_tool("search", {"query": "Service", "in_fields": ["label"]})
            )
            assert {n["id"] for n in result["nodes"]} == {"Svc.User", "Svc.Auth"}
            assert result["total"] == 2

    asyncio.run(go())
