"""Completion verification for TASK L — 데이터 생명주기 ([24]).

The distinction under test: a **correction** says the old value was wrong, so it
is discarded and only the fact of the change is logged ([24-B]); a
**supersession** says the old value was valid but is now stale, so it is archived
before being overwritten ([24-C]). That difference is the point — this project
exists to keep what was once true.

Also covers the write-protection on the archive itself: an archive an AI can
forge or erase records nothing.
"""

import pytest

from visualizebetter.graph.core import (
    MAX_FINDING_BODY_CHARS,
    MAX_FINDING_SUPERSEDED_BYTES,
    MAX_FINDING_SUPERSEDED_ENTRIES,
    MAX_SUPERSEDED_ENTRIES,
    PROVENANCE_PROPERTY,
    SUPERSEDED_PROPERTY,
    Graph,
    _serialized_bytes,
)
from visualizebetter.graph.events import EDGE_UPDATE, FINDING_UPDATE, NODE_UPDATE


@pytest.fixture
def graph():
    g = Graph(name="test")
    g.add_node(id="app.World", label="OrderService", type="class", created_by="ida-agent")
    g.add_node(id="app.Player", label="Player", type="class")
    g.add_edge(source="app.World", target="app.Player", relation="owns")
    return g


@pytest.fixture
def events(graph):
    captured = []
    graph.events.subscribe(captured.append)
    return captured


def _payloads(events, op):
    return [event.data for event in events if event.op == op]


# --- [24-C] supersession — 낡은 값은 보존된다 ---


def test_supersede_archives_the_previous_value(graph):
    graph.update_node("app.World", {"set": {"label": "GameWorld"}}, reason="supersede")

    (entry,) = graph.get_node("app.World").properties[SUPERSEDED_PROPERTY]
    assert entry["prev"] == {"label": "OrderService"}


def test_supersede_still_applies_the_patch(graph):
    graph.update_node("app.World", {"set": {"label": "GameWorld"}}, reason="supersede")

    assert graph.get_node("app.World").label == "GameWorld"


def test_a_superseded_entry_carries_prev_at_and_by(graph):
    graph.update_node("app.World", {"set": {"label": "GameWorld"}}, reason="supersede")

    (entry,) = graph.get_node("app.World").properties[SUPERSEDED_PROPERTY]
    assert set(entry) == {"prev", "at", "by"}
    assert entry["at"]
    assert entry["by"] == "ida-agent"


def test_supersede_archives_only_the_fields_the_patch_touches(graph):
    graph.update_node(
        "app.World",
        {"set": {"properties": {"size": 64}}},
        reason="supersede",
    )
    graph.update_node(
        "app.World",
        {"set": {"properties": {"size": 128}}},
        reason="supersede",
    )

    entries = graph.get_node("app.World").properties[SUPERSEDED_PROPERTY]
    # [23-C] RN7 WW: the first supersede had no prior `size`, so there was
    # nothing to preserve and **no entry is recorded** — an archive of
    # {'prev': {}} preserves nothing while still consuming one of the ten FIFO
    # slots, which is how no-op calls used to evict real supersessions. Only the
    # second one archived a value.
    assert len(entries) == 1
    assert entries[0]["prev"] == {"properties": {"size": 64}}


def test_repeated_supersession_stacks_history_oldest_first(graph):
    for label in ("v2", "v3", "v4"):
        graph.update_node("app.World", {"set": {"label": label}}, reason="supersede")

    entries = graph.get_node("app.World").properties[SUPERSEDED_PROPERTY]
    assert [e["prev"]["label"] for e in entries] == ["OrderService", "v2", "v3"]


def test_node_history_is_capped_at_ten_keeping_the_recent(graph):
    for index in range(MAX_SUPERSEDED_ENTRIES + 5):
        graph.update_node(
            "app.World", {"set": {"label": f"v{index}"}}, reason="supersede"
        )

    entries = graph.get_node("app.World").properties[SUPERSEDED_PROPERTY]
    assert len(entries) == MAX_SUPERSEDED_ENTRIES
    # The oldest went first: the very first label is gone, the latest is kept.
    assert entries[-1]["prev"]["label"] == f"v{MAX_SUPERSEDED_ENTRIES + 3}"


def test_a_later_change_cannot_rewrite_already_archived_history(graph):
    graph.update_node(
        "app.World", {"set": {"properties": {"flags": ["a"]}}}, reason="supersede"
    )
    graph.update_node(
        "app.World", {"set": {"properties": {"flags": ["a", "b"]}}}, reason="supersede"
    )
    # Mutating the live value must not reach into the archive's copy of it.
    graph.get_node("app.World").properties["flags"].append("c")

    entries = graph.get_node("app.World").properties[SUPERSEDED_PROPERTY]
    # [23-C] RN7 WW: the first supersede archived nothing (no prior `flags`), so
    # the surviving entry is the second one — the deep-copy guarantee it checks
    # is unchanged.
    assert entries[-1]["prev"] == {"properties": {"flags": ["a"]}}


def test_supersede_archives_a_removed_property(graph):
    graph.update_node("app.World", {"set": {"properties": {"note": "keep"}}})
    graph.update_node("app.World", {"remove": ["note"]}, reason="supersede")

    (entry,) = graph.get_node("app.World").properties[SUPERSEDED_PROPERTY]
    assert entry["prev"] == {"properties": {"note": "keep"}}


# --- [24-B] correction — 틀린 값은 안 남는다 ---


def test_correction_overwrites_without_keeping_the_wrong_value(graph):
    graph.update_node("app.World", {"set": {"label": "Corrected"}}, reason="correction")

    node = graph.get_node("app.World")
    assert node.label == "Corrected"
    assert SUPERSEDED_PROPERTY not in node.properties


def test_correction_logs_action_at_and_by(graph):
    graph.update_node("app.World", {"set": {"label": "Corrected"}}, reason="correction")

    (entry,) = graph.get_node("app.World").properties[PROVENANCE_PROPERTY]
    assert set(entry) == {"action", "at", "by"}
    assert entry["action"] == "correction"
    assert entry["by"] == "ida-agent"


def test_the_provenance_note_does_not_carry_the_wrong_value(graph):
    graph.update_node("app.World", {"set": {"label": "Corrected"}}, reason="correction")

    (entry,) = graph.get_node("app.World").properties[PROVENANCE_PROPERTY]
    assert "OrderService" not in str(entry)  # [24-B] 틀린 값 자체는 안 남긴다


def test_corrections_accumulate(graph):
    graph.update_node("app.World", {"set": {"label": "a"}}, reason="correction")
    graph.update_node("app.World", {"set": {"label": "b"}}, reason="correction")

    assert len(graph.get_node("app.World").properties[PROVENANCE_PROPERTY]) == 2


# --- reason 검증 (TASK 0 #3 raise 원칙) ---


def test_no_reason_is_a_plain_update(graph):
    graph.update_node("app.World", {"set": {"label": "Plain"}})

    node = graph.get_node("app.World")
    assert node.label == "Plain"
    assert SUPERSEDED_PROPERTY not in node.properties
    assert PROVENANCE_PROPERTY not in node.properties


@pytest.mark.parametrize("reason", ["Correction", "supercede", "supersede ", "", "delete"])
def test_an_unknown_reason_raises(graph, reason):
    with pytest.raises(ValueError, match="unknown reason"):
        graph.update_node("app.World", {"set": {"label": "x"}}, reason=reason)


def test_a_rejected_reason_changes_nothing(graph):
    with pytest.raises(ValueError):
        graph.update_node("app.World", {"set": {"label": "x"}}, reason="typo")

    # Validated before the patch applies — a typo must not half-write the node.
    assert graph.get_node("app.World").label == "OrderService"


def test_reason_is_validated_on_edge_and_finding_too(graph):
    finding_id = graph.add_finding(title="t").finding_id
    with pytest.raises(ValueError, match="unknown reason"):
        graph.update_edge("app.World", "app.Player", "owns", patch={}, reason="nope")
    with pytest.raises(ValueError, match="unknown reason"):
        graph.update_finding(finding_id, {"set": {"title": "x"}}, reason="nope")


# --- edges ([24-D] 세 tool 대칭) ---


def test_edge_supersede_archives_the_previous_value(graph):
    graph.update_edge(
        "app.World", "app.Player", "owns", patch={"set": {"weight": 5.0}},
        reason="supersede",
    )

    (entry,) = graph.edges[("app.World", "app.Player", "owns", "")].properties[
        SUPERSEDED_PROPERTY
    ]
    assert entry["prev"] == {"weight": 1.0}


# --- findings ([24-C] "finding 도 동일") ---


def test_finding_supersede_archives_the_previous_version(graph):
    finding = graph.add_finding(title="Old title", body="old body")
    graph.update_finding(
        finding.finding_id, {"set": {"body": "new body"}}, reason="supersede"
    )

    (entry,) = graph.get_finding(finding.finding_id)._superseded
    assert entry["prev"] == {"body": "old body"}
    assert graph.get_finding(finding.finding_id).body == "new body"


def test_finding_correction_logs_without_archiving(graph):
    finding = graph.add_finding(title="Wrong", body="wrong body")
    graph.update_finding(
        finding.finding_id, {"set": {"body": "right body"}}, reason="correction"
    )

    stored = graph.get_finding(finding.finding_id)
    assert stored.body == "right body"
    assert stored._superseded == []
    assert stored._provenance[0]["action"] == "correction"


def test_finding_history_reaches_the_wire_and_snapshot(graph):
    finding = graph.add_finding(title="t", body="old")
    graph.update_finding(finding.finding_id, {"set": {"body": "new"}}, reason="supersede")

    payload = graph.get_finding(finding.finding_id).to_dict()
    assert payload[SUPERSEDED_PROPERTY][0]["prev"] == {"body": "old"}


# --- ★ [23-B] 크기 불변식: cap 10 개수만으론 finding 을 못 묶는다 ---


def test_finding_history_is_bounded_by_size_not_only_count(graph):
    """A superseded body may be 16KB, so a count cap alone permits a ~160KB
    finding built entirely through supported calls — past the response budget and
    through the invariant get_finding depends on ([23-B])."""
    chunk = MAX_FINDING_BODY_CHARS // 4
    finding = graph.add_finding(title="t", body="0" * chunk)
    for index in range(1, 8):
        graph.update_finding(
            finding.finding_id,
            {"set": {"body": str(index) * chunk}},
            reason="supersede",
        )

    archive = graph.get_finding(finding.finding_id)._superseded
    assert _serialized_bytes(archive) <= MAX_FINDING_SUPERSEDED_BYTES
    # Well under the count cap — size is what bound it here, which is the point.
    assert len(archive) < MAX_FINDING_SUPERSEDED_ENTRIES
    # Bounded by evicting the oldest, so the most recent version survives.
    assert archive[-1]["prev"]["body"].startswith("6")


def test_a_superseded_finding_stays_inside_the_response_budget(graph):
    """The invariant that matters: get_finding hands gold back whole ([23-B]).
    Repeated supersession must not build a finding too big to return."""
    big = "x" * (MAX_FINDING_BODY_CHARS - 1)
    finding = graph.add_finding(title="t", body=big)
    for index in range(12):
        graph.update_finding(
            finding.finding_id,
            {"set": {"body": chr(ord("a") + index) * (MAX_FINDING_BODY_CHARS - 1)}},
            reason="supersede",
        )

    assert _serialized_bytes(graph.get_finding(finding.finding_id).to_dict()) < 50 * 1024


def test_finding_history_keeps_the_newest_even_when_it_alone_is_oversized(graph):
    """Superseding a max-size finding must archive *something* — dropping the one
    entry to satisfy the budget would preserve nothing, which is the failure the
    feature exists to prevent."""
    big = "x" * (MAX_FINDING_BODY_CHARS - 1)
    finding = graph.add_finding(title="t", body=big)
    graph.update_finding(finding.finding_id, {"set": {"body": "small"}}, reason="supersede")

    (entry,) = graph.get_finding(finding.finding_id)._superseded
    assert entry["prev"]["body"] == big


def test_finding_history_has_a_count_cap_as_well(graph):
    finding = graph.add_finding(title="t", body="b")
    for index in range(MAX_FINDING_SUPERSEDED_ENTRIES + 8):
        graph.update_finding(
            finding.finding_id, {"set": {"body": f"b{index}"}}, reason="supersede"
        )

    archive = graph.get_finding(finding.finding_id)._superseded
    assert len(archive) == MAX_FINDING_SUPERSEDED_ENTRIES


# --- ★ [23-B] 쓰기보호: 이력은 위조도 삭제도 안 된다 ---


def test_a_caller_cannot_forge_finding_history(graph):
    """The _citations forgery [23-B] blocks on nodes, arriving at findings by a
    different door: a Finding's history is a *field*, and fields are patchable."""
    finding = graph.add_finding(title="t")

    with pytest.raises(ValueError, match="system-owned"):
        graph.update_finding(
            finding.finding_id,
            {"set": {SUPERSEDED_PROPERTY: [{"prev": {"body": "never said this"}}]}},
        )
    assert graph.get_finding(finding.finding_id)._superseded == []


def test_a_caller_cannot_forge_a_finding_provenance_log(graph):
    finding = graph.add_finding(title="t")

    with pytest.raises(ValueError, match="system-owned"):
        graph.update_finding(
            finding.finding_id,
            {"set": {PROVENANCE_PROPERTY: [{"action": "correction", "by": "someone"}]}},
        )


def test_a_caller_cannot_erase_a_nodes_history(graph):
    """Deleting is writing. An archive the AI can drop preserves nothing."""
    graph.update_node("app.World", {"set": {"label": "v2"}}, reason="supersede")

    with pytest.raises(ValueError, match="cannot be removed"):
        graph.update_node("app.World", {"remove": [SUPERSEDED_PROPERTY]})

    assert len(graph.get_node("app.World").properties[SUPERSEDED_PROPERTY]) == 1


def test_a_caller_cannot_erase_citations_either(graph):
    graph.cite("app.World", "trace://0x1400", "IDA")

    with pytest.raises(ValueError, match="cannot be removed"):
        graph.update_node("app.World", {"remove": ["_citations"]})


# --- [8-C] 이벤트 — 이력이 구독자에게 도달해야 인스펙터가 볼 수 있다 ---


def test_node_update_event_carries_the_resulting_history(graph, events):
    graph.update_node("app.World", {"set": {"label": "v2"}}, reason="supersede")

    (payload,) = _payloads(events, NODE_UPDATE)
    archived = payload["patch"]["set"]["properties"][SUPERSEDED_PROPERTY]
    assert archived[0]["prev"] == {"label": "OrderService"}


def test_node_update_event_carries_the_correction_log(graph, events):
    graph.update_node("app.World", {"set": {"label": "v2"}}, reason="correction")

    (payload,) = _payloads(events, NODE_UPDATE)
    logged = payload["patch"]["set"]["properties"][PROVENANCE_PROPERTY]
    assert logged[0]["action"] == "correction"


def test_the_published_patch_still_carries_the_callers_own_changes(graph, events):
    graph.update_node(
        "app.World",
        {"set": {"label": "v2", "properties": {"size": 8}}},
        reason="supersede",
    )

    (payload,) = _payloads(events, NODE_UPDATE)
    assert payload["patch"]["set"]["label"] == "v2"
    assert payload["patch"]["set"]["properties"]["size"] == 8


def test_a_plain_update_publishes_an_unchanged_patch(graph, events):
    patch = {"set": {"label": "v2"}}
    graph.update_node("app.World", patch)

    (payload,) = _payloads(events, NODE_UPDATE)
    assert payload["patch"] == patch


def test_edge_update_event_carries_the_history(graph, events):
    graph.update_edge(
        "app.World", "app.Player", "owns", patch={"set": {"weight": 2.0}},
        reason="supersede",
    )

    (payload,) = _payloads(events, EDGE_UPDATE)
    assert payload["patch"]["set"]["properties"][SUPERSEDED_PROPERTY][0]["prev"] == {
        "weight": 1.0
    }


def test_finding_update_event_carries_the_history(graph, events):
    finding = graph.add_finding(title="t", body="old")
    graph.update_finding(finding.finding_id, {"set": {"body": "new"}}, reason="supersede")

    (payload,) = _payloads(events, FINDING_UPDATE)
    assert payload["patch"]["set"][SUPERSEDED_PROPERTY][0]["prev"] == {"body": "old"}
