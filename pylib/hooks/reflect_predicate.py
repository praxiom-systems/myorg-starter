"""Predicate evaluator for `[reflect.*]` self_approve_predicate fields.

Each `[reflect.*]` pattern entry in `bash-patterns.toml` may declare a
`self_approve_predicate` field whose value is a boolean expression over
the three classification axes the agent records via `mark_command_safe`:

  reversibility:  low | medium | high
  blast_radius:   local | repo | machine | external
  confidence:     0.0 .. 1.0

The agent's classification, recorded via `mark_command_safe`, must satisfy
the matched pattern's predicate before the hook allows the command on
retry. Predicate failure forces the agent to escalate via
`AskUserQuestion` instead of self-approving.

Default predicate when a `[reflect.*]` entry does not declare one:
`confidence >= 0.7`. Conservative: requires the agent to be reasonably
confident in its self-classification before self-approval is honored.

The grammar is intentionally small to keep predicate authoring auditable:

  expr        := orExpr
  orExpr      := andExpr ( "OR" andExpr )*
  andExpr     := atom ( "AND" atom )*
  atom        := "(" expr ")" | comparison
  comparison  := field op value
  field       := "reversibility" | "blast_radius" | "confidence"
  op          := "==" | "!=" | "<" | "<=" | ">" | ">="
  value       := QUOTED_STRING | NUMBER

Examples:

  confidence >= 0.7
  reversibility != 'low'
  reversibility != 'low' AND blast_radius != 'external'
  (confidence >= 0.8) OR (reversibility == 'high' AND blast_radius == 'local')

Failure modes are deliberately strict — any parse error or unknown field
yields False (deny the self-approve), so a typo never widens permissions.
"""

from __future__ import annotations

import re
from typing import Any

DEFAULT_PREDICATE = "confidence >= 0.7"

_REVERSIBILITY_RANK = {"low": 0, "medium": 1, "high": 2}
_BLAST_RADIUS_RANK = {"local": 0, "repo": 1, "machine": 2, "external": 3}

_FIELD_RANKS = {
    "reversibility": _REVERSIBILITY_RANK,
    "blast_radius": _BLAST_RADIUS_RANK,
}

_TOKEN_RE = re.compile(
    r"""
    \s*(
        AND | OR
        | \( | \)
        | ==|!=|<=|>=|<|>
        | '[^']*'
        | \"[^\"]*\"
        | [0-9]+(?:\.[0-9]+)?
        | [A-Za-z_][A-Za-z_0-9]*
    )
    """,
    re.VERBOSE,
)


class PredicateError(ValueError):
    """Raised when a predicate fails to parse. Caller should treat as deny."""


def evaluate(predicate: str, classification: dict[str, Any]) -> bool:
    """Evaluate a predicate against an agent-supplied classification.

    Returns True iff the predicate matches. Any unknown field, malformed
    value, or parse error yields False (fail closed — predicate failure
    means the agent cannot self-approve and must escalate via
    AskUserQuestion).

    The classification dict is expected to carry at least the keys
    used in the predicate; missing keys cause the comparison to fail
    closed.
    """
    try:
        tokens = _tokenize(predicate)
        result, idx = _parse_or(tokens, 0, classification)
        if idx != len(tokens):
            return False  # trailing tokens — malformed
        return result
    except PredicateError:
        return False
    except Exception:  # safety net — any unexpected failure denies
        return False


def _tokenize(predicate: str) -> list[str]:
    tokens: list[str] = []
    pos = 0
    while pos < len(predicate):
        if predicate[pos].isspace():
            pos += 1
            continue
        m = _TOKEN_RE.match(predicate, pos)
        if not m:
            raise PredicateError(f"unexpected character at offset {pos}")
        tokens.append(m.group(1))
        pos = m.end()
    return tokens


def _parse_or(tokens: list[str], idx: int, cls: dict[str, Any]) -> tuple[bool, int]:
    left, idx = _parse_and(tokens, idx, cls)
    while idx < len(tokens) and tokens[idx] == "OR":
        right, idx = _parse_and(tokens, idx + 1, cls)
        left = left or right
    return left, idx


def _parse_and(tokens: list[str], idx: int, cls: dict[str, Any]) -> tuple[bool, int]:
    left, idx = _parse_atom(tokens, idx, cls)
    while idx < len(tokens) and tokens[idx] == "AND":
        right, idx = _parse_atom(tokens, idx + 1, cls)
        left = left and right
    return left, idx


def _parse_atom(tokens: list[str], idx: int, cls: dict[str, Any]) -> tuple[bool, int]:
    if idx >= len(tokens):
        raise PredicateError("unexpected end of predicate")
    if tokens[idx] == "(":
        inner, idx = _parse_or(tokens, idx + 1, cls)
        if idx >= len(tokens) or tokens[idx] != ")":
            raise PredicateError("missing closing paren")
        return inner, idx + 1
    return _parse_comparison(tokens, idx, cls)


def _parse_comparison(tokens: list[str], idx: int, cls: dict[str, Any]) -> tuple[bool, int]:
    if idx + 2 >= len(tokens):
        raise PredicateError("incomplete comparison")
    field, op, value_tok = tokens[idx], tokens[idx + 1], tokens[idx + 2]
    if op not in ("==", "!=", "<", "<=", ">", ">="):
        raise PredicateError(f"unexpected operator {op!r}")
    value = _parse_value(value_tok)
    return _compare(field, op, value, cls), idx + 3


def _parse_value(tok: str) -> Any:
    if tok.startswith("'") and tok.endswith("'"):
        return tok[1:-1]
    if tok.startswith('"') and tok.endswith('"'):
        return tok[1:-1]
    try:
        if "." in tok:
            return float(tok)
        return int(tok)
    except ValueError:
        raise PredicateError(f"unparseable value {tok!r}")


_KNOWN_FIELDS = {"reversibility", "blast_radius", "confidence"}


def _compare(field: str, op: str, value: Any, cls: dict[str, Any]) -> bool:
    if field not in _KNOWN_FIELDS:
        return False  # unknown field — fail closed (typos can't widen perms)
    if field not in cls:
        return False  # missing field — fail closed
    actual = cls[field]
    if field in _FIELD_RANKS:
        ranks = _FIELD_RANKS[field]
        a_rank = ranks.get(str(actual))
        v_rank = ranks.get(str(value)) if isinstance(value, str) else None
        if a_rank is None or v_rank is None:
            return False
        actual, value = a_rank, v_rank
    try:
        if op == "==":
            return actual == value
        if op == "!=":
            return actual != value
        if op == "<":
            return actual < value
        if op == "<=":
            return actual <= value
        if op == ">":
            return actual > value
        if op == ">=":
            return actual >= value
    except TypeError:
        return False
    return False


__all__ = ["evaluate", "DEFAULT_PREDICATE", "PredicateError"]
