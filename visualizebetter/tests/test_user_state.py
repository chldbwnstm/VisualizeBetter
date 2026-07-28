"""Completion verification for TASK W2 — [5-C] user-state tools.

The AI's window into what the human is doing: the focused node, the click
history, the viewport, and a pollable event stream — all read from the hub's
session state, none mutating the graph. The hub's clock is injected so every time
assertion is deterministic.
"""

import asyncio

import pytest
from fastmcp import Client

from visualizebetter.graph.core import Graph
from visualizebetter.mcp_server import create_server
from visualizebetter.ws.hub import (
    MAX_POLL_LIMIT,
    SELECTION_HISTORY_MAX,
    USER_EVENT_RING_MAX,
    WSHub,
)
from visualizebetter.ws.protocol import CLIENT_EVENT_ADAPTER


class FakeConnection:
    async def send(self, text: str) -> None:
        pass


class Clock:
    """A hand-cranked clock so since_ms / ts are exact ([5-C] 결정적 시간)."""

    def __init__(self, start: float = 1000.0):
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def graph():
    g = Graph(name="test")
    g.add_node(id="a", label="A", type="class")
    g.add_node(id="b", label="B", type="service")
    return g


@pytest.fixture
def hub(graph, clock):
    h = WSHub(graph, clock=clock)
    h.subscribe()
    return h


@pytest.fixture
def conn(hub):
    c = FakeConnection()
    hub.register(c)
    return c


def run(coro):
    return asyncio.run(coro)


def focus(hub, conn, node_id):
    event = CLIENT_EVENT_ADAPTER.validate_python({"op": "focus.set", "data": {"id": node_id}})
    return run(hub._client_focus_set(conn, event))


def filter_set(hub, conn, expression):
    event = CLIENT_EVENT_ADAPTER.validate_python(
        {"op": "filter.set", "data": {"expression": expression}}
    )
    return run(hub._client_filter_set(conn, event))


def view_update(hub, conn, mode, zoom, camera):
    event = CLIENT_EVENT_ADAPTER.validate_python(
        {"op": "view.update", "data": {"mode": mode, "zoom": zoom, "camera_pos": camera}}
    )
    return run(hub._client_view_update(conn, event))


# --- get_focused_node ([5-C]) ---


def test_no_focus_is_null(hub):
    assert hub.get_focused_node() is None


def test_focus_reports_id_and_elapsed_ms(hub, conn, clock):
    focus(hub, conn, "a")
    clock.advance(2.5)
    assert hub.get_focused_node() == {"id": "a", "since_ms": 2500}


def test_refocus_resets_the_clock(hub, conn, clock):
    focus(hub, conn, "a")
    clock.advance(5)
    focus(hub, conn, "b")
    clock.advance(1)
    assert hub.get_focused_node() == {"id": "b", "since_ms": 1000}


# --- get_selection_history ([5-C]) ---


def test_history_is_newest_first(hub, conn):
    for node in ("a", "b", "a"):
        focus(hub, conn, node)
    assert [h["id"] for h in hub.get_selection_history(10)] == ["a", "b", "a"][::-1]


def test_history_last_n_bounds_the_result(hub, conn):
    for node in ("a", "b", "a", "b"):
        focus(hub, conn, node)
    assert [h["id"] for h in hub.get_selection_history(2)] == ["b", "a"]


def test_history_carries_a_timestamp(hub, conn, clock):
    clock.advance(3)
    focus(hub, conn, "a")
    (entry,) = hub.get_selection_history(1)
    assert entry == {"id": "a", "ts": 1003.0}


def test_history_ring_is_bounded(hub, conn):
    for i in range(SELECTION_HISTORY_MAX + 20):
        focus(hub, conn, "a" if i % 2 else "b")
    assert len(hub.selection_history) == SELECTION_HISTORY_MAX


# --- get_view_state ([5-C], [9-C]) ---


def test_view_state_null_until_set(hub):
    assert hub.get_view_state() is None


def test_view_state_reflects_the_last_view_update(hub, conn):
    view_update(hub, conn, "split", 2.0, {"x": 10.0, "y": -5.0})
    state = hub.get_view_state()
    assert state["mode"] == "split"
    assert state["zoom"] == 2.0
    assert state["camera_pos"] == {"x": 10.0, "y": -5.0}


def test_view_state_most_recent_wins(hub, conn):
    view_update(hub, conn, "overview", 1.0, {"x": 0.0, "y": 0.0})
    view_update(hub, conn, "detail", 3.0, {"x": 1.0, "y": 1.0})
    assert hub.get_view_state()["mode"] == "detail"


# --- poll_events ([5-C]) ---


def test_poll_returns_focus_and_filter_changes(hub, conn):
    focus(hub, conn, "a")
    filter_set(hub, conn, 'type == "class"')
    result = hub.poll_events()
    kinds = [e["type"] for e in result["events"]]
    assert kinds == ["focus_change", "filter_change"]
    assert result["events"][0]["data"] == {"id": "a"}
    assert result["events"][1]["data"] == {"expression": 'type == "class"', "matched_count": 1}


def test_poll_only_returns_events_after_the_cursor(hub, conn):
    focus(hub, conn, "a")
    first = hub.poll_events()
    focus(hub, conn, "b")
    second = hub.poll_events(since_cursor=first["cursor"])
    assert [e["data"]["id"] for e in second["events"]] == ["b"]


def test_poll_cursor_advances_when_nothing_new(hub, conn):
    focus(hub, conn, "a")
    result = hub.poll_events()
    again = hub.poll_events(since_cursor=result["cursor"])
    assert again["events"] == []
    assert again["cursor"] == result["cursor"]


def test_poll_limit_caps_the_page(hub, conn):
    for _ in range(5):
        focus(hub, conn, "a")
    result = hub.poll_events(limit=2)
    assert len(result["events"]) == 2
    # The rest is not dropped — it is paginated; a follow-up poll gets it.
    assert result["dropped"] == 0
    rest = hub.poll_events(since_cursor=result["cursor"])
    assert len(rest["events"]) == 3


def test_poll_limit_is_capped_at_the_maximum(hub, conn):
    # More matching events than MAX_POLL_LIMIT are available in the ring...
    for _ in range(MAX_POLL_LIMIT + 50):
        focus(hub, conn, "a")
    result = hub.poll_events(since_cursor=0, limit=10_000)
    # ...so a request over the cap returns exactly MAX_POLL_LIMIT, not the count
    # asked for. ★ Mutation: drop `min(limit, MAX_POLL_LIMIT)` and this becomes 150.
    assert len(result["events"]) == MAX_POLL_LIMIT


def test_poll_event_types_filter(hub, conn):
    focus(hub, conn, "a")
    filter_set(hub, conn, 'type == "class"')
    only_focus = hub.poll_events(event_types=["focus_change"])
    assert [e["type"] for e in only_focus["events"]] == ["focus_change"]


def test_poll_counts_dropped_events(hub, conn):
    # Overflow the ring, then poll from the very beginning: the evicted events
    # are reported as dropped, not silently lost.
    for _ in range(USER_EVENT_RING_MAX + 25):
        focus(hub, conn, "a")
    result = hub.poll_events(since_cursor=0)
    assert result["dropped"] == 25
    assert len(result["events"]) <= MAX_POLL_LIMIT


def test_poll_cursor_is_independent_of_ws_seq(hub, conn, graph):
    # The [8-C] WS seq counts graph events; the poll cursor counts user-state
    # events. Push graph nodes (bump seq) without any focus/filter — the poll
    # cursor must not move.
    graph.add_node(id="c", label="C", type="x")
    graph.add_node(id="d", label="D", type="x")
    assert graph.events.seq >= 2
    focus(hub, conn, "a")
    result = hub.poll_events()
    # One user event → cursor 1, regardless of the WS seq being higher.
    assert result["cursor"] == 1
    assert result["cursor"] != graph.events.seq


# --- MCP tools ([5-C]) ---


@pytest.fixture
def server(graph, hub):
    return create_server(graph, session=hub)


def tool(server, name, **kwargs):
    t = run(server.get_tool(name))
    r = t.fn(**kwargs)
    return run(r) if asyncio.iscoroutine(r) else r


def test_tools_read_through_the_session(server, hub, conn, clock):
    focus(hub, conn, "a")
    clock.advance(1)
    assert tool(server, "get_focused_node") == {"id": "a", "since_ms": 1000}
    focus(hub, conn, "b")
    assert [h["id"] for h in tool(server, "get_selection_history")["history"]] == ["b", "a"]
    filter_set(hub, conn, 'type == "class"')
    kinds = [e["type"] for e in tool(server, "poll_events")["events"]]
    assert "focus_change" in kinds and "filter_change" in kinds


def test_user_state_tools_absent_without_a_session(graph):
    async def names():
        async with Client(create_server(graph)) as c:
            return {t.name for t in await c.list_tools()}

    tools = run(names())
    for name in ("get_focused_node", "get_selection_history", "get_view_state", "poll_events"):
        assert name not in tools


def test_user_state_reads_do_not_mutate(server, hub, conn, graph):
    focus(hub, conn, "a")
    filter_set(hub, conn, 'type == "class"')
    graph.clear_dirty()
    events = []
    graph.events.subscribe(events.append)
    tool(server, "get_focused_node")
    tool(server, "get_selection_history")
    tool(server, "get_view_state")
    tool(server, "poll_events")
    assert graph.dirty is False
    assert events == []


def test_poll_events_rejects_an_unknown_event_type(server):
    async def go():
        async with Client(server) as c:
            return await c.call_tool("poll_events", {"event_types": ["bogus"]})

    with pytest.raises(Exception):  # schema Literal rejects it
        run(go())
