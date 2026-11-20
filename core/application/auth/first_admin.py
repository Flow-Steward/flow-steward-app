"""Atomic first-administrator creation use case."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Protocol

from core.application.auth.config import AUTH_PASSWORD_MIN_LENGTH
from core.application.auth.password_policy import password_policy_error
from core.application.management.errors import ServiceValidationError


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
        issue = password_policy_error(password, AUTH_PASSWORD_MIN_LENGTH)
        if issue:
            raise ServiceValidationError(issue)
        result = self._repository.create_if_empty(
            email=email,
            password_hash=self._password_hash(password),
            display_name=display_name or email,
        )
        if result is None:
            raise FirstAdminAlreadyExists
        return result
