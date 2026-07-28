"""[M2] undo/redo — Graph Core command history (M2e, approved design D-1…D-6).

Design of record (M2e, confirmed):

- **D-1 Global stack.** One history for the whole graph — "undo the last graph
  mutation". ★Multi-client caveat: the stack is shared, so one client (or the
  human via the UI) can undo an action another client (an AI over MCP) made.
  That is intended for M2's single shared graph; per-actor / layer-scoped undo
  is deferred to M3 multi-AI attribution.

- **D-2 Undoable = graph-state mutations only:** node / edge / finding
  add·update·delete, ``cite``, ``clear_layer`` / ``clear_all``, and merge
  imports. **Excluded** (they are WSHub SessionState, not graph state, and are
  not snapshotted — [8-C], [4-C]): view / filter / style / layout / focus /
  snapshot. A **full replace** (``Graph.reload_from`` — snapshot load or a
  replace import) clears both stacks: the history would otherwise point at
  records that no longer exist.

- **D-3 Capture = before/after-image state diff per command.** A command records,
  for every node/edge/finding identity it touches, a deep copy of the record as
  it was *before* the mutation and as it is *after*. Undo restores the before
  images, redo the after images — one uniform path reverses create / update /
  delete / merge / supersede / cascade / clear with no per-op inverse logic.
  ★**Security:** only records the server itself holds are captured (deep-copied
  at mutation time). Caller input is never stored, so undo cannot become a
  channel for forging reserved keys (``_superseded`` / ``_provenance`` /
  ``_citations``) or server-managed fields — the [23-B] write protection holds
  through undo because undo replays a *server-authored* prior state.

- **D-5 On undo/redo the restore re-publishes ordinary mutation events**
  (``node.add`` upsert / ``node.delete`` / … ) on the graph's own event bus. The
  WS hub is subscribed to that bus, so the change fans out to every client with
  **no new [8-C] op** and the existing ``graph.batch`` coalescing applies. A
  ``_suspended`` guard stops the restore from recording *new* history (an
  infinite/double-capture loop otherwise). A brand-new mutation clears the redo
  stack; undo/redo themselves do not.

- **D-6 Bounds.** The undo stack keeps at most ``MAX_HISTORY`` commands, oldest
  evicted first. A bulk op (clear, import) is a single command.
"""

from __future__ import annotations

import copy
from contextlib import contextmanager
from dataclasses import fields
from typing import TYPE_CHECKING, Any, Iterator

from visualizebetter.graph.events import (
    EDGE_ADD,
    EDGE_DELETE,
    EDGE_UPDATE,
    FINDING_ADD,
    FINDING_DELETE,
    FINDING_UPDATE,
    NODE_ADD,
    NODE_DELETE,
    NODE_UPDATE,
)

if TYPE_CHECKING:
    from visualizebetter.graph.core import EdgeKey, Graph

# Identity fields — never patched (they are the record's key, [4-A]/[4-B]/[23-B]).
_NODE_ID_FIELDS = frozenset({"id"})
_EDGE_ID_FIELDS = frozenset({"source", "target", "relation", "key"})
_FINDING_ID_FIELDS = frozenset({"finding_id"})

# [D-6] command-count cap. A command is one user/AI-visible mutation (a single
# push, or one whole clear/import), so 100 steps of undo is generous while
# bounding memory — each command holds deep copies of only the records it
# touched.
MAX_HISTORY = 100

_NODES = "nodes"
_EDGES = "edges"
_FINDINGS = "findings"
_META_FIELDS = ("layers", "active_filter", "focus")


class _Command:
    """One undoable mutation: the before/after image of every record it touched.

    ``before``/``after`` map a collection name to ``{identity: image}`` where an
    image is a deep-copied record or ``None`` (the record was absent). ``meta``
    captures graph-level scalars ([4-C] layers / active_filter / focus) the
    command changed — only ``clear`` actually changes these.
    """

    __slots__ = ("label", "before", "after", "meta_before", "meta_after")

    def __init__(self, label: str) -> None:
        self.label = label
        self.before: dict[str, dict[Any, Any]] = {_NODES: {}, _EDGES: {}, _FINDINGS: {}}
        self.after: dict[str, dict[Any, Any]] = {_NODES: {}, _EDGES: {}, _FINDINGS: {}}
        self.meta_before: dict[str, Any] = {}
        self.meta_after: dict[str, Any] = {}

    def touch(self, collection: str, key: Any, current: Any) -> None:
        """Record the *before* image of ``key`` — only on the first touch."""
        table = self.before[collection]
        if key not in table:
            table[key] = copy.deepcopy(current)

    def capture_after(self, graph: Graph) -> None:
        """At commit: read the *after* image of every touched key, drop no-ops."""
        live = {
            _NODES: graph.nodes,
            _EDGES: graph.edges,
            _FINDINGS: graph.findings,
        }
        for collection, before in self.before.items():
            after = self.after[collection]
            source = live[collection]
            for key, before_image in list(before.items()):
                after_image = copy.deepcopy(source.get(key))
                if after_image == before_image:  # untouched in the end — prune it
                    del before[key]
                    continue
                after[key] = after_image
        # Keep only the meta fields the command actually changed.
        for field in _META_FIELDS:
            if self.meta_before.get(field) != self.meta_after.get(field):
                continue
            self.meta_before.pop(field, None)
            self.meta_after.pop(field, None)

    def is_effective(self) -> bool:
        return any(self.before[c] for c in (_NODES, _EDGES, _FINDINGS)) or bool(
            self.meta_before
        )


class GraphHistory:
    """The undo/redo stacks and the recorder that feeds them ([M2e] D-1…D-6)."""

    def __init__(self, graph: Graph, cap: int = MAX_HISTORY) -> None:
        self.graph = graph
        self.cap = cap
        self.undo_stack: list[_Command] = []
        self.redo_stack: list[_Command] = []
        self._current: _Command | None = None
        self._depth = 0
        self._suspended = False

    # --- recording (called from Graph Core mutations) ---

    @contextmanager
    def command(self, label: str) -> Iterator[None]:
        """Open a recording scope. Re-entrant: only the outermost commits.

        A no-op while suspended (during undo/redo) — the restore replays events
        directly and must not record fresh history.
        """
        if self._suspended:
            yield
            return
        outer = self._current is None
        if outer:
            self._current = _Command(label)
            self._snapshot_meta(self._current.meta_before)
        self._depth += 1
        try:
            yield
        finally:
            self._depth -= 1
            if outer:
                command = self._current
                self._current = None
                self._snapshot_meta(command.meta_after)
                command.capture_after(self.graph)
                if command.is_effective():
                    self.undo_stack.append(command)
                    self._trim(self.undo_stack)
                    # [D-5] a genuine new mutation invalidates the redo future.
                    self.redo_stack.clear()

    def touch_node(self, node_id: str) -> None:
        if self._current is not None:
            self._current.touch(_NODES, node_id, self.graph.nodes.get(node_id))

    def touch_edge(self, identity: EdgeKey) -> None:
        if self._current is not None:
            self._current.touch(_EDGES, identity, self.graph.edges.get(identity))

    def touch_finding(self, finding_id: str) -> None:
        if self._current is not None:
            self._current.touch(_FINDINGS, finding_id, self.graph.findings.get(finding_id))

    def clear(self) -> None:
        """[D-6] full replace (reload_from) drops both stacks."""
        self.undo_stack.clear()
        self.redo_stack.clear()
        self._current = None
        self._depth = 0

    # --- undo / redo ---

    def undo(self) -> dict[str, Any]:
        if not self.undo_stack:
            return {"ok": False, "error": "nothing_to_undo"}
        command = self.undo_stack.pop()
        changed = self._restore(command, command.before, command.meta_before)
        self.redo_stack.append(command)
        return {"ok": True, "label": command.label, "changed": changed}

    def redo(self) -> dict[str, Any]:
        if not self.redo_stack:
            return {"ok": False, "error": "nothing_to_redo"}
        command = self.redo_stack.pop()
        changed = self._restore(command, command.after, command.meta_after)
        self.undo_stack.append(command)
        return {"ok": True, "label": command.label, "changed": changed}

    def can_undo(self) -> bool:
        return bool(self.undo_stack)

    def can_redo(self) -> bool:
        return bool(self.redo_stack)

    # --- internals ---

    def _snapshot_meta(self, into: dict[str, Any]) -> None:
        graph = self.graph
        into["layers"] = list(graph.layers)
        into["active_filter"] = graph.active_filter
        into["focus"] = graph.focus

    def _trim(self, stack: list[_Command]) -> None:
        excess = len(stack) - self.cap
        if excess > 0:
            del stack[:excess]

    def _restore(
        self,
        command: _Command,
        images: dict[str, dict[Any, Any]],
        meta: dict[str, Any],
    ) -> dict[str, int]:
        """Set every touched record to ``images`` and re-publish the [8-C] events.

        Removals first (edges before nodes), then additions (nodes before edges),
        so the graph never holds a dangling edge mid-restore; findings last. Runs
        suspended, so the replayed events record no new history (D-5).
        """
        graph = self.graph
        counts = {_NODES: 0, _EDGES: 0, _FINDINGS: 0}
        self._suspended = True
        try:
            # 1) removals — edges before their nodes.
            for identity, image in images[_EDGES].items():
                if image is None:
                    self._set_edge(identity, None)
                    counts[_EDGES] += 1
            for node_id, image in images[_NODES].items():
                if image is None:
                    self._set_node(node_id, None)
                    counts[_NODES] += 1
            # 2) additions / in-place restores — nodes before their edges.
            for node_id, image in images[_NODES].items():
                if image is not None:
                    self._set_node(node_id, image)
                    counts[_NODES] += 1
            for identity, image in images[_EDGES].items():
                if image is not None:
                    self._set_edge(identity, image)
                    counts[_EDGES] += 1
            # 3) findings (no index, no dangling concern).
            for finding_id, image in images[_FINDINGS].items():
                self._set_finding(finding_id, image)
                counts[_FINDINGS] += 1
            # 4) graph-level scalars the command changed ([4-C]).
            for field in _META_FIELDS:
                if field in meta:
                    setattr(graph, field, copy.deepcopy(meta[field]))
        finally:
            self._suspended = False
        graph._touch()  # a restore is a state change → snapshot-dirty ([23-C])
        return {k: counts[k] for k in (_NODES, _EDGES, _FINDINGS)}

    def _set_node(self, node_id: str, image: Any) -> None:
        """Restore one node to ``image`` and publish the matching [8-C] event.

        ★ The event *type* follows the state transition, so the overview only pays
        the structural (reseed + settle) cost when the graph's shape actually
        changed ([7-D] "no lurch on an attribute change"):
          - re-created (was absent)      -> node.add
          - removed    (image is None)   -> node.delete
          - in-place   (present, present)-> node.update  (attribute-only; no reseed)
        """
        graph = self.graph
        current = graph.nodes.get(node_id)
        if image is None:
            if current is not None:
                # remove_node drops the node's adjacency; its edges are removed in
                # the same command (edges-before-nodes), so nothing is orphaned.
                graph.indices.remove_node(current)
                del graph.nodes[node_id]
                graph.events.publish(NODE_DELETE, {"id": node_id})
            return
        node = copy.deepcopy(image)
        if current is None:
            # A truly re-created node starts with empty adjacency; the edges this
            # command restores repopulate it (nodes-before-edges), as add_edge does.
            graph.indices.add_node(node)
            graph.nodes[node_id] = node
            graph._track_layer(node.layer)
            graph.events.publish(NODE_ADD, node.to_dict())
            return
        # In-place restore (undo of update/cite/merge/supersede): keep adjacency
        # intact — remove_node would wipe the edge keys touching this node. Only the
        # type bucket can change, exactly as update_node uses retype_node. Publish
        # node.update, not node.add, so the overview treats it as an overlay change.
        patch = _reconcile_patch(current, node, _NODE_ID_FIELDS)
        graph.indices.retype_node(node_id, current.type, node.type)
        graph.nodes[node_id] = node
        graph._track_layer(node.layer)
        graph.events.publish(NODE_UPDATE, {"id": node_id, "patch": patch})

    def _set_edge(self, identity: EdgeKey, image: Any) -> None:
        """Restore one edge to ``image``; event type follows the transition (see
        _set_node): re-created -> edge.add, removed -> edge.delete, in-place ->
        edge.update. An edge's identity is immutable, so an in-place restore leaves
        the adjacency index untouched."""
        graph = self.graph
        current = graph.edges.get(identity)
        source, target, relation, key = identity
        if image is None:
            if current is not None:
                graph.indices.remove_edge(identity, current)
                del graph.edges[identity]
                graph.events.publish(
                    EDGE_DELETE,
                    {"source": source, "target": target, "relation": relation, "key": key},
                )
            return
        edge = copy.deepcopy(image)
        if current is None:
            graph.edges[identity] = edge
            graph.indices.add_edge(identity, edge)
            graph._track_layer(edge.layer)
            graph.events.publish(EDGE_ADD, edge.to_dict())
            return
        patch = _reconcile_patch(current, edge, _EDGE_ID_FIELDS)
        graph.edges[identity] = edge  # identity unchanged -> adjacency index stands
        graph._track_layer(edge.layer)
        graph.events.publish(
            EDGE_UPDATE,
            {"source": source, "target": target, "relation": relation, "key": key, "patch": patch},
        )

    def _set_finding(self, finding_id: str, image: Any) -> None:
        """Restore one finding; event type follows the transition (see _set_node):
        re-created -> finding.add, removed -> finding.delete, in-place ->
        finding.update. Findings are not part of the overview topology, so this is
        for event-shape consistency rather than the [7-D] reseed concern."""
        graph = self.graph
        current = graph.findings.get(finding_id)
        if image is None:
            if current is not None:
                del graph.findings[finding_id]
                graph.events.publish(FINDING_DELETE, {"finding_id": finding_id})
            return
        finding = copy.deepcopy(image)
        graph.findings[finding_id] = finding
        graph._track_layer(finding.layer)
        if current is None:
            graph.events.publish(FINDING_ADD, finding.to_dict())
            return
        # Finding has no properties map ([23-B]); a full-field set reconstructs it
        # under the client's merge-apply of finding.update.
        patch = {"set": {
            f.name: copy.deepcopy(getattr(finding, f.name))
            for f in fields(finding)
            if f.name not in _FINDING_ID_FIELDS
        }}
        graph.events.publish(FINDING_UPDATE, {"finding_id": finding_id, "patch": patch})


def _reconcile_patch(current: Any, target: Any, id_fields: frozenset[str]) -> dict[str, Any]:
    """A node/edge update patch that turns ``current`` into ``target`` on the client.

    ``set`` carries every non-identity field of ``target`` (the client overwrites
    scalars and merges ``properties``); ``remove`` lists property keys ``current``
    holds that ``target`` lacks — a merge alone cannot drop them (undo of cite must
    remove ``_citations``, undo of supersede must remove ``_superseded``). Together
    they reconstruct ``target`` exactly via the client's applyPatch.
    """
    set_fields = {
        f.name: copy.deepcopy(getattr(target, f.name))
        for f in fields(target)
        if f.name not in id_fields
    }
    patch: dict[str, Any] = {"set": set_fields}
    removed = [k for k in current.properties if k not in target.properties]
    if removed:
        patch["remove"] = removed
    return patch
