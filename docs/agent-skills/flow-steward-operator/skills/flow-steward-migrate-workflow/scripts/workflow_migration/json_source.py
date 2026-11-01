"""Strict bounded JSON parsing shared by source documents and literal parameters."""

from __future__ import annotations

import json
import math
from typing import Any


def _object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate object member")
        result[key] = value
    return result


def _constant(_: str) -> Any:
    raise ValueError("Nonfinite number")


def _float(text: str) -> float:
    value = float(text)
    if not math.isfinite(value):
        raise ValueError("Nonfinite number")
    return value


def load_json(text: str) -> Any:
    """Reject duplicate names, nonfinite numbers and excessive nesting."""
    result = json.loads(
        text, object_pairs_hook=_object, parse_constant=_constant, parse_float=_float
    )
    stack = [(result, 0)]
    while stack:
        value, depth = stack.pop()
        if depth > 64:
            raise ValueError("JSON nesting exceeds the conversion limit")
        if isinstance(value, dict):
            stack.extend((item, depth + 1) for item in value.values())
        elif isinstance(value, list):
            stack.extend((item, depth + 1) for item in value)
    return result
