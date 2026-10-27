"""Value normalization and coercion shared by transform validation and execution."""

from __future__ import annotations

import copy
import json
import math
from decimal import Decimal, InvalidOperation
from typing import Any

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
        if isinstance(value, list):
            return copy.deepcopy(value)
        raise ValueError(f"{type(value).__name__} is not an array")
    raise ValueError(f"unsupported value type '{value_type}'")


def _money_decimal(value: Any) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise ValueError("value is not a money amount")
    try:
        amount = value if isinstance(value, Decimal) else Decimal(str(value).strip())
    except (InvalidOperation, ValueError) as exc:
        raise ValueError("value is not a money amount") from exc
    if not amount.is_finite():
        raise ValueError("money amount must be finite")
    return amount


def _money_text(value: Any) -> str:
    return format(_money_decimal(value), "f")


def _coerce_text(value: Any) -> Any:
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def _coerce_number(value: Any) -> Any:
    if isinstance(value, bool):
        raise ValueError("boolean is not a number")
    if isinstance(value, (int, float)):
        number = float(value)
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            raise ValueError("empty string is not a number")
        number = float(text)
    else:
        raise ValueError(f"{type(value).__name__} is not a number")
    if not math.isfinite(number):
        raise ValueError("number must be finite")
    return int(number) if number.is_integer() else number


def _coerce_boolean(value: Any) -> Any:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "1", "yes", "on"}:
            return True
        if lowered in {"false", "0", "no", "off", ""}:
            return False
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value != 0
    raise ValueError(f"{type(value).__name__} is not a boolean")
