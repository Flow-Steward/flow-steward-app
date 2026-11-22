"""The first user of an installation is always an administrator, whatever the channel."""

from __future__ import annotations

from typing import Any

import pytest

from core.application.auth.user_admin.contracts import UserCreateInput
from core.application.auth.user_admin.repository import (
    FirstUserMustBeAdminError,
    _roles_for_new_user,
)
from core.application.management.errors import ServiceValidationError

_PASSWORD = "Password123!"  # pragma: allowlist secret - a test fixture


class _Cursor:
    def __init__(self, users_exist: bool) -> None:
        self.users_exist = users_exist
        self.statements: list[str] = []

    def execute(self, sql: str, _params: Any = None) -> None:
        self.statements.append(sql)

    def fetchone(self) -> tuple[bool]:
        return (self.users_exist,)


def test_the_first_user_without_roles_becomes_admin() -> None:
    assert _roles_for_new_user(_Cursor(users_exist=False), []) == ["admin"]


def test_the_first_user_with_admin_keeps_the_requested_roles() -> None:
    assert _roles_for_new_user(_Cursor(users_exist=False), ["admin", "operator"]) == [
        "admin",
        "operator",
    ]


@pytest.mark.parametrize("roles", [["viewer"], ["operator"], ["operator", "viewer"]])
def test_the_first_user_cannot_be_created_without_admin(roles: list[str]) -> None:
    with pytest.raises(FirstUserMustBeAdminError):
        _roles_for_new_user(_Cursor(users_exist=False), roles)


def test_a_later_user_without_roles_becomes_viewer() -> None:
    assert _roles_for_new_user(_Cursor(users_exist=True), []) == ["viewer"]


def test_a_later_user_keeps_explicit_roles() -> None:
    assert _roles_for_new_user(_Cursor(users_exist=True), ["operator"]) == ["operator"]


def test_create_input_has_no_role_default() -> None:
    """The CLI and API pass no role unless asked; the repository decides."""
    assert UserCreateInput(email="first@example.com", password=_PASSWORD).role == ""


def _service(repository: Any):
    from core.application.auth.user_admin.command_service import UserAdminCommandService

    return UserAdminCommandService(
        get_logger=lambda: object(),
        auth_tables_available_fn=lambda _logger: True,
        get_user_by_username_fn=lambda *_args: None,
        get_user_by_id_fn=lambda *_args: {
            "id": "u1",
            "username": "first@example.com",
            "roles": ["admin"],
        },
        normalize_requested_roles_fn=lambda roles: list(roles or []),
        seed_default_roles_fn=lambda _logger: None,
        password_hash_fn=lambda value: f"hash:{value}",
        log_auth_audit_event_fn=lambda *_args, **_kwargs: None,
        repository=repository,
    )


class _Repository:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.roles: list[str] | None = None

    def create_auth_user(self, _bl: Any, **kwargs: Any) -> str:
        self.roles = list(kwargs["roles"])
        if self.error is not None:
            raise self.error
        return "u1"


def test_the_service_leaves_the_default_to_the_repository() -> None:
    repository = _Repository()

    result = _service(repository).create_user(
        UserCreateInput(email="first@example.com", password=_PASSWORD)
    )

    assert repository.roles == []
    assert result.user.roles == ["admin"]


def test_the_service_reports_a_first_user_without_admin_as_a_validation_error() -> None:
    repository = _Repository(FirstUserMustBeAdminError("The first user must be an administrator"))

    with pytest.raises(ServiceValidationError, match="first user must be an administrator"):
        _service(repository).create_user(
            UserCreateInput(email="first@example.com", password=_PASSWORD, role="viewer")
        )


def test_the_service_passes_the_one_requested_role_to_the_repository() -> None:
    repository = _Repository()

    _service(repository).create_user(
        UserCreateInput(email="first@example.com", password=_PASSWORD, role="Admin")
    )

    assert repository.roles == ["admin"]
