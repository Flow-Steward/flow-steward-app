"""Exact scalar money conversion and arithmetic for standard tool adapters.

Arithmetic accepts normalized Decimals and retains their scale in plain text.
Adapters retain their own parameter defaults and input coercion order.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation

from core.infrastructure.rounding import quantize_decimal


def to_decimal(value: object) -> Decimal:
    """Normalize a finite amount without converting through binary floats."""
    if isinstance(value, bool) or value is None:
        raise ValueError("value is not a money amount")
    try:
        amount = value if isinstance(value, Decimal) else Decimal(str(value).strip())
    except (InvalidOperation, ValueError) as exc:
        raise ValueError("value is not a money amount") from exc
    if not amount.is_finite():
        raise ValueError("money amount must be finite")
    return amount


def to_text(value: object) -> str:
    return format(to_decimal(value), "f")


def add(value: Decimal, operand: Decimal) -> str:
    return to_text(value + operand)


def subtract(value: Decimal, operand: Decimal) -> str:
    return to_text(value - operand)


def multiply(value: Decimal, factor: Decimal) -> str:
    return to_text(value * factor)


def round_money(value: Decimal, decimals: int, mode: object = None) -> str:
    return to_text(quantize_decimal(value, decimals=decimals, mode=mode))
