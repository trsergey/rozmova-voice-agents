"""A small JSONLogic subset for ``condition`` nodes (https://jsonlogic.com).

Enough to branch on slots: ``var`` (dotted paths, optional default), comparisons, ``and``,
``or``, ``!`` and ``in``. Truthiness follows JSONLogic, not Python: an empty list is
falsy and ``"0"`` is truthy. An unknown operator raises ``JSONLogicError`` instead of
silently evaluating to false.
"""

import itertools
from collections.abc import Mapping
from typing import Any

from pydantic import JsonValue


class JSONLogicError(ValueError):
    """The rule uses an operator or a shape this subset does not support."""


def truthy(value: Any) -> bool:
    if isinstance(value, list):
        return len(value) > 0
    return bool(value)


def evaluate(rule: JsonValue, data: Mapping[str, JsonValue]) -> Any:
    """Evaluate ``rule`` against ``data`` (the session slots)."""
    if isinstance(rule, list):
        return [evaluate(item, data) for item in rule]
    if not isinstance(rule, dict):
        return rule
    if len(rule) != 1:
        raise JSONLogicError(f"a rule must have exactly one operator, got {sorted(rule)}")
    [(op, raw_args)] = rule.items()
    args: list[JsonValue] = list(raw_args) if isinstance(raw_args, list) else [raw_args]

    if op == "var":
        return _var(args, data)
    if op == "and":
        result: Any = True
        for arg in args:
            result = evaluate(arg, data)
            if not truthy(result):
                return result
        return result
    if op == "or":
        result = False
        for arg in args:
            result = evaluate(arg, data)
            if truthy(result):
                return result
        return result

    values = [evaluate(arg, data) for arg in args]
    match op:
        case "!":
            return not truthy(values[0])
        case "==":
            return values[0] == values[1]
        case "!=":
            return values[0] != values[1]
        case "<" | "<=" | ">" | ">=":
            return _compare(op, values)
        case "in":
            needle, haystack = values
            return isinstance(haystack, list | str) and needle in haystack
    raise JSONLogicError(f"unsupported operator {op!r}")


def _var(args: list[JsonValue], data: Mapping[str, JsonValue]) -> Any:
    path = args[0] if args else ""
    default = args[1] if len(args) > 1 else None
    if path in ("", None):
        return dict(data)
    value: Any = data
    for part in str(path).split("."):
        if isinstance(value, Mapping) and part in value:
            value = value[part]
        elif isinstance(value, list) and part.isdigit() and int(part) < len(value):
            value = value[int(part)]
        else:
            return default
    return value


def _compare(op: str, values: list[Any]) -> bool:
    try:
        pairs = list(itertools.pairwise(values))
        match op:
            case "<":
                return all(a < b for a, b in pairs)
            case "<=":
                return all(a <= b for a, b in pairs)
            case ">":
                return all(a > b for a, b in pairs)
            case _:
                return all(a >= b for a, b in pairs)
    except TypeError:
        # Comparing a missing slot (None) or mixed types is false, not a crash.
        return False
