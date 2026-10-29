"""Deterministic standard tool actions for connector-step workflows."""

from __future__ import annotations

import builtins
import json
from typing import Any

from langchain_core.tools import tool

from core.application.orchestration.tools.standard_json_actions import get, keys, omit, set, values
from core.application.orchestration.tools.standard_number_actions import (
    add,
    ceil,
    coerce,
    divide,
    floor,
    multiply,
    subtract,
)
from core.application.orchestration.tools.standard_text_actions import (
    append,
    pad,
    replace,
    title_case,
    trim,
)
from core.infrastructure import standard_text
from core.infrastructure.html_text import html_to_text as _html_to_text


@tool
def round(value: int | float, decimals: int = 0) -> dict[str, Any]:
    """Round a numeric value to the requested number of decimal places."""
    return {"result": builtins.round(float(value), int(decimals or 0))}


@tool
def upper(value: Any) -> dict[str, Any]:
    """Return the input value as uppercase text."""
    return {"result": standard_text.upper("" if value is None else str(value))}


@tool
def lower(value: Any) -> dict[str, Any]:
    """Return the input value as lowercase text."""
    return {"result": standard_text.lower("" if value is None else str(value))}


@tool
def html_to_text(value: str) -> dict[str, Any]:
    """Extract plain text from HTML, removing scripts, styles and extra whitespace."""
    return {"result": _html_to_text(value)}


@tool
def length(value: Any) -> dict[str, Any]:
    """Return the length of a string, array, or object value."""
    try:
        result = len(value)
    except TypeError:
        result = 0
    return {"result": result}


@tool
def stringify(value: Any) -> dict[str, Any]:
    """Return a deterministic JSON string for the input value."""
    return {"result": json.dumps(value, ensure_ascii=False, sort_keys=True)}


@tool
def pick(value: Any, path: str) -> dict[str, Any]:
    """Pick a dotted path from an object value."""
    cursor = value
    for part in [item for item in str(path or "").split(".") if item]:
        if isinstance(cursor, dict):
            cursor = cursor.get(part)
            continue
        if isinstance(cursor, list):
            try:
                cursor = cursor[int(part)]
                continue
            except (TypeError, ValueError, IndexError):
                cursor = None
                break
        cursor = None
        break
    return {"result": cursor}


def _ensure_resolver_compatible(*tools: Any) -> None:
    for item in tools:
        if not hasattr(item, "args_schema"):
            item.args_schema = None


_ensure_resolver_compatible(round, upper, lower, html_to_text, length, stringify, pick)

_ensure_resolver_compatible(
    get,
    keys,
    omit,
    set,
    values,
    add,
    ceil,
    coerce,
    divide,
    floor,
    multiply,
    subtract,
    append,
    pad,
    replace,
    title_case,
    trim,
)
