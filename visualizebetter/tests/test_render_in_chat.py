"""Completion verification for TASK M2c — [9-D] render_in_chat MCP Apps card.

Covers the tool (valid MCP Apps _meta, correct neighbor subgraph, missing-node
error, READ no-mutation), the ui:// resource (spec-compliant UI mime + HTML), and
the [11] security the gate required: ★<script> breakout defence for AI/import-
sourced labels, a strict no-external CSP, and no serve URL/token in the card.
"""

import asyncio
import json
import re

import pytest
from fastmcp import Client
from fastmcp.apps import UI_MIME_TYPE
from fastmcp.exceptions import ToolError

from visualizebetter.graph.core import Graph
from visualizebetter.mcp_server import create_server
from visualizebetter.render_in_chat import build_subgraph, render_card_html


@pytest.fixture
def graph():
    g = Graph(name="test")
    g.add_node(id="A", label="Alpha", type="class")
    g.add_node(id="B", label="Beta", type="service")
    g.add_node(id="C", label="Gamma", type="module")
    g.add_edge(source="A", target="B", relation="calls")
    g.add_edge(source="A", target="C", relation="ref")
    g.add_edge(source="B", target="C", relation="uses")  # not incident to A
    return g


@pytest.fixture
def mcp(graph):
    return create_server(graph)


def run(coro):
    return asyncio.run(coro)


def call(mcp, name, /, **kwargs):
    tool = run(mcp.get_tool(name))
    result = tool.fn(**kwargs)
    return run(result) if asyncio.iscoroutine(result) else result


# --- the tool ---


def test_render_in_chat_carries_mcp_apps_meta(mcp):
    tool = run(mcp.get_tool("render_in_chat"))
    # ★ the io.modelcontextprotocol/ui app config lands in the tool _meta.
    assert "ui" in (tool.meta or {})


def test_render_in_chat_returns_neighbor_counts(mcp):
    r = call(mcp, "render_in_chat", node_id="A")
    assert r["node_id"] == "A"
    assert r["neighbor_count"] == 2  # B and C
    assert r["edge_count"] == 2      # A→B, A→C (B→C is not incident to A)


def test_render_in_chat_missing_node_is_tool_error(mcp):
    with pytest.raises(ToolError, match="not found"):
        call(mcp, "render_in_chat", node_id="nope")


def test_render_in_chat_does_not_mutate(graph, mcp):
    graph.clear_dirty()
    events: list = []
    graph.events.subscribe(events.append)
    call(mcp, "render_in_chat", node_id="A")
    assert graph.dirty is False
    assert events == []


# --- subgraph correctness ---


def test_build_subgraph_is_the_one_hop_neighborhood(graph):
    sg = build_subgraph(graph, "A")
    assert sg["root"] == {"id": "A", "label": "Alpha", "type": "class"}
    assert {n["id"] for n in sg["neighbors"]} == {"B", "C"}
    # only edges incident to A
    assert {(e["source"], e["target"]) for e in sg["edges"]} == {("A", "B"), ("A", "C")}


# --- the ui:// resource (MCP Apps format) ---


def test_ui_resource_returns_ui_mime_html(mcp):
    async def go():
        async with Client(mcp) as c:
            return await c.read_resource("ui://render_in_chat/A")

    contents = run(go())
    (content,) = contents
    assert content.mimeType == UI_MIME_TYPE  # "text/html;profile=mcp-app"
    assert "<!doctype html>" in content.text.lower()
    assert "Alpha" in content.text and "Beta" in content.text  # subgraph embedded


# --- [11] security ---


def _json_block(html: str) -> str:
    after = html.split('id="visualizebetter-subgraph">', 1)[1]
    return after.split("</script>", 1)[0]


def test_card_json_embed_cannot_break_out_of_script(graph):
    # A hostile, AI/import-sourced label. Escaping the *rendered* label is not
    # enough — the JSON text in <script> must not close the tag or open a comment.
    graph.add_node(id="X", label='</script><img src=x onerror=alert(1)>', type="class")
    graph.add_edge(source="A", target="X", relation="ref")
    html = render_card_html(build_subgraph(graph, "A"))
    block = _json_block(html)

    # ★ no raw breakout sequences survive in the embedded JSON.
    assert "</script>" not in block
    assert "<!--" not in block
    assert "<img" not in block
    # ...and the payload is preserved, just \uXXXX-escaped (JSON.parse restores it).
    assert "\\u003c/script\\u003e" in block
    # sanity: the escaped JSON still parses back to the original label.
    restored = json.loads(block)
    assert restored["neighbors"][0]["label"] == '</script><img src=x onerror=alert(1)>' \
        or any(n["label"] == '</script><img src=x onerror=alert(1)>' for n in restored["neighbors"])


def test_card_has_strict_no_external_csp(graph):
    html = render_card_html(build_subgraph(graph, "A"))
    assert "default-src 'none'" in html
    assert "connect-src 'none'" in html  # ★ no external network from the card
    assert "img-src 'none'" in html


def test_card_carries_no_serve_url_or_token(graph):
    html = render_card_html(build_subgraph(graph, "A")).lower()
    assert "ws://" not in html and "127.0.0.1" not in html and "localhost" not in html
    assert "token" not in html and "authorization" not in html


# [13-B] CH2(2) — the one place the card is allowed to assign markup: an
# empty-state div built from a literal, with nothing caller-supplied in it.
MARKER = "visualizebetter-subgraph"

_ALLOWED_INNER_HTML = re.compile(
    r"""wrap\.innerHTML = '<div id="empty">no subgraph</div>';"""
)


def _template_code(html: str) -> str:
    """The card minus its embedded JSON payload — i.e. the part we author.

    [13-B] CH2(2) — the original assertion grepped the payload and *only* the
    payload; the correct scope is its exact complement. Graph data is data: a
    node label may legitimately contain any string, including "innerHTML", and a
    guard over the authored template must not care what a label says.
    """
    parts = html.split(MARKER)
    if len(parts) < 3:
        return html  # marker shape changed — grep everything rather than nothing
    return parts[0] + MARKER.join(parts[2:])


def test_card_renders_labels_as_svg_text_not_html(graph):
    """Labels reach the DOM via textContent on an SVG <text>, never innerHTML.

    [13-B] CH2(2) — this used to read
    ``html.split("visualizebetter-subgraph")[1]``, and that window was **7% of
    the document**: the marker appears twice (script id, then getElementById), so
    `[1]` is the text *between* them — the embedded JSON payload — while the code
    that renders labels sits outside it. The assertion therefore could not fail
    for the reason it claimed. Worse, it could fail for a reason it did not
    claim: a node label containing the literal string "innerHTML" lands in that
    JSON payload and reddens the test with nothing unsafe happening (both
    reproduced). Now it greps the exact complement — everything we author, none
    of the data.

    ★ This is a **grep guard, not a safety proof.** What actually proves labels
    cannot become markup is the behavioural test in
    ``frontend/e2e/render_in_chat.spec.ts`` — it loads the card in a real browser
    with a ``</script><img src=x onerror=...>`` label and asserts zero ``img``
    elements. This one just keeps a second ``innerHTML =`` from appearing in the
    template unnoticed, over the 93% we wrote rather than the 7% we did not.
    """
    html = render_card_html(build_subgraph(graph, "A"))
    assert ".textContent = " in html

    remaining = _ALLOWED_INNER_HTML.sub("", html)
    assert "innerHTML" not in remaining, (
        "the card template gained a new innerHTML assignment; if it is genuinely"
        " safe, extend _ALLOWED_INNER_HTML with the exact literal and say why"
    )


def test_a_label_named_innerHTML_does_not_redden_this_test(graph):
    """[13-B] CH2(2) — 창 방식이 만들던 **거짓 실패**가 사라졌음을 고정한다.

    라벨은 JSON 페이로드로 들어가므로, 옛 단언은 라벨 문자열이 'innerHTML' 이기만
    하면 안전과 무관하게 빨개졌다(재현됨)."""
    graph.add_node(id="odd", label="innerHTML", type="class")
    html = render_card_html(build_subgraph(graph, "odd"))
    assert "innerHTML" in html  # 전제: 라벨이 실제로 문서에 실린다
    assert "innerHTML" not in _ALLOWED_INNER_HTML.sub("", _template_code(html))
