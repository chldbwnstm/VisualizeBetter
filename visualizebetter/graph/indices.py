"""Graph indices ([14]).

Minimal set required by Graph Core today: ``adjacency`` (node id -> edge keys
touching it, both directions) backs delete_node's cascade rule ([5-A]), and
``by_type`` backs type lookups. by_layer and the wider index set land with the
tasks that need them.

★ Assumption these structures rest on ([13-B] CH1(4)): every key written here —
node id, node ``type``, edge endpoints — is a hashable ``str``. Nothing in this
module checks that, because the check belongs where records are created: core's
``check_new_record``/``check_field_types`` refuse a non-str before anything
reaches an index. When that contract was missing, a ``dict`` type reached
``by_type`` and raised mid-write, leaving the record committed but unindexed.
Callers must not bypass those two.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from visualizebetter.graph.core import Edge, EdgeKey, Node


class Indices:
    def __init__(self) -> None:
        self.by_type: dict[str, set[str]] = {}
        self.adjacency: dict[str, set[EdgeKey]] = {}

    # --- nodes ---

    def add_node(self, node: Node) -> None:
        self.by_type.setdefault(node.type, set()).add(node.id)
        self.adjacency.setdefault(node.id, set())

    def retype_node(self, node_id: str, old_type: str, new_type: str) -> None:
        if old_type == new_type:
            return
        bucket = self.by_type.get(old_type)
        if bucket is not None:
            bucket.discard(node_id)
            if not bucket:
                del self.by_type[old_type]
        self.by_type.setdefault(new_type, set()).add(node_id)

    def remove_node(self, node: Node) -> None:
        bucket = self.by_type.get(node.type)
        if bucket is not None:
            bucket.discard(node.id)
            if not bucket:
                del self.by_type[node.type]
        self.adjacency.pop(node.id, None)

    # --- edges ---

    def add_edge(self, key: EdgeKey, edge: Edge) -> None:
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
