"""Explicit unit-of-work boundary for atomic job lifecycle writes."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from typing import Any


class JobLifecycleUnitOfWork:
    def __init__(self, transaction_factory: Callable[[], AbstractContextManager[Any]]) -> None:
        self._transaction_factory = transaction_factory

    @contextmanager
    def transaction(self) -> Iterator[Any]:
        with self._transaction_factory() as conn:
            yield conn


__all__ = ["JobLifecycleUnitOfWork"]
