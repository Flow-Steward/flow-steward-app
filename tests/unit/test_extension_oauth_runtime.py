from __future__ import annotations

import copy
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
import yaml

from core.application.extensions.oauth_contract import (
    compile_oauth_providers,
    provider_revocation_fingerprint,
    provider_security_fingerprint,
)
from core.application.extensions.oauth_runtime import (
    OAuthRuntimeTokenError,
    OAuthRuntimeTokenService,
)


def _manifest() -> dict[str, Any]:
    manifest = yaml.safe_load(
        Path("tests/fixtures/extensions/fixture_extension_oauth/extension.yaml").read_text(
            encoding="utf-8"
        )
    )
    manifest["action_definitions"] = [
        {"action_id": "verify_identity", "handler": {"mode": "extension"}}
    ]
    return manifest


class _Repository:
    def __init__(self) -> None:
        self.manifest = _manifest()
        self.release_id = "release-a"
        provider = compile_oauth_providers(self.manifest)[0]
        self.configuration = {
            "configuration_id": "configuration-a",
            "tenant_id": "tenant-a",
            "account_id": "account-a",
            "project_id": "project-a",
            "extension_install_id": "install-a",
            "extension_id": "fixture.extension-oauth",
            "extension_release_id": "release-a",
            "provider_id": "example",
            "configuration_revision": 3,
            "authorization_revision": 2,
            "status": "verified",
            "provider_security_fingerprint": provider_security_fingerprint(provider),
            "provider_revocation_fingerprint": provider_revocation_fingerprint(provider),
        }
        self.connection = {
            "connection_id": "connection-a",
            "tenant_id": "tenant-a",
            "account_id": "account-a",
            "project_id": "project-a",
            "extension_install_id": "install-a",
            "extension_id": "fixture.extension-oauth",
            "connection_type": "example_mail",
            "credential_revision": 7,
            "enabled": True,
            "oauth_configuration_id": "configuration-a",
            "oauth_provider_id": "example",
            "oauth_extension_release_id": "release-a",
            "oauth_authorization_revision": 2,
            "oauth_granted_scopes": ["mail.read", "mail.write"],
            "oauth_provider_security_fingerprint": provider_security_fingerprint(provider),
            "oauth_provider_revocation_fingerprint": provider_revocation_fingerprint(provider),
            "connection_config": {"oauth": {"lifecycle_status": "connected"}},
            "secret_refs": {
                "refs": {
                    "_oauth_access_token": "project_connection:connection-a:_oauth_access_token"
                }
            },
        }

    @contextmanager
    def oauth_refresh_claim(self, **scope: Any):
        assert scope["connection_id"] == "connection-a"
        yield

    def lock_oauth_install_for_update(self, **_scope: Any) -> dict[str, Any]:
        return {
            "install_id": "install-a",
            "account_id": "account-a",
            "extension_id": "fixture.extension-oauth",
            "release_id": self.release_id,
            "state": "active",
        }

    def get_configuration_for_scope_for_update(self, **_scope: Any) -> dict[str, Any]:
        return copy.deepcopy(self.configuration)

    def lock_connection(self, **_scope: Any) -> dict[str, Any]:
        return copy.deepcopy(self.connection)


class _TokenStore:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def resolve_access_token(self, **values: Any) -> str:
        self.calls.append(copy.deepcopy(values))
        return "runtime-access-token"


class _SnapshotTokenStore(_TokenStore):
    def __init__(self, *, expires_at: float | None) -> None:
        super().__init__()
        self.expires_at = expires_at

    def resolve_token_snapshot(self, **values: Any) -> dict[str, Any]:
        self.calls.append(copy.deepcopy(values))
        return {
            "access_token": "runtime-access-token",
            "expires_at": self.expires_at,
        }


def _service(repository: _Repository, token_store: _TokenStore | None = None):
    token_store = token_store or _TokenStore()

    @contextmanager
    def transaction():
        yield

    service = OAuthRuntimeTokenService(
        repository=repository,
        token_store=token_store,
        install_loader=lambda _install, _account: {
            "extension_id": "fixture.extension-oauth",
            "release_id": repository.release_id,
            "enabled": True,
            "compiled_manifest": repository.manifest,
        },
        transaction_factory=transaction,
        now=lambda: 1000.0,
    )
    return service, token_store


def _resolve(service: OAuthRuntimeTokenService):
    return service.resolve_runtime_access_token(
        tenant_id="tenant-a",
        account_id="account-a",
        project_id="project-a",
        extension_install_id="install-a",
        extension_id="fixture.extension-oauth",
        provider_id="example",
        configuration_id="configuration-a",
        connection_id="connection-a",
        connection_type="example_mail",
    )


def test_runtime_projection_returns_only_exact_connection_access_token() -> None:
    repository = _Repository()
    service, token_store = _service(repository)

    assert _resolve(service) == {"access_token": "runtime-access-token"}
    assert token_store.calls == [
        {
            "account_id": "account-a",
            "secret_refs": {
                "refs": {
                    "_oauth_access_token": "project_connection:connection-a:_oauth_access_token"
                }
            },
        }
    ]


@pytest.mark.parametrize(
    ("mutation", "expected_code"),
    [
        (("connection", "enabled", False), "oauth_runtime_blocked"),
        (("connection", "oauth_extension_release_id", "release-old"), "oauth_runtime_blocked"),
        (("connection", "oauth_authorization_revision", 1), "oauth_runtime_blocked"),
        (("connection", "oauth_provider_security_fingerprint", "stale"), "oauth_runtime_blocked"),
        (("connection", "oauth_granted_scopes", []), "oauth_scope_required"),
        (("lifecycle", "lifecycle_status", "needs_attention"), "oauth_needs_attention"),
        (
            ("lifecycle", "lifecycle_status", "authorization_required"),
            "oauth_authorization_required",
        ),
        (("lifecycle", "lifecycle_status", "post_connect_pending"), "oauth_runtime_blocked"),
        (
            (
                "connection",
                "secret_refs",
                {
                    "refs": {
                        "_oauth_access_token": (
                            "project_connection:connection-foreign:_oauth_access_token"
                        )
                    }
                },
            ),
            "oauth_runtime_blocked",
        ),
    ],
)
def test_runtime_projection_fails_closed_before_decrypt(mutation, expected_code) -> None:
    repository = _Repository()
    owner, field, value = mutation
    if owner == "lifecycle":
        repository.connection["connection_config"]["oauth"][field] = value
    else:
        getattr(repository, owner)[field] = value
    service, token_store = _service(repository)

    with pytest.raises(OAuthRuntimeTokenError) as exc_info:
        _resolve(service)

    assert exc_info.value.code == expected_code
    assert token_store.calls == []


def test_ready_configuration_requires_provider_revalidation_before_runtime_use() -> None:
    repository = _Repository()
    repository.configuration["status"] = "ready_for_authorization"
    service, token_store = _service(repository)

    with pytest.raises(OAuthRuntimeTokenError) as exc_info:
        _resolve(service)

    assert exc_info.value.code == "oauth_authorization_required"
    assert len(token_store.calls) == 2


def test_needs_attention_configuration_keeps_unexpired_connected_grant_usable() -> None:
    repository = _Repository()
    repository.configuration["status"] = "needs_attention"
    token_store = _SnapshotTokenStore(expires_at=1060.000001)
    service, _ = _service(repository, token_store)

    assert _resolve(service) == {"access_token": "runtime-access-token"}
    assert len(token_store.calls) == 1


@pytest.mark.parametrize(
    "expires_at",
    [None, 1000.0, 1059.0, 1060.0, float("nan"), float("inf"), float("-inf")],
)
def test_needs_attention_configuration_denies_unknown_or_refresh_needed_token(
    expires_at: float | None,
) -> None:
    repository = _Repository()
    repository.configuration["status"] = "needs_attention"
    token_store = _SnapshotTokenStore(expires_at=expires_at)
    service, _ = _service(repository, token_store)

    with pytest.raises(OAuthRuntimeTokenError) as exc_info:
        _resolve(service)

    assert exc_info.value.code == "oauth_needs_attention"
    assert len(token_store.calls) == 1


@pytest.mark.parametrize("expires_at", [float("nan"), float("inf"), float("-inf")])
def test_runtime_denies_non_finite_expiry(expires_at: float) -> None:
    repository = _Repository()
    token_store = _SnapshotTokenStore(expires_at=expires_at)
    service, _ = _service(repository, token_store)

    with pytest.raises(OAuthRuntimeTokenError) as exc_info:
        _resolve(service)

    assert exc_info.value.code == "oauth_runtime_blocked"
    assert len(token_store.calls) == 1


def _set_release_upgrade_rows(
    repository: _Repository,
    manifest: dict[str, Any],
    *,
    affected: bool,
) -> None:
    provider = compile_oauth_providers(manifest)[0]
    repository.manifest = manifest
    repository.release_id = "release-b"
    repository.configuration.update(
        extension_release_id="release-b",
        provider_security_fingerprint=provider_security_fingerprint(provider),
        provider_revocation_fingerprint=provider_revocation_fingerprint(provider),
    )
    repository.connection.update(
        oauth_extension_release_id="release-b",
        oauth_provider_security_fingerprint=provider_security_fingerprint(provider),
        oauth_provider_revocation_fingerprint=provider_revocation_fingerprint(provider),
    )
    if affected:
        repository.connection["connection_config"]["oauth"]["lifecycle_status"] = (
            "authorization_required"
        )
        return


@pytest.mark.parametrize("change", ["cosmetic", "revocation", "required_satisfied"])
def test_release_upgrade_keeps_still_valid_grant_runtime_usable(change: str) -> None:
    repository = _Repository()
    manifest = copy.deepcopy(repository.manifest)
    provider = manifest["runtime"]["extension_contract_v2"]["oauth_providers"][0]
    if change == "cosmetic":
        provider["display_name"] = "Renamed provider"
    elif change == "revocation":
        provider["revocation_endpoint"]["url"] = "https://identity-v2.example.test/revoke"
    else:
        scopes = manifest["runtime"]["extension_contract_v2"]["connection_types"][0]["auth"][
            "scopes"
        ]
        scopes["required"].append("mail.write")
        scopes["optional"] = []
    _set_release_upgrade_rows(repository, manifest, affected=False)
    service, token_store = _service(repository)

    assert _resolve(service) == {"access_token": "runtime-access-token"}
    assert len(token_store.calls) == 1


@pytest.mark.parametrize("change", ["required_missing", "removed_granted"])
def test_release_upgrade_reports_actionable_authorization_for_affected_grant(change: str) -> None:
    repository = _Repository()
    manifest = copy.deepcopy(repository.manifest)
    scopes = manifest["runtime"]["extension_contract_v2"]["connection_types"][0]["auth"]["scopes"]
    if change == "required_missing":
        scopes["required"].append("mail.write")
        scopes["optional"] = []
        repository.connection["oauth_granted_scopes"] = ["mail.read"]
    else:
        scopes["optional"] = []
    _set_release_upgrade_rows(repository, manifest, affected=True)
    service, token_store = _service(repository)

    with pytest.raises(OAuthRuntimeTokenError) as exc_info:
        _resolve(service)

    assert exc_info.value.code == "oauth_authorization_required"
    assert token_store.calls == []
