"""Completion verification for the Graph Core prerequisite ([20] Day 3-4).

Covers: add/get roundtrip, delete_node cascade rule ([5-A]), dirty flag ([23-C]),
event publication ([8-C]), edge 4-tuple identity ([4-B]), placeholder
auto-creation ([5-A]).
"""

import pytest

from visualizebetter.graph.core import (
    CITATIONS_PROPERTY,
    PROVENANCE_PROPERTY,
    SUPERSEDED_PROPERTY,
    PLACEHOLDER_PROPERTY,
    PLACEHOLDER_TYPE,
    Graph,
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
