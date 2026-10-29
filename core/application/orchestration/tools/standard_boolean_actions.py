"""Deterministic standard connector adapters; primitives own scalar algorithms."""

from __future__ import annotations

from typing import Any

from langchain_core.tools import tool

from core.infrastructure.standard_boolean import coerce as coerce_boolean


@tool
def coerce(value: Any) -> dict[str, Any]:
    """Coerce a value deterministically."""
    return {"result": coerce_boolean(value)}
