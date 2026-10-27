"""Condition parsing and Polars expression compilation for tabular feeds."""

from __future__ import annotations

from typing import Any

from core.infrastructure.tabular_feed.common import _as_list, _pl, _text
from core.infrastructure.tabular_feed.constants import CONDITION_KEYS

_MESSAGE_WHEN_MUST_BE_AN_OBJECT = "when must be an object"
_MESSAGE_WHEN_COLUMN_IS_REQUIRED = "when.column is required"


def _coerce_number(value: Any) -> float:
    text = _text(value)
    if text == "":
        return 0.0
    return float(text.replace(",", "."))


def _empty(value: Any) -> bool:
    return value is None or _text(value) == ""


def _compare(left: Any, right: Any, operator: str) -> bool:
    if operator in {"gt", "gte", "lt", "lte"}:
        lhs = _coerce_number(left)
        rhs = _coerce_number(right)
        if operator == "gt":
            return lhs > rhs
        if operator == "gte":
            return lhs >= rhs
        if operator == "lt":
            return lhs < rhs
        return lhs <= rhs
    if operator == "equals":
        return left == right or _text(left) == _text(right)
    if operator == "not_equals":
        return not _compare(left, right, "equals")
    if operator == "in":
        return left in _as_list(right) or _text(left) in {_text(item) for item in _as_list(right)}
    if operator == "not_in":
        return not _compare(left, right, "in")
    return False


def _condition_matches(row: dict[str, Any], condition: Any) -> bool:
    if condition in (None, "", {}):
        return True
    if not isinstance(condition, dict):
        raise ValueError(_MESSAGE_WHEN_MUST_BE_AN_OBJECT)
    unknown = set(condition) - CONDITION_KEYS
    if unknown:
        raise ValueError(f"unknown when key: {min(unknown)}")
    if "all" in condition:
        return all(_condition_matches(row, item) for item in _as_list(condition.get("all")))
    if "any" in condition:
        return any(_condition_matches(row, item) for item in _as_list(condition.get("any")))
    column = _text(condition.get("column"))
    if not column:
        raise ValueError(_MESSAGE_WHEN_COLUMN_IS_REQUIRED)
    value = row.get(column)
    if bool(condition.get("is_empty")):
        return _empty(value)
    if bool(condition.get("is_not_empty")):
        return not _empty(value)
    for operator in ("equals", "not_equals", "in", "not_in", "gt", "gte", "lt", "lte"):
        if operator in condition:
            return _compare(value, condition.get(operator), operator)
    raise ValueError("when requires an operator")


def _condition_columns(condition: Any) -> set[str]:
    if condition in (None, "", {}):
        return set()
    if not isinstance(condition, dict):
        raise ValueError(_MESSAGE_WHEN_MUST_BE_AN_OBJECT)
    unknown = set(condition) - CONDITION_KEYS
    if unknown:
        raise ValueError(f"unknown when key: {min(unknown)}")
    columns: set[str] = set()
    for key in ("all", "any"):
        if key in condition:
            for item in _as_list(condition.get(key)):
                columns.update(_condition_columns(item))
            return columns
    column = _text(condition.get("column"))
    if not column:
        raise ValueError(_MESSAGE_WHEN_COLUMN_IS_REQUIRED)
    columns.add(column)
    return columns


def _when_expr(condition: Any):
    pl = _pl()
    if condition in (None, "", {}):
        return pl.lit(True)
    if not isinstance(condition, dict):
        raise ValueError(_MESSAGE_WHEN_MUST_BE_AN_OBJECT)
    if "all" in condition:
        items = _as_list(condition.get("all"))
        expr = pl.lit(True)
        for item in items:
            expr = expr & _when_expr(item)
        return expr
    if "any" in condition:
        items = _as_list(condition.get("any"))
        expr = pl.lit(False)
        for item in items:
            expr = expr | _when_expr(item)
        return expr
    column = _text(condition.get("column"))
    if not column:
        raise ValueError(_MESSAGE_WHEN_COLUMN_IS_REQUIRED)
    col = pl.col(column)
    if bool(condition.get("is_empty")):
        return col.is_null() | (col.cast(pl.String) == "")
    if bool(condition.get("is_not_empty")):
        return col.is_not_null() & (col.cast(pl.String) != "")
    if "equals" in condition:
        return col.cast(pl.String) == _text(condition.get("equals"))
    if "not_equals" in condition:
        return col.cast(pl.String) != _text(condition.get("not_equals"))
    if "in" in condition:
        return col.cast(pl.String).is_in([_text(item) for item in _as_list(condition.get("in"))])
    if "not_in" in condition:
        return ~col.cast(pl.String).is_in(
            [_text(item) for item in _as_list(condition.get("not_in"))]
        )
    for operator, op_func in {
        "gt": lambda left, right: left > right,
        "gte": lambda left, right: left >= right,
        "lt": lambda left, right: left < right,
        "lte": lambda left, right: left <= right,
    }.items():
        if operator in condition:
            return op_func(
                col.cast(pl.Float64, strict=False),
                float(_text(condition.get(operator))),
            )
    raise ValueError("when requires an operator")
