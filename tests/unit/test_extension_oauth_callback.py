from __future__ import annotations

import copy
import hashlib
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
import yaml

from core.application.extensions.oauth_callback import (
    OAuthCallbackResult,
    OAuthCallbackService,
)
from core.application.extensions.oauth_contract import (
    compile_oauth_providers,
    provider_revocation_fingerprint,
    provider_security_fingerprint,
)
from core.infrastructure.extension_runtime.oauth_transport import (
    OAuthErrorCode,
    OAuthProviderError,
    OAuthProviderOperation,
)


def _manifest() -> dict[str, Any]:
    source = Path("tests/fixtures/extensions/fixture_extension_oauth/extension.yaml")
    manifest = yaml.safe_load(source.read_text(encoding="utf-8"))
    manifest["action_definitions"] = [
        {"action_id": "verify_identity", "handler": {"mode": "extension"}}
    ]
    manifest["runtime"]["extension_contract_v2"]["oauth_providers"][0]["error_help_anchors"][
        "provider_unavailable"
    ] = "provider-down"
    manifest["ui_manifest"] = {
        "pages": [
            {"page_id": "mail"},
            {
                "page_id": "oauth_help",
                "components": [{"type": "markdown", "anchor_id": "provider-down"}],
            },
        ]
    }
    return manifest


class _Repository:
    def __init__(self) -> None:
        provider = compile_oauth_providers(_manifest())[0]
        self.events: list[str] = []
        self.install = {
            "install_id": "install-a",
            "account_id": "account-a",
            "extension_id": "fixture.extension-oauth",
            "release_id": "release-a",
            "state": "active",
        }
        self.disable_after_grant_commit = False
        self.grant_written = False
        self.consumed = False
        self.state = {
            "state_hash": hashlib.sha256(b"opaque-state").hexdigest(),
            "tenant_id": "tenant-a",
            "account_id": "account-a",
            "project_id": "project-a",
            "extension_install_id": "install-a",
            "extension_id": "fixture.extension-oauth",
            "extension_release_id": "release-a",
            "provider_id": "example",
            "callback_id": "oauth_authorization",
            "configuration_id": "11111111-1111-4111-8111-111111111111",
            "configuration_revision": 3,
            "authorization_revision": 2,
            "connection_type": "example_mail",
            "existing_connection_id": None,
            "display_name": "Primary mailbox",
            "intent_id": "connect",
            "intent_payload": {},
            "requested_scopes": ["mail.read", "mail.write"],
            "redirect_uri": "https://app.example.test/callback/exact",
            "pkce_verifier_encrypted": "encrypted-verifier",
            "provider_security_fingerprint": provider_security_fingerprint(provider),
        }
        self.configuration = {
            "configuration_id": self.state["configuration_id"],
            "tenant_id": "tenant-a",
            "account_id": "account-a",
            "project_id": "project-a",
            "extension_install_id": "install-a",
            "extension_id": "fixture.extension-oauth",
            "extension_release_id": "release-a",
            "provider_id": "example",
            "configuration_revision": 3,
            "authorization_revision": 2,
            "status": "ready_for_authorization",
            "non_secret_config": {"client_id": "client-id"},
            "secret_refs": {
                "refs": {
                    "client_secret": (
                        "extension_oauth_configuration:"
                        "11111111-1111-4111-8111-111111111111:client_secret"
                    )
                }
            },
            "provider_security_fingerprint": provider_security_fingerprint(provider),
            "provider_revocation_fingerprint": provider_revocation_fingerprint(provider),
        }
        self.connection = {
            "connection_id": "connection-existing",
            "tenant_id": "tenant-a",
            "account_id": "account-a",
            "project_id": "project-a",
            "extension_install_id": "install-a",
            "extension_id": "fixture.extension-oauth",
            "oauth_provider_id": "example",
            "oauth_configuration_id": self.state["configuration_id"],
            "oauth_extension_release_id": "release-a",
            "oauth_authorization_revision": 1,
            "oauth_provider_security_fingerprint": self.state["provider_security_fingerprint"],
            "connection_type": "example_mail",
            "credential_revision": 7,
            "health_status": "healthy",
            "runtime_status": "ready",
            "last_seen_ok_at": 900.0,
            "last_error": "stale-error",
            "secret_refs": {
                "refs": {
                    "_oauth_refresh_token": (
                        "project_connection:connection-existing:_oauth_refresh_token"
                    ),
                    "_oauth_expires_at": (
                        "project_connection:connection-existing:_oauth_expires_at"
                    ),
                    "extension_secret": ("project_connection:connection-existing:extension_secret"),
                }
            },
            "connection_config": {
                "extension_metadata": {"canary": "preserve-top-level"},
                "oauth": {
                    "provider_subject": "preserve-oauth-metadata",
                    "lifecycle_status": "reconnect_required",
                },
            },
        }
        self.connections = {"connection-existing": self.connection}
        self.created: list[dict[str, Any]] = []
        self.updated: list[dict[str, Any]] = []
        self.post_connect_updates: list[dict[str, Any]] = []
        self.invalid_client_updates: list[dict[str, Any]] = []
        self.other_configuration = {
            **copy.deepcopy(self.configuration),
            "configuration_id": "22222222-2222-4222-8222-222222222222",
            "status": "verified",
        }

    def load_state_for_callback(self, **scope: Any) -> dict[str, Any] | None:
        """The same row the consume targets, read without claiming it."""
        self.events.append("load_state")
        if self.consumed or any(
            str(scope.get(k)) != str(v) for k, v in self._callback_state_scope().items()
        ):
            return None
        return copy.deepcopy(self.state)

    def _callback_state_scope(self) -> dict[str, Any]:
        return {
            "state_hash": self.state["state_hash"],
            "account_id": "account-a",
            "project_id": "project-a",
            "extension_id": "fixture.extension-oauth",
            "callback_id": "oauth_authorization",
        }

    def consume_state_for_callback(self, **scope: Any) -> dict[str, Any] | None:
        self.events.append("consume_state")
        expected = {
            "state_hash": self.state["state_hash"],
            "account_id": "account-a",
            "project_id": "project-a",
            "extension_id": "fixture.extension-oauth",
            "callback_id": "oauth_authorization",
        }
        if self.consumed or any(str(scope.get(k)) != str(v) for k, v in expected.items()):
            return None
        self.consumed = True
        return copy.deepcopy(self.state)

    def get_configuration_for_scope(self, **_scope: Any) -> dict[str, Any]:
        self.events.append("read_configuration")
        return copy.deepcopy(self.configuration)

    def get_configuration_for_scope_for_update(self, **_scope: Any) -> dict[str, Any]:
        self.events.append("lock_configuration")
        return copy.deepcopy(self.configuration)

    def lock_oauth_install_for_update(self, **_scope: Any) -> dict[str, Any]:
        self.events.append("lock_install")
        return copy.deepcopy(self.install)

    def lock_connection(self, **scope: Any) -> dict[str, Any]:
        self.events.append("lock_connection")
        if scope.get("connection_id") == "connection-new" and self.created:
            return {**copy.deepcopy(self.created[-1]), "credential_revision": 1}
        return copy.deepcopy(self.connections.get(str(scope.get("connection_id")), {}))

    def create_oauth_connection(self, **values: Any) -> dict[str, Any]:
        self.events.append("create_connection")
        self.grant_written = True
        self.created.append(copy.deepcopy(values))
        return copy.deepcopy(values)

    def update_oauth_connection(self, **values: Any) -> dict[str, Any] | None:
        self.events.append("update_connection")
        if values["expected_credential_revision"] != self.connection["credential_revision"]:
            return None
        self.updated.append(copy.deepcopy(values))
        self.grant_written = True
        self.connection.update(copy.deepcopy(values))
        self.connection["credential_revision"] = values["expected_credential_revision"] + 1
        return copy.deepcopy(self.connection)

    def mark_configuration_verified(self, **_scope: Any) -> bool:
        self.events.append("mark_configuration_verified")
        return True

    def mark_configuration_invalid_client(self, **scope: Any) -> dict[str, Any] | None:
        self.events.append("mark_configuration_invalid_client")
        self.invalid_client_updates.append(copy.deepcopy(scope))
        if (
            scope.get("configuration_id") != self.configuration["configuration_id"]
            or scope.get("expected_configuration_revision")
            != self.configuration["configuration_revision"]
        ):
            return None
        self.configuration["status"] = "needs_attention"
        self.configuration["last_error"] = {"code": "invalid_client"}
        self.configuration["configuration_revision"] += 1
        return copy.deepcopy(self.configuration)

    def update_oauth_post_connect(self, **values: Any) -> dict[str, Any]:
        self.events.append("update_post_connect")
        self.post_connect_updates.append(copy.deepcopy(values))
        if values["connection_id"] == "connection-new":
            if values["expected_credential_revision"] != 1:
                return {}
            return copy.deepcopy(values)
        connection = self.connections.get(values["connection_id"])
        if (
            not connection
            or values["expected_credential_revision"] != connection["credential_revision"]
        ):
            return {}
        connection["connection_config"] = copy.deepcopy(values["connection_config"])
        connection["credential_revision"] += 1
        return copy.deepcopy(connection)


class _TokenStore:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.writes: list[dict[str, Any]] = []

    def resolve_refresh_token(self, **_values: Any) -> str:
        self.events.append("resolve_refresh_token")
        return "old-refresh-token"

    def resolve_access_token(self, **_values: Any) -> str:
        self.events.append("resolve_access_token")
        return "committed-access-token"

    def write_tokens(self, **values: Any) -> dict[str, Any]:
        self.events.append("write_tokens")
        self.writes.append(copy.deepcopy(values))
        return {
            "refs": {"_oauth_access_token": "access-ref", "_oauth_refresh_token": "refresh-ref"}
        }


def _service(
    repository: _Repository,
    *,
    exchange=None,
    post_connect=None,
    runtime_resolver=None,
) -> OAuthCallbackService:
    events = repository.events
    token_store = _TokenStore(events)

    @contextmanager
    def transaction():
        events.append("tx_begin")
        try:
            yield
        except Exception:
            events.append("tx_rollback")
            raise
        else:
            events.append("tx_commit")
            if repository.disable_after_grant_commit and repository.grant_written:
                repository.disable_after_grant_commit = False
                repository.install["state"] = "disabled"
                events.append("disable_commit")

    install = {
        "extension_id": "fixture.extension-oauth",
        "release_id": "release-a",
        "enabled": True,
        "compiled_manifest": _manifest(),
    }

    def exchange_code(**values: Any) -> dict[str, Any]:
        events.append("exchange")
        assert values["code"] == "authorization-code"
        assert values["redirect_uri"] == repository.state["redirect_uri"]
        assert values["code_verifier"] == "decrypted-verifier"
        assert values["client_id"] == "client-id"
        assert values["client_secret"] == "client-secret"
        if exchange is not None:
            return exchange(**values)
        return {
            "access_token": "access-token-canary",
            "refresh_token": "refresh-token-canary",
            "token_type": "Bearer",
            "expires_in": 3600,
            "scope": "mail.write mail.read mail.write",
        }

    service = OAuthCallbackService(
        repository=repository,
        token_store=token_store,
        install_loader=lambda _install_id, _account_id: copy.deepcopy(install),
        configuration_secret_value=lambda **_values: "client-secret",
        decrypt_verifier=lambda _value: "decrypted-verifier",
        exchange_code=exchange_code,
        transaction_factory=transaction,
        public_base_url="https://app.example.test",
        invoke_post_connect=post_connect,
        resolve_runtime_access_token=(
            runtime_resolver
            or (
                lambda **_values: (
                    events.append("resolve_runtime_access_token")
                    or {"access_token": "committed-access-token"}
                )
            )
        ),
        connection_id_factory=lambda: "connection-new",
    )
    service.test_token_store = token_store
    return service


def _handle(service: OAuthCallbackService, **overrides: Any) -> OAuthCallbackResult:
    values = {
        "extension_id": "fixture.extension-oauth",
        "callback_id": "oauth_authorization",
        "registration": {"account_id": "account-a", "project_id": "project-a"},
        "callback_path": "/callback/exact",
        "opaque_state": "opaque-state",
        "code": "authorization-code",
        "provider_error": None,
    }
    values.update(overrides)
    return service.handle_authorization_callback(**values)


def test_success_consumes_in_short_transaction_then_exchanges_and_commits_atomically() -> None:
    repository = _Repository()
    service = _service(repository)
    result = _handle(service)

    assert result.outcome == "connected"
    assert result.connection_id == "connection-new"
    # The state is read first and spent last, in its own short transaction
    # immediately before the exchange: everything that can reject the callback
    # runs while the state is still available to the legitimate one.
    assert repository.events == [
        "tx_begin",
        "load_state",
        "tx_commit",
        "tx_begin",
        "read_configuration",
        "tx_commit",
        "tx_begin",
        "consume_state",
        "tx_commit",
        "exchange",
        "tx_begin",
        "lock_install",
        "lock_configuration",
        "write_tokens",
        "create_connection",
        "mark_configuration_verified",
        "tx_commit",
    ]
    created = repository.created[0]
    assert created["oauth_requested_scopes"] == ["mail.read", "mail.write"]
    assert created["oauth_granted_scopes"] == ["mail.read", "mail.write"]
    assert "access-token-canary" not in repr(created)
    assert created["connection_config"]["oauth"]["authorization_intent_id"] == "connect"
    assert created["health_status"] == "healthy"
    assert created["runtime_status"] == "ready"
    assert created["last_error"] == ""
    assert created["mark_last_seen_ok"] is True


def test_replay_and_cross_callback_stop_before_exchange_or_mutation() -> None:
    repository = _Repository()
    service = _service(repository)
    assert _handle(service).outcome == "connected"
    before = list(repository.events)
    replay = _handle(service)
    assert replay.outcome == "restart_required"
    # A replayed state is already spent, so the lookup finds nothing and the
    # replay costs a read rather than a write.
    assert repository.events[len(before) :] == ["tx_begin", "load_state", "tx_commit"]

    other = _Repository()
    result = _handle(_service(other), callback_id="foreign_callback")
    assert result.outcome == "restart_required"
    assert "exchange" not in other.events
    assert other.created == []


def test_provider_denial_consumes_state_and_returns_only_closed_help_target() -> None:
    repository = _Repository()
    result = _handle(
        _service(repository),
        code=None,
        provider_error="access_denied",
    )
    assert result.model_dump(exclude_none=True) == {
        "outcome": "authorization_denied",
        "help_target": {"page_id": "oauth_help"},
    }
    assert repository.events == [
        "tx_begin",
        "load_state",
        "tx_commit",
        "tx_begin",
        "read_configuration",
        "tx_commit",
        "tx_begin",
        "consume_state",
        "tx_commit",
        "tx_begin",
        "tx_commit",
    ]
    assert "access_denied" not in repr(result)


def test_callback_carries_only_a_current_declared_return_page() -> None:
    repository = _Repository()
    repository.state["intent_payload"] = {"return_page_id": "mail"}

    result = _handle(_service(repository))

    assert result.outcome == "connected"
    assert result.return_page_id == "mail"
    assert result.connection_id == "connection-new"

    tampered = _Repository()
    tampered.state["intent_payload"] = {"return_page_id": "undeclared"}
    assert _handle(_service(tampered)).return_page_id is None


def test_provider_error_uses_return_page_when_no_help_target_applies() -> None:
    repository = _Repository()
    repository.state["intent_payload"] = {"return_page_id": "mail"}

    result = _handle(
        _service(repository),
        code=None,
        provider_error="temporarily_unavailable",
    )

    assert result.model_dump(exclude_none=True) == {
        "outcome": "provider_unavailable",
        "help_target": {"page_id": "oauth_help", "anchor": "provider-down"},
        "return_page_id": "mail",
    }


def test_provider_errors_map_to_closed_safe_outcomes_after_consuming_state() -> None:
    cases = {
        "server_error": "provider_unavailable",
        "temporarily_unavailable": "provider_unavailable",
        "authorization_required": "restart_required",
        "interaction_required": "restart_required",
        "login_required": "restart_required",
        "consent_required": "restart_required",
        "invalid_scope": "restart_required",
        "unknown-provider-canary": "restart_required",
    }
    for provider_error, expected_outcome in cases.items():
        repository = _Repository()

        result = _handle(
            _service(repository),
            code=None,
            provider_error=provider_error,
        )

        expected_help = {"page_id": "oauth_help"}
        if expected_outcome == "provider_unavailable":
            expected_help["anchor"] = "provider-down"
        assert result.model_dump(exclude_none=True) == {
            "outcome": expected_outcome,
            "help_target": expected_help,
        }
        assert repository.consumed is True
        assert "exchange" not in repository.events
        assert provider_error not in repr(result)


def test_callback_path_must_match_consumed_state_before_denial_or_exchange() -> None:
    repository = _Repository()
    result = _handle(
        _service(repository),
        callback_path="/webhook/extensions/fixture.extension-oauth/oauth_authorization/other",
    )
    assert result.outcome == "restart_required"
    assert repository.consumed is True
    assert "exchange" not in repository.events
    assert repository.created == []


def test_callback_origin_must_match_canonical_public_base_before_exchange() -> None:
    repository = _Repository()
    repository.state["redirect_uri"] = "https://attacker.example.test/callback/exact"

    result = _handle(_service(repository))

    assert result.outcome == "restart_required"
    assert repository.consumed is True
    assert "exchange" not in repository.events
    assert repository.created == []


def test_exchange_failure_leaves_consumed_state_and_zero_token_or_connection_mutation() -> None:
    repository = _Repository()

    def fail(**_values: Any):
        raise RuntimeError("raw-provider-body-canary")

    result = _handle(_service(repository, exchange=fail))
    assert result.outcome == "provider_unavailable"
    assert repository.consumed is True
    assert repository.created == []
    assert "write_tokens" not in repository.events
    assert "raw-provider-body-canary" not in repr(result)


@pytest.mark.parametrize("existing_connection", [False, True])
def test_insufficient_exchange_scopes_fail_before_initial_or_reconnect_token_commit(
    existing_connection: bool,
) -> None:
    repository = _Repository()
    connection_before = copy.deepcopy(repository.connection)
    if existing_connection:
        repository.state["existing_connection_id"] = "connection-existing"
        repository.state["intent_payload"] = {"connection_credential_revision": 7}

    def narrow_scope(**_values: Any) -> dict[str, Any]:
        return {
            "access_token": "must-not-persist",
            "refresh_token": "must-not-persist-refresh",
            "token_type": "Bearer",
            "scope": "profile.read",
        }

    service = _service(repository, exchange=narrow_scope)
    result = _handle(service)

    # The exchange completed: a token set was issued, it simply does not carry
    # every scope this connection type needs. Nothing is persisted — but the
    # provider may have retired the previous grant to issue this one, so a
    # reconnect cannot be told its old authorization survived.
    expected = {
        "outcome": "authorization_required",
        "help_target": {"page_id": "oauth_help", "anchor": "authorize-again"},
    }
    if existing_connection:
        expected["outcome"] = "needs_attention"
        expected["connection_id"] = "connection-existing"
    assert result.model_dump(exclude_none=True) == expected
    assert result.connection_preserved is None
    assert service.test_token_store.writes == []
    assert repository.created == []
    assert repository.updated == []
    if existing_connection:
        assert repository.connection == connection_before
        assert repository.post_connect_updates == []
    else:
        assert repository.post_connect_updates == []
    assert "must-not-persist" not in repr(result)


@pytest.mark.parametrize("existing_connection", [False, True])
def test_exchange_invalid_client_marks_only_selected_configuration_needs_attention(
    existing_connection: bool,
) -> None:
    repository = _Repository()
    connection_before = copy.deepcopy(repository.connection)
    repository.configuration["status"] = "verified"
    if existing_connection:
        repository.state["existing_connection_id"] = "connection-existing"
        repository.state["intent_payload"] = {"connection_credential_revision": 7}

    def fail(**_values: Any) -> dict[str, Any]:
        raise OAuthProviderError(OAuthErrorCode.INVALID_CLIENT, OAuthProviderOperation.EXCHANGE)

    result = _handle(_service(repository, exchange=fail))

    expected = {
        "outcome": "needs_attention",
        "help_target": {"page_id": "oauth_help", "anchor": "client-credentials"},
    }
    if existing_connection:
        expected.update(
            {
                "connection_id": "connection-existing",
                "connection_preserved": True,
            }
        )
    assert result.model_dump(exclude_none=True) == expected
    assert repository.configuration["status"] == "needs_attention"
    assert repository.configuration["last_error"] == {"code": "invalid_client"}
    assert repository.other_configuration["status"] == "verified"
    if existing_connection:
        assert repository.connection == connection_before
        assert repository.post_connect_updates == []
    else:
        assert repository.post_connect_updates == []
    assert repository.invalid_client_updates == [
        {
            "tenant_id": "tenant-a",
            "account_id": "account-a",
            "project_id": "project-a",
            "extension_install_id": "install-a",
            "extension_id": "fixture.extension-oauth",
            "provider_id": "example",
            "configuration_id": "11111111-1111-4111-8111-111111111111",
            "expected_configuration_revision": 3,
        }
    ]
    assert "write_tokens" not in repository.events
    assert "client-secret" not in repr(result)


@pytest.mark.parametrize(
    ("code", "outcome"),
    [
        (OAuthErrorCode.INVALID_GRANT, "authorization_required"),
        (OAuthErrorCode.AUTHORIZATION_REQUIRED, "authorization_required"),
        (OAuthErrorCode.AUTHORIZATION_DENIED, "authorization_denied"),
        (OAuthErrorCode.RATE_LIMITED, "provider_unavailable"),
        (OAuthErrorCode.PROVIDER_UNAVAILABLE, "provider_unavailable"),
        (OAuthErrorCode.MALFORMED_RESPONSE, "provider_unavailable"),
        (OAuthErrorCode.INTERNAL_FAILURE, "provider_unavailable"),
    ],
)
def test_exchange_transport_codes_map_to_closed_safe_outcomes(
    code: OAuthErrorCode,
    outcome: str,
) -> None:
    repository = _Repository()

    def fail(**_values: Any) -> dict[str, Any]:
        raise OAuthProviderError(code, OAuthProviderOperation.EXCHANGE)

    result = _handle(_service(repository, exchange=fail))

    assert result.outcome == outcome
    expected_help = {"page_id": "oauth_help"}
    if code is OAuthErrorCode.AUTHORIZATION_REQUIRED:
        expected_help["anchor"] = "authorize-again"
    if code is OAuthErrorCode.PROVIDER_UNAVAILABLE:
        expected_help["anchor"] = "provider-down"
    assert result.help_target == expected_help
    assert repository.invalid_client_updates == []
    assert "write_tokens" not in repository.events
    assert "OAuthProviderError" not in repr(result)


@pytest.mark.parametrize(
    "code",
    [
        OAuthErrorCode.INVALID_GRANT,
        OAuthErrorCode.AUTHORIZATION_REQUIRED,
        OAuthErrorCode.AUTHORIZATION_DENIED,
        OAuthErrorCode.RATE_LIMITED,
        OAuthErrorCode.PROVIDER_UNAVAILABLE,
        OAuthErrorCode.MALFORMED_RESPONSE,
        OAuthErrorCode.INTERNAL_FAILURE,
    ],
)
def test_reconnect_exchange_error_leaves_the_existing_connection_untouched(
    code: OAuthErrorCode,
) -> None:
    """Whatever the failure, the stored connection is never modified.

    Whether it can still be *called* is a separate question, and one this side
    can only answer for a definite refusal — see the assertions below.
    """
    repository = _Repository()
    repository.state["existing_connection_id"] = "connection-existing"
    repository.state["intent_payload"] = {"connection_credential_revision": 7}
    sibling = copy.deepcopy(repository.connection)
    sibling["connection_id"] = "connection-sibling"
    repository.connections["connection-sibling"] = sibling
    connection_before = copy.deepcopy(repository.connection)
    sibling_before = copy.deepcopy(sibling)

    def fail(**_values: Any) -> dict[str, Any]:
        raise OAuthProviderError(code, OAuthProviderOperation.EXCHANGE)

    result = _handle(_service(repository, exchange=fail))

    # A provider that answered and refused granted nothing, so the old
    # credentials are still good. Anything else happened after the request went
    # out: the provider may have accepted the grant and revoked what this
    # connection was using, and its access token can keep working until it
    # expires — so the connection is flagged rather than called intact.
    definite = code in {
        OAuthErrorCode.INVALID_GRANT,
        OAuthErrorCode.AUTHORIZATION_REQUIRED,
        OAuthErrorCode.AUTHORIZATION_DENIED,
        OAuthErrorCode.RATE_LIMITED,
    }
    expected_outcome = (
        "authorization_required"
        if code in {OAuthErrorCode.INVALID_GRANT, OAuthErrorCode.AUTHORIZATION_REQUIRED}
        else "authorization_denied"
        if code is OAuthErrorCode.AUTHORIZATION_DENIED
        else "provider_unavailable"
        if code is OAuthErrorCode.RATE_LIMITED
        else "needs_attention"
    )
    assert result.outcome == expected_outcome
    assert result.connection_id == "connection-existing"
    assert result.connection_preserved is (True if definite else None)
    assert repository.configuration["status"] == "ready_for_authorization"
    assert repository.connection == connection_before
    assert sibling == sibling_before
    assert repository.post_connect_updates == []
    assert "write_tokens" not in repository.events


@pytest.mark.parametrize(
    ("mutate", "provider_error"),
    [
        (
            lambda repository: repository.state["intent_payload"].update(
                {"connection_credential_revision": 6}
            ),
            "access_denied",
        ),
        (
            lambda repository: repository.connection.update({"project_id": "project-foreign"}),
            "temporarily_unavailable",
        ),
    ],
)
def test_failed_reconnect_omits_stale_or_foreign_connection_context(
    mutate: Any,
    provider_error: str,
) -> None:
    repository = _Repository()
    repository.state["existing_connection_id"] = "connection-existing"
    repository.state["intent_payload"] = {"connection_credential_revision": 7}
    mutate(repository)

    result = _handle(
        _service(repository),
        code=None,
        provider_error=provider_error,
    )

    assert result.outcome in {"authorization_denied", "provider_unavailable"}
    assert result.connection_id is None
    assert result.connection_preserved is None


@pytest.mark.parametrize(
    ("provider_error", "expected_outcome"),
    [
        ("access_denied", "authorization_denied"),
        ("temporarily_unavailable", "provider_unavailable"),
    ],
)
def test_provider_error_preserves_only_a_revalidated_existing_connection(
    provider_error: str,
    expected_outcome: str,
) -> None:
    repository = _Repository()
    repository.state["existing_connection_id"] = "connection-existing"
    repository.state["intent_payload"] = {"connection_credential_revision": 7}

    result = _handle(
        _service(repository),
        code=None,
        provider_error=provider_error,
    )

    assert result.outcome == expected_outcome
    assert result.connection_id == "connection-existing"
    assert result.connection_preserved is True
    assert "write_tokens" not in repository.events


def test_reconnect_locks_parent_then_exact_connection_and_preserves_refresh_token() -> None:
    repository = _Repository()
    repository.state["existing_connection_id"] = "connection-existing"
    repository.state["intent_payload"] = {"connection_credential_revision": 7}

    def without_refresh(**_values: Any) -> dict[str, Any]:
        return {"access_token": "new-access", "token_type": "Bearer", "scope": "mail.read"}

    service = _service(repository, exchange=without_refresh)
    result = _handle(service)
    assert result.connection_id == "connection-existing"
    final_events = repository.events[repository.events.index("lock_configuration") :]
    assert final_events[:3] == [
        "lock_configuration",
        "lock_connection",
        "resolve_refresh_token",
    ]
    write = service.test_token_store.writes[0]
    assert write["existing_refresh_token"] == "old-refresh-token"
    assert repository.updated[0]["expected_credential_revision"] == 7
    assert repository.updated[0]["secret_refs"]["refs"]["extension_secret"].endswith(
        ":extension_secret"
    )
    assert "_oauth_expires_at" not in repository.updated[0]["secret_refs"]["refs"]
    assert repository.updated[0]["connection_config"] == {
        "extension_metadata": {"canary": "preserve-top-level"},
        "oauth": {
            "provider_subject": "preserve-oauth-metadata",
            "lifecycle_status": "connected",
            "token_type": "Bearer",
            "authorization_intent_id": "connect",
        },
    }


def test_reconnect_rejects_foreign_secret_refs_before_resolve_or_write() -> None:
    for secret_name in ("_oauth_refresh_token", "extension_secret"):
        repository = _Repository()
        repository.state["existing_connection_id"] = "connection-existing"
        repository.state["intent_payload"] = {"connection_credential_revision": 7}
        repository.connection["secret_refs"]["refs"][secret_name] = (
            f"project_connection:connection-foreign:{secret_name}"
        )

        result = _handle(_service(repository))

        assert result.outcome == "restart_required"
        assert "resolve_refresh_token" not in repository.events
        assert "write_tokens" not in repository.events


def test_stale_configuration_or_connection_revision_never_writes_tokens() -> None:
    repository = _Repository()
    repository.configuration["authorization_revision"] = 3
    result = _handle(_service(repository))
    assert result.outcome == "restart_required"
    assert "exchange" not in repository.events
    assert "write_tokens" not in repository.events

    reconnect = _Repository()
    reconnect.state["existing_connection_id"] = "connection-existing"
    reconnect.state["intent_payload"] = {"connection_credential_revision": 6}
    result = _handle(_service(reconnect))
    assert result.outcome == "restart_required"
    assert "write_tokens" not in reconnect.events


def test_reconnect_revalidates_connection_release_and_provider_fingerprint() -> None:
    for field, stale_value in (
        ("oauth_extension_release_id", "release-stale"),
        ("oauth_provider_security_fingerprint", "fingerprint-stale"),
    ):
        repository = _Repository()
        repository.state["existing_connection_id"] = "connection-existing"
        repository.state["intent_payload"] = {"connection_credential_revision": 7}
        repository.connection[field] = stale_value

        result = _handle(_service(repository))

        assert result.outcome == "restart_required"
        assert "write_tokens" not in repository.events


def test_post_connect_runs_after_grant_commit_under_install_barrier_with_only_access_token() -> (
    None
):
    repository = _Repository()
    captured: dict[str, Any] = {}

    def post_connect(**values: Any) -> dict[str, Any]:
        assert repository.events[-3:] == [
            "lock_install",
            "lock_configuration",
            "lock_connection",
        ]
        repository.events.append("post_connect")
        captured.update(copy.deepcopy(values))
        return {
            "status": "succeeded",
            "response": {
                "result": {
                    "subject": "verified-subject",
                    "verified_display_name": "Verified Person",
                    "verified_email": "verified@example.test",
                    "safe_metadata": {"account_tier": "standard"},
                    "health": "ok",
                }
            },
        }

    result = _handle(_service(repository, post_connect=post_connect))

    assert result.outcome == "connected"
    assert captured["action_id"] == "verify_identity"
    assert captured["target"] == {
        "connection_id": "connection-new",
        "connection": {
            "connection_id": "connection-new",
            "secrets": {"access_token": "access-token-canary"},
        },
    }
    assert set(captured["context"]) == {
        "account_id",
        "project_id",
        "connection_id",
        "connection_type",
        "granted_scopes",
    }
    serialized = repr(captured)
    assert "refresh-token-canary" not in serialized
    assert "client-secret" not in serialized
    assert "decrypted-verifier" not in serialized
    assert "authorization-code" not in serialized
    update = repository.post_connect_updates[0]
    assert repository.created[0]["health_status"] == "unknown"
    assert repository.created[0]["runtime_status"] == "unavailable"
    assert repository.created[0]["last_error"] == ""
    assert repository.created[0]["mark_last_seen_ok"] is False
    assert update["health_status"] == "healthy"
    assert update["runtime_status"] == "ready"
    assert update["last_error"] == ""
    assert update["mark_last_seen_ok"] is True
    assert update["display_name"] == "Verified Person"
    assert update["connection_config"]["oauth"] == {
        "lifecycle_status": "connected",
        "token_type": "Bearer",
        "subject": "verified-subject",
        "verified_display_name": "Verified Person",
        "verified_email": "verified@example.test",
        "safe_metadata": {"account_tier": "standard"},
        "last_error_code": None,
        "authorization_intent_id": "connect",
    }
    assert repository.events.index("post_connect") > repository.events.index("create_connection")


@pytest.mark.parametrize(
    "reflected_result",
    [
        {
            "subject": "access-token-canary",
            "verified_display_name": "Injected identity",
            "safe_metadata": {},
            "health": "ok",
        },
        {
            "subject": "safe-subject",
            "verified_display_name": "Safe identity",
            "safe_metadata": {"nested": "Bearer access-token-canary"},
            "health": "ok",
        },
    ],
)
def test_post_connect_rejects_reflected_access_token_before_identity_persistence(
    reflected_result: dict[str, Any],
) -> None:
    repository = _Repository()
    invocation = {
        "status": "succeeded",
        "response": {"result": copy.deepcopy(reflected_result)},
    }

    result = _handle(
        _service(
            repository,
            post_connect=lambda **_values: copy.deepcopy(invocation),
        )
    )

    assert result.outcome == "needs_attention"
    assert repository.post_connect_updates
    update = repository.post_connect_updates[0]
    assert update["display_name"] == "Primary mailbox"
    assert update["connection_config"]["oauth"]["lifecycle_status"] == "needs_attention"
    assert update["connection_config"]["oauth"]["last_error_code"] == "internal_failure"
    assert "access-token-canary" not in repr(result)
    serialized = repr(update)
    assert "access-token-canary" not in serialized
    assert "Injected identity" not in serialized


def test_post_connect_accepts_non_reflecting_verified_identity() -> None:
    repository = _Repository()
    invocation = {
        "status": "succeeded",
        "response": {
            "result": {
                "subject": "provider-subject",
                "verified_display_name": "Safe identity",
                "safe_metadata": {"tier": "standard"},
                "health": "ok",
            }
        },
    }

    result = _handle(
        _service(
            repository,
            post_connect=lambda **_values: copy.deepcopy(invocation),
        )
    )

    assert result.outcome == "connected"
    assert repository.post_connect_updates[0]["connection_config"]["oauth"]["subject"] == (
        "provider-subject"
    )


def test_post_connect_scan_budget_exhaustion_uses_the_safe_reflection_failure() -> None:
    repository = _Repository()
    invocation = {
        "status": "succeeded",
        "response": {
            "result": {
                "subject": "safe-subject",
                "health": "ok",
                "oversized_provider_output": "z" * (9 * 1024 * 1024),
            }
        },
    }

    result = _handle(
        _service(
            repository,
            post_connect=lambda **_values: invocation,
        )
    )

    assert result.outcome == "needs_attention"
    update = repository.post_connect_updates[0]
    assert update["connection_config"]["oauth"]["last_error_code"] == "internal_failure"
    assert "oversized_provider_output" not in repr(update)


@pytest.mark.parametrize(
    ("invocation", "expected_status", "expected_error"),
    [
        (
            {"status": "failed", "detail": "raw-provider-copy-canary"},
            "needs_attention",
            "internal_failure",
        ),
        (
            {
                "status": "succeeded",
                "response": {
                    "result": {
                        "subject": "",
                        "health": "authorization_required",
                        "error_code": "authorization_required",
                    }
                },
            },
            "authorization_required",
            "authorization_required",
        ),
    ],
)
def test_post_connect_failure_retains_grant_and_sets_closed_lifecycle(
    invocation: dict[str, Any], expected_status: str, expected_error: str
) -> None:
    repository = _Repository()

    result = _handle(_service(repository, post_connect=lambda **_values: copy.deepcopy(invocation)))

    assert result.outcome == expected_status
    assert repository.created
    update = repository.post_connect_updates[0]
    assert update["connection_config"]["oauth"]["lifecycle_status"] == expected_status
    assert update["connection_config"]["oauth"]["last_error_code"] == expected_error
    assert update["health_status"] == "failed"
    assert update["runtime_status"] == "unavailable"
    assert update["last_error"] == expected_error
    assert update["mark_last_seen_ok"] is False
    assert "raw-provider-copy-canary" not in repr(update)


def test_reconnect_failure_clears_old_verified_identity_and_keeps_new_grant() -> None:
    repository = _Repository()
    repository.configuration["status"] = "verified"
    repository.state["existing_connection_id"] = "connection-existing"
    repository.state["intent_payload"] = {"connection_credential_revision": 7}
    repository.connection["oauth_authorization_revision"] = 2
    repository.connection["connection_config"]["oauth"].update(
        {
            "subject": "old-subject",
            "verified_display_name": "Old Person",
            "verified_email": "old@example.test",
            "safe_metadata": {"old": "identity"},
        }
    )
    invocation = {
        "status": "failed",
        "response": {"ok": False, "error_code": "provider_unavailable", "result": {}},
    }

    result = _handle(_service(repository, post_connect=lambda **_values: copy.deepcopy(invocation)))

    assert result.outcome == "needs_attention"
    assert repository.updated
    assert repository.post_connect_updates
    assert repository.updated[0]["health_status"] == "unknown"
    assert repository.updated[0]["runtime_status"] == "unavailable"
    assert repository.updated[0]["last_error"] == ""
    assert repository.updated[0]["mark_last_seen_ok"] is False
    assert repository.post_connect_updates[0]["health_status"] == "failed"
    assert repository.post_connect_updates[0]["runtime_status"] == "unavailable"
    assert repository.post_connect_updates[0]["last_error"] == "provider_unavailable"
    assert repository.post_connect_updates[0]["mark_last_seen_ok"] is False
    oauth = repository.post_connect_updates[0]["connection_config"]["oauth"]
    assert oauth["lifecycle_status"] == "needs_attention"
    assert oauth["last_error_code"] == "provider_unavailable"
    assert not {
        "subject",
        "verified_display_name",
        "verified_email",
        "safe_metadata",
    } & set(oauth)
    assert repository.post_connect_updates[0]["display_name"] == "Primary mailbox"
    assert repository.updated[0]["secret_refs"]["refs"]["_oauth_access_token"] == "access-ref"


def test_post_connect_retry_uses_committed_access_without_new_consent_and_exact_scope() -> None:
    repository = _Repository()
    repository.configuration["status"] = "verified"
    repository.connection.update(
        {
            "oauth_authorization_revision": 2,
            "oauth_granted_scopes": ["mail.read", "mail.write"],
            "oauth_provider_revocation_fingerprint": repository.configuration[
                "provider_revocation_fingerprint"
            ],
            "connection_config": {
                "oauth": {
                    "lifecycle_status": "needs_attention",
                    "last_error_code": "provider_unavailable",
                }
            },
            "secret_refs": {
                "refs": {
                    "_oauth_access_token": (
                        "project_connection:connection-existing:_oauth_access_token"
                    )
                }
            },
        }
    )
    captured: dict[str, Any] = {}
    resolved: dict[str, Any] = {}

    def resolve_runtime(**values: Any) -> dict[str, str]:
        repository.events.append("resolve_runtime_access_token")
        resolved.update(copy.deepcopy(values))
        repository.configuration["configuration_revision"] += 1
        repository.connection["credential_revision"] += 1
        return {"access_token": "refreshed-access-token"}

    def post_connect(**values: Any) -> dict[str, Any]:
        repository.events.append("post_connect_retry")
        captured.update(copy.deepcopy(values))
        return {
            "status": "succeeded",
            "response": {
                "result": {
                    "subject": "retry-subject",
                    "verified_email": "retry@example.test",
                    "safe_metadata": {},
                    "health": "ok",
                }
            },
        }

    service = _service(
        repository,
        post_connect=post_connect,
        runtime_resolver=resolve_runtime,
    )
    result = service.retry_post_connect(
        tenant_id="tenant-a",
        account_id="account-a",
        project_id="project-a",
        extension_install_id="install-a",
        extension_id="fixture.extension-oauth",
        provider_id="example",
        configuration_id=repository.state["configuration_id"],
        connection_id="connection-existing",
        connection_type="example_mail",
    )

    assert result.outcome == "connected"
    assert "exchange" not in repository.events
    assert resolved["recovery_purpose"] == "post_connect_retry"
    assert captured["target"]["connection"]["secrets"] == {"access_token": "refreshed-access-token"}
    assert repository.post_connect_updates[0]["expected_credential_revision"] == 8
    assert repository.connection["credential_revision"] == 9
    assert repository.post_connect_updates[0]["display_name"] == "retry@example.test"
    dispatch_index = repository.events.index("post_connect_retry")
    assert repository.events[dispatch_index - 3 : dispatch_index] == [
        "lock_install",
        "lock_configuration",
        "lock_connection",
    ]
    update_index = repository.events.index("update_post_connect")
    assert repository.events[update_index - 3 : update_index] == [
        "lock_install",
        "lock_configuration",
        "lock_connection",
    ]

    foreign = _Repository()
    foreign.configuration["status"] = "verified"
    foreign.connection.update(copy.deepcopy(repository.connection))
    foreign_service = _service(
        foreign,
        post_connect=lambda **_values: pytest.fail("cross-project retry invoked action"),
    )
    denied = foreign_service.retry_post_connect(
        tenant_id="tenant-a",
        account_id="account-a",
        project_id="project-foreign",
        extension_install_id="install-a",
        extension_id="fixture.extension-oauth",
        provider_id="example",
        configuration_id=foreign.state["configuration_id"],
        connection_id="connection-existing",
        connection_type="example_mail",
    )
    assert denied.outcome == "restart_required"
    assert "resolve_access_token" not in foreign.events


@pytest.mark.parametrize(
    ("health", "error_code", "expected_outcome"),
    [
        ("needs_attention", "provider_unavailable", "needs_attention"),
        ("authorization_required", "authorization_required", "authorization_required"),
    ],
)
def test_retry_returns_truthful_closed_non_ok_outcome(
    health: str, error_code: str, expected_outcome: str
) -> None:
    repository = _Repository()
    repository.configuration["status"] = "verified"
    repository.connection.update(
        {
            "oauth_authorization_revision": 2,
            "oauth_granted_scopes": ["mail.read", "mail.write"],
            "oauth_provider_revocation_fingerprint": repository.configuration[
                "provider_revocation_fingerprint"
            ],
            "connection_config": {
                "oauth": {
                    "lifecycle_status": "needs_attention",
                    "last_error_code": "provider_unavailable",
                }
            },
            "secret_refs": {
                "refs": {
                    "_oauth_access_token": (
                        "project_connection:connection-existing:_oauth_access_token"
                    )
                }
            },
        }
    )
    service = _service(
        repository,
        post_connect=lambda **_values: {
            "status": "succeeded",
            "response": {"result": {"subject": "", "health": health, "error_code": error_code}},
        },
    )

    result = service.retry_post_connect(
        tenant_id="tenant-a",
        account_id="account-a",
        project_id="project-a",
        extension_install_id="install-a",
        extension_id="fixture.extension-oauth",
        provider_id="example",
        configuration_id=repository.state["configuration_id"],
        connection_id="connection-existing",
        connection_type="example_mail",
    )

    assert result.outcome == expected_outcome


def test_post_connect_final_write_race_never_reports_connected() -> None:
    repository = _Repository()
    repository.update_oauth_post_connect = lambda **_values: None  # type: ignore[method-assign]

    result = _handle(
        _service(
            repository,
            post_connect=lambda **_values: {
                "status": "succeeded",
                "response": {"result": {"subject": "verified", "health": "ok"}},
            },
        )
    )

    assert result.outcome == "restart_required"


def test_grant_and_post_connect_transactions_lock_install_before_owned_rows() -> None:
    repository = _Repository()

    result = _handle(
        _service(
            repository,
            post_connect=lambda **_values: {
                "status": "succeeded",
                "response": {"result": {"subject": "verified", "health": "ok"}},
            },
        )
    )

    assert result.outcome == "connected"
    grant_start = repository.events.index("write_tokens")
    assert repository.events[grant_start - 2 : grant_start + 2] == [
        "lock_install",
        "lock_configuration",
        "write_tokens",
        "create_connection",
    ]
    post_connect_start = repository.events.index("update_post_connect")
    assert repository.events[post_connect_start - 3 : post_connect_start] == [
        "lock_install",
        "lock_configuration",
        "lock_connection",
    ]


def test_disable_after_grant_commit_prevents_post_connect_token_dispatch() -> None:
    repository = _Repository()
    repository.disable_after_grant_commit = True
    invocations: list[dict[str, Any]] = []

    result = _handle(
        _service(
            repository,
            post_connect=lambda **values: invocations.append(copy.deepcopy(values)) or {},
        )
    )

    assert result.outcome == "restart_required"
    assert repository.install["state"] == "disabled"
    assert invocations == []


def test_reconnect_with_a_granted_but_under_scoped_token_promises_nothing() -> None:
    """A completed exchange ends any claim about the previous authorization.

    The provider answered, issued a token set, and the set is short of what this
    connection type needs. That refusal is definite about the *scopes* and says
    nothing about the old credentials: issuing the new grant may already have
    retired them. Reporting the old connection as preserved would be a promise
    that expires quietly an hour later, when its access token stops working.
    """
    repository = _Repository()
    repository.state["existing_connection_id"] = "connection-existing"
    repository.state["intent_payload"] = {"connection_credential_revision": 7}
    connection_before = copy.deepcopy(repository.connection)
    exchanged: list[bool] = []

    def narrow_scope(**_values: Any) -> dict[str, Any]:
        exchanged.append(True)
        return {
            "access_token": "issued-but-under-scoped",
            "refresh_token": "issued-refresh",
            "token_type": "Bearer",
            "scope": "profile.read",
        }

    service = _service(repository, exchange=narrow_scope)
    result = _handle(service)

    assert exchanged == [True], "the exchange must actually have happened"
    assert result.connection_preserved is None
    assert result.outcome == "needs_attention"
    assert result.connection_id == "connection-existing"
    # Nothing is written: the stored connection and its tokens are untouched even
    # though this side can no longer vouch for them.
    assert service.test_token_store.writes == []
    assert repository.connection == connection_before
    assert repository.created == []
    assert repository.updated == []
    assert "issued-but-under-scoped" not in repr(result)


def test_a_first_connection_with_an_under_scoped_token_still_reads_as_authorization_required() -> (
    None
):
    """With no previous connection there is nothing to make a promise about."""
    repository = _Repository()

    def narrow_scope(**_values: Any) -> dict[str, Any]:
        return {"access_token": "at", "token_type": "Bearer", "scope": "profile.read"}

    result = _handle(_service(repository, exchange=narrow_scope))

    assert result.outcome == "authorization_required"
    assert result.connection_id is None
    assert result.connection_preserved is None


def test_a_refusal_before_the_exchange_still_preserves_the_connection() -> None:
    """The boundary cuts at dispatch, not at the error code."""
    repository = _Repository()
    repository.state["existing_connection_id"] = "connection-existing"
    repository.state["intent_payload"] = {"connection_credential_revision": 7}

    def refuse(**_values: Any) -> dict[str, Any]:
        raise OAuthProviderError(
            OAuthErrorCode.AUTHORIZATION_REQUIRED, OAuthProviderOperation.EXCHANGE
        )

    result = _handle(_service(repository, exchange=refuse))

    assert result.outcome == "authorization_required"
    assert result.connection_id == "connection-existing"
    assert result.connection_preserved is True
