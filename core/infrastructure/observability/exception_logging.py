"""Traceback frames without exception payloads or chained driver credentials."""

from __future__ import annotations

from types import TracebackType


def redacted_exception_info(
    error: BaseException,
) -> tuple[type[RuntimeError], RuntimeError, TracebackType | None]:
    """Retain call sites while excluding the original message, cause and context."""
    redacted = RuntimeError("exception detail redacted")
    return RuntimeError, redacted, error.__traceback__
