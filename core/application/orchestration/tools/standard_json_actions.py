"""Deterministic standard connector adapters; primitives own scalar algorithms."""

from __future__ import annotations

from typing import Any

from langchain_core.tools import tool

from core.infrastructure import standard_json as primitive


@tool
def omit(value: Any, paths: Any) -> dict[str, Any]:
    """Omit a value deterministically."""
    normalized = paths if isinstance(paths, list) else [paths]
    return {"result": primitive.omit(value, normalized)}


@tool
def get(value: Any, path: Any, default: Any = primitive.MISSING) -> dict[str, Any]:
    """Get a value deterministically."""
    return {"result": primitive.json_get(value, str(path or "").strip(), default)}


@tool
def set(value: Any, path: Any, new_value: Any) -> dict[str, Any]:
    """Set a value deterministically."""
    return {"result": primitive.set_value(value, str(path or "").strip(), new_value)}


@tool
def keys(value: Any) -> dict[str, Any]:
    """Keys a value deterministically."""
    return {"result": primitive.keys(value)}


@tool
def values(value: Any) -> dict[str, Any]:
    """Values a value deterministically."""
    return {"result": primitive.values(value)}
