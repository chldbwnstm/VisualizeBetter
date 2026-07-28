"""필터 DSL 파서 ([6] 문법 → AST).

lark (LALR + contextual lexer) rather than parsimonious: the contextual lexer is
what lets a bare ``type`` be a field in operand position but ``AND`` be an
operator, and ``degree`` be a function only when a ``(`` follows — [6]'s grammar
leans on exactly that keyword/identifier overlap ("identifier # 단축 표기").

The grammar here is [6] transcribed. Any change to it is a change to the DSL
grammar, which is a fixed contract — this file does not extend it.
"""

from __future__ import annotations

from typing import Any

from lark import Lark, Token, Transformer, v_args
from lark.exceptions import LarkError

from visualizebetter.filter import ast
from visualizebetter.filter.errors import FilterLimitError, FilterSyntaxError
from visualizebetter.filter.limits import MAX_AST_DEPTH, MAX_EXPRESSION_BYTES

# [6] 문법 (BNF-ish) transcribed to lark. Comparisons are ``operand op operand``
# so that a value on the left ("x" in tags) and a function on the left
# (degree(node) > 5) both parse, per [6]'s note.
_GRAMMAR = r"""
?start: expression

?expression: or_expr

?or_expr: and_expr (OR and_expr)*        -> or_
?and_expr: not_expr (AND not_expr)*      -> and_
?not_expr: NOT primary                   -> not_
         | primary

?primary: comparison
        | group_func
        | "(" expression ")"

comparison: operand OP operand

?operand: group_func
        | field
        | value

field: PROPERTY_FIELD                     -> property_field
     | CNAME                             -> name_field

group_func: CNAME "(" [args] ")"
args: arg ("," arg)*
arg: CNAME "=" argval                    -> kwarg
   | argval                             -> posarg
?argval: value
       | CNAME                          -> ident_arg

?value: ESCAPED_STRING                   -> string
      | SIGNED_NUMBER                    -> number
      | BOOL                            -> boolean
      | NULL                            -> null
      | REGEX                           -> regex
      | array

array: "[" [value ("," value)*] "]"

OR: "OR"
AND: "AND"
NOT: "NOT"
BOOL.2: "true" | "false"
NULL.2: "null"

OP: "==" | "!=" | "<=" | ">=" | "<" | ">"
  | "startsWith" | "endsWith" | "contains" | "matches"
  | "notIn" | "in"

REGEX: /\/(\\.|[^\/\\\n])+\//
PROPERTY_FIELD.2: /properties\.[a-zA-Z_][a-zA-Z0-9_]*/

%import common.CNAME
%import common.ESCAPED_STRING
%import common.SIGNED_NUMBER
%import common.WS
%ignore WS
"""


@v_args(inline=True)
class _ToAst(Transformer):
    """lark parse tree → typed AST (visualizebetter.filter.ast)."""

    def or_(self, *items: Any) -> ast.AstNode:
        parts = [i for i in items if not (isinstance(i, Token) and i.type == "OR")]
        return parts[0] if len(parts) == 1 else ast.Or(tuple(parts))

    def and_(self, *items: Any) -> ast.AstNode:
        parts = [i for i in items if not (isinstance(i, Token) and i.type == "AND")]
        return parts[0] if len(parts) == 1 else ast.And(tuple(parts))

    def not_(self, _not: Token, operand: ast.AstNode) -> ast.AstNode:
        return ast.Not(operand)

    def comparison(self, left: Any, op: Token, right: Any) -> ast.Comparison:
        return ast.Comparison(left, str(op), right)

    def property_field(self, tok: Token) -> ast.Field:
        return ast.Field(str(tok)[len("properties."):], is_property=True)

    def name_field(self, name: Token) -> ast.Field:
        # A bare name may be a known attribute or a properties shortcut; the
        # evaluator decides against the record's attribute set ([6]).
        return ast.Field(str(name), is_property=False)

    def group_func(self, name: Token, args: Any = None) -> ast.GroupFunc:
        pos, kw = args if args is not None else ([], [])
        return ast.GroupFunc(str(name), tuple(pos), tuple(kw))

    def args(self, *items: Any) -> tuple[list[Any], list[tuple[str, Any]]]:
        pos: list[Any] = []
        kw: list[tuple[str, Any]] = []
        for item in items:
            if isinstance(item, tuple) and len(item) == 2 and item[0] == "__kw__":
                kw.append(item[1])
            else:
                pos.append(item)
        return pos, kw

    def kwarg(self, name: Token, value: Any) -> tuple[str, tuple[str, Any]]:
        return ("__kw__", (str(name), value))

    def posarg(self, value: Any) -> Any:
        return value

    def ident_arg(self, name: Token) -> ast.Identifier:
        return ast.Identifier(str(name))

    def string(self, tok: Token) -> ast.Literal:
        return ast.Literal(_unescape(str(tok)))

    def number(self, tok: Token) -> ast.Literal:
        text = str(tok)
        return ast.Literal(int(text) if _is_int(text) else float(text))

    def boolean(self, tok: Token) -> ast.Literal:
        return ast.Literal(str(tok) == "true")

    def null(self, _tok: Token) -> ast.Literal:
        return ast.Literal(None)

    def regex(self, tok: Token) -> ast.Regex:
        return ast.Regex(str(tok)[1:-1])  # strip the surrounding slashes

    def array(self, *items: Any) -> ast.Literal:
        # array holds only values ([6]); each is already a Literal.
        return ast.Literal([i.value for i in items])


def _is_int(text: str) -> bool:
    return "." not in text and "e" not in text and "E" not in text


def _unescape(quoted: str) -> str:
    """ESCAPED_STRING → its Python string value, without eval."""
    import json

    return json.loads(quoted)


_PARSER = Lark(_GRAMMAR, parser="lalr", transformer=_ToAst())


def parse(expression: str) -> ast.AstNode:
    """[6] expression → AST. Enforces the length and depth caps before/after parse.

    Length is checked first so a 2GB string is refused before the parser touches
    it; depth is checked on the built AST, which is where nesting is visible.
    """
    if not isinstance(expression, str):
        raise FilterSyntaxError("filter expression must be a string")
    encoded = expression.encode("utf-8")
    if len(encoded) > MAX_EXPRESSION_BYTES:
        raise FilterLimitError(
            f"filter expression is {len(encoded)} bytes, over the {MAX_EXPRESSION_BYTES} limit ([6])"
        )
    if not expression.strip():
        raise FilterSyntaxError("filter expression is empty")

    try:
        tree = _PARSER.parse(expression)
    except LarkError as exc:
        raise FilterSyntaxError(f"could not parse filter: {exc}") from None

    node = tree  # transformer already produced the AST
    d = ast.depth(node)
    if d > MAX_AST_DEPTH:
        raise FilterLimitError(f"filter nests {d} deep, over the {MAX_AST_DEPTH} limit ([6])")
    return node
