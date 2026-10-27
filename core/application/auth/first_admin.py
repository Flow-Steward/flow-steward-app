"""Atomic first-administrator creation use case."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Protocol


class FirstAdminAlreadyExists(Exception):
    """Raised when the zero-user bootstrap invariant no longer holds."""


class FirstAdminRepository(Protocol):
    def create_if_empty(
        self, *, email: str, password_hash: str, display_name: str
    ) -> dict[str, Any] | None: ...


class FirstAdminService:
    def __init__(
        self, *, repository: FirstAdminRepository, password_hash: Callable[[str], str]
    ) -> None:
        self._repository = repository
        self._password_hash = password_hash

    def create(self, *, email: str, password: str, display_name: str) -> dict[str, Any]:
        result = self._repository.create_if_empty(
            email=email,
            password_hash=self._password_hash(password),
            display_name=display_name or email,
        )
        if result is None:
            raise FirstAdminAlreadyExists
        return result
