"""Graph indices ([14]).

Minimal set required by Graph Core today: ``adjacency`` (node id -> edge keys
touching it, both directions) backs delete_node's cascade rule ([5-A]), and
``by_type`` backs type lookups. by_layer and the wider index set land with the
tasks that need them.

★ Assumption these structures rest on ([13-B] CH1(1)): every key written here —
node id, node ``type``, edge endpoints — is a hashable ``str``. The main defence
is where records are created: core's ``check_new_record``/``check_field_types``
refuse a non-str before anything reaches an index, and core registers a record in
its index *before* its dict so "committed but unindexed" is unreachable.

That contract cannot be complete on its own, though — a ``str`` subclass with
``__hash__ = None`` satisfies every declared type and still explodes here. So
each mutator below hashes what it is about to write *first*, and only then
touches a structure. Without that, a write that fails halfway is worse than one
that fails outright: ``retype_node`` discarded the old bucket before adding the
new one, so a bad type did not merely fail to apply — it dropped the node out of
``by_type`` entirely, which is the unrecoverable state the whole CH1 item exists
to prevent.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from visualizebetter.graph.core import Edge, EdgeKey, Node


def _require_hashable(*keys: object) -> None:
    """Fail before the first write, not between two of them.

    Every mutator here writes more than one structure, so a key that raises
    partway through leaves the indices inconsistent with each other and with the
    records. Hashing up front turns that into a clean refusal.
    """
    for key in keys:
        hash(key)


class Indices:
    def __init__(self) -> None:
        self.by_type: dict[str, set[str]] = {}
        self.adjacency: dict[str, set[EdgeKey]] = {}

    # --- nodes ---

    def add_node(self, node: Node) -> None:
        _require_hashable(node.id, node.type)
        self.by_type.setdefault(node.type, set()).add(node.id)
        self.adjacency.setdefault(node.id, set())

    def retype_node(self, node_id: str, old_type: str, new_type: str) -> None:
        _require_hashable(node_id, new_type)
        if old_type == new_type:
            return
        # Add before discard: the reverse order dropped the node out of by_type
        # whenever the new bucket write failed.
        self.by_type.setdefault(new_type, set()).add(node_id)
        bucket = self.by_type.get(old_type)
        if bucket is not None:
            bucket.discard(node_id)
            if not bucket:
                del self.by_type[old_type]

    def remove_node(self, node: Node) -> None:
        bucket = self.by_type.get(node.type)
        if bucket is not None:
            bucket.discard(node.id)
            if not bucket:
                del self.by_type[node.type]
        self.adjacency.pop(node.id, None)

    # --- edges ---

    def add_edge(self, key: EdgeKey, edge: Edge) -> None:
        _require_hashable(key, edge.source, edge.target)
        self.adjacency.setdefault(edge.source, set()).add(key)
        self.adjacency.setdefault(edge.target, set()).add(key)

    def remove_edge(self, key: EdgeKey, edge: Edge) -> None:
        for endpoint in (edge.source, edge.target):
            bucket = self.adjacency.get(endpoint)
            if bucket is not None:
                bucket.discard(key)

    # --- queries ---

    def edges_of(self, node_id: str) -> set[EdgeKey]:
        """Edge keys with node_id as source or target."""
        return set(self.adjacency.get(node_id, ()))

    def nodes_of_type(self, type_: str) -> set[str]:
        return set(self.by_type.get(type_, ()))
