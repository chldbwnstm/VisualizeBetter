"""Completion verification for TASK Y — [5-D] AI visual overlays.

apply_style / clear_style / add_annotation broadcast temporary-overlay WS ops.
apply_style resolves its [6] selector server-side and passes the style through a
strict allowlist ([11]); none of the three touches the graph.
"""

import asyncio
import json

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

from visualizebetter.graph.core import Graph
from visualizebetter.mcp_server import create_server
from visualizebetter.ws.hub import WSHub


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
    g.add_node(id="c", label="C", type="class")
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


def last(hub, conn):
    run(hub.flush())
    return conn.messages[-1]


# --- apply_style: selector evaluation ([5-D], [6]) ---


def test_apply_style_resolves_selector_to_ids(server, hub, conn):
    result = tool(server, "apply_style", selector='type == "class"', style={"color": "#ff8800"})
    msg = last(hub, conn)
    assert msg["op"] == "style.apply"
    assert msg["data"]["ids"] == ["a", "c"]
    assert msg["data"]["style"] == {"color": "#ff8800"}
    assert result["style_id"] == msg["data"]["style_id"]
    assert result["count"] == 2


def test_apply_style_ttl_is_passed(server, hub, conn):
    tool(server, "apply_style", selector='type == "service"', style={"size": 20}, ttl=7)
    assert last(hub, conn)["data"]["ttl"] == 7


def test_apply_style_invalid_selector_is_tool_error(server, conn):
    with pytest.raises(ToolError, match="invalid style selector"):
        tool(server, "apply_style", selector="type === bad", style={"color": "#fff"})
    assert conn.messages == []


def test_apply_style_style_ids_are_deterministic(server, hub, conn):
    a = tool(server, "apply_style", selector='type == "class"', style={"color": "#111111"})
    b = tool(server, "apply_style", selector='type == "class"', style={"color": "#222222"})
    assert a["style_id"] == "style-1"
    assert b["style_id"] == "style-2"


# --- ★ apply_style: [11] style allowlist ---


def test_apply_style_rejects_unknown_keys(server, conn):
    with pytest.raises(ToolError, match="unsupported keys"):
        tool(server, "apply_style", selector='type == "class"', style={"background": "url(x)"})
    assert conn.messages == []


def test_apply_style_rejects_a_non_colour_string(server):
    with pytest.raises(ToolError, match="color must be"):
        tool(server, "apply_style", selector='type == "class"', style={"color": "javascript:alert(1)"})


@pytest.mark.parametrize("color", ["#f80", "#ff8800", "#ff8800cc", "rgba(255,0,0,0.5)", "rgb(1,2,3)"])
def test_apply_style_accepts_valid_colours(server, hub, conn, color):
    tool(server, "apply_style", selector='type == "class"', style={"color": color})
    assert last(hub, conn)["data"]["style"]["color"] == color


def test_apply_style_clamps_size(server, hub, conn):
    tool(server, "apply_style", selector='type == "class"', style={"size": 9999})
    assert last(hub, conn)["data"]["style"]["size"] == 100.0


def test_apply_style_clamps_size_lower_bound(server, hub, conn):
    # ★ below _STYLE_MIN_SIZE clamps up — mutation: drop max(_STYLE_MIN_SIZE, …) → 0.
    tool(server, "apply_style", selector='type == "class"', style={"size": 0})
    assert last(hub, conn)["data"]["style"]["size"] == 1.0


def test_apply_style_rejects_non_number_size(server):
    with pytest.raises(ToolError, match="size must be a number"):
        tool(server, "apply_style", selector='type == "class"', style={"size": "huge"})


def test_apply_style_validates_border(server, hub, conn):
    tool(server, "apply_style", selector='type == "class"', style={"border": {"color": "#fff", "width": 3}})
    assert last(hub, conn)["data"]["style"]["border"] == {"color": "#fff", "width": 3.0}
    with pytest.raises(ToolError, match="border"):
        tool(server, "apply_style", selector='type == "class"', style={"border": {"radius": 5}})


def test_apply_style_clamps_border_width(server, hub, conn):
    # ★ over/under the range clamps — mutation: drop the max(0, min(20, …)) clamp.
    tool(server, "apply_style", selector='type == "class"', style={"border": {"width": 9999}})
    assert last(hub, conn)["data"]["style"]["border"]["width"] == 20.0
    tool(server, "apply_style", selector='type == "class"', style={"border": {"width": -5}})
    assert last(hub, conn)["data"]["style"]["border"]["width"] == 0.0


def test_apply_style_rejects_non_number_border_width(server):
    with pytest.raises(ToolError, match="border width must be a number"):
        tool(server, "apply_style", selector='type == "class"', style={"border": {"width": "thick"}})


def test_apply_style_rejects_invalid_border_color(server):
    # The border color goes through the same [11] colour allowlist as `color`.
    with pytest.raises(ToolError, match="color must be hex or rgb"):
        tool(server, "apply_style", selector='type == "class"', style={"border": {"color": "url(x)"}})


def test_apply_style_rejects_an_empty_style(server):
    with pytest.raises(ToolError, match="at least one"):
        tool(server, "apply_style", selector='type == "class"', style={})


# --- clear_style ([5-D]) ---


def test_clear_style_one(server, hub, conn):
    tool(server, "clear_style", style_id="style-1")
    msg = last(hub, conn)
    assert msg["op"] == "style.clear"
    assert msg["data"]["style_id"] == "style-1"


def test_clear_style_all(server, hub, conn):
    tool(server, "clear_style")
    assert last(hub, conn)["data"]["style_id"] is None


# --- add_annotation ([5-D]) ---


def test_add_annotation_broadcasts_with_coords(server, hub, conn):
    result = tool(server, "add_annotation", x=10.0, y=20.0, text="look here", ttl=3)
    msg = last(hub, conn)
    assert msg["op"] == "annotation.add"
    assert msg["data"] == {
        "annotation_id": result["annotation_id"],
        "x": 10.0,
        "y": 20.0,
        "text": "look here",
        "ttl": 3,
    }


def test_annotation_ids_are_deterministic(server, hub, conn):
    a = tool(server, "add_annotation", x=0, y=0, text="one")
    b = tool(server, "add_annotation", x=0, y=0, text="two")
    assert a["annotation_id"] == "annotation-1"
    assert b["annotation_id"] == "annotation-2"


# --- graph unchanged ([5-D] 무변형) ---


def test_overlays_do_not_mutate_the_graph(server, hub, conn, graph):
    graph.clear_dirty()
    before = (len(graph.nodes), len(graph.edges))
    tool(server, "apply_style", selector='type == "class"', style={"color": "#fff"})
    tool(server, "clear_style")
    tool(server, "add_annotation", x=1, y=2, text="t")
    assert (len(graph.nodes), len(graph.edges)) == before
    assert graph.dirty is False


def test_overlay_tools_absent_without_a_session(graph):
    async def names():
        async with Client(create_server(graph)) as c:
            return {t.name for t in await c.list_tools()}

    tools = run(names())
    for name in ("apply_style", "clear_style", "add_annotation"):
        assert name not in tools
