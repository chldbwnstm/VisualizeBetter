"""Completion verification for the Graph Core prerequisite ([20] Day 3-4).

Covers: add/get roundtrip, delete_node cascade rule ([5-A]), dirty flag ([23-C]),
event publication ([8-C]), edge 4-tuple identity ([4-B]), placeholder
auto-creation ([5-A]).
"""

import json

import pytest

from visualizebetter.graph.core import (
    CITATIONS_PROPERTY,
    MAX_CITATIONS_ENTRIES,
    MAX_FINDING_BODY_CHARS,
    MAX_PROVENANCE_ENTRIES,
    MAX_SUPERSEDED_ENTRIES,
    PROVENANCE_PROPERTY,
    SUPERSEDED_PROPERTY,
    PLACEHOLDER_PROPERTY,
    PLACEHOLDER_TYPE,
    Graph,
    Node,
)


@pytest.fixture
def graph():
    return Graph(name="test")


@pytest.fixture
def events(graph):
    """Collect every event the graph publishes."""
    captured = []
    graph.events.subscribe(captured.append)
    return captured


# --- add / get roundtrip ---


def test_add_node_get_node_roundtrip(graph):
    node = graph.add_node(id="app.OrderService", label="OrderService", type="class")

    assert graph.get_node("app.OrderService") is node
    assert node.label == "OrderService"
    assert node.type == "class"
    assert node.created_at and node.updated_at


def test_get_node_missing_returns_none(graph):
    assert graph.get_node("nope") is None


def test_add_node_optional_fields_roundtrip(graph):
    node = graph.add_node(
        id="n1",
        label="N1",
        type="class",
        properties={"ns": "app.ui"},
        parent_id="p1",
        style_hint={"color": "#fff"},
        position_hint={"x": 1.0, "y": 2.0},
        layer="claude-session-abc",
        tags=["a"],
        ttl=60,
        created_by="claude",
    )

    assert node.properties == {"ns": "app.ui"}
    assert node.parent_id == "p1"
    assert node.style_hint == {"color": "#fff"}
    assert node.position_hint == {"x": 1.0, "y": 2.0}
    assert node.layer == "claude-session-abc"
    assert node.tags == ["a"]
    assert node.ttl == 60
    assert node.created_by == "claude"
    assert graph.layers == ["claude-session-abc"]


def test_add_node_is_idempotent_and_merges_properties(graph):
    graph.add_node(id="n1", label="N1", type="class", properties={"a": 1})
    node = graph.add_node(id="n1", label="N1 renamed", type="struct", properties={"b": 2})

    assert len(graph.nodes) == 1
    assert node.label == "N1 renamed"
    assert node.type == "struct"
    assert node.properties == {"a": 1, "b": 2}


def test_add_edge_get_edge_roundtrip(graph):
    graph.add_node(id="a", label="A", type="class")
    graph.add_node(id="b", label="B", type="class")

    edge = graph.add_edge(source="a", target="b", relation="field")

    assert graph.get_edge("a", "b", "field") is edge
    assert edge.identity == ("a", "b", "field", "")
    assert edge.directed is True
    assert edge.weight == 1.0


# --- edge identity is the (source, target, relation, key) 4-tuple ([4-B]) ---


def test_edge_key_distinguishes_parallel_edges(graph):
    graph.add_node(id="a", label="A", type="class")
    graph.add_node(id="b", label="B", type="class")

    e1 = graph.add_edge(source="a", target="b", relation="field", key="m_health")
    e2 = graph.add_edge(source="a", target="b", relation="field", key="m_mana")

    assert e1 is not e2
    assert len(graph.edges) == 2
    assert graph.get_edge("a", "b", "field", "m_health") is e1
    assert graph.get_edge("a", "b", "field", "m_mana") is e2


def test_edge_identity_differs_by_each_tuple_member(graph):
    graph.add_edge(source="a", target="b", relation="field")
    graph.add_edge(source="a", target="b", relation="call")
    graph.add_edge(source="a", target="c", relation="field")
    graph.add_edge(source="c", target="b", relation="field")

    assert len(graph.edges) == 4


def test_add_edge_same_identity_updates_in_place(graph):
    first = graph.add_edge(source="a", target="b", relation="field", weight=0.2)
    second = graph.add_edge(source="a", target="b", relation="field", weight=0.9)

    assert first is second
    assert len(graph.edges) == 1
    assert second.weight == 0.9


# --- placeholder auto-creation ([5-A]) ---


def test_add_edge_creates_placeholder_nodes_for_missing_endpoints(graph):
    graph.add_edge(source="ghost_a", target="ghost_b", relation="ref")

    for node_id in ("ghost_a", "ghost_b"):
        placeholder = graph.get_node(node_id)
        assert placeholder is not None
        assert placeholder.label == node_id
        assert placeholder.type == PLACEHOLDER_TYPE
        assert placeholder.properties == {PLACEHOLDER_PROPERTY: True}


def test_existing_endpoints_are_not_replaced_by_placeholders(graph):
    graph.add_node(id="a", label="A", type="class")
    graph.add_edge(source="a", target="b", relation="ref")

    assert graph.get_node("a").type == "class"
    assert graph.get_node("b").type == PLACEHOLDER_TYPE


def test_pushing_a_placeholder_node_resolves_it(graph):
    graph.add_edge(source="a", target="late", relation="ref")
    assert graph.get_node("late").properties == {PLACEHOLDER_PROPERTY: True}

    resolved = graph.add_node(
        id="late", label="LateClass", type="class", properties={"ns": "MOD"}
    )

    assert PLACEHOLDER_PROPERTY not in resolved.properties
    assert resolved.type == "class"
    assert resolved.label == "LateClass"
    assert resolved.properties == {"ns": "MOD"}


# --- update patch ([5-A]) ---


def test_update_node_set_merges_and_remove_deletes_property_keys(graph):
    graph.add_node(id="n1", label="N1", type="class", properties={"a": 1, "b": 2})

    node = graph.update_node("n1", {"set": {"properties": {"c": 3}}, "remove": ["a"]})

    assert node.properties == {"b": 2, "c": 3}


def test_update_node_updates_optional_fields(graph):
    graph.add_node(id="n1", label="N1", type="class")

    node = graph.update_node("n1", {"set": {"layer": "gpt-1", "tags": ["x"], "ttl": 5}})

    assert node.layer == "gpt-1"
    assert node.tags == ["x"]
    assert node.ttl == 5


def test_update_node_rejects_server_managed_fields(graph):
    graph.add_node(id="n1", label="N1", type="class")

    with pytest.raises(ValueError):
        graph.update_node("n1", {"set": {"created_at": "1999-01-01"}})


def test_update_node_missing_raises(graph):
    with pytest.raises(KeyError):
        graph.update_node("nope", {"set": {"label": "x"}})


def test_update_edge_applies_patch(graph):
    graph.add_edge(source="a", target="b", relation="field", properties={"x": 1})

    edge = graph.update_edge(
        "a", "b", "field", "", {"set": {"weight": 0.5, "properties": {"y": 2}}}
    )

    assert edge.weight == 0.5
    assert edge.properties == {"x": 1, "y": 2}


def test_update_edge_rejects_identity_fields(graph):
    graph.add_edge(source="a", target="b", relation="field")

    with pytest.raises(ValueError):
        graph.update_edge("a", "b", "field", "", {"set": {"source": "z"}})


# --- delete_node cascade rule ([5-A]) ---


def test_delete_node_without_cascade_is_refused_when_edges_exist(graph):
    graph.add_node(id="a", label="A", type="class")
    graph.add_node(id="b", label="B", type="class")
    graph.add_edge(source="a", target="b", relation="field")
    graph.add_edge(source="b", target="a", relation="call")

    result = graph.delete_node("a")

    assert result == {"ok": False, "error": "has_edges", "edge_count": 2}
    assert graph.get_node("a") is not None
    assert len(graph.edges) == 2


def test_delete_node_with_cascade_removes_connected_edges(graph):
    graph.add_node(id="a", label="A", type="class")
    graph.add_node(id="b", label="B", type="class")
    graph.add_edge(source="a", target="b", relation="field")
    graph.add_edge(source="b", target="a", relation="call")

    result = graph.delete_node("a", cascade=True)

    assert result == {"ok": True}
    assert graph.get_node("a") is None
    assert graph.edges == {}
    assert graph.get_node("b") is not None


def test_delete_node_without_edges_succeeds_without_cascade(graph):
    graph.add_node(id="lonely", label="L", type="class")

    assert graph.delete_node("lonely") == {"ok": True}
    assert graph.get_node("lonely") is None


def test_delete_node_missing_raises(graph):
    with pytest.raises(KeyError):
        graph.delete_node("nope")


def test_delete_edge_removes_only_that_edge(graph):
    graph.add_edge(source="a", target="b", relation="field", key="k1")
    graph.add_edge(source="a", target="b", relation="field", key="k2")

    assert graph.delete_edge("a", "b", "field", "k1") == {"ok": True}
    assert graph.get_edge("a", "b", "field", "k1") is None
    assert graph.get_edge("a", "b", "field", "k2") is not None


def test_delete_edge_missing_raises(graph):
    with pytest.raises(KeyError):
        graph.delete_edge("a", "b", "field")


# --- dirty flag ([23-C]) ---


def test_new_graph_is_not_dirty(graph):
    assert graph.dirty is False


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(lambda g: g.add_node(id="n", label="N", type="t"), id="add_node"),
        pytest.param(lambda g: g.add_edge(source="a", target="b", relation="r"), id="add_edge"),
    ],
)
def test_mutations_set_dirty_flag(graph, mutate):
    mutate(graph)
    assert graph.dirty is True


def test_update_node_sets_dirty_flag(graph):
    graph.add_node(id="n", label="N", type="t")
    graph.clear_dirty()

    graph.update_node("n", {"set": {"label": "N2"}})

    assert graph.dirty is True


def test_update_edge_sets_dirty_flag(graph):
    graph.add_edge(source="a", target="b", relation="r")
    graph.clear_dirty()

    graph.update_edge("a", "b", "r", "", {"set": {"weight": 0.1}})

    assert graph.dirty is True


def test_delete_node_sets_dirty_flag(graph):
    graph.add_node(id="n", label="N", type="t")
    graph.clear_dirty()

    graph.delete_node("n")

    assert graph.dirty is True


def test_delete_edge_sets_dirty_flag(graph):
    graph.add_edge(source="a", target="b", relation="r")
    graph.clear_dirty()

    graph.delete_edge("a", "b", "r")

    assert graph.dirty is True


def test_refused_delete_does_not_set_dirty_flag(graph):
    graph.add_edge(source="a", target="b", relation="r")
    graph.clear_dirty()

    graph.delete_node("a")

    assert graph.dirty is False


def test_clear_dirty_resets_the_flag(graph):
    graph.add_node(id="n", label="N", type="t")
    graph.clear_dirty()

    assert graph.dirty is False


# --- event publication ([8-C]) ---


def test_add_node_publishes_node_add_with_full_node(graph, events):
    node = graph.add_node(id="n1", label="N1", type="class")

    assert len(events) == 1
    assert events[0].op == "node.add"
    assert events[0].data == node.to_dict()


def test_update_node_publishes_node_update_with_id_and_patch(graph, events):
    graph.add_node(id="n1", label="N1", type="class")
    patch = {"set": {"label": "N2"}}

    graph.update_node("n1", patch)

    assert events[-1].op == "node.update"
    assert events[-1].data == {"id": "n1", "patch": patch}


def test_delete_node_publishes_node_delete_with_id(graph, events):
    graph.add_node(id="n1", label="N1", type="class")

    graph.delete_node("n1")

    assert events[-1].op == "node.delete"
    assert events[-1].data == {"id": "n1"}


def test_add_edge_publishes_edge_add_with_full_edge(graph, events):
    graph.add_node(id="a", label="A", type="class")
    graph.add_node(id="b", label="B", type="class")

    edge = graph.add_edge(source="a", target="b", relation="field")

    assert events[-1].op == "edge.add"
    assert events[-1].data == edge.to_dict()


def test_delete_edge_publishes_edge_delete_with_identity_tuple(graph, events):
    graph.add_edge(source="a", target="b", relation="field", key="k")

    graph.delete_edge("a", "b", "field", "k")

    assert events[-1].op == "edge.delete"
    assert events[-1].data == {
        "source": "a",
        "target": "b",
        "relation": "field",
        "key": "k",
    }


def test_update_edge_publishes_edge_update_with_identity_and_patch(graph, events):
    graph.add_edge(source="a", target="b", relation="field", key="k")
    patch = {"set": {"weight": 0.3}}

    graph.update_edge("a", "b", "field", "k", patch)

    assert events[-1].op == "edge.update"
    assert events[-1].data == {
        "source": "a",
        "target": "b",
        "relation": "field",
        "key": "k",
        "patch": patch,
    }


def test_placeholder_creation_publishes_node_add(graph, events):
    graph.add_edge(source="ghost", target="ghost2", relation="ref")

    ops = [e.op for e in events]
    assert ops == ["node.add", "node.add", "edge.add"]


def test_cascade_delete_publishes_edge_delete_then_node_delete(graph, events):
    graph.add_node(id="a", label="A", type="class")
    graph.add_node(id="b", label="B", type="class")
    graph.add_edge(source="a", target="b", relation="field")
    events.clear()

    graph.delete_node("a", cascade=True)

    assert [e.op for e in events] == ["edge.delete", "node.delete"]


def test_refused_delete_publishes_nothing(graph, events):
    graph.add_edge(source="a", target="b", relation="field")
    events.clear()

    graph.delete_node("a")

    assert events == []


def test_event_seq_is_monotonically_increasing(graph, events):
    graph.add_node(id="a", label="A", type="class")
    graph.add_node(id="b", label="B", type="class")
    graph.add_edge(source="a", target="b", relation="field")

    seqs = [e.seq for e in events]
    assert seqs == [1, 2, 3]
    assert graph.events.seq == 3


def test_unsubscribe_stops_delivery(graph):
    captured = []
    unsubscribe = graph.events.subscribe(captured.append)

    graph.add_node(id="a", label="A", type="class")
    unsubscribe()
    graph.add_node(id="b", label="B", type="class")

    assert [e.data["id"] for e in captured] == ["a"]


# --- indices ---


def test_by_type_index_tracks_nodes(graph):
    graph.add_node(id="a", label="A", type="class")
    graph.add_node(id="b", label="B", type="class")
    graph.add_node(id="f", label="F", type="function")

    assert graph.indices.nodes_of_type("class") == {"a", "b"}
    assert graph.indices.nodes_of_type("function") == {"f"}


def test_by_type_index_follows_retype(graph):
    graph.add_node(id="a", label="A", type="class")

    graph.update_node("a", {"set": {"type": "struct"}})

    assert graph.indices.nodes_of_type("class") == set()
    assert graph.indices.nodes_of_type("struct") == {"a"}


def test_by_type_index_drops_deleted_nodes(graph):
    graph.add_node(id="a", label="A", type="class")

    graph.delete_node("a")

    assert graph.indices.nodes_of_type("class") == set()


def test_adjacency_index_covers_both_endpoints(graph):
    graph.add_node(id="a", label="A", type="class")
    graph.add_node(id="b", label="B", type="class")
    graph.add_edge(source="a", target="b", relation="field")

    assert graph.indices.edges_of("a") == {("a", "b", "field", "")}
    assert graph.indices.edges_of("b") == {("a", "b", "field", "")}


def test_adjacency_index_drops_deleted_edges(graph):
    graph.add_edge(source="a", target="b", relation="field")

    graph.delete_edge("a", "b", "field")

    assert graph.indices.edges_of("a") == set()
    assert graph.indices.edges_of("b") == set()


# --- [23-B] 예약키 강제는 core 에 있다 (RN3 부수건) ---


def test_add_node_rejects_reserved_properties_at_creation():
    """[23-B] 는 이 규칙을 MCP 가 아니라 core 에 두라고 명시한다 — import·스냅샷
    로드·어댑터가 core 를 직접 호출하므로 MCP 만 막으면 불변식이 강제되지 않는다.
    update 경로(_apply_patch)는 처음부터 막았지만 생성 경로는 뚫려 있었다."""
    g = Graph()
    with pytest.raises(ValueError, match="reserved"):
        g.add_node(id="n", label="N", type="class", properties={"_citations": [{"url": "forged"}]})
    assert "n" not in g.nodes  # fail-closed: 아무것도 만들지 않는다


def test_add_edge_rejects_reserved_properties_at_creation():
    g = Graph()
    g.add_node(id="a", label="A", type="class")
    g.add_node(id="b", label="B", type="class")
    with pytest.raises(ValueError, match="reserved"):
        g.add_edge(source="a", target="b", relation="calls", properties={"_superseded": []})
    assert g.edges == {}


def test_cite_can_no_longer_meet_a_forged_citations_value():
    """생성 경로가 막히면서 cite() 의 setdefault 크래시가 도달 불가가 된다.

    이전에는 add_node(properties={"_citations": None}) 이 통과해 cite() 가
    None.append 로 터졌다 (hypothesis 가 실제로 찾아낸 경로). 이제 그 상태를
    만들 수 있는 입구가 생성·갱신 양쪽 모두 닫혔다."""
    g = Graph()
    with pytest.raises(ValueError, match="reserved"):
        g.add_node(id="n", label="N", type="class", properties={CITATIONS_PROPERTY: None})

    g.add_node(id="n", label="N", type="class")
    # 최상위 예약 필드는 기존대로 거부된다 (_apply_patch).
    with pytest.raises(ValueError, match="system-owned"):
        g.update_node("n", {"set": {CITATIONS_PROPERTY: None}})

    g.cite("n", "https://example.test/x", "ok")          # 정상 경로는 그대로
    assert len(g.get_node("n").properties[CITATIONS_PROPERTY]) == 1


def test_non_reserved_properties_are_unaffected():
    g = Graph()
    g.add_node(id="n", label="N", type="class", properties={"ns": "app.ui", "count": 3})
    assert g.get_node("n").properties == {"ns": "app.ui", "count": 3}


def test_update_cannot_forge_reserved_keys_nested_in_properties():
    """[23-B] 게이트 A — 최상위 필드만 막으면 위조가 두 번째 문으로 그대로 들어온다.

    {"set": {"properties": {"_citations": ...}}} 는 merge 로 들어가 cite() 가 쌓은
    근거 배열을 통째로 덮었다. core 에서 닫는다 — MCP 만 막으면 import·스냅샷
    로드·어댑터 경로가 우회한다([23-B])."""
    g = Graph()
    g.add_node(id="n", label="N", type="class")
    g.cite("n", "https://example.test/real", "real evidence")

    with pytest.raises(ValueError, match="reserved"):
        g.update_node("n", {"set": {"properties": {CITATIONS_PROPERTY: "FORGED"}}})

    # 진짜 근거가 그대로 남아있다 — 위조 시도가 아무것도 덮지 못했다
    citations = g.get_node("n").properties[CITATIONS_PROPERTY]
    assert len(citations) == 1
    assert citations[0]["url"] == "https://example.test/real"


def test_update_still_allows_ordinary_nested_properties():
    """거부는 예약키에만 — 평범한 properties 갱신은 그대로 동작한다."""
    g = Graph()
    g.add_node(id="n", label="N", type="class", properties={"a": 1})
    g.update_node("n", {"set": {"properties": {"b": 2}}})
    assert g.get_node("n").properties == {"a": 1, "b": 2}


# --- [23-C] RN4 AA — properties 는 타입부터 검증한다 ---


@pytest.mark.parametrize(
    "forged",
    [
        [["_citations", "FORGED"]],          # 쌍 리스트 — dict.update 가 받아준다
        [("_citations", "FORGED")],          # 튜플 리스트
        (("_citations", "FORGED"),),         # 튜플의 튜플
        "not a mapping",
        42,
    ],
)
def test_non_dict_properties_are_rejected_on_node_update(forged):
    """★ 게이트 A 우회 — isinstance(x, dict) 를 *가드* 로 쓰면 쌍 리스트가 그대로
    통과하고 dict.update 가 적용해버린다. 타입을 먼저 검증한다."""
    g = Graph()
    g.add_node(id="n", label="N", type="class")
    g.cite("n", "https://example.test/real", "real evidence")

    with pytest.raises(ValueError):
        g.update_node("n", {"set": {"properties": forged}})

    citations = g.get_node("n").properties[CITATIONS_PROPERTY]
    assert len(citations) == 1 and citations[0]["url"] == "https://example.test/real"


@pytest.mark.parametrize("forged", [[["_citations", "F"]], "str", 7])
def test_non_dict_properties_are_rejected_on_edge_update(forged):
    g = Graph()
    g.add_node(id="a", label="A", type="class")
    g.add_node(id="b", label="B", type="class")
    g.add_edge(source="a", target="b", relation="calls")
    with pytest.raises(ValueError):
        g.update_edge("a", "b", "calls", "", {"set": {"properties": forged}})


@pytest.mark.parametrize("forged", [[["_citations", "F"]], "str", 7])
def test_non_dict_properties_are_rejected_on_create(forged):
    g = Graph()
    with pytest.raises(ValueError):
        g.add_node(id="n", label="N", type="class", properties=forged)
    g.add_node(id="a", label="A", type="class")
    g.add_node(id="b", label="B", type="class")
    with pytest.raises(ValueError):
        g.add_edge(source="a", target="b", relation="calls", properties=forged)


def test_non_string_property_keys_are_rejected_not_crashed():
    """비문자열 키가 startswith 에서 AttributeError 를 내면 항목별 오류 처리를
    탈출해 배치의 뒤 항목을 조용히 유실시킨다 — 거부로 수렴시킨다."""
    g = Graph()
    with pytest.raises(ValueError, match="must be strings"):
        g.add_node(id="n", label="N", type="class", properties={1: "x"})


def test_history_paths_still_work_after_the_type_check():
    """회귀 — cite()·supersede·correction 은 그대로 동작해야 한다."""
    g = Graph()
    g.add_node(id="n", label="N", type="class", properties={"ok": 1})
    g.cite("n", "https://example.test/a", "A")
    g.update_node("n", {"set": {"label": "N2"}}, reason="supersede")
    g.update_node("n", {"set": {"label": "N3"}}, reason="correction")
    node = g.get_node("n")
    assert node.properties[CITATIONS_PROPERTY][0]["url"] == "https://example.test/a"
    assert node.properties[SUPERSEDED_PROPERTY][0]["prev"]["label"] == "N"
    assert node.properties[PROVENANCE_PROPERTY][0]["action"] == "correction"


# --- [23-C] RN5 JJ — patch 의 모양을 예약키 판정 전에 확정한다 ---


_BAD_PATCHES = [
    {"set": {1: "x"}},          # 비문자열 키 → startswith AttributeError 였다
    {"remove": [1]},            # 같은 축, remove 쪽
    {"set": ["label"]},         # set 이 dict 아님 → MCP 체이닝이 터졌다
    {"remove": "label"},        # ★ 조용히 성공(문자 단위 순회, 무동작) 이었다
    {"set": "label"},
    {"remove": {"label": 1}},
    ["set"],                    # ★ patch 자체가 dict 이 아님
    "label",
    42,
]


@pytest.mark.parametrize("patch", _BAD_PATCHES)
def test_malformed_node_patch_is_refused_without_changing_data(patch):
    g = Graph()
    g.add_node(id="n", label="N", type="class", properties={"keep": 1})
    before = g.get_node("n").to_dict()

    with pytest.raises(ValueError):
        g.update_node("n", patch)

    assert g.get_node("n").to_dict() == before


@pytest.mark.parametrize("patch", _BAD_PATCHES)
def test_malformed_edge_patch_is_refused_without_changing_data(patch):
    g = Graph()
    g.add_node(id="a", label="A", type="class")
    g.add_node(id="b", label="B", type="class")
    g.add_edge(source="a", target="b", relation="calls", properties={"keep": 1})
    before = g.edges[("a", "b", "calls", "")].to_dict()

    with pytest.raises(ValueError):
        g.update_edge("a", "b", "calls", "", patch)

    assert g.edges[("a", "b", "calls", "")].to_dict() == before


@pytest.mark.parametrize("patch", [p for p in _BAD_PATCHES if not (isinstance(p, dict) and "remove" in p)])
def test_malformed_finding_patch_is_refused_without_changing_data(patch):
    g = Graph()
    finding = g.add_finding(title="t", body="b")
    before = finding.to_dict()

    with pytest.raises(ValueError):
        g.update_finding(finding.finding_id, patch)

    assert g.get_finding(finding.finding_id).to_dict() == before


def test_wellformed_patches_still_work():
    """회귀 — 정상 patch(속성 set·remove·properties merge)는 그대로 동작한다."""
    g = Graph()
    g.add_node(id="n", label="N", type="class", properties={"a": 1, "drop": 2})

    g.update_node("n", {"set": {"label": "N2", "properties": {"b": 3}}})
    assert g.get_node("n").label == "N2"
    assert g.get_node("n").properties == {"a": 1, "drop": 2, "b": 3}

    g.update_node("n", {"remove": ["drop"]})
    assert g.get_node("n").properties == {"a": 1, "b": 3}

    g.update_node("n", {"remove": ()})          # 빈 tuple 도 허용
    g.update_node("n", {})                       # set/remove 없음도 허용
    assert g.get_node("n").properties == {"a": 1, "b": 3}


# --- [23-C] ★★★★★ RN6 LL — 거부된 patch 는 어떤 흔적도 남기지 않는다 ---


_REJECTED = [
    {"set": {"labell": "N2"}},              # unknown field — LL 의 실측 재현 입력
    {"set": {"_citations": "FORGED"}},      # 예약 필드
    {"set": {"properties": {"_citations": "F"}}},   # 중첩 예약키
    {"set": {"id": "hijack"}},              # server-managed
    {"remove": ["_citations"]},             # 예약키 삭제 시도
    {"sett": {"label": "HACK"}},            # 오타 키 (NN(2))
]


def _node_state(g, node_id="n"):
    node = g.get_node(node_id)
    return (
        node.to_dict(),
        len(node.properties.get(SUPERSEDED_PROPERTY, [])),
        len(node.properties.get(PROVENANCE_PROPERTY, [])),
    )


@pytest.mark.parametrize("reason", [None, "supersede", "correction"])
@pytest.mark.parametrize("patch", _REJECTED)
def test_rejected_node_patch_leaves_no_trace(patch, reason):
    """★ reason 을 **양쪽으로** 돈다 — RN5 가 이 축을 놓친 이유가 _BAD_PATCHES 를
    reason=None 으로만 돌려서였다. supersede/correction 은 _record_lifecycle 이
    검증보다 먼저 돌아 거부된 호출이 _superseded 쓰레기를 남겼다."""
    g = Graph()
    g.add_node(id="n", label="N", type="class", properties={"keep": 1})
    g.cite("n", "https://example.test/real", "real")
    g.update_node("n", {"set": {"label": "REAL"}}, reason="supersede")  # 진짜 이력 1건

    before = _node_state(g)
    events = []
    g.events.subscribe(lambda topic, payload: events.append(topic))

    with pytest.raises(ValueError):
        g.update_node("n", patch, reason=reason)

    assert _node_state(g) == before      # 레코드·이력 길이 전부 동일
    assert events == []                   # [8-C] 이벤트 0건


@pytest.mark.parametrize("reason", [None, "supersede", "correction"])
def test_rejected_edge_and_finding_patches_leave_no_trace(reason):
    g = Graph()
    g.add_node(id="a", label="A", type="class")
    g.add_node(id="b", label="B", type="class")
    g.add_edge(source="a", target="b", relation="calls", properties={"keep": 1})
    finding = g.add_finding(title="t", body="b")
    g.update_finding(finding.finding_id, {"set": {"body": "real"}}, reason="supersede")

    edge_before = g.edges[("a", "b", "calls", "")].to_dict()
    finding_before = g.get_finding(finding.finding_id).to_dict()
    events = []
    g.events.subscribe(lambda topic, payload: events.append(topic))

    with pytest.raises(ValueError):
        g.update_edge("a", "b", "calls", "", {"set": {"labell": "x"}}, reason=reason)
    with pytest.raises(ValueError):
        g.update_finding(finding.finding_id, {"set": {"titlee": "x"}}, reason=reason)

    assert g.edges[("a", "b", "calls", "")].to_dict() == edge_before
    assert g.get_finding(finding.finding_id).to_dict() == finding_before
    assert events == []


def test_failed_calls_cannot_evict_a_real_supersession():
    """★ LL 의 결정적 증상 — 거부된 호출 12회가 MAX_SUPERSEDED_ENTRIES(10) FIFO 를
    돌려 **진짜 supersession 기록을 영구 소실**시켰다. [24-C] 가 지키겠다고 한 것을
    실패한 호출이 파괴하는 상태였다."""
    g = Graph()
    g.add_node(id="n", label="ORIGINAL", type="class")
    g.update_node("n", {"set": {"label": "V2"}}, reason="supersede")
    archived = g.get_node("n").properties[SUPERSEDED_PROPERTY][0]["prev"]["label"]
    assert archived == "ORIGINAL"

    for _ in range(MAX_SUPERSEDED_ENTRIES + 2):
        with pytest.raises(ValueError):
            g.update_node("n", {"set": {"labell": "N2"}}, reason="supersede")

    history = g.get_node("n").properties[SUPERSEDED_PROPERTY]
    assert len(history) == 1                       # 쓰레기 적립 0
    assert history[0]["prev"]["label"] == "ORIGINAL"   # ★ 진짜 기록 생존


def test_finding_size_rejection_still_leaves_it_untouched():
    """LL 로 검증 순서가 바뀌어도 [23-B] 크기 불변식 거부는 그대로 동작한다."""
    g = Graph()
    finding = g.add_finding(title="t", body="b")
    before = finding.to_dict()
    with pytest.raises(ValueError):
        g.update_finding(finding.finding_id, {"set": {"body": "x" * (MAX_FINDING_BODY_CHARS + 1)}},
                         reason="supersede")
    assert g.get_finding(finding.finding_id).to_dict() == before


@pytest.mark.parametrize("patch", [None, [], "", 0, False])
def test_falsy_patches_raise_valueerror_in_all_three_updaters(patch):
    """(NN(1)(3)) None/falsy 는 **ValueError** 로 거부된다 — 세 updater 동일.

    MCP 층은 어떤 예외든 ToolError 로 감싸므로 tool 경유로는 AttributeError 와
    구분되지 않는다. 그래서 core 에서 직접 단언한다: None 이 통과하면
    _apply_patch 의 `patch.get` 이 AttributeError 를 내고, update_edge/
    update_finding 만 `patch or {}` 로 우연히 살아남아 세 tool 이 같은 입력에
    다르게 반응했다."""
    g = Graph()
    g.add_node(id="a", label="A", type="class")
    g.add_node(id="b", label="B", type="class")
    g.add_edge(source="a", target="b", relation="calls")
    finding = g.add_finding(title="t")

    with pytest.raises(ValueError):
        g.update_node("a", patch)
    with pytest.raises(ValueError):
        g.update_edge("a", "b", "calls", "", patch)
    with pytest.raises(ValueError):
        g.update_finding(finding.finding_id, patch)


# --- [23-C] ★★★★★★ RN7 SS/WW/XX/YY — 값 타입 계약과 이력 안전망 ---


def _capture_events(g):
    """★ RN7 YY: publish 는 handler(event) 로 **1인자**를 넘긴다. 2인자 람다를
    쓰면 이벤트가 실제로 발행될 때 핸들러가 TypeError 를 내고 리스트는 영원히
    비어, `assert events == []` 가 절대 실패할 수 없는 죽은 단언이 된다."""
    seen = []
    g.events.subscribe(lambda event: seen.append(event.op))
    return seen


def test_event_capture_actually_captures():
    """YY — 아래 '이벤트 0건' 단언들이 죽은 단언이 아님을 먼저 증명한다."""
    g = Graph()
    g.add_node(id="n", label="N", type="class")
    seen = _capture_events(g)
    g.update_node("n", {"set": {"label": "N2"}})
    assert seen == ["node.update"]


_TYPE_VIOLATIONS = [
    ("type", {}), ("type", []), ("type", 7), ("type", None), ("type", True),
    ("label", {}), ("label", ["a"]), ("label", 7), ("label", None),
    ("layer", 7), ("layer", {}), ("layer", []),
    ("ttl", True), ("ttl", "60"), ("ttl", {}), ("ttl", None),
    ("parent_id", 7), ("parent_id", []),
    ("properties", "x"), ("properties", 7),
    ("tags", "a"), ("tags", 7),
]


@pytest.mark.parametrize("reason", [None, "supersede", "correction"])
@pytest.mark.parametrize(("field_name", "value"), _TYPE_VIOLATIONS)
def test_wrong_value_type_is_refused_without_touching_anything(field_name, value, reason):
    """(SS) ★ blocker — validate_patch 가 값의 **타입**을 안 봐서 _apply_patch 가
    실제로 변조한 **뒤에** 인덱스가 raise 했다. 호출자는 실패를 통보받는데 노드는
    오염된 채 남고, by_type 의 모든 버킷에서 사라져 복구조차 불가능했다."""
    g = Graph()
    g.add_node(id="n", label="N", type="class", properties={"keep": 1}, tags=["t"])
    before = g.get_node("n").to_dict()
    buckets_before = {k: set(v) for k, v in g.indices.by_type.items()}
    seen = _capture_events(g)

    with pytest.raises(ValueError):
        g.update_node("n", {"set": {field_name: value}}, reason=reason)

    assert g.get_node("n").to_dict() == before
    assert {k: set(v) for k, v in g.indices.by_type.items()} == buckets_before
    assert seen == []


def test_wrong_value_type_is_refused_on_edges_and_findings():
    """(SS) 같은 계약이 edge·finding 에도 적용된다."""
    g = Graph()
    g.add_node(id="a", label="A", type="class")
    g.add_node(id="b", label="B", type="class")
    g.add_edge(source="a", target="b", relation="calls")
    finding = g.add_finding(title="t")

    with pytest.raises(ValueError):
        g.update_edge("a", "b", "calls", "", {"set": {"weight": {}}})
    with pytest.raises(ValueError):
        g.update_edge("a", "b", "calls", "", {"set": {"directed": "yes"}})
    with pytest.raises(ValueError):
        g.update_finding(finding.finding_id, {"set": {"title": 7}})
    with pytest.raises(ValueError):
        g.update_finding(finding.finding_id, {"set": {"confidence": "high"}})

    assert g.edges[("a", "b", "calls", "")].weight == 1.0
    assert g.get_finding(finding.finding_id).title == "t"


def test_float_field_accepts_int_but_not_bool():
    """(SS) float 는 int 를 받되 bool 은 거부한다 — bool 이 int 서브클래스라
    명시 배제하지 않으면 플래그가 수치 필드에 조용히 들어간다."""
    g = Graph()
    g.add_node(id="a", label="A", type="class")
    g.add_node(id="b", label="B", type="class")
    g.add_edge(source="a", target="b", relation="calls")

    g.update_edge("a", "b", "calls", "", {"set": {"weight": 3}})
    assert g.edges[("a", "b", "calls", "")].weight == 3
    with pytest.raises(ValueError):
        g.update_edge("a", "b", "calls", "", {"set": {"weight": True}})


def test_every_declared_field_type_is_covered():
    """(SS) 새 필드가 생겼을 때 조용한 구멍이 되지 않게 — 선언 타입이 매핑에
    없으면 통과가 아니라 **오류**여야 한다."""
    from dataclasses import fields as dc_fields

    from visualizebetter.graph.core import _FIELD_TYPES, Edge, Finding, Node

    for cls in (Node, Edge, Finding):
        for f in dc_fields(cls):
            assert f.type in _FIELD_TYPES, (
                f"{cls.__name__}.{f.name} 의 선언 타입 {f.type!r} 이 _FIELD_TYPES 에 없다"
            )


def test_corruption_scenario_cannot_even_start():
    """(SS) ★ 복구 불가 시나리오가 애초에 성립하지 않음을 단언한다."""
    g = Graph()
    g.add_node(id="n", label="N", type="class")

    with pytest.raises(ValueError):
        g.update_node("n", {"set": {"type": {}}}, reason="supersede")

    # 오염이 없으므로 후속 동작이 전부 정상이다
    g.update_node("n", {"set": {"type": "service"}})
    assert g.indices.by_type["service"] == {"n"}
    g.add_node(id="n", label="N", type="class")          # upsert
    g.delete_node("n")
    assert "n" not in g.nodes


# --- WW: 보존할 것이 없으면 이력도 남기지 않는다 ---


@pytest.mark.parametrize("patch", [{}, {"remove": ["nope"]}, {"remove": ()}])
def test_no_op_supersede_cannot_evict_real_history(patch):
    """(WW) ★ 승인되는 patch 로도 LL 의 피해가 도달했다 — 빈 patch 나 없는 키
    remove 는 오타보다 흔한 LLM 실수인데, 무조건 append 가 {'prev': {}} 로 10칸을
    채워 진짜 supersession 을 밀어냈다."""
    g = Graph()
    g.add_node(id="n", label="ORIGINAL", type="class")
    g.update_node("n", {"set": {"label": "V2"}}, reason="supersede")

    for _ in range(MAX_SUPERSEDED_ENTRIES + 2):
        g.update_node("n", patch, reason="supersede")

    history = g.get_node("n").properties[SUPERSEDED_PROPERTY]
    assert len(history) == 1
    assert history[0]["prev"]["label"] == "ORIGINAL"


def test_empty_remove_tuple_is_still_accepted():
    """(WW) 빈 patch 를 거부하지는 않는다 — 문서화된 허용 동작 회귀."""
    g = Graph()
    g.add_node(id="n", label="N", type="class")
    g.update_node("n", {"remove": ()})
    g.update_node("n", {})
    assert g.get_node("n").label == "N"


# --- XX: _provenance 캡 ---


def test_provenance_is_bounded():
    """(XX) correction 반복이 properties 를 무한 성장시켰다 — 2000회에 160KB,
    마지막 WS 페이로드도 160KB, 총 7.81s(매회 전체 로그 deepcopy 라 O(n²))."""
    g = Graph()
    g.add_node(id="n", label="N", type="class")
    for i in range(200):
        g.update_node("n", {"set": {"label": f"v{i}"}}, reason="correction")

    log = g.get_node("n").properties[PROVENANCE_PROPERTY]
    assert len(log) <= MAX_PROVENANCE_ENTRIES
    assert len(json.dumps(g.get_node("n").properties)) < 32_000


# --- YY: 거부 시 undo/redo 불변 ---


def test_rejected_patch_leaves_undo_and_redo_untouched():
    """(YY) LL 의 4축 중 undo/redo 축. 거부된 호출이 스택 깊이를 바꾸거나 redo 를
    소거하면 사용자의 되돌리기 경로가 실패 호출로 망가진다."""
    g = Graph()
    g.add_node(id="n", label="N", type="class")
    g.update_node("n", {"set": {"label": "V2"}})
    g.undo()
    assert g.history.can_redo()

    depth = len(g.history.undo_stack)
    seen = _capture_events(g)
    for patch in ({"set": {"type": {}}}, {"set": {"labell": "x"}}, {"sett": {}}):
        with pytest.raises(ValueError):
            g.update_node("n", patch, reason="supersede")

    assert len(g.history.undo_stack) == depth   # undo 깊이 불변
    assert g.history.can_redo()           # redo 보존
    assert g.get_node("n").label == "N"    # undo 결과 유지
    assert seen == []


# --- [13-B] CH1 — 코어 무결성: 생성 경로 계약 / 이벤트 격리 / _citations 캡 ---


def _capture_events(g):
    """publish 는 handler(event) 로 1인자를 넘긴다 (RN7 YY 의 죽은 단언 참조)."""
    seen = []
    g.events.subscribe(lambda event: seen.append(event.op))
    return seen


_CREATE_VIOLATIONS = [
    ("type", {}), ("type", []), ("type", 7), ("type", True),
    ("label", {}), ("label", ["a"]), ("label", 7),
    ("id", 7), ("id", None), ("id", ["n"]),
    ("layer", 7), ("layer", []),
    ("ttl", True), ("ttl", "60"), ("ttl", {}),
    ("parent_id", 7), ("parent_id", []),
    ("tags", "a"), ("tags", 7),
]


@pytest.mark.parametrize(("field_name", "value"), _CREATE_VIOLATIONS)
def test_creation_path_refuses_the_same_values_update_refuses(field_name, value):
    """(CH1-1) ★ blocker — RN7 SS 는 update 만 닫았다. 같은 `{"type": {}}` 가
    add_node 로는 통과해 인덱스에서 TypeError 를 내고, 그 시점엔 dict 에 이미
    들어가 있어 SS 가 막으려던 복구 불가 상태가 그대로 재현됐다."""
    base = {"id": "n", "label": "N", "type": "class"}
    good = Graph()
    good.add_node(**base)  # 전제: 같은 인자 집합이 정상값으로는 통과한다

    g = Graph()
    with pytest.raises(ValueError):
        g.add_node(**{**base, field_name: value})
    assert g.nodes == {}
    assert dict(g.indices.by_type) == {}


def test_creation_path_refuses_bad_edges_and_findings():
    """(CH1-1) 같은 계약이 세 생성문 전부에 걸린다."""
    g = Graph()
    g.add_node(id="a", label="A", type="class")
    g.add_node(id="b", label="B", type="class")

    for bad in ({"relation": {}}, {"weight": {}}, {"directed": "yes"}, {"key": 7}):
        with pytest.raises(ValueError):
            g.add_edge(**{"source": "a", "target": "b", "relation": "calls", **bad})
    assert g.edges == {}

    for bad in ({"title": {}}, {"node_ids": 5}, {"evidence": "x"}, {"confidence": "high"}):
        kwargs = {"title": "t", **bad}
        with pytest.raises(ValueError):
            g.add_finding(**kwargs)
    assert g.findings == {}


def test_finding_size_check_types_before_len():
    """(CH1-1) `_check_finding_size` 가 len() 을 먼저 불러, dict title 은 len 1 로
    상한 아래를 통과하고 int node_ids 는 TypeError(→ ToolError 로 번역되지 않는
    부류)를 냈다. 타입을 먼저 본다."""
    g = Graph()
    with pytest.raises(ValueError, match="must be str"):
        g.add_finding(title={"a": 1})
    with pytest.raises(ValueError, match="must be list or tuple"):
        g.add_finding(title="t", node_ids=5)



def test_a_raising_subscriber_cannot_undo_a_committed_mutation():
    """(CH1-2) ★ 구독자 하나가 raise 하면 (a) 뒤 구독자들이 이벤트를 못 받고
    (b) 예외가 뮤테이션 호출자에게 전파돼 **이미 커밋된 변경**이 실패로 보고됐다.
    seq 는 이미 소모됐고 M1 에 resync 트리거가 없어 클라는 유실을 모른다."""
    g = Graph()
    g.add_node(id="n", label="N", type="class")

    delivered = []
    g.events.subscribe(lambda event: delivered.append(("first", event.op)))
    g.update_node("n", {"set": {"label": "V1"}})
    assert delivered == [("first", "node.update")]  # 전제: 팬아웃이 실제로 돈다

    def boom(event):
        raise RuntimeError("subscriber exploded")

    g.events.subscribe(boom)
    g.events.subscribe(lambda event: delivered.append(("third", event.op)))

    assert g.update_node("n", {"set": {"label": "V2"}}).label == "V2"
    assert g.get_node("n").label == "V2"
    assert ("third", "node.update") in delivered  # 뒤 구독자까지 도달했다


def test_the_citation_cap_refuses_instead_of_evicting():
    """(CH1-4) 세 예약 배열 중 _citations 만 무캡이었다 — cite() 는 append 후
    리스트 **전체**를 patch 로 발행하고 touch_node 는 매번 노드를 deepcopy 하므로
    XX 가 고친 것과 같은 O(n²) 경로다 (실측 2000건 = 배치 페이로드 214MB).

    ★ 캡을 **FIFO 가 아니라 거부**로 거는 이유: _citations 는 AI 가 도구로 못박은
    저작물이고(Finding.evidence 와 동일 성격 — 그쪽은 이미 초과 시 raise),
    README 는 "돌아왔을 때 검증 가능"을 약속한다. FIFO 는 **첫 근거**부터 지운다.
    게다가 예약키라 set/remove 가 둘 다 거부되므로 AI 는 캡에 닿은 뒤 스스로
    자리를 만들 수도 없다 — 볼 수도 되돌릴 수도 없는 손실이 된다."""
    g = Graph()
    g.add_node(id="n", label="N", type="class")
    for i in range(MAX_CITATIONS_ENTRIES):
        g.cite("n", f"https://example.test/{i}", "src")
    before = [dict(c) for c in g.get_node("n").properties[CITATIONS_PROPERTY]]
    assert len(before) == MAX_CITATIONS_ENTRIES  # 전제: 캡까지는 실제로 들어간다

    with pytest.raises(ValueError, match="maximum"):
        g.cite("n", "https://example.test/overflow", "src")

    after = g.get_node("n").properties[CITATIONS_PROPERTY]
    assert after == before               # 첫 근거가 그대로 남는다
    assert after[0]["url"].endswith("/0")


def test_a_refused_citation_leaves_no_trace():
    """(CH1-4) RN6 LL — 거부는 이력·이벤트·undo 에 흔적을 남기지 않는다.
    검사가 touch_node 보다 **앞**에 있어야 성립한다."""
    g = Graph()
    g.add_node(id="n", label="N", type="class")
    for i in range(MAX_CITATIONS_ENTRIES):
        g.cite("n", f"https://example.test/{i}", "src")

    seen = _capture_events(g)
    g.update_node("n", {"set": {"label": "N2"}})  # 전제: 캡처가 살아 있다
    assert seen == ["node.update"]
    seen.clear()
    depth_before = len(g.history.undo_stack)

    with pytest.raises(ValueError):
        g.cite("n", "https://example.test/overflow", "src")

    assert seen == []
    assert len(g.history.undo_stack) == depth_before

def test_rejected_creation_leaves_every_downstream_surface_working():
    """(CH1 완료검증) ★ 오염 시나리오가 **애초에 성립하지 않음**을 단언한다 —
    거부 이후 요약·정렬·삭제·clear·undo 가 전부 정상 동작해야 한다."""
    g = Graph()
    g.add_node(id="keep", label="Keep", type="class")
    seen = _capture_events(g)
    g.update_node("keep", {"set": {"label": "Kept"}})
    assert seen == ["node.update"]  # 전제: 이벤트 캡처가 살아 있다
    seen.clear()

    for bad in ({"id": "x", "label": "X", "type": {}}, {"id": 7, "label": "X", "type": "t"}):
        with pytest.raises(ValueError):
            g.add_node(**bad)
    with pytest.raises(ValueError):
        g.add_edge(source="keep", target="keep", relation={})
    with pytest.raises(ValueError):
        g.add_finding(title={"a": 1})

    assert seen == []  # 거부는 이벤트를 발행하지 않는다
    assert list(g.nodes) == ["keep"]
    assert g.indices.by_type["class"] == {"keep"}
    assert sorted(n.label for n in g.nodes.values()) == ["Kept"]
    g.undo()
    assert g.get_node("keep").label == "Keep"
    assert g.delete_node("keep")["ok"]
    g.clear_all()
    assert g.nodes == {}


class _Unhashable(str):
    """str 계약은 만족하되 인덱스에는 들어갈 수 없는 값.

    ★ CH1(1) 값 계약이 **볼 수 없는** 유일한 부류다 — 선언 타입이 str 이고
    isinstance 도 True 라, 인덱스만이 이걸 거부할 수 있다. 그래서 "인덱스가 먼저
    받아들인 뒤에 레코드가 커밋한다"는 순서가 타입 검사와 별개로 필요하다."""

    __hash__ = None  # type: ignore[assignment]


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(
            lambda g: g.add_node(id="fresh", label="X", type=_Unhashable("weird")),
            id="create",
        ),
        pytest.param(
            lambda g: g.add_node(id="n", label="Replacement", type=_Unhashable("weird")),
            id="merge-repush",
        ),
        pytest.param(
            lambda g: g.update_node("n", {"set": {"type": _Unhashable("weird")}}),
            id="update",
        ),
        pytest.param(
            lambda g: g.add_edge(source="n", target="m", relation=_Unhashable("weird")),
            id="edge",
        ),
    ],
)
def test_an_index_refusal_leaves_records_and_indices_agreeing(mutate):
    """(CH1-1) ★ 네 경로 전부. 이전에는 레코드를 먼저 바꾼 뒤 인덱스가 raise 해서,
    노드가 자기 type 을 든 채 by_type 에서 **통째로 사라졌다** — 그 뒤 요약·정렬·
    삭제·스냅샷·undo 가 전부 old_type 해싱에서 죽는 복구 불가 상태다. 호출자는
    실패를 통보받으므로 무엇이 깨졌는지도 모른다."""
    g = Graph()
    g.add_node(id="n", label="Original", type="class")
    g.add_node(id="m", label="M", type="class")
    g.add_edge(source="n", target="m", relation="calls")
    before_nodes = {k: v.to_dict() for k, v in g.nodes.items()}
    before_edges = {k: v.to_dict() for k, v in g.edges.items()}
    before_types = {k: set(v) for k, v in g.indices.by_type.items()}
    before_adj = {k: set(v) for k, v in g.indices.adjacency.items()}

    # [13-B] CH1b — 예전에는 인덱스가 TypeError 로 막았다(그래서 순서가 필요했다).
    # 이제는 정확 타입 게이트가 **문에서** ValueError 로 막아 인덱스까지 가지도
    # 않는다. 순서 불변식 자체는 아래 두 테스트가 따로 고정한다.
    with pytest.raises(ValueError):
        mutate(g)

    assert {k: v.to_dict() for k, v in g.nodes.items()} == before_nodes
    assert {k: v.to_dict() for k, v in g.edges.items()} == before_edges
    assert {k: set(v) for k, v in g.indices.by_type.items()} == before_types
    assert {k: set(v) for k, v in g.indices.adjacency.items()} == before_adj

    # 거부 이후에도 모든 후속 표면이 산다 (오염 시나리오가 성립하지 않는다)
    node = g.get_node("n")
    assert node.id in g.indices.by_type[node.type]
    g.update_node("n", {"set": {"type": "service"}})
    assert g.indices.by_type["service"] == {"n"}
    g.delete_node("n", cascade=True)
    assert "n" not in g.nodes and "n" not in g.indices.adjacency


def test_a_refused_retype_leaves_the_old_bucket_intact():
    """(CH1-1) 인덱스 연산은 all-or-nothing 이어야 한다. `retype_node` 는 옛
    버킷을 먼저 비웠기 때문에 새 버킷 쓰기가 실패하면 노드가 어느 버킷에도 없게
    됐다 — 실패한 재-push 가 노드를 타입 필터에서 영구히 지우는 경로다.

    이 불변식은 지금 두 겹으로 지켜진다(선행 해시 검사 + add-before-discard).
    둘 중 하나만 지우면 다른 하나가 가려서 이 테스트가 초록으로 남으므로, 뮤테이션
    검증은 **둘을 동시에** 되돌려서 했다 — 리포트 참조."""
    from visualizebetter.graph.indices import Indices

    indices = Indices()
    node = Graph().add_node(id="n", label="N", type="class")
    indices.add_node(node)
    assert indices.by_type["class"] == {"n"}  # 전제

    with pytest.raises(TypeError):
        indices.retype_node("n", "class", _Unhashable("weird"))  # 인덱스 직접 호출
    assert indices.by_type["class"] == {"n"}

    indices.retype_node("n", "class", "service")  # 정상 경로 회귀
    assert indices.by_type == {"service": {"n"}}


def test_a_failed_index_removal_leaves_the_record_in_place():
    """(CH1-3 개정) 삭제 방향도 같은 규칙이다 — "실패할 수 있는 쪽(인덱스)이
    먼저". record-first 였을 때는 인덱스 제거가 raise 하면 호출자는 실패를 받는데
    레코드는 이미 사라져 있었다(= 실패 통보인데 실제로는 삭제됨)."""
    g = Graph()
    g.add_node(id="n", label="N", type="class")
    g.add_node(id="m", label="M", type="class")
    g.add_edge(source="n", target="m", relation="calls")

    boom = RuntimeError("index removal failed")
    original = g.indices.remove_node

    def explode(node):
        raise boom

    g.indices.remove_node = explode
    try:
        with pytest.raises(RuntimeError):
            g.delete_node("n", cascade=True)
    finally:
        g.indices.remove_node = original

    assert "n" in g.nodes                       # 실패면 레코드는 남는다
    assert g.get_node("n").label == "N"
    g.delete_node("n", cascade=True)            # 정상 경로 회귀
    assert "n" not in g.nodes and "n" not in g.indices.adjacency


def test_index_registration_still_precedes_the_record_write():
    """(CH1b) 게이트가 생기면서 "인덱스가 실패하는 값"이 공개 API 로는 도달 불가가
    됐다 — 그렇다고 순서 보장을 지울 수는 없다. 다음 구멍이 열릴 때 마지막으로
    남는 방어이므로, 인덱스를 직접 실패시켜 순서를 계속 고정한다."""
    g = Graph()
    g.add_node(id="keep", label="K", type="class")
    before = dict(g.nodes)

    def explode(node):
        raise RuntimeError("index refused")

    g.indices.add_node = explode
    with pytest.raises(RuntimeError):
        g.add_node(id="ghost", label="G", type="class")
    assert dict(g.nodes) == before          # 인덱스가 먼저였으므로 dict 은 무변경

    def explode_edge(key, edge):
        raise RuntimeError("index refused")

    g.indices.add_edge = explode_edge
    with pytest.raises(RuntimeError):
        g.add_edge(source="keep", target="keep", relation="self")
    assert g.edges == {}


# --- [13-B] CH1b — 저장 가능성 게이트 ---


def _nest(depth):
    """평문 JSON 으로 도달하는 중첩 — Python 서브클래스가 전혀 필요 없다."""
    value = {"leaf": 1}
    for _ in range(depth):
        value = {"n": value}
    return value


SURROGATE = "bad" + chr(0xD800)

_UNSTORABLE = [
    pytest.param(
        lambda g: g.add_node(id="x", label="X", type="t", properties=_nest(900)),
        id="properties-900-deep",
    ),
    pytest.param(lambda g: g.add_node(id="x", label=SURROGATE, type="t"), id="surrogate-label"),
    pytest.param(lambda g: g.add_node(id=SURROGATE, label="X", type="t"), id="surrogate-id"),
    pytest.param(
        lambda g: g.add_node(id="x", label="X", type="t", properties={"k": SURROGATE}),
        id="surrogate-in-properties",
    ),
    pytest.param(
        lambda g: g.add_edge(source="keep", target="keep", relation="r", weight=float("nan")),
        id="nan-weight",
    ),
    pytest.param(
        lambda g: g.add_edge(source="keep", target="keep", relation="r", weight=float("inf")),
        id="inf-weight",
    ),
    pytest.param(lambda g: g.add_finding(title="T", node_ids=[{"a": 1}]), id="node_ids-dict"),
    pytest.param(lambda g: g.add_finding(title="T", evidence=[7]), id="evidence-int"),
    pytest.param(lambda g: g.add_node(id="x", label="X", type="t", tags=[None]), id="tags-none"),
    pytest.param(
        lambda g: g.update_node("keep", {"set": {"label": SURROGATE}}), id="update-surrogate"
    ),
    pytest.param(
        lambda g: g.update_node("keep", {"set": {"properties": _nest(900)}}),
        id="update-deep-properties",
    ),
]


@pytest.mark.parametrize("mutate", _UNSTORABLE)
def test_unstorable_values_are_refused_at_the_door(mutate):
    """(CH1b) ★ 부류를 하나씩 막는 걸 그만두고 **저장 가능성**을 직접 묻는다.

    여기 값들의 공통점은 '평범한 JSON 으로 도달한다'는 것이다 — Python 서브클래싱은
    필요 없다. 900 중첩은 1.9KB 평문(1MB import 캡 한참 아래)인데 노드를 커밋시킨 뒤
    delete·cascade·update·재push·cite·clear_all 을 **전부 영구히** 죽였고(history 의
    deepcopy 에서 RecursionError), 그 노드는 어떤 도구로도 지울 수 없었다.
    서로게이트 하나는 /graph.json 을 500 으로 만들고 자동 스냅샷을 매 틱 실패시켜
    디스크에 스냅샷이 0개가 되게 했다."""
    g = Graph()
    g.add_node(id="keep", label="Keep", type="class")
    before_nodes = {k: v.to_dict() for k, v in g.nodes.items()}
    before_edges = {k: v.to_dict() for k, v in g.edges.items()}
    before_findings = {k: v.to_dict() for k, v in g.findings.items()}
    seen = _capture_events(g)

    with pytest.raises(ValueError):
        mutate(g)

    # (b) 그래프 완전 무변경
    assert {k: v.to_dict() for k, v in g.nodes.items()} == before_nodes
    assert {k: v.to_dict() for k, v in g.edges.items()} == before_edges
    assert {k: v.to_dict() for k, v in g.findings.items()} == before_findings
    assert seen == []

    # (c) ★ 핵심 단언 — 오염이 애초에 성립하지 않으므로 모든 후속 표면이 산다
    assert json.dumps(
        {"nodes": [n.to_dict() for n in g.nodes.values()]},
        ensure_ascii=False,
        allow_nan=False,
    )
    g.add_node(id="after", label="After", type="class")
    g.cite("keep", "https://example.test/1", "src")
    g.update_node("keep", {"set": {"label": "Kept"}})
    g.add_edge(source="keep", target="after", relation="calls")
    g.undo()
    g.delete_node("after", cascade=True)
    g.clear_all()
    assert g.nodes == {} and g.edges == {}


def test_the_capture_of_a_refused_value_is_not_a_dead_assertion():
    """(CH1b) 위 테스트가 '전제에 도달했는지'를 스스로 증명한다 — 같은 모양의
    **정상** 값은 실제로 들어가야 한다."""
    g = Graph()
    g.add_node(id="keep", label="Keep", type="class")
    seen = _capture_events(g)
    g.add_node(id="ok", label="OK", type="t", properties=_nest(4), tags=["a"])
    g.add_node(id="a", label="A", type="t")
    g.add_edge(source="keep", target="a", relation="r", weight=1.5)
    g.add_finding(title="T", node_ids=["keep"], evidence=["https://x"])
    assert seen == ["node.add", "node.add", "edge.add", "finding.add"]
    assert g.get_node("ok").properties == _nest(4)


def test_exact_types_retire_the_subclass_family():
    """(CH1b 게이트 1) isinstance 를 쓰는 한 계약은 부류를 하나씩 쫓아다닌다.
    `type(v) is str` 로 바꾸면 hash 파괴·eq 파괴·미래의 무엇이든 한 번에 닫힌다."""

    class NoHash(str):
        __hash__ = None  # type: ignore[assignment]

    class NoEq(str):
        def __eq__(self, other):
            return False

        __hash__ = str.__hash__

    g = Graph()
    for bad in (NoHash("weird"), NoEq("weird")):
        with pytest.raises(ValueError, match="subclass"):
            g.add_node(id="x", label="X", type=bad)
    assert g.nodes == {}


def test_bool_is_still_refused_where_a_number_belongs():
    """(CH1b 게이트 1) 정확 타입이 되면서 bool/int 특례가 사라졌다 —
    `type(True) is bool` 이라 int 필드의 튜플에 애초에 없다. 회귀로 고정한다."""
    g = Graph()
    with pytest.raises(ValueError):
        g.add_node(id="x", label="X", type="t", ttl=True)
    g.add_node(id="a", label="A", type="t")
    g.add_node(id="b", label="B", type="t")
    with pytest.raises(ValueError):
        g.add_edge(source="a", target="b", relation="r", weight=True)
    g.add_edge(source="a", target="b", relation="r", weight=3)  # int 는 float 필드 OK
    assert g.edges[("a", "b", "r", "")].weight == 3


def test_the_depth_check_survives_what_it_rejects():
    """(CH1b 게이트 4) 깊이 검사가 재귀였다면 그 자신이 첫 희생자가 된다 —
    막으려는 값이 정확히 스택을 터뜨리는 값이기 때문이다. 순환 참조도 깊이 상한이
    함께 막는다(무한 순회가 아니라 32 단계에서 끝난다)."""
    from visualizebetter.graph.core import MAX_CALLER_VALUE_DEPTH, check_storable

    g = Graph()
    with pytest.raises(ValueError, match="nests deeper"):
        g.add_node(id="x", label="X", type="t", properties=_nest(5000))

    cycle: dict = {}
    cycle["self"] = cycle
    with pytest.raises(ValueError, match="nests deeper"):
        check_storable(Node, {"properties": cycle})

    g.add_node(id="ok", label="OK", type="t", properties=_nest(MAX_CALLER_VALUE_DEPTH - 2))
    assert "ok" in g.nodes


def test_a_record_over_the_size_ceiling_is_refused():
    """(CH1b 게이트 4) [23-B] 는 finding 에만 크기 불변식을 줬다 — 노드/엣지의
    properties 는 깊이도 크기도 무제한이었다."""
    from visualizebetter.graph.core import MAX_VALUE_BYTES

    g = Graph()
    with pytest.raises(ValueError, match="serialised"):
        g.add_node(id="x", label="X", type="t", properties={"blob": "y" * (MAX_VALUE_BYTES + 10)})
    assert g.nodes == {}


def test_set_null_converges_across_the_three_update_tools():
    """(CH1b 결함 6) `{"set": null}` 이 노드에서는 AttributeError(→ tool 의
    except ValueError 를 탈출해 맨 트레이스백), 엣지·finding 에서는 **성공한
    no-op** 이었다. RN6 NN(1) 이 닫은 '세 tool, 한 입력, 세 답'이 부활한 것이고
    성공한 no-op 은 `{"sett": ...}` 와 같은 조용한 소실 부류다."""
    g = Graph()
    g.add_node(id="a", label="A", type="t")
    g.add_node(id="b", label="B", type="t")
    g.add_edge(source="a", target="b", relation="r")
    fid = g.add_finding(title="T").finding_id
    before = (g.get_node("a").to_dict(), g.get_edge("a", "b", "r").to_dict(),
              g.get_finding(fid).to_dict())

    for patch in ({"set": None}, {"set": []}, {"set": "x"}, {"remove": None}):
        for call in (
            lambda p=patch: g.update_node("a", p),
            lambda p=patch: g.update_edge("a", "b", "r", "", p),
            lambda p=patch: g.update_finding(fid, p),
        ):
            with pytest.raises(ValueError):
                call()

    assert (g.get_node("a").to_dict(), g.get_edge("a", "b", "r").to_dict(),
            g.get_finding(fid).to_dict()) == before


def test_a_non_finite_number_is_named_by_its_field():
    """(CH1b 게이트 2) 게이트 (5) 의 allow_nan=False 도 NaN 을 막지만, 그 메시지는
    "Out of range float values are not JSON compliant" 라 **어느 필드인지 말하지
    않는다**. AI 가 고칠 수 있으려면 필드 이름이 있어야 하므로 (2) 를 따로 둔다 —
    두 겹이 서로를 가리는 관계이고, 구분되는 실제 차이가 여기다."""
    g = Graph()
    g.add_node(id="a", label="A", type="class")
    for bad in (float("nan"), float("inf"), float("-inf")):
        with pytest.raises(ValueError, match="'weight' must be a finite number"):
            g.add_edge(source="a", target="a", relation="r", weight=bad)
    assert g.edges == {}


def _depth_of(value):
    """CH1c A — 맵 전체의 실제 중첩 깊이(서버 예약 배열 포함)."""
    if type(value) is dict:
        return max([_depth_of(v) + 1 for v in value.values()] or [0])
    if type(value) is list:
        return max([_depth_of(v) + 1 for v in value] or [0])
    return 0


# --- [13-B] CH1c — 상한의 적용 대상 ---


def test_the_server_own_bookkeeping_cannot_brick_an_accepted_value():
    """(CH1c A) ★ 핵심 단언. 깊이 32 를 **레코드 전체**에 재면 서버 자신의 기록이
    노드를 벽돌로 만든다: 호출자 값 깊이 31 → supersede 1회 → properties 맵 깊이
    35(오버헤드 정확히 +4: properties → _superseded → 엔트리 → 'prev' → 값).
    즉 생성 게이트가 **통과시킨** 값이 갱신·복원 게이트에서 거부되고, 예약키라
    remove 도 안 되므로 호출자는 스스로 복구할 수 없다."""
    deep = _nest(29)  # properties 맵 안에서 깊이 31
    g = Graph()
    g.add_node(id="n", label="N", type="class", properties={"deep": deep})
    assert _depth_of(g.get_node("n").properties) == 31  # 전제

    g.update_node("n", {"set": {"properties": {"deep": deep, "x": 1}}}, reason="supersede")
    g.update_node("n", {"set": {"properties": {"deep": deep, "y": 2}}}, reason="supersede")
    grown = _depth_of(g.get_node("n").properties)
    assert grown == 35, f"서버 오버헤드가 +4 가 아니다: {grown}"

    # (a) 갱신이 계속 된다 (b) cite 가 된다
    g.update_node("n", {"set": {"label": "N2"}})
    g.cite("n", "https://example.test/1", "src")
    assert g.get_node("n").label == "N2"
    assert len(g.get_node("n").properties[CITATIONS_PROPERTY]) == 1


def test_the_caller_depth_cap_and_the_structural_cap_are_different_constants():
    """(CH1c A) 두 상한이 같은 상수면 위 시나리오가 다시 성립한다."""
    from visualizebetter.graph.core import MAX_CALLER_VALUE_DEPTH, MAX_STRUCTURE_DEPTH

    assert MAX_CALLER_VALUE_DEPTH == 32
    assert MAX_STRUCTURE_DEPTH == 128
    assert MAX_STRUCTURE_DEPTH > MAX_CALLER_VALUE_DEPTH + 4  # 서버 오버헤드 여유


def test_reserved_arrays_do_not_spend_the_callers_byte_budget():
    """(CH1c B) 같은 함정의 바이트 버전. 예약 배열(_citations 100건 ≈ 15.5KB,
    _provenance 50, _superseded 10)이 호출자 예산을 쓰면, 정당한 properties 를 가진
    노드가 상한에 닿는 순간 cite() 가 거부된다 — 그리고 CH1 에서 확정한 대로 cite 는
    evict 가 아니라 refuse 이고 예약키는 remove 도 안 되므로, AI 는 근거를 남길 수도
    자리를 만들 수도 없게 된다."""
    from visualizebetter.graph.core import MAX_CALLER_PROPERTIES_BYTES

    g = Graph()
    # 호출자 상한에 근접한 정당한 properties
    g.add_node(id="n", label="N", type="class",
               properties={"snippet": "x" * (MAX_CALLER_PROPERTIES_BYTES - 200)})
    for i in range(MAX_CITATIONS_ENTRIES):
        g.cite("n", f"https://example.test/evidence/{i:04d}", f"source {i}")
    for i in range(30):
        g.update_node("n", {"set": {"label": f"v{i}"}}, reason="correction")

    record = len(json.dumps(g.get_node("n").to_dict(), ensure_ascii=False).encode("utf-8"))
    assert record > MAX_CALLER_PROPERTIES_BYTES  # 전제: 서버 기록이 실제로 얹혔다

    # 예약 배열이 얹혔어도 호출자 경로는 계속 산다
    g.update_node("n", {"set": {"label": "still-editable"}})
    with pytest.raises(ValueError, match="maximum"):
        g.cite("n", "https://example.test/one-more", "src")   # 건수 상한 거부는 유지


def test_the_callers_own_properties_are_still_capped():
    """(CH1c B) 분리가 '호출자 상한이 사라졌다'가 되면 안 된다."""
    from visualizebetter.graph.core import MAX_CALLER_PROPERTIES_BYTES

    g = Graph()
    with pytest.raises(ValueError, match="caller-supplied properties"):
        g.add_node(id="n", label="N", type="class",
                   properties={"blob": "x" * (MAX_CALLER_PROPERTIES_BYTES + 100)})
    assert g.nodes == {}


# --- CH1c D — float 정규화 ---


def test_an_int_in_a_float_field_is_stored_as_a_float():
    """(CH1c D) int 수용은 유지한다 — push_batch 는 _EdgeSpecBounds 검증 결과를
    버리고 raw spec 을 넘기고 import 는 JSON 을 그대로 넘기므로 weight=7 이 실제로
    도달한다. 문제는 정규화가 없어 SQLite REAL 이 7 → 7.0 으로 바꾸는 것이었다:
    저장 전 그래프와 load 결과의 타입이 갈리고 /graph.json 바이트도 달라진다."""
    g = Graph()
    g.add_node(id="a", label="A", type="t")
    g.add_node(id="b", label="B", type="t")
    edge = g.add_edge(source="a", target="b", relation="r", weight=7)
    finding = g.add_finding(title="T", confidence=1)

    assert type(edge.weight) is float and edge.weight == 7.0
    assert type(finding.confidence) is float and finding.confidence == 1.0

    g.update_edge("a", "b", "r", "", {"set": {"weight": 3}})
    assert type(g.edges[("a", "b", "r", "")].weight) is float


# --- CH1c E — 에러 문구 ---


def test_gate_errors_name_the_field_the_path_and_the_violation():
    """(CH1c E) 게이트는 항상 ValueError 하위로 올리고, 원시 메시지('position 23',
    'Circular reference detected')처럼 **어느 필드인지 말하지 않는** 문구를 그대로
    새게 두지 않는다."""
    g = Graph()

    with pytest.raises(ValueError, match=r"at properties\.snippet"):
        g.add_node(id="n", label="N", type="t",
                   properties={"ok": 1, "snippet": "bad" + chr(0xD800)})

    with pytest.raises(ValueError, match=r"element \[1\] is int"):
        g.add_node(id="n", label="N", type="t", tags=["ok", 7])

    cycle: dict = {"a": 1}
    cycle["self"] = cycle
    with pytest.raises(ValueError, match="nests deeper"):
        g.add_node(id="n", label="N", type="t", properties=cycle)

    assert g.nodes == {}


def test_a_nodes_supersession_log_is_bounded_by_bytes_too():
    """(CH1c B) MAX_SUPERSEDED_ENTRIES 는 **건수**만 묶는다 — 엔트리의 'prev' 는
    보관하는 값만큼 크므로, 32KB 속성을 10회 supersede 하면 아카이브 323,555 B /
    레코드 356KB 가 된다. 레코드 상한의 5배가 전 스냅샷·전 wire 페이로드에 실리고,
    전부 지원되는 호출만으로 만들어진다. Finding 은 [24-C] 이래 같은 상한이 있었고,
    임의 properties 를 지는 노드에는 덜 필요한 게 아니라 더 필요했다.

    ★ FIFO 인 이유: 이건 **서버**의 부기이므로 잃는 것이 로그 해상도뿐이다
    (거부는 AI 가 저작한 것 — _citations, Finding.evidence — 에 쓴다)."""
    from visualizebetter.graph.core import MAX_NODE_SUPERSEDED_BYTES

    g = Graph()
    big = "y" * 30_000
    g.add_node(id="n", label="N", type="class", properties={"blob": big})
    for i in range(12):
        g.update_node("n", {"set": {"properties": {"blob": big[: -i - 1]}}},
                      reason="supersede")

    archive = g.get_node("n").properties[SUPERSEDED_PROPERTY]
    assert archive, "supersede 가 실제로 기록되지 않았다 — 죽은 단언"
    size = len(json.dumps(archive, ensure_ascii=False).encode("utf-8"))
    assert size <= MAX_NODE_SUPERSEDED_BYTES + 30_000  # 마지막 1건은 남긴다
    assert len(json.dumps(g.get_node("n").to_dict(), ensure_ascii=False)) < 200_000


def test_an_import_gate_error_says_which_item():
    """(CH1c E) payload 는 수천 건을 실을 수 있다 — 위치가 없으면 호출자가 자기
    JSON 을 이분 탐색해야 한다."""
    from fastmcp.exceptions import ToolError

    from visualizebetter.mcp_server import import_payload

    g = Graph()
    nodes = [{"id": f"n{i}", "label": "L", "type": "t"} for i in range(40)]
    nodes[39]["tags"] = [1]
    with pytest.raises(ToolError, match=r"nodes\[39\] \(id='n39'\)"):
        import_payload(g, {"nodes": nodes}, merge=True)
    assert g.nodes == {}

    edges = [{"source": "a", "target": "b", "relation": "r", "weight": float("nan")}]
    with pytest.raises(ToolError, match=r"edges\[0\]"):
        import_payload(g, {"nodes": [{"id": "a", "label": "A", "type": "t"},
                                     {"id": "b", "label": "B", "type": "t"}],
                            "edges": edges}, merge=True)
    assert g.nodes == {}
