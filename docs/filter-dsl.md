# Filter DSL — signature & semantics reference

This file is the **canonical signature reference** for the filter DSL group
functions, which the project plan ([6]) points at rather than restating. The
grammar, operators, comparison semantics, and safety caps are defined in plan
section [6]; this document adds the parts [6] delegates here — the group-function
signatures — and pins the evaluation details that [6] leaves to the
implementation. Signatures below were ratified by the architect (TASK R
STOP&ASK); changing them is a DSL-contract change (STOP&ASK).

The one evaluator described here is shared by [5-B]'s `filter` / `edge_filter`,
WS `filter.set`, and the `apply_style` / `suggest_filter` selectors ([6]).

## Grammar (from [6])

```
expression := or_expr
or_expr    := and_expr ('OR' and_expr)*
and_expr   := not_expr ('AND' not_expr)*
not_expr   := 'NOT'? primary
primary    := comparison | group_func | '(' expression ')'
comparison := operand op operand
operand    := field | value | group_func
field      := 'id' | 'label' | 'type' | 'layer' | 'created_at'
            | 'properties.' identifier | 'tags' | identifier
op         := '==' '!=' '<' '>' '<=' '>=' 'startsWith' 'endsWith'
            | 'contains' 'matches' 'in' 'notIn'
value      := string | number | boolean | regex | array | null
group_func := function '(' args? ')'
```

`OR`/`AND`/`NOT` are uppercase keywords. A bare `identifier` that is not one of
the known field names is a shortcut for `properties.<identifier>`.

## Fields

**Nodes**: `id`, `label`, `type`, `layer`, `created_at` are attributes; `tags`
is the tag array; `properties.<k>` (or a bare unknown name) reads a property.

**Edges** ([5-B] `edge_filter`): `source`, `target`, `relation`, `key`, `layer`,
`created_at`, `created_by`, `weight`, `directed` are attributes; `tags` and
`properties.<k>` as for nodes. Group functions are **not** available in an edge
filter — using one is an error, not a silent no-match.

## Operators & comparison semantics (from [6], schema-less)

- A **missing key** or a **type mismatch** makes that comparison **false** — not
  an error, not a third truth value. `NOT` flips that definite false.
- `<`, `>`, `<=`, `>=` evaluate only when **both** operands are numbers, or
  **both** are strings; otherwise false.
- `null` matches only via `== null` / `!= null`. An **absent** key is not null:
  `properties.x == null` is false when `x` is absent (the key-absence rule wins),
  and true only when `x` is present with the value `null`.
- **No coercion.** `"10" == 10` is false. `true == 1` is false (a boolean is not
  a number).
- `startsWith` / `endsWith` / `contains` are string↔string (else false).
  `contains` is substring; array membership is `in` / `notIn`.
- `matches` takes a `/regex/` on the right, evaluated with **google-re2**
  (linear-time, non-backtracking) so an untrusted pattern cannot cause ReDoS.

## Group functions (undirected by default)

Traversal **defaults to undirected** — the graph mixes directed and undirected
edges, and these functions express *proximity*, not flow. "Within *k* hops" means
graph distance **1…k**: a node is **not** in its own neighbourhood, and is not
`connected_to` / `path_to` itself.

`connected_to`, `in_neighborhood`, and `path_to` take an optional **`direction=`**
(M2) — a quoted `"in"`, `"out"`, or `"both"` (default `"both"` = the undirected
behaviour above). It is quoted because bare `in` collides with the `in` list
operator. The traversal spreads out from the target/type node(s); at each hop
`"out"` follows an edge whose source is the node being traversed, `"in"` one whose
target is it, `"both"` either — matching `get_neighbors` ([5-B]). A `directed=False`
edge has no direction and is followed in every mode. Any other value is a syntax
error, not a silent fallback to undirected.

| Function | Signature | Returns | Meaning |
|---|---|---|---|
| `degree` | `degree(node)` | number | Count of edges incident to the current node (undirected, each edge once). `node` is the keyword for the current evaluation target. |
| `connected_to` | `connected_to("X", within=k, direction="both")` | bool | Current node is within *k* hops of the **node whose id is `X`**, under `direction`. `within` optional, default `1`, max `5`. |
| `in_neighborhood` | `in_neighborhood("T", within=k, direction="both")` | bool | Current node is within *k* hops of **some node whose type is `T`** (a different node), under `direction`. `within` optional, default `1`, max `5`. |
| `has_neighbor_of_type` | `has_neighbor_of_type("T")` | bool | Current node has a direct (1-hop) undirected neighbour of type `T`. (No `direction`; use `in_neighborhood("T", within=1, direction=…)` for a directed 1-hop.) |
| `path_to` | `path_to("X", direction="both")` | bool | A path of length 1…8 exists from the current node to the **node whose id is `X`**, under `direction`. No `within`; search is bounded at 8 hops. |

### Equivalences and boundaries

- `has_neighbor_of_type("T")` **≡** `in_neighborhood("T", within=1)` — the 1-hop
  case gets its own readable name, like SQL `IN` vs `= ANY`. The evaluator
  computes them by the same path, so they cannot drift apart.
- `connected_to("X")` (distance-bounded proximity to a specific node) and
  `path_to("X")` (connectivity, deeper bound) express different intents and both
  exist. Actual **path enumeration** is not the DSL's job — that is the
  `find_paths()` MCP tool ([5-B], TASK T).
- `connected_to`/`path_to` target a **node id**; `in_neighborhood`/
  `has_neighbor_of_type` target a **type**. General predicate proximity (being
  near *any node matching an arbitrary comparison*) is out of M1 scope, because
  the grammar's `arg` cannot carry a comparison.

## Safety caps (from [6]; untrusted input, [11])

| Cap | Value | On breach |
|---|---|---|
| Expression length | 2048 bytes | `FilterLimitError` |
| AST depth | 64 (derived — [6] mandates a cap without a number) | `FilterLimitError` |
| `connected_to`/`in_neighborhood` `within` | ≤ 5 | `FilterLimitError` |
| `path_to` search depth | ≤ 8 | (built in; no arg to exceed) |
| Nodes visited per traversal | ≤ 50,000 | `FilterLimitError` |
| Regex engine | google-re2 (linear time) | invalid pattern → `FilterSyntaxError` |

## Public API (`visualizebetter.filter`)

```python
compile_filter(expression: str) -> CompiledFilter   # parse once, reuse
CompiledFilter.evaluate_nodes(graph) -> set[str]     # matching node ids
CompiledFilter.evaluate_edges(graph) -> set[EdgeKey] # matching edge identities
evaluate_nodes(expression, graph) -> set[str]        # parse + evaluate
evaluate_edges(expression, graph) -> set[EdgeKey]
```

Errors: `FilterError` (base), `FilterSyntaxError` (bad expression / grammar),
`FilterLimitError` (a safety cap exceeded).

## Examples

```
type == "class"
ns startsWith "app.ui"                       # bare ns → properties.ns
properties.field_count > 10
label matches /^UI.*View$/
"authored_by_me" in tags
degree(node) > 5
connected_to("app.OrderService", within=3)
in_neighborhood("service", within=2)
has_neighbor_of_type("Controller")
path_to("app.EntryPoint")
(type == "class" AND ns startsWith "app.Core.Service")
  OR (type == "component" AND connected_to("app.PaymentService", within=2))
```
