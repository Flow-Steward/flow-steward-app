from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path

import pytest
import yaml

from core.application.extensions.oauth_connection_lifecycle import (
    OAuthConnectionLifecycleService,
)
from core.application.extensions.oauth_contract import (
    compile_oauth_providers,
    provider_revocation_fingerprint,
    provider_security_fingerprint,
)
from core.application.management.errors import ServiceConflictError, ServiceNotFoundError
from core.application.management.project_connection_secrets import connection_secret_ref_key
from core.application.management.scope import ScopeContext


def _manifest() -> dict:
    manifest = yaml.safe_load(
        Path("tests/fixtures/extensions/fixture_extension_oauth/extension.yaml").read_text(
            encoding="utf-8"
        )
    )
    manifest["action_definitions"] = [
        {"action_id": "verify_identity", "handler": {"mode": "extension"}}
    ]
    return manifest


def _scope(*, project_id: str = "project-1") -> ScopeContext:
    return ScopeContext(
        tenant_id="tenant-1",
        account_id="account-1",
        project_id=project_id,
        principal_type="operator",
    )


class _Repository:
    def __init__(self, row: dict) -> None:
        self.row = deepcopy(row)
        self.block_attempts = 0
        self.claim_attempts = 0
        self.claimed_operation_ids: list[str] = []
        self.disconnect_lease_expired = False
        self.disconnect_metadata_recoverable = False
        self.invalidated = 0

    def lock_connection_for_lifecycle(self, **scope):
        expected = {
            "connection_id": "connection-1",
            "account_id": "account-1",
            "project_id": "project-1",
            "extension_install_id": "install-1",
            "extension_id": "fixture.extension-oauth",
            "provider_id": "example",
            "configuration_id": "configuration-1",
        }
        if any(str(scope.get(key)) != value for key, value in expected.items()):
            return None
        return deepcopy(self.row)

    def block_connection_disconnect(self, **scope):
        self.block_attempts += 1
        lifecycle = self.row["connection_config"]["oauth"]["lifecycle_status"]
        if self.row["enabled"] is not True or lifecycle in {"disconnecting", "disconnected"}:
            return False
        if self.row["credential_revision"] != scope["expected_credential_revision"]:
            return False
        self.row["credential_revision"] += 1
        self.row["enabled"] = False
        self.row["connection_config"]["oauth"].update(
            {
                "lifecycle_status": "disconnecting",
                "disconnect_operation_id": scope["disconnect_operation_id"],
                "disconnect_started_at": "database-now",
                "disconnect_lease_expires_at": "database-lease-expiry",
            }
        )
        return True

    def claim_stale_connection_disconnect(self, **scope):
        self.claim_attempts += 1
        oauth = self.row["connection_config"]["oauth"]
        if (
            not (self.disconnect_lease_expired or self.disconnect_metadata_recoverable)
            or self.row["enabled"] is not False
            or oauth.get("lifecycle_status") != "disconnecting"
            or (oauth.get("disconnect_operation_id") or "")
            != scope["expected_disconnect_operation_id"]
            or self.row["credential_revision"] != scope["expected_credential_revision"]
        ):
            return False
        self.row["credential_revision"] += 1
        oauth.update(
            {
                "disconnect_operation_id": scope["disconnect_operation_id"],
                "disconnect_started_at": "database-recovery-now",
                "disconnect_lease_expires_at": "database-recovery-lease-expiry",
            }
        )
        self.claimed_operation_ids.append(scope["disconnect_operation_id"])
        self.disconnect_lease_expired = False
        return True

    def lock_connection_for_disconnect_finalize(self, **scope):
        return self.lock_connection_for_lifecycle(**scope)

    def invalidate_connection_lifecycle_states(self, **_scope):
        self.invalidated += 1
        return 1

    def finalize_connection_disconnect(self, **scope):
        oauth = self.row["connection_config"]["oauth"]
        if (
            self.row["credential_revision"] != scope["expected_credential_revision"]
            or oauth.get("disconnect_operation_id") != scope["expected_disconnect_operation_id"]
        ):
            return False
        self.row["enabled"] = True
        self.row["oauth_granted_scopes"] = []
        self.row["secret_refs"] = {}
        oauth["lifecycle_status"] = "disconnected"
        oauth.pop("disconnect_operation_id", None)
        oauth.pop("disconnect_started_at", None)
        oauth.pop("disconnect_lease_expires_at", None)
        return True


def _row() -> dict:
    provider = compile_oauth_providers(_manifest())[0]
    return {
        "connection_id": "connection-1",
        "account_id": "account-1",
        "project_id": "project-1",
        "extension_install_id": "install-1",
        "extension_id": "fixture.extension-oauth",
        "oauth_provider_id": "example",
        "oauth_configuration_id": "configuration-1",
        "oauth_extension_release_id": "release-1",
        "oauth_authorization_revision": 1,
        "oauth_provider_security_fingerprint": provider_security_fingerprint(provider),
        "oauth_provider_revocation_fingerprint": provider_revocation_fingerprint(provider),
        "credential_revision": 3,
        "enabled": True,
        "oauth_granted_scopes": ["mail.read"],
        "connection_config": {"oauth": {"lifecycle_status": "connected"}},
        "secret_refs": {
            "refs": {
                "_oauth_refresh_token": connection_secret_ref_key(
                    "connection-1", "_oauth_refresh_token"
                )
            }
        },
        "configuration_release_id": "release-1",
        "configuration_revision": 1,
        "configuration_authorization_revision": 1,
        "configuration_non_secret_config": {"client_id": "client-1"},
        "configuration_secret_refs": {
            "refs": {"client_secret": "extension_oauth_configuration:configuration-1:client_secret"}
        },
        "configuration_security_fingerprint": provider_security_fingerprint(provider),
        "configuration_revocation_fingerprint": provider_revocation_fingerprint(provider),
    }


def _service(
    repository: _Repository,
    events: list[str],
    *,
    install_loader=None,
    on_revoke=None,
) -> OAuthConnectionLifecycleService:
    @contextmanager
    def transaction():
        events.append("tx_begin")
        try:
            yield
        finally:
            events.append("tx_end")

    def revoke(**values):
        events.append("remote_revoke")
        assert values["token"] == "refresh-canary"
        if on_revoke is not None:
            on_revoke()
        raise RuntimeError("provider echoed refresh-canary")

    return OAuthConnectionLifecycleService(
        repository=repository,
        install_loader=install_loader
        or (
            lambda _install, _account: {
                "extension_id": "fixture.extension-oauth",
                "release_id": "release-1",
                "compiled_manifest": _manifest(),
            }
        ),
        token_snapshot=lambda **_values: {"refresh_token": "refresh-canary"},
        configuration_secret_value=lambda **_values: "client-secret-canary",
        delete_secret=lambda **values: events.append(f"delete:{values['ref_key']}"),
        revoke_token=revoke,
        transaction_factory=transaction,
    )


def test_disconnect_blocks_then_revokes_outside_transaction_and_always_cleans_local() -> None:
    repository = _Repository(_row())
    events: list[str] = []

    result = _service(repository, events).disconnect(
        _scope(), "install-1", "example", "configuration-1", "connection-1"
    )

    assert result == {
        "status": "disconnected",
        "connection_id": "connection-1",
        "remote_revocation": "failed",
    }
    assert events[:5] == [
        "tx_begin",
        "tx_end",
        "tx_begin",
        "tx_end",
        "remote_revoke",
    ]
    assert events[-1] == "tx_end"
    assert repository.invalidated == 1
    assert repository.row["secret_refs"] == {}
    assert repository.row["connection_config"]["oauth"]["lifecycle_status"] == "disconnected"


def test_disconnect_foreign_project_fails_closed_before_token_resolution() -> None:
    repository = _Repository(_row())
    events: list[str] = []

    _raises_callable_242_1 = _service(repository, events).disconnect
    _raises_input_242_2 = _scope(project_id="foreign-project")
    with pytest.raises(ServiceNotFoundError, match="Connection not found"):
        _raises_callable_242_1(
            _raises_input_242_2, "install-1", "example", "configuration-1", "connection-1"
        )

    assert repository.row["secret_refs"]
    assert "remote_revoke" not in events


@pytest.mark.parametrize(
    ("lifecycle_status", "enabled"),
    [("disconnecting", True), ("disconnected", True), ("connected", False)],
)
def test_inconsistent_or_disconnected_state_fails_before_block_or_side_effects(
    lifecycle_status: str, enabled: bool
) -> None:
    row = _row()
    row["enabled"] = enabled
    row["connection_config"]["oauth"]["lifecycle_status"] = lifecycle_status
    repository = _Repository(row)
    before = deepcopy(repository.row)
    events: list[str] = []

    _raises_callable_269_1 = _service(repository, events).disconnect
    _raises_input_269_2 = _scope()
    with pytest.raises(ServiceConflictError, match="Connection cannot be disconnected"):
        _raises_callable_269_1(
            _raises_input_269_2, "install-1", "example", "configuration-1", "connection-1"
        )

    assert repository.block_attempts == 0
    assert repository.claim_attempts == 0
    assert repository.invalidated == 0
    assert repository.row == before
    assert "remote_revoke" not in events
    assert not any(event.startswith("delete:") for event in events)


def test_live_disconnect_lease_rejects_repeat_before_external_side_effects() -> None:
    row = _row()
    row["credential_revision"] += 1
    row["enabled"] = False
    row["connection_config"]["oauth"].update(
        {
            "lifecycle_status": "disconnecting",
            "disconnect_operation_id": "operation-live",
            "disconnect_started_at": "database-now",
            "disconnect_lease_expires_at": "database-future",
        }
    )
    repository = _Repository(row)
    before = deepcopy(repository.row)
    events: list[str] = []

    _raises_callable_298_1 = _service(repository, events).disconnect
    _raises_input_298_2 = _scope()
    with pytest.raises(ServiceConflictError, match="Disconnect already in progress"):
        _raises_callable_298_1(
            _raises_input_298_2, "install-1", "example", "configuration-1", "connection-1"
        )

    assert repository.block_attempts == 0
    assert repository.claim_attempts == 1
    assert repository.invalidated == 0
    assert repository.row == before
    assert "remote_revoke" not in events
    assert not any(event.startswith("delete:") for event in events)


def test_stale_disconnect_lease_is_claimed_and_finalized_without_blocking_again() -> None:
    row = _row()
    row["credential_revision"] += 1
    row["enabled"] = False
    row["connection_config"]["oauth"].update(
        {
            "lifecycle_status": "disconnecting",
            "disconnect_operation_id": "operation-stale",
            "disconnect_started_at": "database-past",
            "disconnect_lease_expires_at": "database-expired",
        }
    )
    repository = _Repository(row)
    repository.disconnect_lease_expired = True
    events: list[str] = []

    result = _service(repository, events).disconnect(
        _scope(), "install-1", "example", "configuration-1", "connection-1"
    )

    assert result == {
        "status": "disconnected",
        "connection_id": "connection-1",
        "remote_revocation": "failed",
    }
    assert repository.block_attempts == 0
    assert repository.claim_attempts == 1
    assert len(repository.claimed_operation_ids) == 1
    assert repository.claimed_operation_ids[0] != "operation-stale"
    assert repository.invalidated == 0
    assert repository.row["credential_revision"] == 5
    assert repository.row["enabled"] is True
    oauth = repository.row["connection_config"]["oauth"]
    assert oauth["lifecycle_status"] == "disconnected"
    assert (
        not {
            "disconnect_operation_id",
            "disconnect_started_at",
            "disconnect_lease_expires_at",
        }
        & oauth.keys()
    )
    assert repository.row["secret_refs"] == {}
    assert "remote_revoke" in events
    assert (
        events.count(f"delete:{connection_secret_ref_key('connection-1', '_oauth_refresh_token')}")
        == 1
    )


def test_stale_finalizer_cannot_delete_or_finalize_after_recovery_claim() -> None:
    repository = _Repository(_row())
    events: list[str] = []
    service = _service(repository, events)
    exact_scope, provider = service._resolve_scope(
        _scope(), "install-1", "example", "configuration-1", "connection-1"
    )
    stale_prepared = service._prepare_disconnect(exact_scope, provider)
    repository.disconnect_lease_expired = True
    recovered_prepared = service._prepare_disconnect(exact_scope, provider)
    refs_before = deepcopy(repository.row["secret_refs"])
    deletes_before = [event for event in events if event.startswith("delete:")]

    with pytest.raises(ServiceConflictError, match="Connection changed; retry disconnect"):
        service._finalize_disconnect(stale_prepared)

    assert repository.row["secret_refs"] == refs_before
    assert [event for event in events if event.startswith("delete:")] == deletes_before
    assert repository.row["connection_config"]["oauth"]["lifecycle_status"] == "disconnecting"
    service._finalize_disconnect(recovered_prepared)
    assert repository.row["connection_config"]["oauth"]["lifecycle_status"] == "disconnected"


@pytest.mark.parametrize(
    "corrupt_oauth",
    [
        {"disconnect_lease_expires_at": "database-expired"},
        {
            "disconnect_operation_id": "malformed operation id",
            "disconnect_lease_expires_at": "not-a-timestamp",
        },
    ],
)
def test_corrupt_disconnect_recovery_metadata_is_repaired_and_finalized(
    corrupt_oauth: dict[str, str],
) -> None:
    row = _row()
    row["credential_revision"] += 1
    row["enabled"] = False
    row["connection_config"]["oauth"] = {
        "lifecycle_status": "disconnecting",
        **corrupt_oauth,
    }
    repository = _Repository(row)
    repository.disconnect_metadata_recoverable = True
    events: list[str] = []

    result = _service(repository, events).disconnect(
        _scope(), "install-1", "example", "configuration-1", "connection-1"
    )

    assert result["status"] == "disconnected"
    assert repository.claim_attempts == 1
    assert repository.row["credential_revision"] == 5
    assert repository.row["enabled"] is True
    assert repository.row["connection_config"]["oauth"]["lifecycle_status"] == "disconnected"
    assert repository.row["secret_refs"] == {}
    assert "remote_revoke" in events


def test_disconnect_provider_disappearance_after_prepare_still_cleans_local() -> None:
    repository = _Repository(_row())
    events: list[str] = []
    loads = 0

    def install_loader(_install: str, _account: str):
        nonlocal loads
        loads += 1
        if loads == 1:
            return {
                "extension_id": "fixture.extension-oauth",
                "release_id": "release-1",
                "compiled_manifest": _manifest(),
            }
        return None

    result = _service(repository, events, install_loader=install_loader).disconnect(
        _scope(), "install-1", "example", "configuration-1", "connection-1"
    )

    assert result["remote_revocation"] == "not_available"
    assert "remote_revoke" not in events
    assert repository.row["secret_refs"] == {}
    assert repository.row["connection_config"]["oauth"]["lifecycle_status"] == "disconnected"


def test_disconnect_release_fingerprint_drift_during_revoke_still_cleans_local() -> None:
    repository = _Repository(_row())
    events: list[str] = []

    def drift_release_metadata() -> None:
        repository.row["oauth_extension_release_id"] = "release-2"
        repository.row["oauth_provider_security_fingerprint"] = "replacement-fingerprint"
        repository.row["oauth_provider_revocation_fingerprint"] = "replacement-revocation"

    result = _service(repository, events, on_revoke=drift_release_metadata).disconnect(
        _scope(), "install-1", "example", "configuration-1", "connection-1"
    )

    assert result["remote_revocation"] == "failed"
    assert repository.row["secret_refs"] == {}
    assert repository.row["connection_config"]["oauth"]["lifecycle_status"] == "disconnected"


def test_disconnect_of_a_connection_pinned_to_an_earlier_release_cleans_local() -> None:
    """After an extension update every disconnect answered "Connection not found".

    The connection still existed and held credentials; the only way out was to
    delete the whole OAuth configuration, which orphaned the workflow steps
    bound to it.
    """
    repository = _Repository(_row())
    events: list[str] = []

    def updated_install(_install: str, _account: str):
        return {
            "extension_id": "fixture.extension-oauth",
            "release_id": "release-2",
            "compiled_manifest": _manifest(),
        }

    result = _service(repository, events, install_loader=updated_install).disconnect(
        _scope(), "install-1", "example", "configuration-1", "connection-1"
    )

    assert result["status"] == "disconnected"
    assert result["remote_revocation"] == "not_available"
    assert "remote_revoke" not in events
    assert repository.row["secret_refs"] == {}
    assert repository.row["connection_config"]["oauth"]["lifecycle_status"] == "disconnected"
