"""Deterministic standard connector adapters; primitives own scalar algorithms."""

from __future__ import annotations

import json
from typing import Any

from langchain_core.tools import tool

from core.infrastructure import standard_text as primitive
from core.infrastructure.standard_numbers import coerce as coerce_number

#: Padding formats a value for display; a width taken from workflow data must not
#: make a worker allocate gigabytes of fill.
MAX_PAD_WIDTH = 10_000


@tool
def trim(value: Any) -> dict[str, Any]:
    """Trim a value deterministically."""
    return {"result": primitive.trim(str(value))}


@tool
def title_case(value: Any) -> dict[str, Any]:
    """Title case a value deterministically."""
    return {"result": primitive.title_case(str(value))}


@tool
def replace(value: Any, find: Any, replace_with: Any) -> dict[str, Any]:
    """Replace a value deterministically."""
    return {"result": primitive.replace(str(value), str(find or ""), str(replace_with or ""))}


@tool
def append(value: Any, append_value: Any) -> dict[str, Any]:
    """Append a value deterministically."""
    if isinstance(append_value, (dict, list)):
        append_value = json.dumps(append_value, ensure_ascii=False)
    return {
        "result": primitive.append(f"{value}", f"{'' if append_value is None else append_value}")
    }


@tool
def pad(value: Any, width: Any, fill_char: Any = " ", side: Any = "left") -> dict[str, Any]:
    """Pad a value deterministically."""
    places = int(coerce_number(width))
    if places > MAX_PAD_WIDTH:
        raise ValueError(f"width must not exceed {MAX_PAD_WIDTH}")
    fill = str(fill_char or " ")
    if len(fill) != 1:
        raise ValueError("fill_char must contain exactly one character")
    normalized_side = str(side or "left").strip().lower()
    if normalized_side not in {"left", "right"}:
        raise ValueError("side must be left or right")
    return {"result": primitive.pad(str(value), places, fill, normalized_side)}
