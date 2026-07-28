"""Completion verification for TASK M2e — [M2] undo/redo command history.

Covers the coordinator-approved design (D-1…D-6): every mutation kind reverses
correctly (push / update / delete-cascade / clear / cite / supersede / finding /
import / push_batch), redo re-applies, a new mutation clears redo, the ``_suspended``
replay guard stops undo/redo from recording fresh history, a full replace clears
both stacks, the stack is FIFO-capped at 100, undo re-publishes [8-C] events, the
MCP undo()/redo() tools and the WS undo/redo ops work — and the ★security
invariant the gate required: undo is not a reserved-key / server-field forgery
vector, because it only ever replays a server-captured before-image.
"""

import asyncio

import pytest

from visualizebetter.graph.core import Graph
from visualizebetter.mcp_server import create_server, import_payload
from visualizebetter.ws.hub import WSHub


def _graph() -> Graph:
    g = Graph(name="t")
    g.add_node(id="A", label="Alpha", type="class")
    g.add_node(id="B", label="Beta", type="service")
    g.add_edge(source="A", target="B", relation="calls")
    return g


def run(coro):
    return asyncio.run(coro)


def call(mcp, name, /, **kwargs):
    tool = run(mcp.get_tool(name))
    result = tool.fn(**kwargs)
    return run(result) if asyncio.iscoroutine(result) else result


# --- each mutation kind reverses correctly (D-3) ---


def test_undo_push_node_removes_it():
    g = Graph()
    g.add_node(id="A", label="A", type="class")
    r = g.undo()
    assert r["ok"] and r["label"] == "node.add"
    assert "A" not in g.nodes


def test_undo_push_edge_removes_edge_and_auto_placeholders():
    g = Graph()
    # both endpoints are auto-created placeholders — the whole thing is one command.
    g.add_edge(source="X", target="Y", relation="r")
    assert ("X", "Y", "r", "") in g.edges and "X" in g.nodes and "Y" in g.nodes
    g.undo()
    assert ("X", "Y", "r", "") not in g.edges
    assert "X" not in g.nodes and "Y" not in g.nodes  # placeholders undone too


def test_undo_update_node_restores_the_previous_value():
    g = _graph()
    g.update_node("A", {"set": {"label": "Alpha2"}})
    assert g.nodes["A"].label == "Alpha2"
    g.undo()
    assert g.nodes["A"].label == "Alpha"


def test_undo_delete_node_cascade_restores_node_edges_and_adjacency():
    g = _graph()
    g.delete_node("A", cascade=True)
    assert "A" not in g.nodes and ("A", "B", "calls", "") not in g.edges
    g.undo()
    assert "A" in g.nodes and ("A", "B", "calls", "") in g.edges
    # ★ adjacency is rebuilt, so a subsequent cascade delete still finds the edge.
    assert g.indices.edges_of("A") == {("A", "B", "calls", "")}


def test_undo_clear_all_restores_the_whole_graph():
    g = _graph()
    before = (sorted(g.nodes), sorted(k for k in g.edges))
    g.clear_all()
    assert not g.nodes and not g.edges
    g.undo()
    assert (sorted(g.nodes), sorted(k for k in g.edges)) == before
    assert g.indices.edges_of("A") == {("A", "B", "calls", "")}


def test_undo_clear_layer_restores_that_layer():
    g = Graph()
    g.add_node(id="A", label="A", type="class", layer="ai-1")
    g.add_node(id="B", label="B", type="class", layer="ai-2")
    g.clear_layer("ai-1")
    assert "A" not in g.nodes and "B" in g.nodes
    g.undo()
    assert "A" in g.nodes and g.nodes["A"].layer == "ai-1"
    assert "ai-1" in g.layers


def test_undo_cite_removes_the_citation():
    g = _graph()
    g.cite("A", "ida://0x401000", "sub_401000")
    assert g.nodes["A"].properties.get("_citations")
    g.undo()
    assert "_citations" not in g.nodes["A"].properties


def test_undo_finding_add_update_delete():
    g = _graph()
    f = g.add_finding(title="F1", node_ids=["A"])
    fid = f.finding_id
    g.undo()
    assert fid not in g.findings
    g.redo()
    assert fid in g.findings
    g.update_finding(fid, {"set": {"title": "F2"}})
    g.undo()
    assert g.findings[fid].title == "F1"
    g.delete_finding(fid)
    g.undo()
    assert fid in g.findings


# --- redo, redo-clear, replay guard (D-5) ---


def test_redo_reapplies_the_undone_mutation():
    g = Graph()
    g.add_node(id="A", label="A", type="class")
    g.undo()
    assert "A" not in g.nodes
    r = g.redo()
    assert r["ok"] and "A" in g.nodes


def test_a_new_mutation_clears_the_redo_stack():
    g = Graph()
    g.add_node(id="A", label="A", type="class")
    g.undo()
    assert g.history.can_redo()
    g.add_node(id="B", label="B", type="class")  # a genuine new mutation
    assert not g.history.can_redo()


def test_undo_redo_only_shuffle_between_stacks_never_record():
    """★replay guard: undo/redo must not record fresh history (no double-capture)."""
    g = Graph()
    g.add_node(id="A", label="A", type="class")
    assert (len(g.history.undo_stack), len(g.history.redo_stack)) == (1, 0)
    g.undo()
    assert (len(g.history.undo_stack), len(g.history.redo_stack)) == (0, 1)
    g.redo()
    assert (len(g.history.undo_stack), len(g.history.redo_stack)) == (1, 0)


def test_an_ineffective_mutation_does_not_clear_redo():
    g = _graph()
    g.add_node(id="Z", label="Z", type="class")
    g.undo()  # redo now holds add(Z)
    assert g.history.can_redo()
    # A still has an edge → cascade=False is refused and mutates nothing.
    result = g.delete_node("A", cascade=False)
    assert result["ok"] is False
    assert g.history.can_redo()  # the no-op recorded nothing, redo survives


def test_nothing_to_undo_or_redo_is_a_clean_error():
    g = Graph()
    assert g.undo() == {"ok": False, "error": "nothing_to_undo"}
    assert g.redo() == {"ok": False, "error": "nothing_to_redo"}


# --- batching: import / push_batch are one command (D-6) ---


def test_import_merge_is_a_single_undo_command():
    g = Graph()
    import_payload(
        g,
        {"nodes": [{"id": "x", "label": "X", "type": "class"},
                   {"id": "y", "label": "Y", "type": "class"}]},
        merge=True,
    )
    assert "x" in g.nodes and "y" in g.nodes
    assert len(g.history.undo_stack) == 1  # one step reverses the whole import
    g.undo()
    assert "x" not in g.nodes and "y" not in g.nodes


def test_batch_command_groups_many_mutations_into_one():
    g = Graph()
    with g.batch_command("push_batch"):
        g.add_node(id="a", label="a", type="class")
        g.add_node(id="b", label="b", type="class")
    assert len(g.history.undo_stack) == 1
    g.undo()
    assert "a" not in g.nodes and "b" not in g.nodes


# --- full replace clears both stacks (D-6) ---


def test_full_replace_reload_from_clears_history():
    g = _graph()
    g.add_node(id="C", label="C", type="class")
    assert g.history.can_undo()
    g.reload_from(Graph())
    assert not g.history.can_undo() and not g.history.can_redo()


def test_import_replace_clears_history():
    g = _graph()
    assert g.history.can_undo()
    import_payload(g, {"nodes": [{"id": "z", "label": "Z", "type": "class"}]}, merge=False)
    # a replace import goes through reload_from → the pre-replace history is gone.
    assert not g.history.can_undo() and not g.history.can_redo()


# --- bound: FIFO cap at 100 (D-6) ---


def test_history_is_fifo_capped_at_100():
    g = Graph()
    for i in range(150):
        g.add_node(id=f"n{i}", label="x", type="class")
    assert len(g.history.undo_stack) == 100
    # The oldest 50 commands (n0..n49) were evicted; undoing all 100 leaves them.
    for _ in range(100):
        g.undo()
    assert all(f"n{i}" in g.nodes for i in range(50))
    assert all(f"n{i}" not in g.nodes for i in range(50, 150))


# --- [8-C] broadcast (D-5) ---


def test_undo_republishes_ordinary_events():
    g = Graph()
    g.add_node(id="A", label="A", type="class")
    ops: list[str] = []
    g.events.subscribe(lambda e: ops.append(e.op))
    g.undo()
    assert "node.delete" in ops  # the hub, subscribed to this bus, would broadcast it
    ops.clear()
    g.redo()
    assert "node.add" in ops


# --- ★ security: undo is not a forgery vector (D-3) ---


def test_mutation_guard_undo_uses_the_before_image_not_the_after():
    """★ If the recorder stored the after-image as 'before', undo would restore the
    post-mutation value and this fails — the core guarantee of a before-image undo."""
    g = Graph()
    g.add_node(id="A", label="v1", type="class")
    g.update_node("A", {"set": {"label": "v2"}})
    g.undo()
    assert g.nodes["A"].label == "v1"  # before, never v2
    g.redo()
    assert g.nodes["A"].label == "v2"


def test_history_images_are_isolated_deep_copies():
    g = Graph()
    g.add_node(id="A", label="A", type="class", properties={"k": [1]})
    g.update_node("A", {"set": {"label": "A2"}})
    # Mutate the live record in place — the captured before-image must not share it.
    g.nodes["A"].properties["k"].append(999)
    g.undo()
    assert g.nodes["A"].properties["k"] == [1]


def test_undo_is_not_a_reserved_key_forgery_vector():
    g = Graph()
    g.add_node(id="A", label="A", type="class")
    # A caller cannot set a reserved key directly ([23-B]) — it never enters history.
    with pytest.raises(ValueError):
        g.update_node("A", {"set": {"_superseded": [{"prev": {"label": "forged"}}]}})
    # A legitimate supersede archives a *server-authored* entry (server shape)...
    g.update_node("A", {"set": {"label": "A2"}}, reason="supersede")
    entry = g.nodes["A"].properties["_superseded"][0]
    assert set(entry) == {"prev", "at", "by"}
    # ...undo removes the whole archive (restores the before-image)...
    g.undo()
    assert "_superseded" not in g.nodes["A"].properties
    # ...and redo restores exactly the server archive — undo/redo only ever move
    # server-captured state, so no caller text can pose as recorded history.
    g.redo()
    assert g.nodes["A"].properties["_superseded"] == [entry]


def test_undo_of_correction_restores_the_provenance_before_image():
    g = Graph()
    g.add_node(id="A", label="right", type="class")
    g.update_node("A", {"set": {"label": "corrected"}}, reason="correction")
    assert g.nodes["A"].properties.get("_provenance")
    g.undo()
    assert "_provenance" not in g.nodes["A"].properties
    assert g.nodes["A"].label == "right"


# --- MCP undo()/redo() tools ---


def test_mcp_undo_redo_tools_round_trip():
    g = Graph()
    mcp = create_server(g)
    call(mcp, "push_node", id="A", label="A", type="class")
    r = call(mcp, "undo")
    assert r["ok"] and r["label"] == "node.add" and "A" not in g.nodes
    r = call(mcp, "redo")
    assert r["ok"] and "A" in g.nodes


def test_mcp_undo_tool_reports_nothing_to_undo():
    g = Graph()
    mcp = create_server(g)
    assert call(mcp, "undo") == {"ok": False, "error": "nothing_to_undo"}


# --- WS undo/redo op (D-4 UI round-trip) ---


def test_ws_undo_redo_ops_reverse_and_reapply_for_all_clients():
    g = Graph()
    g.add_node(id="A", label="A", type="class")
    hub = WSHub(g)
    hub.subscribe()
    seen: list[str] = []
    g.events.subscribe(lambda e: seen.append(e.op))

    run(hub.handle_client_event(None, {"op": "undo", "data": {}}))
    assert "A" not in g.nodes and "node.delete" in seen  # broadcast to the bus

    run(hub.handle_client_event(None, {"op": "redo", "data": {}}))
    assert "A" in g.nodes and "node.add" in seen


def test_ws_undo_op_is_accepted_by_the_protocol_validator():
    from visualizebetter.ws.protocol import CLIENT_EVENT_ADAPTER

    assert CLIENT_EVENT_ADAPTER.validate_python({"op": "undo", "data": {}}).op == "undo"
    assert CLIENT_EVENT_ADAPTER.validate_python({"op": "redo"}).op == "redo"


# --- M2g #2: undo event type follows the state transition ([7-D] no-lurch) ---


def _ops(graph):
    seen = []
    graph.events.subscribe(lambda e: seen.append(e.op))
    return seen


def test_undo_of_an_attribute_update_publishes_node_update_not_node_add():
    # ★ mutation guard: an attribute-only undo must NOT re-publish node.add — that
    # sends the overview down the structural reseed path ([7-D] ~2.2s settle). It
    # is an in-place restore (before and after both present) -> node.update.
    g = Graph()
    g.add_node(id="A", label="v1", type="class")
    g.update_node("A", {"set": {"label": "v2"}})
    seen = _ops(g)
    g.undo()
    assert seen == ["node.update"]
    assert g.nodes["A"].label == "v1"


def test_undo_of_cite_publishes_node_update_that_removes_the_citation():
    g = Graph()
    g.add_node(id="A", label="A", type="class")
    g.cite("A", "ida://0x1", "s")
    seen = []
    payloads = []
    g.events.subscribe(lambda e: (seen.append(e.op), payloads.append(e.data)))
    g.undo()
    assert seen == ["node.update"]
    # merge alone cannot drop a key, so the reconcile patch removes _citations.
    assert payloads[0]["patch"].get("remove") == ["_citations"]
    assert "_citations" not in g.nodes["A"].properties


def test_undo_of_a_delete_recreates_with_node_add():
    g = Graph()
    g.add_node(id="A", label="A", type="class")
    g.delete_node("A")
    seen = _ops(g)
    g.undo()  # the node was absent, so its restore is a re-creation
    assert seen == ["node.add"]
    assert "A" in g.nodes


def test_undo_of_an_add_removes_with_node_delete():
    g = Graph()
    seen: list[str] = []
    g.events.subscribe(lambda e: seen.append(e.op))
    g.add_node(id="A", label="A", type="class")
    seen.clear()
    g.undo()
    assert seen == ["node.delete"]
    assert "A" not in g.nodes


def test_undo_of_an_edge_attribute_update_publishes_edge_update():
    g = Graph()
    g.add_node(id="A", label="A", type="class")
    g.add_node(id="B", label="B", type="class")
    g.add_edge(source="A", target="B", relation="r", weight=1.0)
    g.update_edge("A", "B", "r", "", patch={"set": {"weight": 5.0}})
    seen = _ops(g)
    g.undo()
    assert seen == ["edge.update"]
    assert g.get_edge("A", "B", "r", "").weight == 1.0


def test_undo_of_a_finding_attribute_update_publishes_finding_update():
    g = Graph()
    f = g.add_finding(title="F1")
    g.update_finding(f.finding_id, {"set": {"title": "F2"}})
    seen = _ops(g)
    g.undo()
    assert seen == ["finding.update"]
    assert g.findings[f.finding_id].title == "F1"


def test_inplace_undo_reconstructs_state_exactly_via_the_patch():
    # The node.update patch must reproduce the before-image on a client applying it:
    # simulate the client's merge-set + remove and compare to the real restored node.
    g = Graph()
    g.add_node(id="A", label="A", type="class", properties={"keep": 1, "gone": 2})
    g.update_node("A", {"set": {"label": "B", "properties": {"gone": 9, "new": 3}}})
    payloads = []
    g.events.subscribe(lambda e: payloads.append(e.data))
    # snapshot the client's view (= current server node) before undo
    before_client = g.nodes["A"].to_dict()
    g.undo()
    patch = payloads[0]["patch"]
    # apply the patch the way the frontend applyPatch does
    client = dict(before_client)
    client["properties"] = dict(before_client["properties"])
    for k, v in patch["set"].items():
        if k == "properties":
            client["properties"] = {**client["properties"], **v}
        else:
            client[k] = v
    for k in patch.get("remove", []):
        client["properties"].pop(k, None)
    assert client == g.nodes["A"].to_dict()  # client matches the restored server node
