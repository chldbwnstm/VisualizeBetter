"""Completion verification for TASK X — [5-D] AI → screen (suggest/navigate).

suggest_filter / focus_on / set_layout each broadcast a WS op so the human's
screen responds; none of them changes the graph. A bad filter suggestion is
refused at the tool, not broadcast.
"""

import asyncio
import json

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

from visualizebetter.graph.core import Graph
from visualizebetter.mcp_server import create_server
from visualizebetter.ws.hub import WSHub
from visualizebetter.ws.protocol import CLIENT_EVENT_ADAPTER  # noqa: F401  (parity import)


class FakeConnection:
    def __init__(self):
        self.sent: list[str] = []

    async def send(self, text: str) -> None:
        self.sent.append(text)

    @property
    def messages(self) -> list[dict]:
        return [json.loads(t) for t in self.sent]


@pytest.fixture
def graph():
    g = Graph(name="test")
    g.add_node(id="a", label="A", type="class")
    g.add_node(id="b", label="B", type="service")
    return g


@pytest.fixture
def hub(graph):
    h = WSHub(graph)
    h.subscribe()
    return h


@pytest.fixture
def conn(hub):
    c = FakeConnection()
    hub.register(c)
    return c


@pytest.fixture
def server(graph, hub):
    return create_server(graph, session=hub)


def run(coro):
    return asyncio.run(coro)


def tool(server, name, **kwargs):
    t = run(server.get_tool(name))
    r = t.fn(**kwargs)
    return run(r) if asyncio.iscoroutine(r) else r


def last_broadcast(hub, conn):
    run(hub.flush())
    return conn.messages[-1]


# --- suggest_filter ([5-D], [6]) ---


def test_suggest_filter_broadcasts_filter_suggest(server, hub, conn):
    tool(server, "suggest_filter", dsl_expr='type == "class"', reason="these are classes")
    msg = last_broadcast(hub, conn)
    assert msg["op"] == "filter.suggest"
    assert msg["data"] == {"expression": 'type == "class"', "reason": "these are classes"}


def test_suggest_filter_rejects_an_invalid_expression(server, conn):
    # A suggestion the human could not apply is worse than none — refused, not sent.
    with pytest.raises(ToolError, match="invalid filter suggestion"):
        tool(server, "suggest_filter", dsl_expr="type === bad", reason="x")
    assert conn.messages == []


def test_suggest_filter_validates_at_parse_time(server, hub, conn):
    # compile_filter is a parse check (syntax / length / depth) — the [6] within
    # cap is an evaluate-time limit, so within=99 parses and is broadcast; the
    # human's apply catches it via the shared-filter path (TASK V). A suggestion
    # only needs to be parseable to be shown.
    tool(server, "suggest_filter", dsl_expr='connected_to("a", within=99)', reason="x")
    assert last_broadcast(hub, conn)["op"] == "filter.suggest"


def test_suggest_filter_does_not_apply_a_filter(server, hub, conn):
    tool(server, "suggest_filter", dsl_expr='type == "class"', reason="r")
    # A suggestion is not an applied filter — the shared filter stays empty.
    assert hub.active_filter is None


# --- focus_on ([5-D]) ---


def test_focus_on_broadcasts_focus_set(server, hub, conn, graph):
    tool(server, "focus_on", node_id="a")
    msg = last_broadcast(hub, conn)
    assert msg["op"] == "focus.set"
    assert msg["data"] == {"id": "a"}
    assert graph.focus == "a"


def test_focus_on_accepts_a_zoom_hint_without_changing_the_op(server, hub, conn):
    # zoom_level is advisory in M1; the wire op stays focus.set{id} ([8-C] unchanged).
    tool(server, "focus_on", node_id="b", zoom_level=3.0)
    msg = last_broadcast(hub, conn)
    assert msg["data"] == {"id": "b"}


def test_focus_on_does_not_touch_human_focus_tracking(server, hub, conn):
    # [5-C] get_focused_node reports the human's choice; an AI focus_on must not
    # forge it into the click history.
    tool(server, "focus_on", node_id="a")
    assert hub.get_focused_node() is None
    assert hub.get_selection_history(10) == []


# --- set_layout ([5-D], [7-B]) ---


def test_set_layout_broadcasts_layout_set(server, hub, conn):
    tool(server, "set_layout", algorithm="dagre")
    msg = last_broadcast(hub, conn)
    assert msg["op"] == "layout.set"
    assert msg["data"]["algorithm"] == "dagre"


def test_set_layout_passes_options(server, hub, conn):
    tool(server, "set_layout", algorithm="preset", options={"positions": {"a": [1, 2]}})
    msg = last_broadcast(hub, conn)
    assert msg["data"]["options"] == {"positions": {"a": [1, 2]}}


def test_set_layout_rejects_an_unknown_algorithm(server):
    async def go():
        async with Client(server) as c:
            return await c.call_tool("set_layout", {"algorithm": "spiral"})

    with pytest.raises(Exception):  # schema Literal rejects it
        run(go())


# --- graph unchanged ([5-D] 무변형) ---


def test_view_control_does_not_mutate_the_graph(server, hub, conn, graph):
    graph.clear_dirty()
    before = (len(graph.nodes), len(graph.edges), len(graph.findings))
    tool(server, "suggest_filter", dsl_expr='type == "class"', reason="r")
    tool(server, "focus_on", node_id="a")
    tool(server, "set_layout", algorithm="fcose")
    assert (len(graph.nodes), len(graph.edges), len(graph.findings)) == before
    # These broadcast WS ops but change no graph data, so the graph is not dirty.
    assert graph.dirty is False


def test_view_control_tools_absent_without_a_session(graph):
    async def names():
        async with Client(create_server(graph)) as c:
            return {t.name for t in await c.list_tools()}

    tools = run(names())
    for name in ("suggest_filter", "focus_on", "set_layout"):
        assert name not in tools
