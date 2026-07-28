"""[9-D] render_in_chat — a compact neighbor-subgraph card for the Claude Desktop
chat, embedded via the MCP Apps extension (io.modelcontextprotocol/ui).

The card is **self-contained** ([11]): the subgraph is a server-built snapshot
embedded in the HTML, rendered by inline JS/SVG. It never links back to serve — the
MCP Apps sandbox is allow-scripts only (no allow-same-origin), so a card could not
reach serve, and must not carry the serve URL or a token. The live full view is the
separate browser window ([9-D]); this is a snapshot.

Security ([11], the [구조 변경] gate's conditions):
  - The subgraph data is AI/import-sourced, so the JSON embedded in <script> is the
    key injection surface. `_safe_json_for_script` escapes ``<``, ``>``, ``&`` (and
    the JS line separators) to their \\uXXXX forms, so the text can contain no
    ``</script>``, ``<!--`` or ``-->`` breakout — JSON.parse restores the originals.
  - Labels render as SVG <text> textContent (never innerHTML), so no markup a label
    carries is interpreted.
  - A strict inline CSP (default-src 'none', inline script/style only, no external
    connect/img/font) blocks every external request.
"""

from __future__ import annotations

import json
from typing import Any

from visualizebetter.graph.core import Graph

RENDER_IN_CHAT_URI_TEMPLATE = "ui://render_in_chat/{node_id}"

_SUBGRAPH_PLACEHOLDER = "__VISUALIZEBETTER_SUBGRAPH_JSON__"


def _safe_json_for_script(data: Any) -> str:
    """[11] JSON safe to drop inside a <script> — no tag/comment breakout.

    Escaping the rendered labels is not enough: the JSON *text* must not contain
    ``</script>``, ``<!--`` or ``-->``. Escaping ``<``/``>``/``&`` to \\uXXXX (valid
    inside a JSON string, decoded by JSON.parse) removes every such sequence, and
    U+2028/U+2029 are escaped because they are raw newlines in a JS string.
    """
    text = json.dumps(data, ensure_ascii=False)
    return (
        text.replace("&", "\\u0026")
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace(" ", "\\u2028")
        .replace(" ", "\\u2029")
    )


def build_subgraph(graph: Graph, node_id: str) -> dict[str, Any]:
    """[5-B] the node's 1-hop undirected neighbor subgraph (READ-only, no mutation).

    Reuses the same adjacency get_neighbors/_neighbor_summary read from ([5-B] Q1
    undirected): the root, its distinct neighbors, and the incident edges.
    """
    root = graph.get_node(node_id)
    if root is None:
        raise KeyError(node_id)
    neighbors: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []
    seen: set[str] = set()
    for source, target, relation, _key in graph.indices.edges_of(node_id):
        edges.append({"source": source, "target": target, "relation": relation})
        other = target if source == node_id else source
        if other == node_id or other in seen:
            continue
        node = graph.get_node(other)
        if node is None:
            continue
        seen.add(other)
        neighbors.append({"id": node.id, "label": node.label, "type": node.type})
    return {
        "root": {"id": root.id, "label": root.label, "type": root.type},
        "neighbors": neighbors,
        "edges": edges,
    }


def render_card_html(subgraph: dict[str, Any]) -> str:
    """The self-contained MCP Apps card ([9-D]) for a subgraph — data embedded."""
    return _CARD_TEMPLATE.replace(_SUBGRAPH_PLACEHOLDER, _safe_json_for_script(subgraph))


# The whole card in one string ([9-D] "template 1 file + Python JSON injection").
# default-src 'none' + inline-only: no external network, matching the self-contained
# rule. The render is intentionally light (inline SVG) — cosmos/cytoscape belong to
# the full browser app, not a chat card.
_CARD_TEMPLATE = (
    """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; img-src 'none'; connect-src 'none'; font-src 'none'; base-uri 'none'; form-action 'none'; frame-src 'none'" />
<title>VisualizeBetter</title>
<style>
  html,body{margin:0;background:#020617;color:#e2e8f0;font:13px system-ui,sans-serif}
  #wrap{position:relative;width:100%;height:320px;overflow:hidden}
  svg{width:100%;height:100%;display:block}
  .edge{stroke:#334155;stroke-width:1.5}
  .node{cursor:default}
  .node circle{stroke:#0f172a;stroke-width:1.5}
  .label{fill:#94a3b8;font-size:11px;pointer-events:none}
  #tip{position:absolute;display:none;background:#0f172a;border:1px solid #334155;
       border-radius:6px;padding:4px 8px;font-size:11px;color:#e2e8f0;pointer-events:none;max-width:220px}
  #empty{padding:16px;color:#64748b}
</style>
</head>
<body>
<div id="wrap"><div id="tip"></div></div>
<script type="application/json" id="visualizebetter-subgraph">"""
    + _SUBGRAPH_PLACEHOLDER
    + """</script>
<script>
(function(){
  var data;
  try { data = JSON.parse(document.getElementById('visualizebetter-subgraph').textContent); }
  catch(e){ data = null; }
  var wrap = document.getElementById('wrap');
  if(!data || !data.root){ wrap.innerHTML = '<div id="empty">no subgraph</div>'; return; }
  var W = wrap.clientWidth || 480, H = 320, cx = W/2, cy = H/2;
  var SVGNS = 'http://www.w3.org/2000/svg';
  function el(n, a){ var e = document.createElementNS(SVGNS, n); for(var k in a) e.setAttribute(k, a[k]); return e; }
  var color = { root:'#38bdf8' };
  function typeColor(t){ return ({class:'#a78bfa',service:'#34d399',component:'#fbbf24',module:'#f472b6'})[t] || '#64748b'; }

  var svg = el('svg', {viewBox:'0 0 '+W+' '+H});
  // positions: root center, neighbors on a ring
  var pos = {}; pos[data.root.id] = {x:cx, y:cy};
  var ns = data.neighbors || [];
  var R = Math.min(cx, cy) - 44;
  for(var i=0;i<ns.length;i++){
    var ang = (2*Math.PI*i)/Math.max(1, ns.length) - Math.PI/2;
    pos[ns[i].id] = {x: cx + R*Math.cos(ang), y: cy + R*Math.sin(ang)};
  }
  // edges
  (data.edges||[]).forEach(function(ed){
    var a = pos[ed.source], b = pos[ed.target];
    if(a && b) svg.appendChild(el('line', {class:'edge', x1:a.x, y1:a.y, x2:b.x, y2:b.y}));
  });
  var tip = document.getElementById('tip');
  function drawNode(nd, isRoot){
    var p = pos[nd.id]; if(!p) return;
    var g = el('g', {class:'node'});
    g.appendChild(el('circle', {cx:p.x, cy:p.y, r:isRoot?16:11, fill:isRoot?color.root:typeColor(nd.type)}));
    var t = el('text', {class:'label', x:p.x, y:p.y + (isRoot?30:24), 'text-anchor':'middle'});
    t.textContent = (nd.label || nd.id);           // textContent — never HTML
    g.appendChild(t);
    // hover tooltip = label + type ([9-D] minimal interactivity)
    g.addEventListener('mousemove', function(ev){
      tip.textContent = (nd.label||nd.id) + '  ·  ' + (nd.type||'');
      tip.style.display='block'; tip.style.left=(ev.clientX+10)+'px'; tip.style.top=(ev.clientY+10)+'px';
    });
    g.addEventListener('mouseleave', function(){ tip.style.display='none'; });
    svg.appendChild(g);
  }
  ns.forEach(function(n){ drawNode(n, false); });
  drawNode(data.root, true);
  wrap.appendChild(svg);
})();
</script>
</body>
</html>
"""
)
