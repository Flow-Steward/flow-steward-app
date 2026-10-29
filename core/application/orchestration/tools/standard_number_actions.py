"""Deterministic standard connector adapters; primitives own scalar algorithms."""

from __future__ import annotations

from typing import Any

from langchain_core.tools import tool

from core.infrastructure import standard_numbers as primitive


@tool
def coerce(value: Any) -> dict[str, Any]:
    """Coerce a value deterministically."""
    return {"result": primitive.coerce(value)}


@tool
def add(value: Any, operand: Any) -> dict[str, Any]:
    """Add a value deterministically."""
    return {"result": primitive.add(primitive.coerce(value), float(primitive.coerce(operand)))}


@tool
def subtract(value: Any, operand: Any) -> dict[str, Any]:
    """Subtract a value deterministically."""
    return {"result": primitive.subtract(primitive.coerce(value), float(primitive.coerce(operand)))}


@tool
def multiply(value: Any, factor: Any) -> dict[str, Any]:
    """Multiply a value deterministically."""
    return {"result": primitive.multiply(primitive.coerce(value), float(primitive.coerce(factor)))}


@tool
def divide(value: Any, divisor: Any) -> dict[str, Any]:
    """Divide a value deterministically."""
    normalized = float(primitive.coerce(divisor))
    if normalized == 0:
        raise ValueError("divisor must not be zero")
    return {"result": primitive.divide(primitive.coerce(value), normalized)}


@tool
def floor(value: Any) -> dict[str, Any]:
    """Floor a value deterministically."""
    return {"result": primitive.floor(primitive.coerce(value))}


@tool
def ceil(value: Any) -> dict[str, Any]:
    """Ceil a value deterministically."""
    return {"result": primitive.ceil(primitive.coerce(value))}
