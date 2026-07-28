"""Completion verification for TASK 3 — MCP tool 노출 ([5-G], [5-F], [23-F] TASK 3).

Covers: record -> list -> get -> update -> delete flow by direct tool call,
created_at desc ordering, body excluded from the list, 50KB truncation ([5]
공통 규칙), anchor summary incl. missing anchors, and real MCP dispatch +
Pydantic validation through an in-memory client.
"""

import asyncio
import json

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

from visualizebetter.graph.core import (
    MAX_FINDING_BODY_CHARS,
    MAX_FINDING_EVIDENCE,
    MAX_FINDING_NODE_IDS,
    MAX_FINDING_TAGS,
    MAX_FINDING_TITLE_CHARS,
    Graph,
)
from visualizebetter.mcp_server import MAX_RESPONSE_BYTES, create_server


def _size(payload):
    """[5] 공통 규칙의 직렬화 기준."""
    return len(json.dumps(payload, ensure_ascii=False).encode("utf-8"))


@pytest.fixture
def graph():
    return Graph(name="test")


@pytest.fixture
def mcp(graph):
    return create_server(graph)


def call(mcp, name, **kwargs):
    """Invoke a registered tool's underlying function directly."""
    tool = asyncio.run(mcp.get_tool(name))
    return tool.fn(**kwargs)


def client_call(mcp, name, args):
    """Invoke through real MCP dispatch (schema validation included)."""

    async def go():
        async with Client(mcp) as c:
            return await c.call_tool(name, args)

    return asyncio.run(go())


# --- registration ---


def test_all_six_tools_are_registered(mcp):
    for name in (
        "record_finding",
        "update_finding",
        "list_findings",
        "get_finding",
        "delete_finding",
        "cite",
    ):
        assert asyncio.run(mcp.get_tool(name)) is not None


# --- record -> list -> get -> update -> delete flow ---


def test_record_finding_returns_ok_and_finding_id(mcp):
    result = call(mcp, "record_finding", title="결제 실패의 핵심 경로")

    assert result["ok"] is True
    assert result["finding_id"]


def test_record_then_get_roundtrip(mcp):
    recorded = call(
        mcp,
        "record_finding",
        title="gold",
        body="상세 근거",
        confidence=0.9,
        evidence=["trace://0x1400"],
        tags=["auth"],
    )

    got = call(mcp, "get_finding", finding_id=recorded["finding_id"])

    assert got["ok"] is True
    assert got["finding"]["title"] == "gold"
    assert got["finding"]["body"] == "상세 근거"
    assert got["finding"]["evidence"] == ["trace://0x1400"]
    assert got["finding"]["confidence"] == 0.9
    assert got["finding"]["tags"] == ["auth"]


def test_full_flow_record_list_get_update_delete(mcp):
    recorded = call(mcp, "record_finding", title="t", body="b")
    finding_id = recorded["finding_id"]

    listed = call(mcp, "list_findings")
    assert listed["total"] == 1

    got = call(mcp, "get_finding", finding_id=finding_id)
    assert got["finding"]["title"] == "t"

    updated = call(mcp, "update_finding", finding_id=finding_id, patch={"set": {"title": "t2"}})
    assert updated["ok"] is True
    assert updated["finding"]["title"] == "t2"

    deleted = call(mcp, "delete_finding", finding_id=finding_id)
    assert deleted["ok"] is True
    assert call(mcp, "list_findings")["total"] == 0


def test_record_finding_is_not_idempotent(mcp):
    """[5-G]: 매 호출 = 새 finding."""
    first = call(mcp, "record_finding", title="same")
    second = call(mcp, "record_finding", title="same")

    assert first["finding_id"] != second["finding_id"]
    assert call(mcp, "list_findings")["total"] == 2


def test_delete_finding_returns_ok(mcp):
    recorded = call(mcp, "record_finding", title="t")

    assert call(mcp, "delete_finding", finding_id=recorded["finding_id"]) == {"ok": True}


# --- list_findings: created_at desc ([5-G]) ---


def test_list_findings_is_sorted_created_at_desc(mcp, graph):
    graph.add_finding(title="oldest")
    graph.add_finding(title="middle")
    graph.add_finding(title="newest")
    # created_at may tie at clock resolution; force distinct stamps.
    for offset, finding in enumerate(graph.findings.values()):
        finding.created_at = f"2026-07-1{offset + 1}T00:00:00+00:00"

    listed = call(mcp, "list_findings")

    assert [f["title"] for f in listed["findings"]] == ["newest", "middle", "oldest"]


def test_list_findings_ordering_applies_before_pagination(mcp, graph):
    for i in range(5):
        graph.add_finding(title=f"f{i}")
    for i, finding in enumerate(graph.findings.values()):
        finding.created_at = f"2026-07-0{i + 1}T00:00:00+00:00"

    listed = call(mcp, "list_findings", limit=2)

    assert [f["title"] for f in listed["findings"]] == ["f4", "f3"]
    assert listed["total"] == 5


# --- list_findings: body excluded, [5-G] row fields ---


def test_list_findings_rows_exclude_body(mcp):
    call(mcp, "record_finding", title="t", body="아주 긴 본문" * 100)

    (row,) = call(mcp, "list_findings")["findings"]

    assert "body" not in row


def test_list_findings_row_fields_match_spec(mcp):
    call(mcp, "record_finding", title="t", body="b", evidence=["e"], layer="l1")

    (row,) = call(mcp, "list_findings")["findings"]

    assert set(row) == {
        "finding_id",
        "title",
        "confidence",
        "node_ids",
        "created_by",
        "created_at",
        "tags",
    }


# --- list_findings: filters + pagination ---


def test_list_findings_filters_by_min_confidence(mcp):
    call(mcp, "record_finding", title="low", confidence=0.2)
    call(mcp, "record_finding", title="high", confidence=0.9)

    listed = call(mcp, "list_findings", min_confidence=0.5)

    assert [f["title"] for f in listed["findings"]] == ["high"]
    assert listed["total"] == 1


def test_list_findings_filters_by_node_id(mcp):
    call(mcp, "record_finding", title="anchored", node_ids=["a"])
    call(mcp, "record_finding", title="elsewhere", node_ids=["z"])

    listed = call(mcp, "list_findings", node_id="a")

    assert [f["title"] for f in listed["findings"]] == ["anchored"]


def test_list_findings_filters_by_layer(mcp):
    call(mcp, "record_finding", title="claude", layer="claude-1")
    call(mcp, "record_finding", title="gpt", layer="gpt-1")

    listed = call(mcp, "list_findings", layer="gpt-1")

    assert [f["title"] for f in listed["findings"]] == ["gpt"]


def test_list_findings_total_counts_all_matches_before_pagination(mcp):
    for i in range(5):
        call(mcp, "record_finding", title=f"f{i}")

    listed = call(mcp, "list_findings", limit=2, offset=1)

    assert len(listed["findings"]) == 2
    assert listed["total"] == 5


def test_list_findings_empty(mcp):
    assert call(mcp, "list_findings") == {"findings": [], "total": 0}


# --- [5] 공통 규칙: 50KB 절단 ---


def test_small_response_is_not_marked_truncated(mcp):
    call(mcp, "record_finding", title="t")

    assert "truncated" not in call(mcp, "list_findings")


# A title at the [23-B] limit is ~3KB of UTF-8 Korean, so a few dozen rows
# overflow the 50KB list budget while every finding stays individually legal.
_HEAVY_TITLE = "굵" * MAX_FINDING_TITLE_CHARS
_HEAVY_ROWS = 60


def test_oversized_response_is_truncated_with_total_preserved(mcp):
    for _ in range(_HEAVY_ROWS):
        call(mcp, "record_finding", title=_HEAVY_TITLE)

    listed = call(mcp, "list_findings", limit=_HEAVY_ROWS)

    assert listed["truncated"] is True
    assert listed["total"] == _HEAVY_ROWS, "total reports every match, not the trimmed page"
    assert len(listed["findings"]) < _HEAVY_ROWS


def test_truncated_response_fits_the_budget(mcp):
    for _ in range(_HEAVY_ROWS):
        call(mcp, "record_finding", title=_HEAVY_TITLE)

    listed = call(mcp, "list_findings", limit=_HEAVY_ROWS)

    assert _size(listed) <= MAX_RESPONSE_BYTES


# --- list_findings bounds ([5-G]) — enforced by Pydantic, so dispatch is required ---


def test_negative_offset_is_rejected(mcp):
    """음수 offset 은 파이썬 슬라이싱상 조용히 뒤에서 자르는 버그 표면."""
    with pytest.raises(ToolError):
        client_call(mcp, "list_findings", {"offset": -1})


def test_offset_zero_is_accepted(mcp):
    assert client_call(mcp, "list_findings", {"offset": 0}).data["total"] == 0


def test_limit_below_one_is_rejected(mcp):
    with pytest.raises(ToolError):
        client_call(mcp, "list_findings", {"limit": 0})

    with pytest.raises(ToolError):
        client_call(mcp, "list_findings", {"limit": -5})


def test_limit_above_500_is_rejected(mcp):
    with pytest.raises(ToolError):
        client_call(mcp, "list_findings", {"limit": 501})


@pytest.mark.parametrize("limit", [1, 50, 500])
def test_limit_within_bounds_is_accepted(mcp, limit):
    assert client_call(mcp, "list_findings", {"limit": limit}).data["total"] == 0


# --- get_finding returns gold whole — never truncated ([23-B] 크기 불변식) ---


def test_get_finding_returns_the_body_intact(mcp):
    body = "본문" * 100
    recorded = call(mcp, "record_finding", title="t", body=body)

    got = call(mcp, "get_finding", finding_id=recorded["finding_id"])

    assert got["finding"]["body"] == body
    assert "truncated" not in got


def test_get_finding_is_not_truncated_at_maximum_size(mcp):
    """The largest finding the invariants permit still comes back whole."""
    recorded = call(
        mcp,
        "record_finding",
        title="제" * MAX_FINDING_TITLE_CHARS,
        body="본" * MAX_FINDING_BODY_CHARS,
        node_ids=[f"node-{i:05d}" for i in range(MAX_FINDING_NODE_IDS)],
        evidence=[f"https://example.test/doc/{i}" for i in range(MAX_FINDING_EVIDENCE)],
        tags=[f"tag-{i}" for i in range(MAX_FINDING_TAGS)],
    )

    got = call(mcp, "get_finding", finding_id=recorded["finding_id"])

    assert "truncated" not in got
    assert len(got["finding"]["body"]) == MAX_FINDING_BODY_CHARS
    assert len(got["finding"]["evidence"]) == MAX_FINDING_EVIDENCE
    assert len(got["anchors"]) == MAX_FINDING_NODE_IDS


# --- [23-B] 크기 불변식 — Pydantic mirrors core so oversize is rejected early ---


def _sized(field: str, count: int):
    """Build a value of exactly `count` for a size-bounded finding field."""
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


@pytest.mark.parametrize("field,cap", _BOUNDED_FIELDS, ids=[f[0] for f in _BOUNDED_FIELDS])
def test_record_finding_rejects_oversize_input(mcp, field, cap):
    with pytest.raises(ToolError):
        client_call(mcp, "record_finding", {"title": "t", field: _sized(field, cap + 1)})


@pytest.mark.parametrize("field,cap", _BOUNDED_FIELDS, ids=[f[0] for f in _BOUNDED_FIELDS])
def test_record_finding_accepts_input_at_the_limit(mcp, field, cap):
    args = {"title": "t", field: _sized(field, cap)}
    assert client_call(mcp, "record_finding", args).data["ok"] is True


def test_oversize_finding_is_never_stored(mcp, graph):
    with pytest.raises(ToolError):
        client_call(
            mcp, "record_finding", {"title": "t", "body": "본" * (MAX_FINDING_BODY_CHARS + 1)}
        )

    assert graph.findings == {}


def test_update_finding_rejects_oversize_patch(mcp):
    recorded = call(mcp, "record_finding", title="t")

    with pytest.raises(ToolError):
        call(
            mcp,
            "update_finding",
            finding_id=recorded["finding_id"],
            patch={"set": {"evidence": [f"e{i}" for i in range(MAX_FINDING_EVIDENCE + 1)]}},
        )


# --- get_finding: anchor summary (Q1=A) ---


def test_get_finding_anchor_summary_has_id_label_type(mcp, graph):
    graph.add_node(id="a", label="A", type="class")
    recorded = call(mcp, "record_finding", title="t", node_ids=["a"])

    got = call(mcp, "get_finding", finding_id=recorded["finding_id"])

    assert got["anchors"] == [{"id": "a", "label": "A", "type": "class"}]


def test_get_finding_marks_anchors_that_do_not_exist(mcp):
    """record_finding does not create nodes — an anchor may not exist yet."""
    recorded = call(mcp, "record_finding", title="t", node_ids=["ghost"])

    got = call(mcp, "get_finding", finding_id=recorded["finding_id"])

    assert got["anchors"] == [{"id": "ghost", "missing": True}]


def test_get_finding_anchor_summary_mixes_present_and_missing(mcp, graph):
    graph.add_node(id="a", label="A", type="class")
    recorded = call(mcp, "record_finding", title="t", node_ids=["a", "ghost"])

    got = call(mcp, "get_finding", finding_id=recorded["finding_id"])

    assert got["anchors"] == [
        {"id": "a", "label": "A", "type": "class"},
        {"id": "ghost", "missing": True},
    ]


def test_record_finding_does_not_create_anchor_nodes(mcp, graph):
    call(mcp, "record_finding", title="t", node_ids=["ghost"])

    assert graph.nodes == {}


def test_get_finding_anchors_follow_delete_node_cleanup(mcp, graph):
    graph.add_node(id="a", label="A", type="class")
    recorded = call(mcp, "record_finding", title="t", node_ids=["a"])

    graph.delete_node("a")
    got = call(mcp, "get_finding", finding_id=recorded["finding_id"])

    assert got["anchors"] == []


# --- update_finding delegation ([5-G] rules) ---


def test_update_finding_allows_evidence_and_layer(mcp):
    recorded = call(mcp, "record_finding", title="t")

    updated = call(
        mcp,
        "update_finding",
        finding_id=recorded["finding_id"],
        patch={"set": {"evidence": ["e"], "layer": "l2"}},
    )

    assert updated["finding"]["evidence"] == ["e"]
    assert updated["finding"]["layer"] == "l2"


def test_update_finding_rejects_server_managed_fields(mcp):
    recorded = call(mcp, "record_finding", title="t")

    with pytest.raises(ToolError):
        call(
            mcp,
            "update_finding",
            finding_id=recorded["finding_id"],
            patch={"set": {"created_at": "1999-01-01"}},
        )


def test_update_finding_rejects_remove(mcp):
    """Finding has no properties map, so [5-A]'s remove has no target."""
    recorded = call(mcp, "record_finding", title="t")

    with pytest.raises(ToolError):
        call(
            mcp,
            "update_finding",
            finding_id=recorded["finding_id"],
            patch={"remove": ["anything"]},
        )


# --- not-found surfaces as a tool error, not a masked internal error ---


@pytest.mark.parametrize(
    "name,kwargs",
    [
        ("get_finding", {"finding_id": "nope"}),
        ("update_finding", {"finding_id": "nope", "patch": {"set": {"title": "x"}}}),
        ("delete_finding", {"finding_id": "nope"}),
        ("cite", {"node_id": "nope", "source_url": "u", "source_title": "t"}),
    ],
)
def test_missing_record_raises_tool_error(mcp, name, kwargs):
    with pytest.raises(ToolError):
        call(mcp, name, **kwargs)


def test_not_found_reason_reaches_the_client_unmasked(mcp):
    """ToolError is the reason not-found says *what* was missing, not "internal error"."""
    with pytest.raises(ToolError, match="finding not found"):
        client_call(mcp, "get_finding", {"finding_id": "nope"})


# --- cite ([5-F]) ---


def test_cite_accumulates_on_the_node(mcp, graph):
    graph.add_node(id="a", label="A", type="class")

    call(mcp, "cite", node_id="a", source_url="trace://0x1400", source_title="IDA")
    result = call(mcp, "cite", node_id="a", source_url="C:/dump.exe", source_title="Dump")

    assert result["ok"] is True
    assert len(result["node"]["properties"]["_citations"]) == 2


# --- real MCP dispatch + Pydantic validation ---


def test_tools_are_callable_through_mcp_dispatch(mcp):
    result = client_call(mcp, "record_finding", {"title": "via client"})

    assert result.data["ok"] is True


def test_confidence_out_of_range_is_rejected_by_schema(mcp):
    """[23-B] confidence 0.0~1.0 — validated at the tool boundary."""
    with pytest.raises(ToolError):
        client_call(mcp, "record_finding", {"title": "t", "confidence": 5.0})

    with pytest.raises(ToolError):
        client_call(mcp, "record_finding", {"title": "t", "confidence": -0.1})


def test_confidence_within_range_is_accepted(mcp):
    for value in (0.0, 0.5, 1.0):
        result = client_call(mcp, "record_finding", {"title": "t", "confidence": value})
        assert result.data["ok"] is True


def test_wrong_type_is_rejected_by_schema(mcp):
    with pytest.raises(ToolError):
        client_call(mcp, "record_finding", {"title": "t", "confidence": "not-a-float"})


def test_missing_required_argument_is_rejected(mcp):
    with pytest.raises(ToolError):
        client_call(mcp, "record_finding", {})
