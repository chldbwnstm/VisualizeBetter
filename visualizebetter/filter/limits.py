"""필터 DSL 안전 상한 ([6], [11] — 비신뢰 입력).

The same evaluator backs WS ``filter.set`` and MCP tools, so every expression is
attacker-controlled. These caps are the boundary that keeps a hostile filter from
turning into a DoS. Values are [6]'s except AST_MAX_DEPTH, which [6] mandates
("AST 깊이 상한") without a number — set here and documented as a derived constant.
"""

from __future__ import annotations

# [6] 식 길이 2KB. Bytes, not characters: the cost is in what the parser and
# regex engine chew through, and a multibyte-heavy expression is not cheaper.
MAX_EXPRESSION_BYTES = 2048

# [6] "AST 깊이 상한" — no number given, so chosen here. A 2KB expression cannot
# nest very deep (each level needs parens and an operator), so this is a backstop
# against a pathological but short input, not a functional limit. Documented in
# docs/filter-dsl.md as derived, not specified by [6].
MAX_AST_DEPTH = 64

# [6] connected_to / in_neighborhood within 상한 5.
MAX_WITHIN = 5
DEFAULT_WITHIN = 1

# [6] path_to 상한 8 (search depth; path_to takes no `within`).
MAX_PATH_HOPS = 8

# [6] 순회 방문 노드 수 상한 50K — per traversal (one group-function BFS). A single
# BFS that would visit more than this on a dense graph is refused; the outer
# per-node predicate loop is not a "traversal" and is not capped here.
MAX_VISITED_NODES = 50_000

# [6] matches: google-re2 (linear-time, non-backtracking) rather than stdlib re,
# which cannot bound backtracking and so is a ReDoS vector on untrusted patterns.
# Verified to install as a wheel on this environment (Win/py3.13).
REGEX_ENGINE = "google-re2"
