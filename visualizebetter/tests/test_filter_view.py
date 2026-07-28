"""Completion verification for TASK V (backend) — shared filter view.

The hub evaluates a human's filter with the [6] DSL, stores it, and broadcasts the
visible set; the [5-C] read tools let the AI see the same set. An invalid filter
must not crash the hub and must not change the shared filter.
"""

import asyncio
import json

import pytest
from fastmcp import Client

from visualizebetter.graph.core import Graph
from visualizebetter.mcp_server import create_server
from visualizebetter.ws.hub import WSHub
from visualizebetter.ws.protocol import CLIENT_EVENT_ADAPTER


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
    g.add_node(id="a", label="Aa", type="class")
    g.add_node(id="b", label="Bb", type="service")
    g.add_node(id="c", label="Cc", type="class")
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


def run(coro):
    return asyncio.run(coro)


def filter_set(hub, conn, expression):
    event = CLIENT_EVENT_ADAPTER.validate_python(
        {"op": "filter.set", "data": {"expression": expression}}
    )
    return run(hub._client_filter_set(conn, event))


# --- hub filter.set DSL evaluation ([8-C], [6]) ---


def test_valid_filter_computes_visible_set(hub, conn):
    filter_set(hub, conn, 'type == "class"')
    assert hub.visible_ids == {"a", "c"}
    assert hub.active_filter == 'type == "class"'


def test_valid_filter_broadcasts_visible_ids(hub, conn):
    filter_set(hub, conn, 'type == "class"')
    run(hub.flush())
    (msg,) = [m for m in conn.messages if m["op"] == "filter.set"]
    assert sorted(msg["data"]["visible_ids"]) == ["a", "c"]
    assert msg["data"]["error"] is None


def test_group_function_filter_evaluates(hub, conn, graph):
    graph.add_edge(source="a", target="b", relation="uses")
    filter_set(hub, conn, "degree(node) > 0")
    assert hub.visible_ids == {"a", "b"}


def test_empty_filter_clears_the_view(hub, conn):
    filter_set(hub, conn, 'type == "class"')
    filter_set(hub, conn, "")
    assert hub.active_filter is None
    assert hub.visible_ids is None


# --- ★ invalid filter: no crash, shared view unchanged, per-conn error ---


def test_invalid_filter_does_not_crash_and_keeps_the_shared_filter(hub, conn):
    filter_set(hub, conn, 'type == "class"')  # a good filter is active
    conn.sent.clear()

    filter_set(hub, conn, "type === nonsense")  # must not raise

    # The shared filter is untouched — nobody else's view lurches on a typo.
    assert hub.active_filter == 'type == "class"'
    assert hub.visible_ids == {"a", "c"}


def test_invalid_filter_replies_to_the_requester_only(hub, conn):
    other = FakeConnection()
    hub.register(other)
    filter_set(hub, conn, "type === nonsense")
    run(hub.flush())

    reply = conn.messages[-1]
    assert reply["op"] == "filter.set"
    assert reply["data"]["error"]
    assert reply["data"]["visible_ids"] == []
    # The other client heard nothing — the error is not broadcast.
    assert other.messages == []


def test_invalid_filter_reply_carries_current_seq(hub, conn, graph):
    # A reply without a seq would NaN the client's monotonic seq ([8-C]).
    graph.add_node(id="d", label="Dd", type="class")  # bumps the event seq
    filter_set(hub, conn, "within(bogus")
    reply = conn.messages[-1]
    assert reply["seq"] == graph.events.seq


def test_limit_breaching_filter_is_reported_not_crashed(hub, conn):
    filter_set(hub, conn, 'connected_to("a", within=99)')  # [6] within cap is 5
    reply = conn.messages[-1]
    assert reply["op"] == "filter.set"
    assert "within" in reply["data"]["error"]
    assert hub.active_filter is None  # never applied


# --- [5-C] get_active_filter / get_visible_nodes ---


@pytest.fixture
def server(graph, hub):
    return create_server(graph, session=hub)


def tool(server, name, **kwargs):
    t = run(server.get_tool(name))
    r = t.fn(**kwargs)
    return run(r) if asyncio.iscoroutine(r) else r


def test_get_active_filter_reports_expression_and_count(server, hub, conn):
    filter_set(hub, conn, 'type == "class"')
    assert tool(server, "get_active_filter") == {
        "expression": 'type == "class"',
        "matched_count": 2,
    }


def test_get_active_filter_null_when_no_filter(server):
    assert tool(server, "get_active_filter") == {"expression": None, "matched_count": None}


def test_get_visible_nodes_returns_the_matched_ids(server, hub, conn):
    filter_set(hub, conn, 'type == "class"')
    r = tool(server, "get_visible_nodes")
    assert r["ids"] == ["a", "c"]
    assert r["total"] == 2
    assert r["truncated"] is False


def test_get_visible_nodes_all_when_no_filter(server):
    r = tool(server, "get_visible_nodes")
    assert r["ids"] == ["a", "b", "c"]
    assert r["total"] == 3


def test_get_visible_nodes_pagination(server, hub, conn):
    filter_set(hub, conn, 'type == "class"')
    r = tool(server, "get_visible_nodes", limit=1, offset=0)
    assert r["ids"] == ["a"]
    assert r["total"] == 2
    assert r["truncated"] is True


def test_view_read_tools_absent_without_a_session(graph):
    async def names():
        async with Client(create_server(graph)) as c:
            return {t.name for t in await c.list_tools()}

    tools = run(names())
    assert "get_active_filter" not in tools
    assert "get_visible_nodes" not in tools


def test_view_read_does_not_mutate(server, hub, conn, graph):
    filter_set(hub, conn, 'type == "class"')
    graph.clear_dirty()
    events = []
    graph.events.subscribe(events.append)
    tool(server, "get_active_filter")
    tool(server, "get_visible_nodes")
    assert graph.dirty is False
    assert events == []


# --- ★ human → AI shared view: same set through both paths ---


def test_human_filter_then_ai_reads_the_same_set(server, hub, conn):
    # Human applies a filter over the WS; the AI reads it back via [5-C].
    filter_set(hub, conn, "degree(node) >= 0")  # matches every node
    visible = set(tool(server, "get_visible_nodes")["ids"])
    assert visible == set(hub.visible_ids)
    assert tool(server, "get_active_filter")["matched_count"] == len(visible)
