"""필터 DSL — 계획서 [6] 의 순수 평가기.

The DSL is the schema-less predicate language over the graph ([6]). One evaluator
is shared by [5-B]'s ``filter`` parameter, WS ``filter.set``, and
apply_style/suggest_filter selectors, so the semantics live here once. MCP/WS
wiring is a later TASK (T); this module is parser + evaluator + limits only.

Group-function signatures and the undirected/limit semantics are ratified in
``docs/filter-dsl.md`` — that file is the canonical signature reference the plan
points at ([6]).
"""

from visualizebetter.filter.errors import FilterError, FilterLimitError, FilterSyntaxError
from visualizebetter.filter.evaluate import (
    CompiledFilter,
    compile_filter,
    evaluate_edges,
    evaluate_nodes,
)

__all__ = [
    "CompiledFilter",
    "FilterError",
    "FilterLimitError",
    "FilterSyntaxError",
    "compile_filter",
    "evaluate_edges",
    "evaluate_nodes",
]
