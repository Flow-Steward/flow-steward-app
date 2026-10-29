"""Deterministic standard connector adapters; primitives own scalar algorithms."""

from __future__ import annotations

from typing import Any

from langchain_core.tools import tool

from core.infrastructure import standard_money as primitive
from core.infrastructure.standard_numbers import coerce as coerce_number


@tool
def coerce(value: Any) -> dict[str, Any]:
    """Coerce a value deterministically."""
    return {"result": primitive.to_text(value)}


@tool
def add(value: Any, operand: Any) -> dict[str, Any]:
    """Add a value deterministically."""
    return {"result": primitive.add(primitive.to_decimal(value), primitive.to_decimal(operand))}


@tool
def subtract(value: Any, operand: Any) -> dict[str, Any]:
    """Subtract a value deterministically."""
    return {
        "result": primitive.subtract(primitive.to_decimal(value), primitive.to_decimal(operand))
    }


@tool
def multiply(value: Any, factor: Any) -> dict[str, Any]:
    """Multiply a value deterministically."""
    return {"result": primitive.multiply(primitive.to_decimal(value), primitive.to_decimal(factor))}


@tool
def round(value: Any, decimals: Any = 2, mode: Any = None) -> dict[str, Any]:
    """Round a value deterministically."""
    places = int(coerce_number(decimals))
    if places < 0:
        raise ValueError("decimals must be non-negative")
    return {"result": primitive.round_money(primitive.to_decimal(value), places, mode)}
