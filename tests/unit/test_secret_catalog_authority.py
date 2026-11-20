"""Exact-only secret scopes and who may change, bind and approve catalog secrets (ADR 0057)."""

from __future__ import annotations

import pytest

from core.application.auth.scope_contracts import EXACT_ONLY_SCOPES, scope_granted
from core.application.management.errors import ServiceForbiddenError
from core.application.management.scope import ScopeContext
from core.application.secrets import authority
from core.infrastructure.db import platform_logger_common

WILDCARDS = ("admin:all", "*:*", "secrets:*", "*:bind", "*:write")


@pytest.mark.parametrize("scope", sorted(EXACT_ONLY_SCOPES))
@pytest.mark.parametrize("wildcard", WILDCARDS)
def test_no_wildcard_grants_a_secret_scope(scope, wildcard) -> None:
    assert scope_granted({wildcard}, scope) is False
    assert platform_logger_common._scope_granted({wildcard}, scope) is False


@pytest.mark.parametrize("scope", sorted(EXACT_ONLY_SCOPES))
def test_the_exact_scope_still_grants_next_to_wildcards(scope) -> None:
    assert scope_granted({scope, *WILDCARDS}, scope) is True
    assert platform_logger_common._scope_granted({scope}, scope) is True


def test_both_scope_checks_name_the_same_exact_scopes() -> None:
    assert platform_logger_common._EXACT_ONLY_SCOPES == EXACT_ONLY_SCOPES


def test_other_scopes_keep_their_wildcards() -> None:
    assert scope_granted({"admin:all"}, "workflows:write") is True
    assert scope_granted({"workflows:*"}, "workflows:write") is True


def _scope(**fields) -> ScopeContext:
    return ScopeContext(account_id="acct", **fields)


@pytest.mark.parametrize("auth_source", ["local", "external"])
def test_an_interactive_operator_writes_values_but_does_not_configure(auth_source) -> None:
    """ADR 0066 amendment: values are the operator's; bindings, approval and delete
    are configuration and the admin's."""
    person = _scope(principal_type="operator", auth_source=auth_source)
    authority.require_secret_write(person)
    for check in (
        authority.require_secret_bind,
        authority.require_secret_delete,
        authority.require_approval_authority,
    ):
        with pytest.raises(ServiceForbiddenError, match="Admin role required"):
            check(person)


def test_an_interactive_admin_writes_binds_deletes_and_approves() -> None:
    person = _scope(principal_type="admin", auth_source="local")
    authority.require_secret_write(person)
    authority.require_secret_bind(person)
    authority.require_secret_delete(person)
    authority.require_approval_authority(person)


def test_a_viewer_does_none_of_it() -> None:
    viewer = _scope(principal_type="viewer", auth_source="local")
    for check in (
        authority.require_secret_write,
        authority.require_secret_bind,
        authority.require_secret_delete,
        authority.require_approval_authority,
    ):
        with pytest.raises(ServiceForbiddenError):
            check(viewer)


def test_a_service_account_deletes_with_the_exact_write_scope() -> None:
    """Machine authority is unchanged: `secrets:write` still deletes."""
    machine = _scope(
        principal_type="service_account",
        auth_source="machine",
        token_scopes=frozenset({"secrets:write"}),
    )
    authority.require_secret_delete(machine)


@pytest.mark.parametrize("source", ["pat", "machine", "token", "oauth"])
def test_a_token_needs_the_exact_scope_whatever_its_role(source) -> None:
    broad = _scope(principal_type="admin", auth_source=source, token_scopes=frozenset(WILDCARDS))
    with pytest.raises(ServiceForbiddenError):
        authority.require_secret_bind(broad)
    with pytest.raises(ServiceForbiddenError):
        authority.require_secret_write(broad)
    exact = _scope(
        principal_type="operator",
        auth_source=source,
        token_scopes=frozenset({"secrets:bind", "secrets:write"}),
    )
    authority.require_secret_bind(exact)
    authority.require_secret_write(exact)
    with pytest.raises(ServiceForbiddenError):
        authority.require_approval_authority(exact)


@pytest.mark.parametrize(
    "fields",
    [
        {"principal_type": "service_account", "auth_source": "local"},
        {"principal_type": "admin", "auth_source": ""},
        {"principal_type": "admin", "auth_source": "mcp_dev"},
        {"principal_type": "system_actor", "auth_source": "local"},
    ],
)
def test_only_a_trusted_interactive_session_counts_as_a_person(fields) -> None:
    scope = _scope(**fields)
    assert authority.is_interactive_human(scope) is False
    with pytest.raises(ServiceForbiddenError):
        authority.require_approval_authority(scope)


def test_a_person_is_decided_by_the_stored_permissions_on_the_scope() -> None:
    """ADR 0076: the request's permissions, read from ``role_permissions``, decide."""
    operator = _scope(
        principal_type="operator",
        auth_source="local",
        permissions=frozenset({"secrets.bind"}),
    )
    authority.require_secret_bind(operator)
    with pytest.raises(ServiceForbiddenError, match="Operator role required"):
        authority.require_secret_write(operator)


def test_the_secret_checks_use_the_catalogue_keys() -> None:
    from core.application.auth.permissions import PERMISSIONS

    for key in (
        authority.SECRET_VALUE_WRITE_PERMISSION,
        authority.SECRET_DELETE_PERMISSION,
        authority.SECRET_BIND_PERMISSION,
        authority.SECRET_APPROVE_PERMISSION,
    ):
        assert PERMISSIONS[key].exact_match
