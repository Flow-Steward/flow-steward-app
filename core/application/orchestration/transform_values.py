"""Value normalization and coercion shared by transform validation and execution."""

from __future__ import annotations

import copy
import json
from decimal import Decimal
from typing import Any

from core.infrastructure import standard_array, standard_boolean, standard_money, standard_numbers

TRANSFORM_VALUE_TYPES = {"text", "number", "money", "boolean", "object", "array", "json"}


def _normalize_type(value: Any) -> str:
    text = str(value or "json").strip().lower()
    if text == "string":
        return "text"
    if text == "list":
        return "array"
    return text if text in TRANSFORM_VALUE_TYPES else ""


def _types_compatible(expected: str, actual: str) -> bool:
    expected = _normalize_type(expected)
    actual = _normalize_type(actual)
    if expected == actual:
        return True
    if expected == "json":
        return bool(actual)
    return False


def _coerce_value(value: Any, value_type: str) -> Any:
    normalized = _normalize_type(value_type)
    if normalized == "json":
        return value
    if normalized == "text":
        return _coerce_text(value)
    if normalized == "number":
        return _coerce_number(value)
    if normalized == "money":
        return _money_text(value)
    if normalized == "boolean":
        return _coerce_boolean(value)
    if normalized == "object":
        if isinstance(value, dict):
            return copy.deepcopy(value)
        raise ValueError(f"{type(value).__name__} is not an object")
    if normalized == "array":
        return standard_array.coerce(value)
    raise ValueError(f"unsupported value type '{value_type}'")


def _money_decimal(value: Any) -> Decimal:
    return standard_money.to_decimal(value)


def _money_text(value: Any) -> str:
    return standard_money.to_text(value)


def _coerce_text(value: Any) -> Any:
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def _coerce_number(value: Any) -> int | float:
    return standard_numbers.coerce(value)


def _coerce_boolean(value: Any) -> bool:
    return standard_boolean.coerce(value)
