"""Completion verification for TASK 1 — Finding 데이터 모델 ([23-B], [5-G]).

Covers: add -> get roundtrip, list filters (min_confidence / node_id / layer),
anchor cleanup on delete_node, dirty flag ([23-C]), finding.* events ([8-C]).
"""

import pytest

from visualizebetter.graph.core import (
    MAX_FINDING_BODY_CHARS,
    MAX_FINDING_EVIDENCE,
    MAX_FINDING_NODE_IDS,
    MAX_FINDING_TAGS,
    MAX_FINDING_TITLE_CHARS,
    Graph,
)


@pytest.fixture
def graph():
    return Graph(name="test")


@pytest.fixture
def events(graph):
    captured = []
    graph.events.subscribe(captured.append)
    return captured


# --- add -> get roundtrip ---


def test_add_finding_get_finding_roundtrip(graph):
    finding = graph.add_finding(title="결제 실패의 핵심 경로")

    assert graph.get_finding(finding.finding_id) is finding
    assert finding.title == "결제 실패의 핵심 경로"


def test_add_finding_defaults_match_spec(graph):
    finding = graph.add_finding(title="t")

    assert finding.body == ""
    assert finding.node_ids == []
    assert finding.confidence == 0.8
    assert finding.evidence == []
    assert finding.layer is None
    assert finding.tags == []
    assert finding.created_at and finding.updated_at


def test_add_finding_all_fields_roundtrip(graph):
    finding = graph.add_finding(
        title="gold",
        body="상세 근거",
        node_ids=["a", "b"],
        confidence=0.95,
        evidence=["trace://0x1400", "https://example.test/doc"],
        layer="claude-session-abc",
        tags=["auth"],
        created_by="claude",
    )

    stored = graph.get_finding(finding.finding_id)
    assert stored.body == "상세 근거"
    assert stored.node_ids == ["a", "b"]
    assert stored.confidence == 0.95
    assert stored.evidence == ["trace://0x1400", "https://example.test/doc"]
    assert stored.layer == "claude-session-abc"
    assert stored.tags == ["auth"]
    assert stored.created_by == "claude"
    assert graph.layers == ["claude-session-abc"]


def test_get_finding_missing_returns_none(graph):
    assert graph.get_finding("nope") is None


def test_finding_id_is_unique_per_call(graph):
    """[5-G]: idempotent 아님 — 매 호출 = 새 finding."""
    first = graph.add_finding(title="same")
    second = graph.add_finding(title="same")

    assert first.finding_id != second.finding_id
    assert len(graph.findings) == 2


def test_add_finding_copies_sequence_arguments(graph):
    node_ids = ["a"]
    finding = graph.add_finding(title="t", node_ids=node_ids)

    node_ids.append("b")

    assert finding.node_ids == ["a"]


def test_findings_accepts_tuple_defaults(graph):
    finding = graph.add_finding(title="t", node_ids=("a",), evidence=("e",), tags=("x",))

    assert finding.node_ids == ["a"]
    assert finding.evidence == ["e"]
    assert finding.tags == ["x"]


# --- [23-B] 크기 불변식 ---
#
# Enforced in core, not only at the MCP boundary: adapters / import / snapshot
# load call Graph Core directly ([5-E], [12]). Bounding a finding at creation is
# what lets get_finding return gold whole instead of truncating it on read.


def _sized(field: str, count: int):
    if field in ("title", "body"):
        return "글" * count
    return [f"{field[0]}{i}" for i in range(count)]


_BOUNDED_FIELDS = [
    ("title", MAX_FINDING_TITLE_CHARS),
    ("body", MAX_FINDING_BODY_CHARS),
    ("node_ids", MAX_FINDING_NODE_IDS),
    ("evidence", MAX_FINDING_EVIDENCE),
    ("tags", MAX_FINDING_TAGS),
]
_FIELD_IDS = [f[0] for f in _BOUNDED_FIELDS]


@pytest.mark.parametrize("field,cap", _BOUNDED_FIELDS, ids=_FIELD_IDS)
def test_add_finding_rejects_oversize(graph, field, cap):
    with pytest.raises(ValueError):
        graph.add_finding(**{"title": "t", field: _sized(field, cap + 1)})


@pytest.mark.parametrize("field,cap", _BOUNDED_FIELDS, ids=_FIELD_IDS)
def test_add_finding_accepts_at_the_limit(graph, field, cap):
    assert graph.add_finding(**{"title": "t", field: _sized(field, cap)}) is not None


def test_rejected_finding_is_not_stored(graph):
    with pytest.raises(ValueError):
        graph.add_finding(title="t", body="본" * (MAX_FINDING_BODY_CHARS + 1))

    assert graph.findings == {}


def test_rejected_finding_does_not_set_dirty_flag(graph):
    graph.clear_dirty()

    with pytest.raises(ValueError):
        graph.add_finding(title="t", body="본" * (MAX_FINDING_BODY_CHARS + 1))

    assert graph.dirty is False


def test_rejected_finding_publishes_nothing(graph, events):
    with pytest.raises(ValueError):
        graph.add_finding(title="t", body="본" * (MAX_FINDING_BODY_CHARS + 1))

    assert events == []


def test_update_finding_rejects_oversize_patch(graph):
    finding = graph.add_finding(title="t")

    with pytest.raises(ValueError):
        graph.update_finding(
            finding.finding_id,
            {"set": {"node_ids": [f"n{i}" for i in range(MAX_FINDING_NODE_IDS + 1)]}},
        )


def test_rejected_patch_leaves_the_finding_untouched(graph):
    """The size check runs before the patch applies, so nothing is half-written."""
    finding = graph.add_finding(title="original", node_ids=["a"])

    with pytest.raises(ValueError):
        graph.update_finding(
            finding.finding_id,
            {
                "set": {
                    "title": "changed",
                    "tags": [f"t{i}" for i in range(MAX_FINDING_TAGS + 1)],
                }
            },
        )

    assert finding.title == "original"
    assert finding.tags == []
    assert finding.node_ids == ["a"]


def test_update_finding_accepts_a_patch_at_the_limit(graph):
    finding = graph.add_finding(title="t")

    updated = graph.update_finding(
        finding.finding_id,
        {"set": {"evidence": [f"e{i}" for i in range(MAX_FINDING_EVIDENCE)]}},
    )

    assert len(updated.evidence) == MAX_FINDING_EVIDENCE


# --- findings are a first-class collection, not graph nodes ([23-B]) ---


def test_findings_do_not_enter_the_graph_topology(graph):
    graph.add_node(id="a", label="A", type="class")

    graph.add_finding(title="gold", node_ids=["a"])

    assert len(graph.nodes) == 1
    assert graph.edges == {}
    assert graph.indices.nodes_of_type("finding") == set()
    assert graph.indices.edges_of("a") == set()


# --- list_findings ([5-G]) ---


def test_list_findings_returns_page_and_total(graph):
    for i in range(3):
        graph.add_finding(title=f"f{i}")

    page, total = graph.list_findings()

    assert len(page) == 3
    assert total == 3


def test_list_findings_filters_by_min_confidence(graph):
    graph.add_finding(title="low", confidence=0.2)
    graph.add_finding(title="mid", confidence=0.5)
    graph.add_finding(title="high", confidence=0.9)

    page, total = graph.list_findings(min_confidence=0.5)

    assert [f.title for f in page] == ["mid", "high"]
    assert total == 2


def test_list_findings_filters_by_node_id(graph):
    graph.add_finding(title="anchored", node_ids=["a", "b"])
    graph.add_finding(title="elsewhere", node_ids=["c"])
    graph.add_finding(title="unanchored")

    page, total = graph.list_findings(node_id="a")

    assert [f.title for f in page] == ["anchored"]
    assert total == 1


def test_list_findings_filters_by_layer(graph):
    graph.add_finding(title="claude", layer="claude-1")
    graph.add_finding(title="gpt", layer="gpt-1")

    page, total = graph.list_findings(layer="gpt-1")

    assert [f.title for f in page] == ["gpt"]
    assert total == 1


def test_list_findings_filters_combine(graph):
    graph.add_finding(title="hit", layer="l1", confidence=0.9, node_ids=["a"])
    graph.add_finding(title="wrong_layer", layer="l2", confidence=0.9, node_ids=["a"])
    graph.add_finding(title="low_conf", layer="l1", confidence=0.1, node_ids=["a"])
    graph.add_finding(title="wrong_node", layer="l1", confidence=0.9, node_ids=["z"])

    page, total = graph.list_findings(layer="l1", min_confidence=0.5, node_id="a")

    assert [f.title for f in page] == ["hit"]
    assert total == 1


def test_list_findings_paginates_with_limit_and_offset(graph):
    for i in range(5):
        graph.add_finding(title=f"f{i}")

    page, total = graph.list_findings(limit=2, offset=1)

    assert [f.title for f in page] == ["f1", "f2"]
    assert total == 5, "total counts all matches, before limit/offset"


def test_list_findings_default_limit_is_50(graph):
    for i in range(60):
        graph.add_finding(title=f"f{i}")

    page, total = graph.list_findings()

    assert len(page) == 50
    assert total == 60


def test_list_findings_empty_graph(graph):
    assert graph.list_findings() == ([], 0)


# --- update_finding ([5-G]) ---


def test_update_finding_sets_listed_fields(graph):
    finding = graph.add_finding(title="old", confidence=0.3)

    updated = graph.update_finding(
        finding.finding_id,
        {"set": {"title": "new", "body": "b", "confidence": 0.9, "tags": ["t"]}},
    )

    assert updated.title == "new"
    assert updated.body == "b"
    assert updated.confidence == 0.9
    assert updated.tags == ["t"]


def test_update_finding_can_rewrite_anchors(graph):
    finding = graph.add_finding(title="t", node_ids=["a"])

    updated = graph.update_finding(finding.finding_id, {"set": {"node_ids": ["b", "c"]}})

    assert updated.node_ids == ["b", "c"]


def test_update_finding_rejects_server_managed_fields(graph):
    finding = graph.add_finding(title="t")

    with pytest.raises(ValueError):
        graph.update_finding(finding.finding_id, {"set": {"finding_id": "forged"}})

    with pytest.raises(ValueError):
        graph.update_finding(finding.finding_id, {"set": {"created_at": "1999-01-01"}})


def test_update_finding_rejects_unknown_fields(graph):
    finding = graph.add_finding(title="t")

    with pytest.raises(ValueError):
        graph.update_finding(finding.finding_id, {"set": {"nonsense": 1}})


def test_update_finding_rejects_remove_since_finding_has_no_properties(graph):
    """[23-B] Finding has no properties map, so [5-A]'s remove has no target."""
    finding = graph.add_finding(title="t")

    with pytest.raises(ValueError):
        graph.update_finding(finding.finding_id, {"remove": ["anything"]})


def test_update_finding_bumps_updated_at(graph):
    finding = graph.add_finding(title="t")
    created = finding.updated_at

    graph.update_finding(finding.finding_id, {"set": {"title": "t2"}})

    assert finding.updated_at >= created


def test_update_finding_missing_raises(graph):
    with pytest.raises(KeyError):
        graph.update_finding("nope", {"set": {"title": "x"}})


# --- delete_finding ([5-G]) ---


def test_delete_finding_removes_it(graph):
    finding = graph.add_finding(title="t")

    assert graph.delete_finding(finding.finding_id) == {"ok": True}
    assert graph.get_finding(finding.finding_id) is None
    assert graph.findings == {}


def test_delete_finding_missing_raises(graph):
    with pytest.raises(KeyError):
        graph.delete_finding("nope")


# --- anchor cleanup on delete_node (TASK 1 요구) ---


def test_delete_node_removes_that_id_from_anchoring_findings(graph):
    graph.add_node(id="a", label="A", type="class")
    graph.add_node(id="b", label="B", type="class")
    finding = graph.add_finding(title="gold", node_ids=["a", "b"])

    graph.delete_node("a")

    assert finding.node_ids == ["b"]


def test_finding_survives_losing_its_last_anchor(graph):
    graph.add_node(id="a", label="A", type="class")
    finding = graph.add_finding(title="gold", node_ids=["a"])

    graph.delete_node("a")

    assert graph.get_finding(finding.finding_id) is finding
    assert finding.node_ids == []
    assert finding.title == "gold"


def test_delete_node_leaves_other_findings_untouched(graph):
    graph.add_node(id="a", label="A", type="class")
    graph.add_node(id="z", label="Z", type="class")
    anchored = graph.add_finding(title="anchored", node_ids=["a"])
    other = graph.add_finding(title="other", node_ids=["z"])

    graph.delete_node("a")

    assert anchored.node_ids == []
    assert other.node_ids == ["z"]


def test_cascade_delete_also_cleans_anchors(graph):
    graph.add_node(id="a", label="A", type="class")
    graph.add_node(id="b", label="B", type="class")
    graph.add_edge(source="a", target="b", relation="field")
    finding = graph.add_finding(title="gold", node_ids=["a", "b"])

    graph.delete_node("a", cascade=True)

    assert finding.node_ids == ["b"]


def test_refused_delete_does_not_touch_anchors(graph):
    graph.add_node(id="a", label="A", type="class")
    graph.add_node(id="b", label="B", type="class")
    graph.add_edge(source="a", target="b", relation="field")
    finding = graph.add_finding(title="gold", node_ids=["a", "b"])

    result = graph.delete_node("a")

    assert result["ok"] is False
    assert finding.node_ids == ["a", "b"]


def test_delete_node_updates_the_node_id_filter_of_list_findings(graph):
    graph.add_node(id="a", label="A", type="class")
    graph.add_finding(title="gold", node_ids=["a"])

    graph.delete_node("a")

    assert graph.list_findings(node_id="a") == ([], 0)


# --- dirty flag ([23-C]) ---


def test_add_finding_sets_dirty_flag(graph):
    graph.add_finding(title="t")

    assert graph.dirty is True


def test_update_finding_sets_dirty_flag(graph):
    finding = graph.add_finding(title="t")
    graph.clear_dirty()

    graph.update_finding(finding.finding_id, {"set": {"title": "t2"}})

    assert graph.dirty is True


def test_delete_finding_sets_dirty_flag(graph):
    finding = graph.add_finding(title="t")
    graph.clear_dirty()

    graph.delete_finding(finding.finding_id)

    assert graph.dirty is True


def test_list_findings_does_not_set_dirty_flag(graph):
    graph.add_finding(title="t")
    graph.clear_dirty()

    graph.list_findings()

    assert graph.dirty is False


# --- events ([8-C]) ---


def test_add_finding_publishes_finding_add_with_full_finding(graph, events):
    finding = graph.add_finding(title="t", node_ids=["a"])

    assert events[-1].op == "finding.add"
    assert events[-1].data == finding.to_dict()


def test_update_finding_publishes_finding_update_with_id_and_patch(graph, events):
    finding = graph.add_finding(title="t")
    patch = {"set": {"confidence": 0.1}}

    graph.update_finding(finding.finding_id, patch)

    assert events[-1].op == "finding.update"
    assert events[-1].data == {"finding_id": finding.finding_id, "patch": patch}


def test_delete_finding_publishes_finding_delete_with_id(graph, events):
    finding = graph.add_finding(title="t")

    graph.delete_finding(finding.finding_id)

    assert events[-1].op == "finding.delete"
    assert events[-1].data == {"finding_id": finding.finding_id}


def test_anchor_cleanup_publishes_finding_update(graph, events):
    graph.add_node(id="a", label="A", type="class")
    finding = graph.add_finding(title="gold", node_ids=["a", "b"])
    events.clear()

    graph.delete_node("a")

    assert [e.op for e in events] == ["node.delete", "finding.update"]
    assert events[-1].data == {
        "finding_id": finding.finding_id,
        "patch": {"set": {"node_ids": ["b"]}},
    }


def test_anchor_cleanup_publishes_once_per_affected_finding(graph, events):
    graph.add_node(id="a", label="A", type="class")
    graph.add_finding(title="one", node_ids=["a"])
    graph.add_finding(title="two", node_ids=["a"])
    graph.add_finding(title="unrelated", node_ids=["z"])
    events.clear()

    graph.delete_node("a")

    finding_updates = [e for e in events if e.op == "finding.update"]
    assert len(finding_updates) == 2


def test_finding_events_share_the_graph_seq_sequence(graph, events):
    graph.add_node(id="a", label="A", type="class")
    graph.add_finding(title="t")

    assert [e.op for e in events] == ["node.add", "finding.add"]
    assert [e.seq for e in events] == [1, 2]


def test_list_findings_publishes_nothing(graph, events):
    graph.add_finding(title="t")
    events.clear()

    graph.list_findings()

    assert events == []
