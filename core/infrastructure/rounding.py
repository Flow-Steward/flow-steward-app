"""The rounding modes the number and money actions declare, and how to apply them.

`number.round` and `money.round` have always accepted a `mode` parameter. Both
engines ignored it, so asking for a ceiling silently produced half-up and the only
way to round money up was a float composition that produced wrong money. This is
the one place that maps a declared mode onto exact decimal arithmetic, so the two
engines cannot drift apart again.
"""

from __future__ import annotations

from decimal import ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_EVEN, ROUND_HALF_UP, Decimal

DEFAULT_ROUNDING_MODE = "half_up"
SUPPORTED_ROUNDING_MODES: tuple[str, ...] = ("half_up", "half_even", "ceil", "floor")

_DECIMAL_ROUNDING = {
    "half_up": ROUND_HALF_UP,
    "half_even": ROUND_HALF_EVEN,
    # Ceiling and floor are directed towards positive and negative infinity, so a
    # negative amount rounds the way an accountant expects rather than by magnitude.
    "ceil": ROUND_CEILING,
    "floor": ROUND_FLOOR,
}


class UnsupportedRoundingModeError(ValueError):
    """A rounding mode that no engine can apply."""


def normalize_rounding_mode(mode: object) -> str:
    """Return a supported mode name, or refuse rather than silently downgrading."""
    normalized = str(mode or "").strip().lower() or DEFAULT_ROUNDING_MODE
    if normalized not in _DECIMAL_ROUNDING:
        raise UnsupportedRoundingModeError(
            f"unsupported rounding mode '{normalized}'; "
            f"use one of {', '.join(SUPPORTED_ROUNDING_MODES)}"
        )
    return normalized


def decimal_rounding(mode: object) -> str:
    """Return the `decimal` rounding constant for a declared mode."""
    return _DECIMAL_ROUNDING[normalize_rounding_mode(mode)]


def quantum(decimals: int) -> Decimal:
    """Return the quantisation step for a decimal place count."""
    places = int(decimals)
    return Decimal(1) if places <= 0 else Decimal(1).scaleb(-places)


def quantize_decimal(value: Decimal, *, decimals: int, mode: object) -> Decimal:
    """Quantise exactly, applying the declared mode."""
    return value.quantize(quantum(decimals), rounding=decimal_rounding(mode))


__all__ = [
    "DEFAULT_ROUNDING_MODE",
    "SUPPORTED_ROUNDING_MODES",
    "UnsupportedRoundingModeError",
    "decimal_rounding",
    "normalize_rounding_mode",
    "quantize_decimal",
    "quantum",
]
