"""필터 DSL AST ([6]).

Typed nodes rather than a raw lark Tree, so the evaluator reads structure by
attribute and the depth check ([6] AST 깊이 상한) is a plain walk. A sentinel
MISSING stands for "the operand has no value here" — an absent property key or a
field that does not exist on this record. [6]'s schema-less rule turns MISSING
into a false comparison, never an error.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Union


class _Missing:
    """The absence of a value ([6] 키 부재). Distinct from null, which is a value."""

    _instance: "_Missing | None" = None

    def __new__(cls) -> "_Missing":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __repr__(self) -> str:
        return "MISSING"


MISSING = _Missing()


# --- operands ([6] operand := field | value | group_func) ---


@dataclass(frozen=True)
class Field:
    """[6] field. ``name`` is the identifier; ``is_property`` forces properties.<name>.

    A bare identifier that is not a known attribute is a properties shortcut
    ([6]: "identifier # 단축 표기 — properties.<identifier>"), so is_property is
    resolved at parse time only for the explicit ``properties.`` form; a bare name
    is left for the evaluator, which knows the record's attribute set.
    """

    name: str
    is_property: bool


@dataclass(frozen=True)
class Literal:
    """[6] value — string / number / boolean / null / array (regex is separate)."""

    value: Any


@dataclass(frozen=True)
class Regex:
    """[6] regex literal — only meaningful as the right side of ``matches``."""

    pattern: str


@dataclass(frozen=True)
class GroupFunc:
    """[6] group_func. ``args`` are positional; ``kwargs`` are name=value.

    A bare identifier argument (e.g. the ``node`` keyword in ``degree(node)``) is
    kept as an Identifier so the evaluator can bind it to the current node.
    """

    name: str
    args: tuple[Any, ...]
    kwargs: tuple[tuple[str, Any], ...]


@dataclass(frozen=True)
class Identifier:
    """A bare identifier used as a group-function argument ([6] arg := ... identifier).

    The only one [6] uses is ``node`` (the current evaluation target); the
    evaluator rejects any other, since no group function takes another.
    """

    name: str


Operand = Union[Field, Literal, Regex, GroupFunc]


# --- boolean structure ([6] expression / or / and / not / comparison) ---


@dataclass(frozen=True)
class Comparison:
    """[6] comparison := operand op operand."""

    left: Operand
    op: str
    right: Operand


@dataclass(frozen=True)
class Not:
    operand: "AstNode"


@dataclass(frozen=True)
class And:
    parts: tuple["AstNode", ...]


@dataclass(frozen=True)
class Or:
    parts: tuple["AstNode", ...]


AstNode = Union[Or, And, Not, Comparison, GroupFunc]


def depth(node: Any) -> int:
    """AST nesting depth ([6] 깊이 상한 검사용)."""
    if isinstance(node, Or) or isinstance(node, And):
        return 1 + max((depth(p) for p in node.parts), default=0)
    if isinstance(node, Not):
        return 1 + depth(node.operand)
    if isinstance(node, Comparison):
        return 1 + max(depth(node.left), depth(node.right))
    if isinstance(node, GroupFunc):
        return 1
    return 0
