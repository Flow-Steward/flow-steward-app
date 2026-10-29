"""Deterministic standard connector adapters; primitives own scalar algorithms."""

from __future__ import annotations

from typing import Any

from langchain_core.tools import tool

from core.infrastructure import standard_array as primitive
from core.infrastructure.standard_numbers import coerce as coerce_number


@tool
def length(value: Any) -> dict[str, Any]:
    """Length a value deterministically."""
    return {"result": primitive.length(primitive.coerce(value))}


@tool
def compact(value: Any) -> dict[str, Any]:
    """Compact a value deterministically."""
    return {"result": primitive.compact(primitive.coerce(value))}


@tool
def first(value: Any, default: Any = None) -> dict[str, Any]:
    """First a value deterministically."""
    return {"result": primitive.first(primitive.coerce(value), default)}


@tool
def at(value: Any, index: Any, default: Any = None) -> dict[str, Any]:
    """At a value deterministically."""
    items = primitive.coerce(value)
    position = int(coerce_number(index))
    return {"result": primitive.at(items, position, default)}


@tool
def join_text(value: Any, separator: Any) -> dict[str, Any]:
    """Join text a value deterministically."""
    normalized = str(separator or "")
    return {"result": primitive.join_text(primitive.coerce(value), normalized)}
