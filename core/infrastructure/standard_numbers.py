"""Strict scalar number coercion and arithmetic for standard tool adapters.

Arithmetic accepts normalized values; adapters own parameter defaults and coercion
order. Decimal quantization shares the existing rounding-mode contract.
"""

from __future__ import annotations

import math
from decimal import Decimal

from core.infrastructure.rounding import quantize_decimal


def coerce(value: object) -> int | float:
    """Normalize a finite number with the existing transform coercion rules."""
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


def add(value: int | float, operand: float) -> float:
    return value + operand


def subtract(value: int | float, operand: float) -> float:
    return value - operand


def multiply(value: int | float, factor: float) -> float:
    return value * factor


def divide(value: int | float, divisor: float) -> float:
    if divisor == 0:
        raise ValueError("divisor must not be zero")
    return value / divisor


def round_number(value: int | float, decimals: int, mode: object = None) -> float:
    return float(quantize_decimal(Decimal(str(value)), decimals=decimals, mode=mode))


def floor(value: int | float) -> int:
    return math.floor(float(value))


def ceil(value: int | float) -> int:
    return math.ceil(float(value))
