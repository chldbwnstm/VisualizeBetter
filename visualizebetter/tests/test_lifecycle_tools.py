"""Completion verification for TASK L — MCP 노출 ([24-D]).

The core semantics live in test_lifecycle.py. What matters here is the boundary:
the AI must be *told* the two reasons exist (schema), an invalid one must be
refused by Pydantic before the tool body runs, and the reserved-key protections
must survive the arrival of two new reserved keys.
"""

import asyncio

import pytest
from fastmcp import Client

from visualizebetter.graph.core import (
    PROVENANCE_PROPERTY,
    SUPERSEDED_PROPERTY,
    Graph,
)
from visualizebetter.mcp_server import create_server


@pytest.fixture
def graph():
    g = Graph(name="test")
    g.add_node(id="app.World", label="OrderService", type="class")
    g.add_node(id="app.Player", label="Player", type="class")
    g.add_edge(source="app.World", target="app.Player", relation="owns")
    return g


@pytest.fixture
def mcp(graph):
    return create_server(graph)


def call(mcp, name, **kwargs):
    tool = asyncio.run(mcp.get_tool(name))
    return tool.fn(**kwargs)


def client_call(mcp, name, args):
    """Invoke through real MCP dispatch (schema validation included)."""

    async def go():
        async with Client(mcp) as c:
            return await c.call_tool(name, args)

    return asyncio.run(go())


def _schema(mcp, name):
    async def go():
        async with Client(mcp) as c:
            return {t.name: t.inputSchema for t in await c.list_tools()}[name]

    return asyncio.run(go())


# --- [24-D] "Pydantic 이 reason enum 검증" ---


@pytest.mark.parametrize("tool", ["update_node", "update_edge", "update_finding"])
def test_the_reason_enum_is_advertised_to_the_ai(mcp, tool):
    """A distinction the AI cannot see in the schema is a distinction it will not
    use — the enum is how correction vs supersession becomes reachable."""
    schema = _schema(mcp, tool)
    rendered = str(schema["properties"]["reason"])

    assert "correction" in rendered
    assert "supersede" in rendered


@pytest.mark.parametrize("tool", ["update_node", "update_edge", "update_finding"])
def test_reason_is_optional(mcp, tool):
    schema = _schema(mcp, tool)

    assert "reason" not in schema.get("required", [])


def test_an_invalid_reason_is_refused_by_schema_validation(mcp):
    """A typo'd 'supercede' must not degrade into a plain update — that would
    silently discard the value the caller meant to preserve."""
    from fastmcp.exceptions import ToolError

    with pytest.raises(ToolError, match="correction"):
        client_call(
            mcp,
            "update_node",
            {"id": "app.World", "patch": {"set": {"label": "x"}}, "reason": "supercede"},
        )


def test_a_schema_rejected_reason_leaves_the_node_untouched(mcp, graph):
    from fastmcp.exceptions import ToolError

    with pytest.raises(ToolError):
        client_call(
            mcp,
            "update_node",
            {"id": "app.World", "patch": {"set": {"label": "x"}}, "reason": "nonsense"},
        )

    # Refused by the schema, so the tool body never ran.
    assert graph.get_node("app.World").label == "OrderService"
    assert SUPERSEDED_PROPERTY not in graph.get_node("app.World").properties


# --- reason 왕복 ([24-B]/[24-C]) ---


def test_supersede_through_the_tool_archives_the_previous_value(mcp, graph):
    result = call(
        mcp, "update_node", id="app.World", patch={"set": {"label": "GameWorld"}},
        reason="supersede",
    )

    archived = result["node"]["properties"][SUPERSEDED_PROPERTY]
    assert archived[0]["prev"] == {"label": "OrderService"}
    assert result["node"]["label"] == "GameWorld"


def test_correction_through_the_tool_logs_without_archiving(mcp):
    result = call(
        mcp, "update_node", id="app.World", patch={"set": {"label": "Fixed"}},
        reason="correction",
    )

    assert result["node"]["properties"][PROVENANCE_PROPERTY][0]["action"] == "correction"
    assert SUPERSEDED_PROPERTY not in result["node"]["properties"]


def test_supersede_round_trips_through_real_mcp_dispatch(mcp, graph):
    result = client_call(
        mcp,
        "update_node",
        {"id": "app.World", "patch": {"set": {"label": "GameWorld"}}, "reason": "supersede"},
    )

    assert not result.is_error
    archived = graph.get_node("app.World").properties[SUPERSEDED_PROPERTY]
    assert archived[0]["prev"] == {"label": "OrderService"}


def test_edge_supersede_through_the_tool(mcp):
    result = call(
        mcp, "update_edge", source="app.World", target="app.Player", relation="owns",
        patch={"set": {"weight": 3.0}}, reason="supersede",
    )

    assert result["edge"]["properties"][SUPERSEDED_PROPERTY][0]["prev"] == {"weight": 1.0}


def test_finding_supersede_through_the_tool(mcp, graph):
    finding = graph.add_finding(title="t", body="old body")

    result = call(
        mcp, "update_finding", finding_id=finding.finding_id,
        patch={"set": {"body": "new body"}}, reason="supersede",
    )

    assert result["finding"][SUPERSEDED_PROPERTY][0]["prev"] == {"body": "old body"}
    assert result["finding"]["body"] == "new body"


def test_no_reason_still_works_as_a_plain_update(mcp, graph):
    result = call(mcp, "update_node", id="app.World", patch={"set": {"label": "Plain"}})

    assert result["node"]["label"] == "Plain"
    assert SUPERSEDED_PROPERTY not in result["node"]["properties"]


# --- [23-B] 예약키 거부가 유지되는가 (새 예약키 2개가 생겼다) ---


def test_pushing_a_forged_superseded_property_is_still_refused(mcp):
    from fastmcp.exceptions import ToolError

    with pytest.raises(ToolError, match="reserved"):
        call(
            mcp, "push_node", id="app.Forged", label="x", type="class",
            properties={SUPERSEDED_PROPERTY: [{"prev": {"label": "never"}}]},
        )


def test_updating_a_forged_superseded_property_is_still_refused(mcp):
    from fastmcp.exceptions import ToolError

    with pytest.raises(ToolError, match="reserved"):
        call(
            mcp, "update_node", id="app.World",
            patch={"set": {"properties": {SUPERSEDED_PROPERTY: []}}},
        )


def test_a_forged_history_cannot_hide_behind_a_real_supersede(mcp, graph):
    """The server path writes history; the caller path must not — even in the
    same call that legitimately supersedes something."""
    from fastmcp.exceptions import ToolError

    with pytest.raises(ToolError, match="reserved"):
        call(
            mcp, "update_node", id="app.World",
            patch={"set": {"label": "v2", "properties": {SUPERSEDED_PROPERTY: []}}},
            reason="supersede",
        )
    assert SUPERSEDED_PROPERTY not in graph.get_node("app.World").properties


def test_forging_finding_history_through_the_tool_is_refused(mcp, graph):
    """A Finding's history is a field, not a properties key — so the properties
    guard does not cover it and _FINDING_SERVER_MANAGED must."""
    from fastmcp.exceptions import ToolError
    finding = graph.add_finding(title="t")

    with pytest.raises(ToolError, match="system-owned"):
        call(
            mcp, "update_finding", finding_id=finding.finding_id,
            patch={"set": {SUPERSEDED_PROPERTY: [{"prev": {"body": "never said this"}}]}},
        )
    assert graph.get_finding(finding.finding_id)._superseded == []


def test_erasing_history_through_the_tool_is_refused(mcp, graph):
    from fastmcp.exceptions import ToolError
    call(mcp, "update_node", id="app.World", patch={"set": {"label": "v2"}}, reason="supersede")

    with pytest.raises(ToolError, match="cannot be removed"):
        call(mcp, "update_node", id="app.World", patch={"remove": [SUPERSEDED_PROPERTY]})

    assert len(graph.get_node("app.World").properties[SUPERSEDED_PROPERTY]) == 1
