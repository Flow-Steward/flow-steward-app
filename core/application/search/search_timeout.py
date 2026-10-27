from __future__ import annotations

import time
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

from core.application.management.errors import ServiceUnavailableError


class SearchQueryTimeoutError(ServiceUnavailableError):
    def __init__(self) -> None:
        super().__init__(
            "Search request timed out",
            payload={"error": "Search request timed out", "code": "search_timeout"},
        )


_SEARCH_DEADLINE: ContextVar[float | None] = ContextVar("search_deadline", default=None)


@contextmanager
def search_timeout_budget(timeout_seconds: float) -> Iterator[None]:
    deadline = time.monotonic() + max(0.001, float(timeout_seconds))
    token = _SEARCH_DEADLINE.set(deadline)
    try:
        yield
    finally:
        _SEARCH_DEADLINE.reset(token)


def remaining_search_timeout_ms() -> int | None:
    deadline = _SEARCH_DEADLINE.get()
    if deadline is None:
        return None
    remaining_ms = int((deadline - time.monotonic()) * 1000)
    if remaining_ms <= 0:
        raise SearchQueryTimeoutError
    return max(1, remaining_ms)


def is_postgres_query_cancellation(exc: BaseException) -> bool:
    current: BaseException | None = exc
    while current is not None:
        sqlstate = (
            str(getattr(current, "sqlstate", "") or "").strip().upper()
            or str(getattr(current, "pgcode", "") or "").strip().upper()
        )
        if sqlstate == "57014":
            return True
        current = current.__cause__ or current.__context__
    return False


__all__ = [
    "SearchQueryTimeoutError",
    "is_postgres_query_cancellation",
    "remaining_search_timeout_ms",
    "search_timeout_budget",
]
