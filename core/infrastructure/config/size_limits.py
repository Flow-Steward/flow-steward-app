"""Shared, fail-safe resolution of byte-size limits from environment variables.

Each limit is read ONCE at its owning module's import time and stored in a
module-level constant — never re-read inside request/processing loops. Pass the
raw ``os.getenv("FS_...")`` value to :func:`clamp_size_limit_bytes` so the env
name stays a literal at the call site (keeping it visible to the env-audit scan)
while validation/clamping lives here.

Resolution rules (fail-safe):
- unset / empty / non-numeric  -> the (range-clamped) ``default``
- value below ``minimum``       -> ``minimum``
- value above ``maximum``       -> ``maximum``
- otherwise                     -> the provided value
"""

from __future__ import annotations


def clamp_size_limit_bytes(
    raw_value: str | None,
    *,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    """Resolve a byte-size limit from a raw env value, clamped to a safe range."""
    lo, hi = (minimum, maximum) if minimum <= maximum else (maximum, minimum)
    safe_default = min(max(int(default), lo), hi)

    text = str(raw_value).strip() if raw_value is not None else ""
    if not text:
        return safe_default
    try:
        value = int(text)
    except (TypeError, ValueError):
        return safe_default
    if value < lo:
        return lo
    if value > hi:
        return hi
    return value


__all__ = ["clamp_size_limit_bytes"]
