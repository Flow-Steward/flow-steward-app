"""ADR 0076 §7: a user holds one system role, and every input names one."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from core.application.auth.user_admin.contracts import (
    UserCreateInput,
    UserRolesUpdateInput,
    UserUpdateInput,
)
from core.application.management.requests import AuthUserCreateRequest, AuthUserUpdateRequest

_NEW_USER = {"email": "maya@example.com", "password": "Password123!"}  # pragma: allowlist secret


def test_the_user_form_sends_one_role() -> None:
    assert AuthUserCreateRequest(**_NEW_USER, role="operator").requested_role() == "operator"
    assert AuthUserUpdateRequest(role="admin").requested_role() == "admin"


def test_without_a_role_the_server_default_applies() -> None:
    assert AuthUserCreateRequest(**_NEW_USER).requested_role() is None
    assert AuthUserUpdateRequest(display_name="Maya").requested_role() is None


@pytest.mark.parametrize(
    ("roles", "expected"),
    (
        (["viewer", "admin"], "admin"),
        (["operator", "viewer"], "operator"),
        (["Viewer"], "viewer"),
    ),
)
def test_a_list_of_roles_keeps_the_highest_system_role(roles: list[str], expected: str) -> None:
    """Fixtures and smoke scripts in this repository still send a list."""
    assert AuthUserCreateRequest(**_NEW_USER, roles=roles).requested_role() == expected
    assert AuthUserUpdateRequest(roles=roles).requested_role() == expected


def test_a_list_without_a_system_role_is_refused_by_the_contract() -> None:
    requested = AuthUserUpdateRequest(roles=["owner"]).requested_role()

    assert requested == "owner"
    with pytest.raises(ValidationError, match="role must be one of admin, operator or viewer"):
        UserUpdateInput(user_id="u1", role=requested)


def test_an_empty_list_asks_for_no_role_and_is_refused_on_update() -> None:
    requested = AuthUserUpdateRequest(roles=[]).requested_role()

    assert requested == ""
    with pytest.raises(ValidationError, match="role must be one of"):
        UserUpdateInput(user_id="u1", role=requested)


def test_role_and_roles_together_are_refused() -> None:
    with pytest.raises(ValidationError, match="send role or roles, not both"):
        AuthUserCreateRequest(**_NEW_USER, role="viewer", roles=["admin"])
    with pytest.raises(ValidationError, match="send role or roles, not both"):
        AuthUserUpdateRequest(role="viewer", roles=["admin"])


@pytest.mark.parametrize("role", ("admin", " Operator ", "VIEWER"))
def test_the_contracts_hold_one_normalized_system_role(role: str) -> None:
    expected = role.strip().lower()

    assert UserCreateInput(**_NEW_USER, role=role).role == expected
    assert UserUpdateInput(user_id="u1", role=role).role == expected
    assert UserRolesUpdateInput(user_id="u1", role=role).role == expected


def test_the_contracts_have_no_role_list() -> None:
    for model in (UserCreateInput, UserUpdateInput, UserRolesUpdateInput):
        assert "roles" not in model.model_fields
        assert "role" in model.model_fields
