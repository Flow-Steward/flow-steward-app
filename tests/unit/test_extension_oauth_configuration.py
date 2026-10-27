from __future__ import annotations

from contextlib import contextmanager, nullcontext
from copy import deepcopy
from uuid import UUID

import pytest

from core.application.management.errors import (
    ServiceConflictError,
    ServiceForbiddenError,
    ServiceNotFoundError,
    ServicePreconditionError,
    ServiceValidationError,
)
from core.application.management.scope import ScopeContext


def _provider() -> dict:
    return {
        "provider_id": "example",
        "display_name": "Example",
        "description": "Example OAuth",
        "setup_page_id": "setup",
        "help_page_id": "help",
        "flow": {"type": "authorization_code", "pkce": "s256"},
        "credential_fields": [
            {
                "field_id": "client_id",
                "value_type": "text",
                "required": True,
                "secret": False,
                "change_policy": "reauthorization_required",
                "label": "Client ID",
                "help_text": "Client ID help",
                "validation": {"min_length": 3, "max_length": 12},
            },
            {
                "field_id": "client_secret",
                "value_type": "secret",
                "required": True,
                "secret": True,
                "change_policy": "runtime_revalidate",
                "label": "Client secret",
                "help_text": "Client secret help",
                "validation": {"min_length": 4, "max_length": 16},
            },
            {
                "field_id": "note",
                "value_type": "text",
                "required": False,
                "secret": False,
                "change_policy": "cosmetic",
                "label": "Note",
                "help_text": "Note help",
                "validation": {"min_length": 2, "max_length": 20},
            },
            {
                "field_id": "browser_origin",
                "value_type": "text",
                "required": False,
                "secret": False,
                "change_policy": "runtime_revalidate",
                "label": "Browser origin",
                "help_text": "Optional canonical browser origin.",
                "validation": {
                    "type": "https_origin",
                    "min_length": 9,
                    "max_length": 2048,
                },
            },
        ],
        "authorization_endpoint": {"url": "https://identity.example/authorize"},
        "token_endpoint": {
            "url": "https://identity.example/token",
            "client_auth_method": "client_secret_post",
        },
        "revocation_endpoint": {"url": "https://identity.example/revoke"},
        "callback_id": "oauth_authorization",
        "authorization_intents": [
            {"intent_id": "connect", "static_parameters": {}, "user_inputs": []}
        ],
    }


def _descriptor() -> dict:
    return {
        "extension_id": "fixture.oauth",
        "callbacks": [
            {
                "callback_id": "oauth_authorization",
                "purpose": "oauth_authorization",
                "methods": ["GET"],
                "connection_scoped": False,
            }
        ],
        "action_definitions": [],
        "ui_manifest": {
            "version": "1",
            "pages": [
                {"page_id": "setup", "title": "Setup", "components": []},
                {"page_id": "help", "title": "Help", "components": []},
            ],
        },
        "runtime": {"extension_contract_v2": {"oauth_providers": [_provider()]}},
    }


class MemoryRepository:
    def __init__(self) -> None:
        self.rows: dict[str, dict] = {}
        self.connections: list[dict] = []
        self.invalidated: list[str] = []
        self.marked: list[str] = []
        self.deleted_connections: list[str] = []
        self.locked: list[str] = []

    def create_configuration(self, **payload):
        row = deepcopy(payload)
        row["created_at"] = "now"
        row["updated_at"] = "now"
        self.rows[str(row["configuration_id"])] = row
        return deepcopy(row)

    def list_configurations(self, **scope):
        return [
            deepcopy(row)
            for row in self.rows.values()
            if all(str(row.get(key)) == str(value) for key, value in scope.items())
        ]

    def get_configuration_for_scope(self, **scope):
        row = self.rows.get(str(scope.pop("configuration_id")))
        if row is None or not all(str(row.get(key)) == str(value) for key, value in scope.items()):
            return None
        return deepcopy(row)

    def get_configuration_for_scope_for_update(self, **scope):
        self.locked.append(str(scope["configuration_id"]))
        return self.get_configuration_for_scope(**scope)

    def update_configuration(self, **payload):
        key = str(payload["configuration_id"])
        row = self.rows.get(key)
        if row is None or row["configuration_revision"] != payload.pop(
            "expected_configuration_revision"
        ):
            return None
        row.update(deepcopy(payload))
        return deepcopy(row)

    def list_connections_for_configuration(self, **scope):
        return [
            deepcopy(row)
            for row in self.connections
            if str(row.get("oauth_configuration_id")) == str(scope["configuration_id"])
            and all(
                str(row.get(left)) == str(scope[right])
                for left, right in (
                    ("account_id", "account_id"),
                    ("project_id", "project_id"),
                    ("extension_install_id", "extension_install_id"),
                    ("extension_id", "extension_id"),
                    ("oauth_provider_id", "provider_id"),
                )
            )
        ]

    def list_connections_for_configuration_for_update(self, **scope):
        return self.list_connections_for_configuration(**scope)

    def invalidate_pending_states(self, **scope):
        self.invalidated.append(str(scope["configuration_id"]))
        return 1

    def mark_connections_reauthorization_required(self, **scope):
        owned = self.list_connections_for_configuration(**scope)
        ids = [row["connection_id"] for row in owned]
        for row in self.connections:
            if row["connection_id"] in ids:
                row.setdefault("connection_config", {}).setdefault("oauth", {})[
                    "lifecycle_status"
                ] = "authorization_required"
        self.marked.extend(ids)
        return len(ids)

    def delete_connections_for_configuration(self, **scope):
        owned = self.list_connections_for_configuration(**scope)
        owned_ids = {row["connection_id"] for row in owned}
        self.connections = [
            row for row in self.connections if row.get("connection_id") not in owned_ids
        ]
        self.deleted_connections.extend(sorted(owned_ids))
        return len(owned_ids)

    def repin_connections_to_release(self, **scope):
        release_id = scope.pop("extension_release_id")
        owned = {row["connection_id"] for row in self.list_connections_for_configuration(**scope)}
        for row in self.connections:
            if row["connection_id"] in owned:
                row["oauth_extension_release_id"] = release_id
        return len(owned)

    def delete_configuration(self, **scope):
        key = str(scope.pop("configuration_id"))
        expected = scope.pop("expected_configuration_revision")
        row = self.rows.get(key)
        if (
            row is None
            or row["configuration_revision"] != expected
            or not all(str(row.get(name)) == str(value) for name, value in scope.items())
        ):
            return False
        del self.rows[key]
        return True


class MemorySecrets:
    def __init__(self) -> None:
        self.values: dict[tuple[str, str], str] = {}
        self.writes: list[tuple[str, str, str]] = []
        self.deletes: list[tuple[str, str]] = []

    def present(self, *, account_id: str, ref_key: str) -> bool:
        return (account_id, ref_key) in self.values

    def write(self, *, account_id: str, ref_key: str, value: str) -> None:
        self.values[(account_id, ref_key)] = value
        self.writes.append((account_id, ref_key, value))

    def delete(self, *, account_id: str, ref_key: str) -> None:
        self.values.pop((account_id, ref_key), None)
        self.deletes.append((account_id, ref_key))


def _service(repo=None, secrets=None, transaction_factory=None, **dependencies):
    from core.application.extensions.oauth_configuration import OAuthConfigurationService

    install = {
        "install_id": "install-1",
        "extension_id": "fixture.oauth",
        "release_id": "release-1",
        "compiled_manifest": _descriptor(),
    }
    return OAuthConfigurationService(
        repository=repo or MemoryRepository(),
        secret_store=secrets or MemorySecrets(),
        install_loader=lambda install_id, account_id: (
            deepcopy(install) if install_id == "install-1" and account_id == "acct-1" else None
        ),
        confirmation_key=b"task-3-confirmation-key",
        transaction_factory=transaction_factory or nullcontext,
        **dependencies,
    )


def _scope(role="operator", *, account="acct-1", project="project-1", tenant="tenant-1"):
    return ScopeContext(
        account_id=account,
        tenant_id=tenant,
        project_id=project,
        principal_type=role,
    )


def _create(service, name="Primary"):
    from core.application.extensions.oauth_configuration import OAuthConfigurationSave

    return service.save_configuration(
        _scope(),
        "install-1",
        "example",
        OAuthConfigurationSave(
            display_name=name,
            values={"client_id": "abc", "note": "first"},
            secret_replacements={"client_secret": "secret"},
        ),
        expected_revision=None,
    )


def test_secret_ref_key_is_configuration_scoped_and_rejects_empty_parts() -> None:
    from core.application.extensions.oauth_configuration_secrets import (
        configuration_secret_ref_key,
    )

    assert configuration_secret_ref_key("cfg-1", "client_secret") == (
        "extension_oauth_configuration:cfg-1:client_secret"
    )
    with pytest.raises(ValueError):
        configuration_secret_ref_key("", "client_secret")


def test_configuration_service_requires_an_explicit_transaction_boundary() -> None:
    from core.application.extensions.oauth_configuration import OAuthConfigurationService

    _raises_input_289_1 = MemoryRepository()
    _raises_input_289_2 = MemorySecrets()
    with pytest.raises(TypeError, match="transaction_factory"):
        OAuthConfigurationService(
            repository=_raises_input_289_1,
            secret_store=_raises_input_289_2,
            install_loader=lambda _install_id, _account_id: None,
            confirmation_key=b"required-transaction-boundary",
        )


def test_create_supports_multiple_named_opaque_configurations_and_redacts_secrets() -> None:
    service = _service()
    first = _create(service, "Primary")
    second = _create(service, "Backup")

    UUID(first.configuration_id)
    UUID(second.configuration_id)
    assert first.configuration_id != second.configuration_id
    assert [
        row.display_name for row in service.list_configurations(_scope(), "install-1", "example")
    ] == [
        "Backup",
        "Primary",
    ]
    secret = next(field for field in first.fields if field.field_id == "client_secret")
    assert secret.secret_present is True
    assert not hasattr(secret, "secret_value")
    assert "extension_oauth_configuration" not in first.model_dump_json()


@pytest.mark.parametrize(
    ("values", "secrets", "message"),
    [
        ({}, {"client_secret": "secret"}, "client_id is required"),
        ({"client_id": "   "}, {"client_secret": "secret"}, "client_id is required"),
        ({"client_id": "ab"}, {"client_secret": "secret"}, "client_id is too short"),
        ({"client_id": "x" * 13}, {"client_secret": "secret"}, "client_id is too long"),
        ({"client_id": "abc"}, {}, "client_secret is required"),
        ({"client_id": "abc"}, {"client_secret": "abc"}, "client_secret is too short"),
        ({"client_id": "abc"}, {"client_secret": "x" * 17}, "client_secret is too long"),
    ],
)
def test_server_enforces_manifest_required_and_length_bounds_before_secret_write(
    values, secrets, message
) -> None:
    from core.application.extensions.oauth_configuration import OAuthConfigurationSave

    secret_store = MemorySecrets()
    service = _service(secrets=secret_store)
    _raises_input_337_1 = _scope()
    _raises_input_337_2 = OAuthConfigurationSave(
        display_name="Bypassed browser", values=values, secret_replacements=secrets
    )
    with pytest.raises(ServiceValidationError, match=message):
        service.save_configuration(
            _raises_input_337_1, "install-1", "example", _raises_input_337_2, expected_revision=None
        )
    assert secret_store.writes == []


def test_declared_https_origin_validation_rejects_service_bypass() -> None:
    from core.application.extensions.oauth_configuration import OAuthConfigurationSave

    service = _service()
    synthetic_client_secret = "client-secret"  # pragma: allowlist secret

    for origin in (
        "not-a-url",
        "http://identity.example.test",
        "http://localhost:3000",
        "https://user@identity.example.test",
        "https://identity.example.test/path",
        "https://identity.example.test?query=1",
        "https://identity.example.test#fragment",
        "https://[invalid",
        "https://identity.example.test:invalid",
        "https://identity.example.test:0",
        "https://identity.example.test:443",
        "https://identity.example.test/",
        "https://Identity.example.test",
        "https://identity..example.test",
    ):
        _raises_input_374_1 = _scope()
        _raises_input_374_2 = OAuthConfigurationSave(
            display_name="Browser bypass",
            values={"client_id": "client-id", "browser_origin": origin},
            secret_replacements={"client_secret": synthetic_client_secret},
        )
        with pytest.raises(ServiceValidationError) as exc_info:
            service.preview_configuration_change(
                _raises_input_374_1,
                "install-1",
                "example",
                _raises_input_374_2,
                expected_revision=None,
            )
        assert exc_info.value.payload == {
            "error": "invalid_credential_field",
            "field_id": "browser_origin",
            "reason": "invalid_format",
        }

    impact = service.preview_configuration_change(
        _scope(),
        "install-1",
        "example",
        OAuthConfigurationSave(
            display_name="Canonical origin",
            values={
                "client_id": "client-id",
                "browser_origin": "https://identity.example.test",
            },
            secret_replacements={"client_secret": synthetic_client_secret},
        ),
        expected_revision=None,
    )
    assert impact.changed_fields == ["browser_origin", "client_id", "client_secret"]


def test_blank_secret_edit_preserves_stored_value_and_explicit_replacement_writes() -> None:
    from core.application.extensions.oauth_configuration import OAuthConfigurationSave

    secrets = MemorySecrets()
    service = _service(secrets=secrets)
    created = _create(service)
    initial_writes = list(secrets.writes)

    cosmetic = service.save_configuration(
        _scope(),
        "install-1",
        "example",
        OAuthConfigurationSave(
            configuration_id=created.configuration_id,
            display_name="Renamed",
            values={"note": "changed"},
            secret_replacements={"client_secret": ""},
        ),
        expected_revision=created.revision,
    )
    assert secrets.writes == initial_writes
    assert cosmetic.status == "ready_for_authorization"

    replaced = service.save_configuration(
        _scope(),
        "install-1",
        "example",
        OAuthConfigurationSave(
            configuration_id=created.configuration_id,
            display_name="Renamed",
            values={},
            secret_replacements={"client_secret": "new-secret"},
        ),
        expected_revision=cosmetic.revision,
    )
    assert secrets.writes[-1][2] == "new-secret"
    assert replaced.authorization_revision == cosmetic.authorization_revision


def test_reauthorization_change_requires_revision_bound_confirmation_and_marks_only_refs() -> None:
    from core.application.extensions.oauth_configuration import OAuthConfigurationSave

    repo = MemoryRepository()
    service = _service(repo=repo)
    created = _create(service)
    repo.connections.extend(
        [
            {
                "connection_id": "owned",
                "account_id": "acct-1",
                "project_id": "project-1",
                "extension_install_id": "install-1",
                "extension_id": "fixture.oauth",
                "oauth_provider_id": "example",
                "oauth_configuration_id": created.configuration_id,
                "display_name": "Owned",
                "connection_type": "mail",
            },
            {
                "connection_id": "foreign",
                "account_id": "acct-2",
                "project_id": "project-2",
                "extension_install_id": "install-1",
                "extension_id": "fixture.oauth",
                "oauth_provider_id": "example",
                "oauth_configuration_id": created.configuration_id,
            },
        ]
    )
    command = OAuthConfigurationSave(
        configuration_id=created.configuration_id,
        display_name="Primary",
        values={"client_id": "changed"},
    )
    impact = service.preview_configuration_change(
        _scope(), "install-1", "example", command, expected_revision=created.revision
    )
    assert impact.requires_confirmation is True
    assert [row.connection_id for row in impact.connections] == ["owned"]

    _raises_input_489_1 = _scope()
    with pytest.raises(ServicePreconditionError):
        service.save_configuration(
            _raises_input_489_1, "install-1", "example", command, expected_revision=created.revision
        )
    saved = service.save_configuration(
        _scope(),
        "install-1",
        "example",
        command,
        expected_revision=created.revision,
        confirmation_token=impact.confirmation_token,
    )
    assert repo.marked == ["owned"]
    assert repo.invalidated == [created.configuration_id]
    assert saved.status == "ready_for_authorization"

    _raises_input_505_1 = _scope()
    with pytest.raises((ServiceConflictError, ServicePreconditionError, ServiceValidationError)):
        service.save_configuration(
            _raises_input_505_1,
            "install-1",
            "example",
            command,
            expected_revision=created.revision,
            confirmation_token=impact.confirmation_token,
        )


@pytest.mark.parametrize(
    "lease",
    ["2099-01-01T00:00:00+00:00", "not-a-timestamp"],
)
def test_reauthorization_change_preserves_an_in_flight_disconnect(
    lease: str,
) -> None:
    from core.application.extensions.oauth_configuration import OAuthConfigurationSave

    repo = MemoryRepository()
    service = _service(repo=repo)
    created = _create(service)
    disconnecting = {
        "connection_id": "disconnecting",
        "account_id": "acct-1",
        "project_id": "project-1",
        "extension_install_id": "install-1",
        "extension_id": "fixture.oauth",
        "oauth_provider_id": "example",
        "oauth_configuration_id": created.configuration_id,
        "credential_revision": 4,
        "enabled": False,
        "oauth_granted_scopes": ["mail.read"],
        "secret_refs": {"refs": {"_oauth_refresh_token": "private-ref"}},
        "connection_config": {
            "oauth": {
                "lifecycle_status": "disconnecting",
                "disconnect_operation_id": "private-operation",
                "disconnect_lease_expires_at": lease,
            }
        },
    }
    repo.connections.append(disconnecting)
    before = deepcopy(disconnecting)
    command = OAuthConfigurationSave(
        configuration_id=created.configuration_id,
        display_name="Primary",
        values={"client_id": "changed"},
    )
    impact = service.preview_configuration_change(
        _scope(), "install-1", "example", command, expected_revision=created.revision
    )

    _raises_input_559_1 = _scope()
    with pytest.raises(ServicePreconditionError) as exc_info:
        service.save_configuration(
            _raises_input_559_1,
            "install-1",
            "example",
            command,
            expected_revision=created.revision,
            confirmation_token=impact.confirmation_token,
        )

    assert exc_info.value.payload == {
        "error": "oauth_configuration_disconnect_in_progress",
        "connection_ids": ["disconnecting"],
    }
    assert repo.connections[0] == before


@pytest.mark.parametrize(
    "lease",
    ["2099-01-01T00:00:00+00:00", "not-a-timestamp"],
)
def test_configuration_delete_rejects_and_preserves_an_in_flight_disconnect(
    lease: str,
) -> None:
    repo = MemoryRepository()
    secrets = MemorySecrets()
    service = _service(repo=repo, secrets=secrets)
    created = _create(service)
    disconnecting = {
        "connection_id": "disconnecting",
        "account_id": "acct-1",
        "project_id": "project-1",
        "extension_install_id": "install-1",
        "extension_id": "fixture.oauth",
        "oauth_provider_id": "example",
        "oauth_configuration_id": created.configuration_id,
        "credential_revision": 4,
        "enabled": False,
        "oauth_granted_scopes": ["mail.read"],
        "secret_refs": {"refs": {"_oauth_refresh_token": "private-ref"}},
        "connection_config": {
            "oauth": {
                "lifecycle_status": "disconnecting",
                "disconnect_operation_id": "private-operation",
                "disconnect_lease_expires_at": lease,
            }
        },
    }
    repo.connections.append(disconnecting)
    connection_before = deepcopy(disconnecting)
    configuration_before = deepcopy(repo.rows[created.configuration_id])
    secrets_before = deepcopy(secrets.values)
    impact = service.preview_configuration_delete(
        _scope(),
        "install-1",
        "example",
        created.configuration_id,
        expected_revision=created.revision,
    )

    _raises_input_619_1 = _scope()
    with pytest.raises(ServicePreconditionError) as exc_info:
        service.delete_configuration(
            _raises_input_619_1,
            "install-1",
            "example",
            created.configuration_id,
            expected_revision=created.revision,
            confirmation_token=impact.confirmation_token,
        )

    assert exc_info.value.payload == {
        "error": "oauth_configuration_disconnect_in_progress",
        "connection_ids": ["disconnecting"],
    }
    assert repo.connections[0] == connection_before
    assert repo.rows[created.configuration_id] == configuration_before
    assert secrets.values == secrets_before


def test_existing_reauthorization_change_requires_confirmation_even_without_grants() -> None:
    from core.application.extensions.oauth_configuration import OAuthConfigurationSave

    service = _service()
    created = _create(service)
    command = OAuthConfigurationSave(
        configuration_id=created.configuration_id,
        display_name="Primary",
        values={"client_id": "changed"},
    )
    impact = service.preview_configuration_change(
        _scope(), "install-1", "example", command, expected_revision=created.revision
    )
    assert impact.requires_confirmation is True
    assert impact.connections == []
    _raises_input_653_1 = _scope()
    with pytest.raises(ServicePreconditionError):
        service.save_configuration(
            _raises_input_653_1, "install-1", "example", command, expected_revision=created.revision
        )


def test_runtime_revalidate_preserves_verified_grants_and_invalidates_pending_states() -> None:
    from core.application.extensions.oauth_configuration import OAuthConfigurationSave

    repo = MemoryRepository()
    service = _service(repo=repo)
    created = _create(service)
    stored = repo.rows[str(created.configuration_id)]
    stored["status"] = "verified"
    original_authorization_revision = stored["authorization_revision"]
    repo.connections.append(
        {
            "connection_id": "grant-1",
            "account_id": "acct-1",
            "project_id": "project-1",
            "extension_install_id": "install-1",
            "extension_id": "fixture.oauth",
            "oauth_provider_id": "example",
            "oauth_configuration_id": created.configuration_id,
        }
    )
    saved = service.save_configuration(
        _scope(),
        "install-1",
        "example",
        OAuthConfigurationSave(
            configuration_id=created.configuration_id,
            display_name="Primary",
            secret_replacements={"client_secret": "rotated"},
        ),
        expected_revision=created.revision,
    )
    assert saved.status == "ready_for_authorization"
    assert saved.authorization_revision == original_authorization_revision
    assert repo.marked == []
    assert repo.connections[0]["connection_id"] == "grant-1"
    assert repo.invalidated == [created.configuration_id]


def test_viewer_can_list_but_cannot_preview_or_save() -> None:
    from core.application.extensions.oauth_configuration import OAuthConfigurationSave

    service = _service()
    _create(service)
    viewer_rows = service.list_configurations(_scope("viewer"), "install-1", "example")
    assert len(viewer_rows) == 1
    assert viewer_rows[0].fields == []
    operator_rows = service.list_configurations(_scope("operator"), "install-1", "example")
    assert {field.field_id for field in operator_rows[0].fields} == {
        "browser_origin",
        "client_id",
        "client_secret",
        "note",
    }
    command = OAuthConfigurationSave(display_name="Nope", values={"client_id": "abc"})
    _raises_input_713_1 = _scope("viewer")
    with pytest.raises(ServiceForbiddenError):
        service.preview_configuration_change(
            _raises_input_713_1, "install-1", "example", command, expected_revision=None
        )
    _raises_input_717_1 = _scope("viewer")
    with pytest.raises(ServiceForbiddenError):
        service.save_configuration(
            _raises_input_717_1, "install-1", "example", command, expected_revision=None
        )


def test_cross_scope_configuration_lookup_is_indistinguishable_from_missing() -> None:
    from core.application.extensions.oauth_configuration import OAuthConfigurationSave

    service = _service()
    created = _create(service)
    command = OAuthConfigurationSave(
        configuration_id=created.configuration_id,
        display_name="Primary",
        values={"note": "x"},
    )
    for scope in (
        _scope(account="acct-2"),
        _scope(project="project-2"),
        _scope(tenant="tenant-2"),
    ):
        with pytest.raises(ServiceNotFoundError, match="Configuration not found"):
            service.preview_configuration_change(
                scope, "install-1", "example", command, expected_revision=created.revision
            )


def test_stale_revision_and_bad_confirmation_never_write_secret_material() -> None:
    from core.application.extensions.oauth_configuration import OAuthConfigurationSave

    repo = MemoryRepository()
    secrets = MemorySecrets()
    service = _service(repo=repo, secrets=secrets)
    created = _create(service)
    repo.connections.append(
        {
            "connection_id": "owned",
            "account_id": "acct-1",
            "project_id": "project-1",
            "extension_install_id": "install-1",
            "extension_id": "fixture.oauth",
            "oauth_provider_id": "example",
            "oauth_configuration_id": created.configuration_id,
        }
    )
    baseline = list(secrets.writes)
    replacement = OAuthConfigurationSave(
        configuration_id=created.configuration_id,
        display_name="Primary",
        values={"client_id": "changed"},
        secret_replacements={"client_secret": "do-not-write"},
    )
    _raises_input_769_1 = _scope()
    with pytest.raises(ServiceConflictError):
        service.save_configuration(
            _raises_input_769_1, "install-1", "example", replacement, expected_revision=99
        )
    assert secrets.writes == baseline

    _raises_input_775_1 = _scope()
    with pytest.raises(ServicePreconditionError):
        service.save_configuration(
            _raises_input_775_1,
            "install-1",
            "example",
            replacement,
            expected_revision=created.revision,
            confirmation_token="bad-token",
        )
    assert secrets.writes == baseline


def test_omitted_non_secret_and_blank_secret_are_preserved_in_stored_row() -> None:
    from core.application.extensions.oauth_configuration import OAuthConfigurationSave

    repo = MemoryRepository()
    service = _service(repo=repo)
    created = _create(service)
    saved = service.save_configuration(
        _scope(),
        "install-1",
        "example",
        OAuthConfigurationSave(
            configuration_id=created.configuration_id,
            display_name="Only renamed",
            secret_replacements={"client_secret": ""},
        ),
        expected_revision=created.revision,
    )
    stored = repo.rows[created.configuration_id]
    assert stored["non_secret_config"] == {"client_id": "abc", "note": "first"}
    assert stored["secret_refs"]["refs"]["client_secret"].endswith(":client_secret")
    assert saved.authorization_revision == created.authorization_revision


def test_non_secret_values_are_trimmed_while_secret_bytes_are_preserved() -> None:
    from core.application.extensions.oauth_configuration import OAuthConfigurationSave

    repo = MemoryRepository()
    secrets = MemorySecrets()
    service = _service(repo=repo, secrets=secrets)
    created = service.save_configuration(
        _scope(),
        "install-1",
        "example",
        OAuthConfigurationSave(
            display_name="Normalized",
            values={"client_id": "  abc  ", "note": "  first  "},
            secret_replacements={"client_secret": "  padded  "},
        ),
        expected_revision=None,
    )

    assert repo.rows[created.configuration_id]["non_secret_config"] == {
        "client_id": "abc",
        "note": "first",
    }
    assert secrets.writes[-1][2] == "  padded  "


def test_delete_requires_bound_confirmation_for_references_and_removes_only_owned_secrets() -> None:
    repo = MemoryRepository()
    secrets = MemorySecrets()
    service = _service(repo=repo, secrets=secrets)
    created = _create(service)
    owned_token_ref = "project_connection:owned:_oauth_access_token"
    foreign_ref = "project_connection:foreign:_oauth_access_token"
    unrelated_ref = "unrelated:account-secret"
    secrets.values[("acct-1", owned_token_ref)] = "owned-token"
    secrets.values[("acct-1", foreign_ref)] = "foreign-token"
    secrets.values[("acct-1", unrelated_ref)] = "unrelated"
    repo.connections.extend(
        [
            {
                "connection_id": "owned",
                "account_id": "acct-1",
                "project_id": "project-1",
                "extension_install_id": "install-1",
                "extension_id": "fixture.oauth",
                "oauth_provider_id": "example",
                "oauth_configuration_id": created.configuration_id,
                "secret_refs": {
                    "refs": {
                        "_oauth_access_token": owned_token_ref,
                        "crafted": unrelated_ref,
                    }
                },
            },
            {
                "connection_id": "foreign",
                "account_id": "acct-1",
                "project_id": "project-1",
                "extension_install_id": "install-1",
                "extension_id": "fixture.oauth",
                "oauth_provider_id": "example",
                "oauth_configuration_id": str(UUID(int=0)),
                "secret_refs": {"refs": {"_oauth_access_token": foreign_ref}},
            },
        ]
    )
    impact = service.preview_configuration_delete(
        _scope(),
        "install-1",
        "example",
        created.configuration_id,
        expected_revision=created.revision,
    )
    assert impact.requires_confirmation is True
    _raises_input_883_1 = _scope()
    with pytest.raises(ServicePreconditionError):
        service.delete_configuration(
            _raises_input_883_1,
            "install-1",
            "example",
            created.configuration_id,
            expected_revision=created.revision,
        )
    service.delete_configuration(
        _scope(),
        "install-1",
        "example",
        created.configuration_id,
        expected_revision=created.revision,
        confirmation_token=impact.confirmation_token,
    )
    assert repo.locked == [
        created.configuration_id,
        created.configuration_id,
        created.configuration_id,
    ]
    assert created.configuration_id not in repo.rows
    assert repo.deleted_connections == ["owned"]
    assert ("acct-1", owned_token_ref) not in secrets.values
    assert ("acct-1", foreign_ref) in secrets.values
    assert ("acct-1", unrelated_ref) in secrets.values
    assert repo.invalidated == [created.configuration_id]


def test_explicit_delete_remains_available_for_a_stale_release_configuration() -> None:
    repo = MemoryRepository()
    secrets = MemorySecrets()
    service = _service(repo=repo, secrets=secrets)
    created = _create(service)
    repo.rows[created.configuration_id]["extension_release_id"] = "retired-release"

    service.delete_configuration(
        _scope(),
        "install-1",
        "example",
        created.configuration_id,
        expected_revision=created.revision,
    )

    assert created.configuration_id not in repo.rows
    assert not secrets.values


def test_configuration_delete_revokes_outside_transaction_and_keeps_local_cleanup_on_failure() -> (
    None
):
    from core.application.management.project_connection_secrets import connection_secret_ref_key

    repo = MemoryRepository()
    secrets = MemorySecrets()
    transaction_open = [False]
    events: list[str] = []
    audit_events: list[dict] = []

    @contextmanager
    def transaction():
        transaction_open[0] = True
        events.append("tx_begin")
        try:
            yield
        finally:
            transaction_open[0] = False
            events.append("tx_end")

    def snapshot(**_values):
        assert transaction_open[0] is True
        return {"access_token": "access-canary", "refresh_token": "refresh-canary"}

    def revoke(**values):
        assert transaction_open[0] is False
        events.append("remote_revoke")
        assert values["token"] == "refresh-canary"
        assert values["client_id"] == "abc"
        assert values["client_secret"] == "secret"
        raise RuntimeError("raw provider failure")

    service = _service(
        repo=repo,
        secrets=secrets,
        transaction_factory=transaction,
        token_snapshot=snapshot,
        configuration_secret_value=lambda **values: secrets.values[
            (values["account_id"], values["ref_key"])
        ],
        revoke_token=revoke,
        revocation_audit=lambda **values: audit_events.append(values),
    )
    created = _create(service)
    row = repo.rows[created.configuration_id]
    connection_id = "owned-revoke"
    repo.connections.append(
        {
            "connection_id": connection_id,
            "account_id": "acct-1",
            "project_id": "project-1",
            "extension_install_id": "install-1",
            "extension_id": "fixture.oauth",
            "connection_type": "example_mail",
            "oauth_configuration_id": created.configuration_id,
            "oauth_provider_id": "example",
            "oauth_provider_security_fingerprint": row["provider_security_fingerprint"],
            "oauth_provider_revocation_fingerprint": row["provider_revocation_fingerprint"],
            "secret_refs": {
                "refs": {
                    "_oauth_access_token": connection_secret_ref_key(
                        connection_id, "_oauth_access_token"
                    ),
                    "_oauth_refresh_token": connection_secret_ref_key(
                        connection_id, "_oauth_refresh_token"
                    ),
                }
            },
        }
    )
    impact = service.preview_configuration_delete(
        _scope(),
        "install-1",
        "example",
        created.configuration_id,
        expected_revision=created.revision,
    )

    service.delete_configuration(
        _scope(),
        "install-1",
        "example",
        created.configuration_id,
        expected_revision=created.revision,
        confirmation_token=impact.confirmation_token,
    )

    assert events[-5:] == ["tx_begin", "tx_end", "remote_revoke", "tx_begin", "tx_end"]
    assert created.configuration_id not in repo.rows
    assert repo.connections == []
    assert audit_events == [
        {
            "extension_id": "fixture.oauth",
            "account_id": "acct-1",
            "project_id": "project-1",
            "event": "configuration_delete",
            "outcome": "failed",
            "resource_id": created.configuration_id,
        }
    ]
    assert "canary" not in repr(audit_events)


def test_invalid_client_marks_only_selected_configuration() -> None:
    repo = MemoryRepository()
    service = _service(repo=repo)
    first = _create(service, "Primary")
    second = _create(service, "Backup")

    changed = service.mark_invalid_client(_scope(), "install-1", "example", first.configuration_id)
    assert changed.status == "needs_attention"
    untouched = next(
        row
        for row in service.list_configurations(_scope(), "install-1", "example")
        if row.configuration_id == second.configuration_id
    )
    assert untouched.status == "ready_for_authorization"


def test_optional_non_secret_with_minimum_may_be_cleared_or_omitted() -> None:
    from core.application.extensions.oauth_configuration import OAuthConfigurationSave

    repo = MemoryRepository()
    service = _service(repo=repo)
    created = _create(service)
    saved = service.save_configuration(
        _scope(),
        "install-1",
        "example",
        OAuthConfigurationSave(
            configuration_id=created.configuration_id,
            display_name="Primary",
            values={"note": ""},
        ),
        expected_revision=created.revision,
    )
    assert repo.rows[created.configuration_id]["non_secret_config"]["note"] == ""
    assert saved.revision == created.revision + 1


def test_confirmation_is_bound_to_exact_secret_replacement_without_exposing_it() -> None:
    from core.application.extensions.oauth_configuration import OAuthConfigurationSave

    repo = MemoryRepository()
    secrets = MemorySecrets()
    service = _service(repo=repo, secrets=secrets)
    created = _create(service)
    repo.connections.append(
        {
            "connection_id": "owned",
            "account_id": "acct-1",
            "project_id": "project-1",
            "extension_install_id": "install-1",
            "extension_id": "fixture.oauth",
            "oauth_provider_id": "example",
            "oauth_configuration_id": created.configuration_id,
        }
    )
    command = OAuthConfigurationSave(
        configuration_id=created.configuration_id,
        display_name="Primary",
        values={"client_id": "changed"},
        secret_replacements={"client_secret": "secret-one"},
    )
    impact = service.preview_configuration_change(
        _scope(), "install-1", "example", command, expected_revision=created.revision
    )
    assert "secret-one" not in str(impact.confirmation_token)
    altered = command.model_copy(update={"secret_replacements": {"client_secret": "secret-two"}})
    _raises_input_1101_1 = _scope()
    with pytest.raises(ServicePreconditionError):
        service.save_configuration(
            _raises_input_1101_1,
            "install-1",
            "example",
            altered,
            expected_revision=created.revision,
            confirmation_token=impact.confirmation_token,
        )
    assert all(value != "secret-two" for _account, _ref, value in secrets.writes)


def test_nonblank_secret_replacement_is_stored_byte_for_byte() -> None:
    from core.application.extensions.oauth_configuration import OAuthConfigurationSave

    secrets = MemorySecrets()
    service = _service(secrets=secrets)
    created = _create(service)
    service.save_configuration(
        _scope(),
        "install-1",
        "example",
        OAuthConfigurationSave(
            configuration_id=created.configuration_id,
            display_name="Primary",
            secret_replacements={"client_secret": "  padded  "},
        ),
        expected_revision=created.revision,
    )
    assert secrets.writes[-1][2] == "  padded  "


def test_repository_conflict_is_detected_before_replacement_secret_write() -> None:
    from core.application.extensions.oauth_configuration import OAuthConfigurationSave

    class ConflictRepository(MemoryRepository):
        def update_configuration(self, **payload):
            return None

    repo = ConflictRepository()
    secrets = MemorySecrets()
    service = _service(repo=repo, secrets=secrets)
    created = _create(service)
    baseline = list(secrets.writes)
    _raises_input_1145_1 = _scope()
    _raises_input_1145_2 = OAuthConfigurationSave(
        configuration_id=created.configuration_id,
        display_name="Primary",
        secret_replacements={"client_secret": "replacement"},
    )
    with pytest.raises(ServiceConflictError):
        service.save_configuration(
            _raises_input_1145_1,
            "install-1",
            "example",
            _raises_input_1145_2,
            expected_revision=created.revision,
        )
    assert secrets.writes == baseline


@pytest.mark.parametrize(
    ("column", "drifted"),
    [
        ("extension_release_id", "release-2"),
        ("provider_security_fingerprint", "drifted-security"),
        ("provider_revocation_fingerprint", "drifted-revocation"),
    ],
)
def test_ordinary_save_cannot_bypass_release_impact(column: str, drifted: str) -> None:
    from core.application.extensions.oauth_configuration import OAuthConfigurationSave

    repo = MemoryRepository()
    secrets = MemorySecrets()
    service = _service(repo=repo, secrets=secrets)
    created = _create(service)
    repo.rows[created.configuration_id][column] = drifted
    before = deepcopy(repo.rows)
    writes = list(secrets.writes)
    command = OAuthConfigurationSave(
        configuration_id=created.configuration_id,
        display_name="Cosmetic rename",
        values={"note": "changed"},
        secret_replacements={"client_secret": "must-not-write"},
    )
    for operation in (
        lambda: service.preview_configuration_change(
            _scope(), "install-1", "example", command, expected_revision=created.revision
        ),
        lambda: service.save_configuration(
            _scope(), "install-1", "example", command, expected_revision=created.revision
        ),
    ):
        with pytest.raises(ServicePreconditionError) as exc_info:
            operation()
        assert exc_info.value.payload == {"error": "oauth_release_transition_required"}
    assert repo.rows == before
    assert secrets.writes == writes
    assert repo.invalidated == []
    assert repo.marked == []


def test_mutation_locks_configuration_before_reading_confirmed_connection_set() -> None:
    from core.application.extensions.oauth_configuration import OAuthConfigurationSave

    repo = MemoryRepository()
    service = _service(repo=repo)
    created = _create(service)
    service.save_configuration(
        _scope(),
        "install-1",
        "example",
        OAuthConfigurationSave(
            configuration_id=created.configuration_id,
            display_name="Renamed",
        ),
        expected_revision=created.revision,
    )
    assert repo.locked == [created.configuration_id]


def test_transaction_rolls_back_row_and_secret_when_late_secret_write_fails() -> None:
    from core.application.extensions.oauth_configuration import OAuthConfigurationSave

    repo = MemoryRepository()

    class FailingSecrets(MemorySecrets):
        def write(self, *, account_id: str, ref_key: str, value: str) -> None:
            super().write(account_id=account_id, ref_key=ref_key, value=value)
            if value == "replacement":
                raise RuntimeError("simulated secret store failure")

    secrets = FailingSecrets()

    @contextmanager
    def transaction():
        row_snapshot = deepcopy(repo.rows)
        connection_snapshot = deepcopy(repo.connections)
        value_snapshot = deepcopy(secrets.values)
        try:
            yield
        except Exception:
            repo.rows = row_snapshot
            repo.connections = connection_snapshot
            secrets.values = value_snapshot
            raise

    service = _service(repo=repo, secrets=secrets, transaction_factory=transaction)
    created = _create(service)
    before = deepcopy(repo.rows[created.configuration_id])
    _raises_input_1249_1 = _scope()
    _raises_input_1249_2 = OAuthConfigurationSave(
        configuration_id=created.configuration_id,
        display_name="Changed",
        secret_replacements={"client_secret": "replacement"},
    )
    with pytest.raises(RuntimeError, match="simulated secret store failure"):
        service.save_configuration(
            _raises_input_1249_1,
            "install-1",
            "example",
            _raises_input_1249_2,
            expected_revision=created.revision,
        )
    assert repo.rows[created.configuration_id] == before
    assert all(value != "replacement" for value in secrets.values.values())


def _stranded_on_an_earlier_release(repo, configuration_id: str) -> None:
    """The state an extension update that did not move OAuth left behind."""
    repo.rows[configuration_id]["extension_release_id"] = "release-0"
    repo.connections.append(
        {
            "connection_id": "conn-shop",
            "oauth_configuration_id": configuration_id,
            "account_id": "acct-1",
            "project_id": "project-1",
            "extension_install_id": "install-1",
            "extension_id": "fixture.oauth",
            "oauth_provider_id": "example",
            "oauth_extension_release_id": "release-0",
        }
    )


def test_reconcile_moves_a_configuration_and_its_connections_to_the_installed_release() -> None:
    """Deleting the configuration was the only way out, and it orphaned workflow steps."""
    repo = MemoryRepository()
    service = _service(repo)
    created = _create(service)
    _stranded_on_an_earlier_release(repo, created.configuration_id)

    view = service.reconcile_release(_scope(), "install-1", "example", created.configuration_id)

    assert repo.rows[created.configuration_id]["extension_release_id"] == "release-1"
    assert view.revision == created.revision + 1
    assert [
        (row["connection_id"], row["oauth_extension_release_id"]) for row in repo.connections
    ] == [("conn-shop", "release-1")]
    assert repo.rows[created.configuration_id]["secret_refs"]


def test_reconcile_refuses_a_release_that_changed_how_the_provider_signs_in() -> None:
    from core.application.management.errors import ServicePreconditionError

    repo = MemoryRepository()
    service = _service(repo)
    created = _create(service)
    _stranded_on_an_earlier_release(repo, created.configuration_id)
    repo.rows[created.configuration_id]["provider_security_fingerprint"] = "earlier-sign-in"
    operator = _scope()

    with pytest.raises(ServicePreconditionError) as raised:
        service.reconcile_release(operator, "install-1", "example", created.configuration_id)

    assert raised.value.payload["error"] == "oauth_release_reapproval_required"
    assert repo.rows[created.configuration_id]["extension_release_id"] == "release-0"
    assert repo.connections[0]["oauth_extension_release_id"] == "release-0"


def test_reconcile_needs_an_operator() -> None:
    repo = MemoryRepository()
    service = _service(repo)
    created = _create(service)

    viewer = _scope("viewer")

    with pytest.raises(ServiceForbiddenError):
        service.reconcile_release(viewer, "install-1", "example", created.configuration_id)


def test_delete_preview_names_the_workflow_steps_bound_to_its_connections() -> None:
    repo = MemoryRepository()
    service = _service(repo)
    created = _create(service)
    repo.connections.append(
        {
            "connection_id": "conn-shop",
            "display_name": "shopify",
            "oauth_configuration_id": created.configuration_id,
            "account_id": "acct-1",
            "project_id": "project-1",
            "extension_install_id": "install-1",
            "extension_id": "fixture.oauth",
            "oauth_provider_id": "example",
        }
    )
    asked: list[dict] = []

    def bound_steps(**values):
        asked.append(values)
        return [
            {
                "workflow_id": "wine_shopify",
                "workflow_name": "wine shopify",
                "group_name": "Export Shopify products",
                "step_id": "export_shopify_products",
                "step_name": "Export Shopify products",
                "connection_id": "conn-shop",
            }
        ]

    repo.list_workflow_steps_bound_to_connections = bound_steps

    impact = service.preview_configuration_delete(
        _scope(),
        "install-1",
        "example",
        created.configuration_id,
        expected_revision=created.revision,
    )

    assert asked == [{"account_id": "acct-1", "connection_ids": ["conn-shop"]}]
    assert [(step.workflow_name, step.step_name) for step in impact.workflow_steps] == [
        ("wine shopify", "Export Shopify products")
    ]
