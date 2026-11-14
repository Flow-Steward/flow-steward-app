"""The one host resolver of an operation's project connection (ADR 0067)."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from core.application.extensions import operation_targets
from core.application.management.errors import (
    ServiceConflictError,
    ServiceForbiddenError,
    ServiceValidationError,
)
from core.application.management.scope import ScopeContext
from core.application.secrets.approval_gate import SecretConfigurationUnapproved

_CATALOG_REFS = {
    "refs": {"api_token": "catalog:secret-1"},
    "catalog": {"api_token": "secret-1"},
}


def _connection(**overrides) -> dict:
    row = {
        "connection_id": "conn-1",
        "account_id": "acct-1",
        "project_id": "7",
        "extension_id": "example.connector",
        "extension_install_id": "install-1",
        "connection_type": "service_profile",
        "connection_config": {"base_url": "https://service.example"},
        "secret_refs": dict(_CATALOG_REFS),
        "enabled": True,
    }
    row.update(overrides)
    return row


def _store(connection: dict, *, reads_secrets: bool = False) -> MagicMock:
    # Without ``cursor`` the resolver reads no stored secret and no OAuth grant.
    methods = ["get_project_connection", "get_project", "get_provider_secret"]
    store = MagicMock(spec=[*methods, "cursor"] if reads_secrets else methods)
    store.get_project_connection.return_value = connection
    store.get_provider_secret.return_value = None
    return store


def _target(store: MagicMock, **overrides):
    arguments = {
        "connection_id": "conn-1",
        "account_id": "acct-1",
        "project_id": "7",
        "extension_id": "example.connector",
        "extension_install_id": "install-1",
        "required_connection_type_ids": frozenset({"service_profile"}),
    }
    arguments.update(overrides)
    return operation_targets._direct_action_connection_target(store, **arguments)


def test_an_unapproved_configuration_is_refused_with_its_code_before_any_secret_is_read():
    store = _store(_connection(), reads_secrets=True)
    with (
        patch(
            "core.application.secrets.approval_gate.require_approved_connection_use",
            side_effect=SecretConfigurationUnapproved(),
        ),
        patch(
            "core.infrastructure.security.connection_secret_store.resolve_connection_secrets",
            side_effect=AssertionError("no stored secret may be read"),
        ) as read_secrets,
        pytest.raises(ServiceConflictError) as refused,
    ):
        _target(store)

    assert refused.value.status_code == 409
    assert refused.value.payload == {
        "error": "connection_secret_resolution_failed:conn-1:secret_configuration_unapproved",
        "code": "secret_configuration_unapproved",
    }
    read_secrets.assert_not_called()


def test_another_secret_failure_stays_a_validation_error_without_a_code():
    store = _store(_connection(), reads_secrets=True)
    with (
        patch(
            "core.application.management.project_connection_secrets.resolve_connection_secrets",
            side_effect=RuntimeError("driver said: https://vault.example/raw"),
        ),
        pytest.raises(ServiceValidationError) as refused,
    ):
        _target(store)

    assert not isinstance(refused.value, ServiceConflictError)
    assert refused.value.status_code == 400
    assert refused.value.payload == {
        "error": "connection_secret_resolution_failed:conn-1:secret_value_unavailable"
    }
    assert "vault.example" not in repr(refused.value.payload)


def _scope(**overrides) -> ScopeContext:
    values = {"account_id": "acct-1", "actor_id": "jane", "principal_type": "operator"}
    values.update(overrides)
    return ScopeContext(**values)


def _resolve(store: MagicMock, **overrides):
    arguments = {
        "connection_id": "conn-1",
        "project_id": "7",
        "scope": _scope(),
        "extension": {"extension_key": "example.connector", "extension_install_id": "install-1"},
        "extension_id": "example.connector",
        "account_id": "acct-1",
        "required_connection_type_ids": frozenset({"service_profile"}),
    }
    arguments.update(overrides)
    return operation_targets.resolve_operation_connection(store, **arguments)


def test_the_resolver_checks_project_access_before_it_loads_the_connection():
    store = _store(_connection(secret_refs={}))
    store.get_project.return_value = {"id": "7", "account_id": "acct-other"}

    with pytest.raises(ServiceForbiddenError):
        _resolve(store)

    store.get_project_connection.assert_not_called()


def test_the_resolver_returns_the_connection_with_its_checked_scope():
    store = _store(_connection(secret_refs={}))
    store.get_project.return_value = {"id": "7", "account_id": "acct-1"}
    authorized: set[str] = set()

    resolved = _resolve(store, authorized_project_ids=authorized)

    assert authorized == {"7"}
    assert resolved.connection_id == "conn-1"
    assert resolved.audit_fields() == {"project_id": "7", "connection_id": "conn-1"}
    assert resolved.runtime_context() == {
        "project_id": "7",
        "connection_id": "conn-1",
        "extension_install_id": "install-1",
        "oauth_configuration_id": "",
        "oauth_provider_id": "",
    }
    assert resolved.connection["config"] == {"base_url": "https://service.example"}


def test_the_resolver_does_not_recheck_a_project_the_caller_already_passed():
    store = _store(_connection(secret_refs={}))
    store.get_project.side_effect = AssertionError("already authorized")

    resolved = _resolve(store, authorized_project_ids={"7"})

    assert resolved.project_id == "7"


@pytest.mark.parametrize(
    ("row", "code"),
    [
        (_connection(project_id="8", secret_refs={}), "target.connection_id must reference"),
        (_connection(account_id="acct-2", secret_refs={}), "target.connection_id must reference"),
        (
            _connection(extension_id="example.other", secret_refs={}),
            "target.connection_id must reference",
        ),
        (
            _connection(extension_install_id="install-2", secret_refs={}),
            "target.connection_id must reference",
        ),
        (_connection(enabled=False, secret_refs={}), "target.connection_id must reference"),
        (
            _connection(connection_type="other_type", secret_refs={}),
            "connection_runtime_requirement_failed:conn-1",
        ),
    ],
)
def test_the_resolver_refuses_a_connection_outside_the_operation(row: dict, code: str):
    store = _store(row)
    store.get_project.return_value = {"id": "7", "account_id": "acct-1"}

    with pytest.raises(ServiceValidationError) as refused:
        _resolve(store)

    assert str(refused.value.payload["error"]).startswith(code)


def test_the_resolver_needs_the_install():
    store = _store(_connection(secret_refs={}))
    store.get_project.return_value = {"id": "7", "account_id": "acct-1"}

    with pytest.raises(ServiceValidationError) as refused:
        _resolve(store, extension={"extension_key": "example.connector"})

    assert refused.value.payload == {
        "error": "extension_install_id is required for extension operations"
    }
    store.get_project_connection.assert_not_called()


def test_a_resource_query_reports_an_unapproved_connection_with_its_code():
    from core.infrastructure.extension_runtime import extension_runtime_helpers as helpers

    extension = {
        "extension_key": "example.connector",
        "query_definitions": [{"query_id": "list_items", "handler": {"mode": "extension"}}],
    }
    with (
        patch.object(
            helpers, "resolve_extension_runtime_record", return_value=(extension, MagicMock())
        ),
        patch.object(helpers, "build_extension_compiled_manifest", return_value={}),
        patch(
            "core.application.extensions.operation_support.extension_operation_contract",
            return_value=SimpleNamespace(
                connection_type_ids=["service_profile"], required_oauth_scopes=[]
            ),
        ),
        patch(
            "core.application.extensions.operation_targets._direct_action_connection_target",
            side_effect=ServiceConflictError(
                "connection_secret_resolution_failed:conn-1:secret_configuration_unapproved",
                payload={
                    "error": "connection_secret_resolution_failed:conn-1:"
                    "secret_configuration_unapproved",
                    "code": "secret_configuration_unapproved",
                },
            ),
        ),
        patch(
            "core.infrastructure.extension_runtime.extension_runtime.run_extension_query"
        ) as run_query,
    ):
        provider = helpers.get_extension_query_provider("install-1", get_logger_only=lambda: None)
        result = provider(
            {
                "query_id": "list_items",
                "scope": {"account_id": "acct-1", "project_id": "7", "connection_id": "conn-1"},
                "query_manifest": {
                    "operation_id": "list_items",
                    "connection_type_id": "service_profile",
                },
            }
        )

    run_query.assert_not_called()
    assert result == {
        "items": [],
        "next_cursor": "",
        "total": 0,
        "error": "connection_secret_resolution_failed:conn-1:secret_configuration_unapproved",
        "error_code": "secret_configuration_unapproved",
    }
