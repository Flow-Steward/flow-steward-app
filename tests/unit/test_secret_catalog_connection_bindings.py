"""Binding stored secrets to a connection's credential fields, as one decision."""

from __future__ import annotations

import pytest

from core.application.management.errors import ServiceForbiddenError, ServiceValidationError
from core.application.management.scope import ScopeContext
from core.application.secrets.service import SecretCatalogService


def _service() -> SecretCatalogService:
    def _no_database():
        raise AssertionError("no transaction may open before the authority check")

    return SecretCatalogService(_no_database)


def test_binding_needs_bind_authority_before_any_database_work() -> None:
    machine = ScopeContext(account_id="acct-1", actor_id="worker", principal_type="operator")

    with pytest.raises(ServiceForbiddenError, match="secrets:bind"):
        _service().bind_connection_fields("conn-1", machine, bindings={"api_token": "secret-1"})


def test_binding_refuses_an_oauth_grant_field_and_an_empty_choice() -> None:
    person = ScopeContext(
        account_id="acct-1", actor_id="person", principal_type="operator", auth_source="local"
    )

    with pytest.raises(ServiceValidationError, match="OAuth grant"):
        _service().bind_connection_fields(
            "conn-1", person, bindings={"_oauth_access_token": "secret-1"}
        )
    with pytest.raises(ServiceValidationError, match="at least one"):
        _service().bind_connection_fields("conn-1", person, bindings={})
