"""Completion verification for TASK Z4 — [5-E] JSON import/export.

Covers export (JSON inside the data dir, size, filter subgraph, no caller path,
unsupported format), import_graph (counts, merge/replace, 1MB cap, ★reserved-key
forgery blocked, idempotent), import_from_file (★path traversal / outside-root
refused), the ★round-trip (export → replace-import = same logical graph), and the
★[15] KPI (10K nodes < 5s). READ-vs-write: export never mutates.
"""

import asyncio
import inspect
import json
import time
from pathlib import Path

import pytest
from fastmcp.exceptions import ToolError

from visualizebetter.graph.core import Graph
from visualizebetter.graph.snapshots import AutoSnapshotter, SnapshotStore
from visualizebetter.mcp_server import MAX_IMPORT_INLINE_BYTES, create_server


@pytest.fixture
def graph():
    g = Graph(name="test")
    g.add_node(
        id="a", label="Alpha", type="class",
        properties={"field_count": 3}, tags=["core"], layer="L1",
    )
    g.add_node(id="b", label="Beta", type="service")
    g.add_edge(source="a", target="b", relation="calls", weight=2.0, properties={"note": "x"})
    g.add_finding(title="gold", node_ids=["a"], confidence=0.9)
    return g


@pytest.fixture
def store(tmp_path):
    return SnapshotStore(tmp_path / "data")


@pytest.fixture
def snapshotter(graph, store):
    return AutoSnapshotter(graph, store)


@pytest.fixture
def mcp(graph, store, snapshotter):
    return create_server(graph, store=store, snapshotter=snapshotter)


def run(coro):
    return asyncio.run(coro)


def call(mcp, tool_name, /, **kwargs):
    tool = run(mcp.get_tool(tool_name))
    result = tool.fn(**kwargs)
    return run(result) if asyncio.iscoroutine(result) else result


def _fresh(tmp_path, name):
    """A fresh empty server + store, so import counts start from zero."""
    g = Graph(name=name)
    st = SnapshotStore(tmp_path / name)
    m = create_server(g, store=st, snapshotter=AutoSnapshotter(g, st))
    return g, m, st


def _logical(g: Graph):
    """The content the round-trip must preserve — not server timestamps/ids."""
    return {
        "nodes": {
            n.id: (n.label, n.type, n.properties, n.layer, tuple(n.tags))
            for n in g.nodes.values()
        },
        "edges": {
            (e.source, e.target, e.relation, e.key, e.weight, tuple(sorted(e.properties.items())))
            for e in g.edges.values()
        },
        "findings": sorted((f.title, tuple(f.node_ids), f.confidence) for f in g.findings.values()),
    }


# --- export_graph ---


def test_export_writes_json_inside_the_data_dir(mcp, store):
    r = call(mcp, "export_graph")
    assert r["format"] == "json"
    p = Path(r["path"])
    assert p.is_file() and p.suffix == ".json"
    # ★ inside the server data directory, never a caller-chosen location ([11]).
    assert p.resolve().is_relative_to(store.data_dir.resolve())
    assert r["size"] == p.stat().st_size


def test_export_payload_carries_the_graph(mcp):
    doc = json.loads(Path(call(mcp, "export_graph")["path"]).read_text("utf-8"))
    assert {n["id"] for n in doc["nodes"]} == {"a", "b"}
    assert len(doc["edges"]) == 1
    assert doc["findings"][0]["title"] == "gold"


def test_export_has_no_caller_path_parameter(mcp):
    # ★ export exposes only format+filter — the caller cannot direct the output path.
    tool = run(mcp.get_tool("export_graph"))
    params = set(inspect.signature(tool.fn).parameters)
    assert "path" not in params
    assert params <= {"format", "filter"}


def test_export_filter_is_a_subgraph(mcp):
    doc = json.loads(
        Path(call(mcp, "export_graph", filter='type == "class"')["path"]).read_text("utf-8")
    )
    assert {n["id"] for n in doc["nodes"]} == {"a"}  # only the class node
    assert doc["edges"] == []  # b excluded → the a→b edge drops from the subgraph


def test_export_unsupported_format_is_a_clear_error(mcp):
    # graphml/dot/cytoscape are supported since M2d; a truly-unknown format errors.
    with pytest.raises(ToolError, match="not supported"):
        call(mcp, "export_graph", format="yaml")


def test_export_does_not_mutate(mcp, graph):
    graph.clear_dirty()
    events: list = []
    graph.events.subscribe(events.append)
    call(mcp, "export_graph")
    assert graph.dirty is False
    assert events == []


# --- import_graph ---


def test_import_graph_adds_counts(tmp_path):
    g, m, _ = _fresh(tmp_path, "imp1")
    payload = {
        "nodes": [{"id": "x", "label": "X", "type": "class"}, {"id": "y", "label": "Y", "type": "class"}],
        "edges": [{"source": "x", "target": "y", "relation": "calls"}],
    }
    assert call(m, "import_graph", data=payload) == {"added_nodes": 2, "added_edges": 1}
    assert set(g.nodes) == {"x", "y"}


def test_import_graph_accepts_a_json_string(tmp_path):
    g, m, _ = _fresh(tmp_path, "imp2")
    r = call(m, "import_graph", data=json.dumps({"nodes": [{"id": "x", "label": "X", "type": "class"}]}))
    assert r["added_nodes"] == 1


def test_import_graph_merge_is_idempotent(tmp_path):
    g, m, _ = _fresh(tmp_path, "imp3")
    payload = {"nodes": [{"id": "x", "label": "X", "type": "class"}]}
    call(m, "import_graph", data=payload)
    again = call(m, "import_graph", data=payload)
    assert again["added_nodes"] == 0  # nothing new
    assert len(g.nodes) == 1          # ★ not doubled — identity idempotent


def test_import_graph_merge_false_replaces(tmp_path):
    g, m, _ = _fresh(tmp_path, "imp4")
    g.add_node(id="old", label="Old", type="class")
    r = call(m, "import_graph", data={"nodes": [{"id": "new", "label": "New", "type": "class"}]}, merge=False)
    assert set(g.nodes) == {"new"}   # ★ full replace, old gone
    assert r["added_nodes"] == 1


def test_import_graph_over_1mb_is_rejected(tmp_path):
    g, m, _ = _fresh(tmp_path, "imp5")
    with pytest.raises(ToolError, match="exceeds"):
        call(m, "import_graph", data="x" * (MAX_IMPORT_INLINE_BYTES + 10))
    assert len(g.nodes) == 0


def test_import_reserved_key_forgery_is_blocked_atomically(tmp_path):
    g, m, _ = _fresh(tmp_path, "imp6")
    forged = {"nodes": [{"id": "x", "label": "X", "type": "class",
                         "properties": {"_citations": [{"url": "evil"}]}}]}
    with pytest.raises(ToolError, match="reserved"):
        call(m, "import_graph", data=forged)
    assert "x" not in g.nodes  # ★ nothing applied — fail-closed, atomic


def test_import_drops_server_managed_finding_history(tmp_path):
    g, m, _ = _fresh(tmp_path, "imp7")
    # A forged finding history must not be trusted: _superseded is not importable.
    call(m, "import_graph", data={"findings": [
        {"title": "t", "node_ids": [], "_superseded": [{"prev": {"x": 1}, "at": "z", "by": "attacker"}]}
    ]})
    (finding,) = g.findings.values()
    assert finding._superseded == []  # server-managed history was dropped, not forged


# --- import_from_file (path safety [11]) ---


def test_import_from_file_reads_inside_data_dir(tmp_path):
    g, m, st = _fresh(tmp_path, "imp8")
    (st.data_dir / "in.json").write_text(
        json.dumps({"nodes": [{"id": "x", "label": "X", "type": "class"}]}), "utf-8"
    )
    assert call(m, "import_from_file", path="in.json")["added_nodes"] == 1
    assert "x" in g.nodes


def test_import_from_file_refuses_dotdot_traversal(tmp_path):
    g, m, st = _fresh(tmp_path, "imp9")
    (st.data_dir.parent / "secret.json").write_text(
        json.dumps({"nodes": [{"id": "leak", "label": "L", "type": "class"}]}), "utf-8"
    )
    with pytest.raises(ToolError, match="within the server data directory"):
        call(m, "import_from_file", path="../secret.json")
    assert "leak" not in g.nodes


def test_import_from_file_refuses_absolute_path_outside_root(tmp_path):
    g, m, st = _fresh(tmp_path, "imp10")
    secret = tmp_path / "outside.json"
    secret.write_text(json.dumps({"nodes": []}), "utf-8")
    with pytest.raises(ToolError, match="within the server data directory"):
        call(m, "import_from_file", path=str(secret.resolve()))


# --- ★ round-trip: export → replace-import = the same logical graph ---


def test_round_trip_preserves_the_graph(mcp, graph):
    before = _logical(graph)
    exp = call(mcp, "export_graph")
    # A full-replace import from the exported file is export → (clear) → import.
    call(mcp, "import_from_file", path=Path(exp["path"]).name, merge=False)
    assert _logical(graph) == before


# --- ★ [15] KPI: bulk import stays fast ---


def test_import_from_file_10k_nodes_under_5s(tmp_path):
    g, m, st = _fresh(tmp_path, "perf")
    nodes = [{"id": f"n{i}", "label": f"N{i}", "type": "class"} for i in range(10_000)]
    (st.data_dir / "big.json").write_text(json.dumps({"nodes": nodes}), "utf-8")

    start = time.perf_counter()
    r = call(m, "import_from_file", path="big.json")
    elapsed = time.perf_counter() - start

    assert r["added_nodes"] == 10_000
    assert elapsed < 5.0, f"import of 10K nodes took {elapsed:.2f}s (KPI: <5s, [15])"


# --- CLI (cli.py [13]) ---

from typer.testing import CliRunner  # noqa: E402
from visualizebetter.cli import app  # noqa: E402

_runner = CliRunner()


def test_cli_export_then_import(tmp_path):
    data_dir = tmp_path / "cli-data"
    seed = Graph(name="seed")
    seed.add_node(id="x", label="X", type="class")
    seed.add_node(id="y", label="Y", type="class")
    seed.add_edge(source="x", target="y", relation="calls")
    run(SnapshotStore(data_dir).save_snapshot(seed, name="seed"))

    res = _runner.invoke(app, ["export", "--data-dir", str(data_dir)])
    assert res.exit_code == 0, res.output
    exported = res.output.strip().split("-> ")[-1]
    doc = json.loads(Path(exported).read_text("utf-8"))
    assert {n["id"] for n in doc["nodes"]} == {"x", "y"}

    data_dir2 = tmp_path / "cli-data2"
    res2 = _runner.invoke(app, ["import", exported, "--replace", "--data-dir", str(data_dir2)])
    assert res2.exit_code == 0, res2.output
    assert "imported 2 nodes, 1 edges" in res2.output
    snaps = run(SnapshotStore(data_dir2).list_snapshots())
    assert any(s["name"].startswith("import-") for s in snaps)


def test_cli_import_rejects_unsupported_format(tmp_path):
    bad = tmp_path / "g.xml"
    bad.write_text("{}", "utf-8")
    res = _runner.invoke(app, ["import", str(bad), "--format", "graphml", "--data-dir", str(tmp_path / "d")])
    assert res.exit_code != 0


# --- [8-C] import merge=False broadcasts a resync signal (Z5-A, audit #10) ---


def test_import_merge_false_broadcasts_snapshot_load(tmp_path):
    g, m, _ = _fresh(tmp_path, "resync")
    g.add_node(id="old", label="Old", type="class")
    events: list = []
    g.events.subscribe(events.append)

    call(m, "import_graph", data={"nodes": [{"id": "new", "label": "New", "type": "class"}]}, merge=False)

    # ★ A full replace must signal live clients to resync (like load_snapshot).
    # Mutation: drop the publish("snapshot.load", …) and this op is absent.
    assert "snapshot.load" in [e.op for e in events]


def test_import_merge_true_does_not_broadcast_snapshot_load(tmp_path):
    g, m, _ = _fresh(tmp_path, "resync2")
    events: list = []
    g.events.subscribe(events.append)
    call(m, "import_graph", data={"nodes": [{"id": "x", "label": "X", "type": "class"}]}, merge=True)
    # An additive merge publishes node adds, not a full-replace resync signal.
    assert "snapshot.load" not in [e.op for e in events]


# --- M2d: export graphml / dot / cytoscape ([5-E]) + ★[11] escape ---

import xml.dom.minidom  # noqa: E402
from visualizebetter.mcp_server import (  # noqa: E402
    _serialize_cytoscape,
    _serialize_dot,
    _serialize_graphml,
)


def test_export_graphml_is_valid_xml(mcp):
    r = call(mcp, "export_graph", format="graphml")
    assert r["format"] == "graphml"
    doc = xml.dom.minidom.parse(r["path"])  # parses ⇒ well-formed XML
    assert doc.getElementsByTagName("node")
    assert doc.getElementsByTagName("edge")


def test_export_dot_is_a_digraph(mcp):
    r = call(mcp, "export_graph", format="dot")
    assert r["format"] == "dot"
    text = Path(r["path"]).read_text("utf-8")
    assert text.startswith("digraph visualizebetter {")
    assert text.rstrip().endswith("}")
    assert " -> " in text  # an edge


def test_export_cytoscape_is_cytoscape_elements(mcp):
    r = call(mcp, "export_graph", format="cytoscape")
    assert r["format"] == "cytoscape"
    doc = json.loads(Path(r["path"]).read_text("utf-8"))
    assert set(doc["elements"]) == {"nodes", "edges"}
    assert doc["elements"]["nodes"][0]["data"]["id"]
    assert doc["elements"]["edges"][0]["data"]["source"]


def test_export_new_formats_land_in_the_data_dir(mcp, store):
    for fmt in ("graphml", "dot", "cytoscape"):
        p = Path(call(mcp, "export_graph", format=fmt)["path"])
        assert p.is_file()
        # ★ inside the server data dir — path safety identical to json ([11]).
        assert p.resolve().is_relative_to(store.data_dir.resolve())


def test_export_still_rejects_an_unknown_format(mcp):
    with pytest.raises(ToolError, match="not supported"):
        call(mcp, "export_graph", format="png")


def test_export_format_honours_the_filter_subgraph(mcp):
    doc = json.loads(
        Path(call(mcp, "export_graph", format="cytoscape", filter='type == "class"')["path"]).read_text("utf-8")
    )
    assert {n["data"]["id"] for n in doc["elements"]["nodes"]} == {"a"}  # only the class node


def _hostile_graph():
    g = Graph(name="hostile")
    g.add_node(id="A", label="Alpha", type="class")
    g.add_node(id="X", label='</node><evil a="1">& "q" ] ; injected [ {', type="class")
    g.add_edge(source="A", target="X", relation="calls")
    return g


def test_graphml_escapes_a_hostile_label_no_breakout():
    g = _hostile_graph()
    gml = _serialize_graphml(g, None)
    xml.dom.minidom.parseString(gml)  # ★ still well-formed despite the hostile label
    assert "</node><evil" not in gml   # the label did not close the element
    assert "&lt;/node&gt;" in gml      # it was XML-escaped instead


def test_dot_escapes_a_hostile_label_no_breakout():
    g = _hostile_graph()
    dot = _serialize_dot(g, None)
    # the raw label — with its unescaped quote — must not appear as dot syntax.
    assert '"] ; injected [' not in dot
    assert '\\"q\\"' in dot  # the inner quotes were escaped


def test_cytoscape_keeps_a_hostile_label_as_a_json_string():
    doc = json.loads(_serialize_cytoscape(_hostile_graph(), None))
    labels = [n["data"]["label"] for n in doc["elements"]["nodes"]]
    assert any(lbl.startswith("</node>") for lbl in labels)  # preserved, inert (JSON)


def test_export_graph_docstring_reflects_supported_formats(mcp):
    # M2d made graphml/dot/cytoscape real; the tool description (what the AI reads)
    # must not still call them "M2 미지원" — that would steer callers away from
    # formats that now work ([nit #10]/M2g #6).
    tool = run(mcp.get_tool("export_graph"))
    doc = (tool.description or "") + (tool.fn.__doc__ or "")
    assert "graphml" in doc and "cytoscape" in doc and "dot" in doc
    assert "미지원" not in doc


# --- [23-C] ★ 리네임 회귀: 구 봉투 키(mcpgraph_export) 파일은 계속 임포트된다 ---


def test_legacy_mcpgraph_export_envelope_still_imports(tmp_path):
    """리네임 전 export 파일의 봉투 키는 mcpgraph_export 였다. 사용자가 들고 있는
    구 파일이 새 바이너리에서 조용히 거부되면 안 된다 — import 는 봉투 키를
    검증하지 않고 nodes/edges/findings 만 읽는다는 계약을 회귀로 고정한다."""
    g, m, _ = _fresh(tmp_path, "imp_legacy_envelope")
    legacy_payload = {
        "mcpgraph_export": 1,  # 구 브랜드 봉투 키 — 무시되고 통과해야 한다
        "metadata": {"name": "old-world"},
        "layers": ["L1"],
        "nodes": [
            {"id": "n1", "label": "N1", "type": "class", "layer": "L1"},
            {"id": "n2", "label": "N2", "type": "service"},
        ],
        "edges": [{"source": "n1", "target": "n2", "relation": "calls"}],
        "findings": [{"title": "carried gold", "node_ids": ["n1"], "confidence": 0.9}],
    }

    result = json.loads(json.dumps(call(m, "import_graph", data=legacy_payload)))

    assert result["added_nodes"] == 2
    assert result["added_edges"] == 1
    assert "n1" in g.nodes and "n2" in g.nodes
    (finding,) = g.findings.values()
    assert finding.title == "carried gold"


# --- [13-B] CH1(5) — 거부 ⇒ 무변경이 사실이어야 한다 ---


_PARTIAL_PAYLOADS = [
    pytest.param(
        {"nodes": [{"id": "ok1", "label": "A", "type": "t"}, {"label": "B", "type": "t"}]},
        "missing 'id'",
        id="node-missing-id",
    ),
    pytest.param(
        {
            "nodes": [{"id": "ok1", "label": "A", "type": "t"}],
            "edges": [{"source": "ok1", "target": "ok1", "relation": "r"}, {"source": "ok1"}],
        },
        "missing",
        id="edge-missing-relation",
    ),
    pytest.param(
        {
            "nodes": [{"id": "ok1", "label": "A", "type": "t"}],
            "findings": [{"title": "good"}, {"body": "no title"}],
        },
        "missing 'title'",
        id="finding-missing-title",
    ),
    pytest.param(
        {"nodes": [{"id": "ok1", "label": "A", "type": "t"}, {"id": "bad", "type": {}}]},
        "",
        id="node-bad-value-type",
    ),
    pytest.param(
        {
            "nodes": [{"id": "ok1", "label": "A", "type": "t"}],
            "edges": [{"source": "ok1", "target": "ok1", "relation": "r", "weight": {}}],
        },
        "",
        id="edge-bad-value-type",
    ),
    pytest.param(
        {
            "nodes": [{"id": "ok1", "label": "A", "type": "t"}],
            "findings": [{"title": "x", "node_ids": 5}],
        },
        "",
        id="finding-bad-value-type",
    ),
]


@pytest.mark.parametrize(("payload", "_match"), _PARTIAL_PAYLOADS)
def test_a_rejected_import_applies_nothing(payload, _match):
    """(CH1-5) ★ 필수 키·값 타입 검사가 적용 루프 **안**에 있어, 40번째 노드에
    id 가 없으면 39개가 들어간 뒤 실패가 보고됐다. 호출자는 '아무 일도 없었다'는
    응답을 받는데 그래프는 이미 움직였고, merge=True 면 그 39개가 이벤트까지
    발행하고 undo 커맨드에 합류해 되돌릴 방법이 없었다."""
    from visualizebetter.mcp_server import import_payload

    graph = Graph()
    good = Graph()
    # 전제: 같은 payload 의 성한 부분은 정상적으로 들어간다 — 거부가 '원래 아무것도
    # 안 들어가는 payload' 때문이 아님을 먼저 증명한다
    import_payload(good, {"nodes": payload["nodes"][:1]}, merge=True)
    assert good.nodes, "성한 부분조차 들어가지 않았다 — 죽은 단언"

    with pytest.raises((ToolError, ValueError)):
        import_payload(graph, payload, merge=True)

    assert graph.nodes == {}
    assert graph.edges == {}
    assert graph.findings == {}
    assert not graph.dirty
    assert not graph.history.can_undo()


def test_a_rejected_merge_import_leaves_the_existing_graph_intact():
    """(CH1-5) 살아 있는 그래프 위 merge 에서도 같다 — 부분 적용은 기존 상태와
    섞여 되돌릴 수 없는 혼합물이 된다."""
    from visualizebetter.mcp_server import import_payload

    graph = Graph()
    graph.add_node(id="existing", label="E", type="t")
    before = graph.get_node("existing").to_dict()

    payload = {
        "nodes": [
            {"id": "new1", "label": "N1", "type": "t"},
            {"id": "new2", "label": "N2", "type": "t"},
            {"label": "nameless", "type": "t"},
        ]
    }
    with pytest.raises(ToolError):
        import_payload(graph, payload, merge=True)

    assert list(graph.nodes) == ["existing"]
    assert graph.get_node("existing").to_dict() == before


def test_a_valid_import_still_works():
    """(CH1-5) 회귀 — 사전 검사가 정상 payload 를 막지 않는다."""
    from visualizebetter.mcp_server import import_payload

    graph = Graph()
    result = import_payload(
        graph,
        {
            "nodes": [{"id": "a", "label": "A", "type": "t"}, {"id": "b", "label": "B", "type": "t"}],
            "edges": [{"source": "a", "target": "b", "relation": "calls"}],
            "findings": [{"title": "F", "node_ids": ["a"]}],
        },
        merge=True,
    )
    assert result == {"added_nodes": 2, "added_edges": 1}
    assert len(graph.findings) == 1
