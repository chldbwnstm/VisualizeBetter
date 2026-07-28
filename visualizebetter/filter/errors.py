"""필터 DSL 에러 타입 ([6]).

Every failure is a subclass of FilterError, so a caller (MCP/WS, TASK T) can turn
the whole family into one tool error without catching parser internals. The split
matters for the message the AI sees: a syntax error is "fix your expression", a
limit error is "your expression is too expensive" — [6]'s safety caps.
"""

from __future__ import annotations


class FilterError(Exception):
    """Base for anything the filter DSL rejects ([6])."""


class FilterSyntaxError(FilterError):
    """The expression did not parse, or used a construct outside [6]'s grammar."""


class FilterLimitError(FilterError):
    """A [6] safety cap was exceeded — length, depth, within, path, or visited.

    Raised rather than silently truncated: the caps exist because the expression
    is untrusted input ([11]), and a silently trimmed traversal would return a
    wrong answer instead of refusing an abusive one.
    """
