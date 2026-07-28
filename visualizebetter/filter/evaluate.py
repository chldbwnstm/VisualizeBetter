"""필터 DSL 평가기 ([6] 의미론).

Schema-less, per [6]: a missing key or a type mismatch makes that comparison
false — never an error, never three-valued. NOT flips that false like any other.
Ordered comparisons need both sides the same kind (both number or both string);
null matches only via ==null/!=null; there is no number/string coercion.

Group functions are undirected and reduce, where possible, to a set computed once
per expression (a BFS from the target, not per candidate node) so a filter over
10K nodes stays inside [6]'s 500ms budget. Signatures are ratified in
docs/filter-dsl.md.
"""

from __future__ import annotations

from collections import deque
from typing import TYPE_CHECKING, Any, Callable, Iterable

import re2

from visualizebetter.filter import ast
from visualizebetter.filter.ast import MISSING
from visualizebetter.filter.errors import FilterError, FilterLimitError, FilterSyntaxError
from visualizebetter.graph.core import is_reserved_property
from visualizebetter.filter.limits import (
    DEFAULT_WITHIN,
    MAX_PATH_HOPS,
    MAX_VISITED_NODES,
    MAX_WITHIN,
)
from visualizebetter.filter.parser import parse

if TYPE_CHECKING:
    from visualizebetter.graph.core import Edge, EdgeKey, Graph, Node

_NODE_FIELDS = "id", "label", "type", "layer", "created_at"
_EDGE_FIELDS = "source", "target", "relation", "key", "layer", "created_at", "created_by"

_GROUP_FUNCS = frozenset(
    {"degree", "connected_to", "in_neighborhood", "has_neighbor_of_type", "path_to"}
)


# --- resolving an operand to a value for one record ---


def _resolve_node_field(node: Node, name: str, is_property: bool) -> Any:
    """[6] field → value for a node. Absent → MISSING (schema-less)."""
    if not is_property:
        if name in _NODE_FIELDS:
            return getattr(node, name)
        if name == "tags":
            return list(node.tags)
    # properties.<name> explicitly, or a bare name that is not a known attribute
    # ([6] 단축 표기 — properties.<identifier>). Reserved '_'-prefixed keys ([23-B])
    # are system-owned (citations/provenance/history); the filter — which also backs
    # untrusted WS filter.set — must not read them, so they resolve as MISSING/absent.
    if is_reserved_property(name):
        return MISSING
    return node.properties.get(name, MISSING)


def _resolve_edge_field(edge: Edge, name: str, is_property: bool) -> Any:
    """[6] field → value for an edge. Absent → MISSING."""
    if not is_property:
        if name in _EDGE_FIELDS:
            return getattr(edge, name)
        if name == "weight":
            return edge.weight
        if name == "directed":
            return edge.directed
        if name == "tags":
            return list(edge.tags)
    # Reserved '_'-prefixed keys ([23-B]) are hidden from the filter on edges too.
    if is_reserved_property(name):
        return MISSING
    return edge.properties.get(name, MISSING)


# --- comparison semantics ([6]) ---


def _compare(left: Any, op: str, right: Any) -> bool:
    """Apply one operator with [6]'s schema-less rules.

    MISSING on either side means the comparison is false for every operator: a
    key that is not there cannot equal, differ from, or be ordered against
    anything ([6] 키 부재 → false). This is what makes NOT well-behaved — it flips
    a definite false, not an unknown.
    """
    if left is MISSING or right is MISSING:
        return False

    if op == "==":
        return _equal(left, right)
    if op == "!=":
        return not _equal(left, right)
    if op in ("<", ">", "<=", ">="):
        return _ordered(left, op, right)
    if op == "startsWith":
        return isinstance(left, str) and isinstance(right, str) and left.startswith(right)
    if op == "endsWith":
        return isinstance(left, str) and isinstance(right, str) and left.endswith(right)
    if op == "contains":
        return isinstance(left, str) and isinstance(right, str) and right in left
    if op == "matches":
        # right is a compiled re2 pattern. left must be a string; anything else is
        # a type mismatch → false, not an error.
        return isinstance(left, str) and right.search(left) is not None
    if op == "in":
        return isinstance(right, list) and left in right
    if op == "notIn":
        return isinstance(right, list) and left not in right
    raise FilterSyntaxError(f"unknown operator: {op}")  # unreachable via the grammar


def _equal(left: Any, right: Any) -> bool:
    """== with [6]'s null and no-coercion rules.

    null (None) matches only null: "a" == null is false, not a coercion. bool is
    kept distinct from number so that true == 1 is false — Python treats them as
    equal, which would be an implicit coercion [6] forbids.
    """
    if left is None or right is None:
        return left is None and right is None
    if isinstance(left, bool) != isinstance(right, bool):
        return False
    if isinstance(left, str) != isinstance(right, str):
        return False
    return left == right


def _ordered(left: Any, op: str, right: Any) -> bool:
    """<,>,<=,>= — only when both are number, or both are string ([6])."""
    both_number = _is_number(left) and _is_number(right)
    both_string = isinstance(left, str) and isinstance(right, str)
    if not (both_number or both_string):
        return False
    if op == "<":
        return left < right
    if op == ">":
        return left > right
    if op == "<=":
        return left <= right
    return left >= right


def _is_number(value: Any) -> bool:
    # bool is an int subclass; a boolean is not a number for ordering ([6]).
    return isinstance(value, (int, float)) and not isinstance(value, bool)


# --- group functions ([6], undirected; signatures in docs/filter-dsl.md) ---


_DIRECTIONS = ("in", "out", "both")


def _directed_neighbours(graph: Graph, at: str, direction: str) -> Iterable[str]:
    """Ids adjacent to ``at`` under ``direction`` ([6] M2 direction=).

    Matches get_neighbors ([5-B]): ``out`` follows an edge whose source is ``at``,
    ``in`` one whose target is ``at``, ``both`` either. A ``directed=False`` edge has
    no direction, so it is followed in every mode. ``both`` (the [6] default)
    reproduces the original undirected traversal exactly — the M1 behaviour is
    unchanged when direction is omitted.
    """
    for source, target, relation, key in graph.indices.edges_of(at):
        edge = graph.edges.get((source, target, relation, key))
        if direction == "both" or edge is None or not edge.directed:
            yield target if source == at else source
        elif direction == "out":
            if source == at:
                yield target
        else:  # "in"
            if target == at:
                yield source


def _reachable_from_sources(
    graph: Graph, sources: Iterable[str], max_hops: int, direction: str = "both"
) -> set[str]:
    """Nodes 1..max_hops undirected hops from *some* source, each source's own
    distance-0 self excluded ([6], undirected).

    Per-source rather than one shared frontier, and that difference is load-bearing
    with a set of sources: in_neighborhood("service") must match a service that
    neighbours *another* service, but a single shared visited-set would seed both
    services at distance 0 and never record either as reached. Running each
    source's BFS independently (excluding only that source) records a source that
    is within range of a *different* source — which is exactly what makes
    has_neighbor_of_type("T") equal in_neighborhood("T", within=1). A node is still
    never in its own neighbourhood, and is not connected_to itself.

    One group-function evaluation is one [6] traversal: the MAX_VISITED_NODES cap
    is a single budget spanning every source's BFS, refused (not truncated) if
    exceeded.
    """
    reached: set[str] = set()
    visited = 0
    for source in sources:
        seen = {source}
        frontier: deque[tuple[str, int]] = deque([(source, 0)])
        while frontier:
            node_id, dist = frontier.popleft()
            if dist >= max_hops:
                continue
            for neighbour in _directed_neighbours(graph, node_id, direction):
                if neighbour in seen:
                    continue
                seen.add(neighbour)
                reached.add(neighbour)
                visited += 1
                if visited > MAX_VISITED_NODES:
                    raise FilterLimitError(
                        f"traversal visited over {MAX_VISITED_NODES} nodes ([6])"
                    )
                frontier.append((neighbour, dist + 1))
    return reached


def _read_group_kwargs(
    func: ast.GroupFunc, *, within_default: int, within_cap: int, allow_within: bool
) -> tuple[int, str]:
    """Parse a group function's named args ([6]): ``within=`` (bounded) and the M2
    ``direction=`` (in|out|both, default both). An unknown kwarg — or ``within`` on a
    function that has none (path_to) — is a syntax error, and an invalid direction is
    refused rather than silently treated as undirected."""
    within = within_default
    direction = "both"
    for name, value in func.kwargs:
        if name == "within" and allow_within:
            if not (isinstance(value, ast.Literal) and _is_number(value.value)):
                raise FilterSyntaxError("within= must be a number")
            within = int(value.value)
        elif name == "direction":
            if not (isinstance(value, ast.Literal) and isinstance(value.value, str)):
                raise FilterSyntaxError("direction= must be a string")
            if value.value not in _DIRECTIONS:
                raise FilterSyntaxError(
                    f"direction= must be one of in/out/both, got {value.value!r} ([6])"
                )
            direction = value.value
        else:
            raise FilterSyntaxError(f"{func.name} has no argument named {name!r}")
    if allow_within:
        if within < 1:
            raise FilterSyntaxError("within must be at least 1")
        if within > within_cap:
            raise FilterLimitError(f"within={within} exceeds the limit of {within_cap} ([6])")
    return within, direction


def _string_arg(func: ast.GroupFunc, index: int) -> str:
    """A positional string literal argument, e.g. connected_to("X")."""
    if index >= len(func.args):
        raise FilterSyntaxError(f"{func.name} is missing a required argument")
    arg = func.args[index]
    if not (isinstance(arg, ast.Literal) and isinstance(arg.value, str)):
        raise FilterSyntaxError(f"{func.name} expects a string argument")
    return arg.value


class _GroupMatcher:
    """A group function precompiled to a per-node predicate for one graph.

    Every boolean function reduces to membership in a set computed once here —
    connected_to("X") is "current ∈ nodes within k of X", which is one BFS from X,
    not a BFS per candidate. degree is the exception: it is O(1) per node, so it
    stays a closure over the node.
    """

    def __init__(self, predicate: Callable[[Node], bool]):
        self.predicate = predicate


def _build_group_matcher(func: ast.GroupFunc, graph: Graph) -> _GroupMatcher:
    name = func.name
    if name not in _GROUP_FUNCS:
        raise FilterSyntaxError(f"unknown function: {name}")

    if name == "degree":
        _require_node_keyword(func)
        return _GroupMatcher(lambda node: len(graph.indices.edges_of(node.id)) > 0)

    if name == "connected_to":
        target = _string_arg(func, 0)
        within, direction = _read_group_kwargs(
            func, within_default=DEFAULT_WITHIN, within_cap=MAX_WITHIN, allow_within=True
        )
        reach = (
            _reachable_from_sources(graph, [target], within, direction)
            if target in graph.nodes
            else set()
        )
        return _GroupMatcher(lambda node: node.id in reach)

    if name == "path_to":
        target = _string_arg(func, 0)
        # path_to has no within=, but takes the M2 direction=.
        _within, direction = _read_group_kwargs(
            func, within_default=MAX_PATH_HOPS, within_cap=MAX_PATH_HOPS, allow_within=False
        )
        reach = (
            _reachable_from_sources(graph, [target], MAX_PATH_HOPS, direction)
            if target in graph.nodes
            else set()
        )
        return _GroupMatcher(lambda node: node.id in reach)

    if name == "in_neighborhood":
        node_type = _string_arg(func, 0)
        within, direction = _read_group_kwargs(
            func, within_default=DEFAULT_WITHIN, within_cap=MAX_WITHIN, allow_within=True
        )
        sources = graph.indices.nodes_of_type(node_type)
        reach = (
            _reachable_from_sources(graph, sources, within, direction) if sources else set()
        )
        return _GroupMatcher(lambda node: node.id in reach)

    # has_neighbor_of_type("T") — the 1-hop case, kept as its own name for
    # readability. Equal to in_neighborhood("T", within=1) by construction.
    node_type = _string_arg(func, 0)
    if func.kwargs:
        raise FilterSyntaxError("has_neighbor_of_type takes no keyword arguments ([6])")
    sources = graph.indices.nodes_of_type(node_type)
    reach = _reachable_from_sources(graph, sources, 1) if sources else set()
    return _GroupMatcher(lambda node: node.id in reach)


def _degree_value(func: ast.GroupFunc, graph: Graph, node: Node) -> int:
    """degree(node) as a number, for use inside a comparison ([6] degree(node)>5)."""
    _require_node_keyword(func)
    return len(graph.indices.edges_of(node.id))


def _require_node_keyword(func: ast.GroupFunc) -> None:
    if func.kwargs or len(func.args) != 1 or not isinstance(func.args[0], ast.Identifier):
        raise FilterSyntaxError("degree takes exactly one argument, the keyword `node`")
    if func.args[0].name != "node":
        raise FilterSyntaxError(
            f"degree's argument must be `node`, not {func.args[0].name!r}"
        )


# --- compiling a whole expression against a graph ---


class CompiledFilter:
    """A parsed filter, ready to run against a graph.

    Parse once (``compile_filter``), evaluate many. The parse is graph-independent;
    the group-function sets depend on the graph, so they are (re)built per
    evaluate call — cheap, since each is one BFS.
    """

    def __init__(self, node: ast.AstNode):
        self._ast = node
        self._compile_regexes(node)

    def _compile_regexes(self, node: Any) -> None:
        """Compile every ``matches`` pattern once, with re2 (linear time, [6])."""
        if isinstance(node, ast.Comparison):
            for side in (node.left, node.right):
                if isinstance(side, ast.Regex):
                    object.__setattr__(side, "_compiled", _compile_regex(side.pattern))
        elif isinstance(node, (ast.And, ast.Or)):
            for part in node.parts:
                self._compile_regexes(part)
        elif isinstance(node, ast.Not):
            self._compile_regexes(node.operand)

    def evaluate_nodes(self, graph: Graph) -> set[str]:
        """Node ids for which the expression is true ([5-B] filter)."""
        matchers = _collect_group_matchers(self._ast, graph)
        return {
            node.id
            for node in graph.nodes.values()
            if _eval_node(self._ast, node, graph, matchers)
        }

    def evaluate_edges(self, graph: Graph) -> set[EdgeKey]:
        """Edge identities for which the expression is true ([5-B] edge_filter).

        Group functions are node-centric; using one in an edge filter is a
        FilterError rather than a silently-false match, so a mistaken edge filter
        is reported instead of quietly returning nothing.
        """
        _reject_group_funcs(self._ast)
        return {edge.identity for edge in graph.edges.values() if _eval_edge(self._ast, edge)}


def _compile_regex(pattern: str) -> Any:
    try:
        return re2.compile(pattern)
    except Exception as exc:  # re2 raises on an invalid pattern
        raise FilterSyntaxError(f"invalid regex /{pattern}/: {exc}") from None


def _collect_group_matchers(node: Any, graph: Graph) -> dict[int, _GroupMatcher]:
    """Precompute each distinct group-function occurrence once (keyed by identity)."""
    matchers: dict[int, _GroupMatcher] = {}

    def walk(n: Any) -> None:
        if isinstance(n, ast.GroupFunc):
            matchers[id(n)] = _build_group_matcher(n, graph)
        elif isinstance(n, ast.Comparison):
            for side in (n.left, n.right):
                walk(side)
        elif isinstance(n, (ast.And, ast.Or)):
            for part in n.parts:
                walk(part)
        elif isinstance(n, ast.Not):
            walk(n.operand)

    walk(node)
    return matchers


def _reject_group_funcs(node: Any) -> None:
    def walk(n: Any) -> None:
        if isinstance(n, ast.GroupFunc):
            raise FilterError(
                f"group function {n.name}() is not available in an edge filter ([6])"
            )
        if isinstance(n, ast.Comparison):
            for side in (n.left, n.right):
                walk(side)
        elif isinstance(n, (ast.And, ast.Or)):
            for part in n.parts:
                walk(part)
        elif isinstance(n, ast.Not):
            walk(n.operand)

    walk(node)


def _eval_node(
    node_ast: Any, record: Node, graph: Graph, matchers: dict[int, _GroupMatcher]
) -> bool:
    if isinstance(node_ast, ast.Or):
        return any(_eval_node(p, record, graph, matchers) for p in node_ast.parts)
    if isinstance(node_ast, ast.And):
        return all(_eval_node(p, record, graph, matchers) for p in node_ast.parts)
    if isinstance(node_ast, ast.Not):
        return not _eval_node(node_ast.operand, record, graph, matchers)
    if isinstance(node_ast, ast.GroupFunc):
        # A bare boolean group function as a primary.
        return matchers[id(node_ast)].predicate(record)
    if isinstance(node_ast, ast.Comparison):
        left = _operand_value_node(node_ast.left, record, graph, matchers)
        right = _operand_value_node(node_ast.right, record, graph, matchers)
        return _compare(left, node_ast.op, right)
    raise FilterSyntaxError("malformed filter AST")


def _operand_value_node(
    operand: Any, record: Node, graph: Graph, matchers: dict[int, _GroupMatcher]
) -> Any:
    if isinstance(operand, ast.Field):
        return _resolve_node_field(record, operand.name, operand.is_property)
    if isinstance(operand, ast.Literal):
        return operand.value
    if isinstance(operand, ast.Regex):
        return getattr(operand, "_compiled")
    if isinstance(operand, ast.GroupFunc):
        if operand.name == "degree":
            return _degree_value(operand, graph, record)
        # A boolean function used as a comparison operand yields its bool.
        return matchers[id(operand)].predicate(record)
    raise FilterSyntaxError("malformed filter operand")


def _eval_edge(node_ast: Any, edge: Edge) -> bool:
    if isinstance(node_ast, ast.Or):
        return any(_eval_edge(p, edge) for p in node_ast.parts)
    if isinstance(node_ast, ast.And):
        return all(_eval_edge(p, edge) for p in node_ast.parts)
    if isinstance(node_ast, ast.Not):
        return not _eval_edge(node_ast.operand, edge)
    if isinstance(node_ast, ast.Comparison):
        left = _operand_value_edge(node_ast.left, edge)
        right = _operand_value_edge(node_ast.right, edge)
        return _compare(left, node_ast.op, right)
    raise FilterSyntaxError("malformed edge filter AST")


def _operand_value_edge(operand: Any, edge: Edge) -> Any:
    if isinstance(operand, ast.Field):
        return _resolve_edge_field(edge, operand.name, operand.is_property)
    if isinstance(operand, ast.Literal):
        return operand.value
    if isinstance(operand, ast.Regex):
        return getattr(operand, "_compiled")
    raise FilterSyntaxError("malformed edge filter operand")


# --- public API ---


def compile_filter(expression: str) -> CompiledFilter:
    """Parse ``expression`` into a reusable filter ([6])."""
    return CompiledFilter(parse(expression))


def evaluate_nodes(expression: str, graph: Graph) -> set[str]:
    """Node ids matching ``expression`` ([5-B] filter, [6] semantics)."""
    return compile_filter(expression).evaluate_nodes(graph)


def evaluate_edges(expression: str, graph: Graph) -> set[EdgeKey]:
    """Edge identities matching ``expression`` ([5-B] edge_filter)."""
    return compile_filter(expression).evaluate_edges(graph)
