from __future__ import annotations

from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import pytest

from core.application.auth.config import DEFAULT_ACCOUNT_ID
from core.application.extensions.oauth_configuration_secrets import (
    configuration_secret_ref_key,
)
from core.application.extensions.oauth_contract import (
    OAuthAuthorizationIntent,
    OAuthCredentialField,
    OAuthEndpointPermission,
    OAuthProviderContract,
    provider_revocation_fingerprint,
    provider_security_fingerprint,
)
from core.application.extensions.oauth_lifecycle import (
    PreparedOAuthRevocation,
    execute_extension_oauth_revocation,
    invalidate_install_oauth_on_disable,
    prepare_extension_oauth_revocation,
    record_extension_oauth_lifecycle_audit,
    record_extension_oauth_uninstall_audit,
)
from core.application.management.project_connection_secrets import connection_secret_ref_key


def _provider() -> OAuthProviderContract:
    return OAuthProviderContract(
        provider_id="example",
        display_name="Example",
        description="Example OAuth",
        setup_page_id="setup",
        help_page_id="help",
        error_help_anchors={},
        credential_fields=(
            OAuthCredentialField(
                field_id="client_id",
                value_type="text",
                required=True,
                secret=False,
                change_policy="reauthorization_required",
                label="Client ID",
                help_text="Client ID help",
                placeholder="",
                autocomplete="",
                validation={"min_length": 1, "max_length": 100},
            ),
            OAuthCredentialField(
                field_id="client_secret",
                value_type="secret",
                required=True,
                secret=True,
                change_policy="runtime_revalidate",
                label="Client secret",
                help_text="Client secret help",
                placeholder="",
                autocomplete="new-password",
                validation={"min_length": 1, "max_length": 100},
            ),
        ),
        authorization_endpoint=OAuthEndpointPermission("https://identity.example/authorize"),
        token_endpoint=OAuthEndpointPermission(
            "https://identity.example/token", "client_secret_post"
        ),
        revocation_endpoint=OAuthEndpointPermission("https://identity.example/revoke"),
        callback_id="oauth_authorization",
        authorization_intents=(OAuthAuthorizationIntent("connect", {}, ()),),
        post_connect_action_id=None,
    )


def _owned_rows(provider: OAuthProviderContract) -> tuple[dict, dict]:
    configuration_id = "configuration-1"
    connection_id = "connection-1"
    configuration = {
        "configuration_id": configuration_id,
        "account_id": "account-1",
        "non_secret_config": {"client_id": "client-canary"},
        "secret_refs": {
            "refs": {
                "client_secret": configuration_secret_ref_key(configuration_id, "client_secret")
            }
        },
        "provider_security_fingerprint": provider_security_fingerprint(provider),
        "provider_revocation_fingerprint": provider_revocation_fingerprint(provider),
    }
    connection = {
        "connection_id": connection_id,
        "account_id": "account-1",
        "oauth_configuration_id": configuration_id,
        "oauth_provider_id": provider.provider_id,
        "oauth_provider_security_fingerprint": provider_security_fingerprint(provider),
        "oauth_provider_revocation_fingerprint": provider_revocation_fingerprint(provider),
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
    return configuration, connection


def test_revoke_preparation_prefers_refresh_token_and_hides_secrets_from_repr() -> None:
    provider = _provider()
    configuration, connection = _owned_rows(provider)
    prepared = prepare_extension_oauth_revocation(
        provider=provider,
        configuration=configuration,
        connection=connection,
        token_snapshot=lambda **_values: {
            "access_token": "access-canary",
            "refresh_token": "refresh-canary",
        },
        configuration_secret_value=lambda **_values: "secret-canary",
    )

    assert prepared is not None
    assert prepared.token == "refresh-canary"
    assert "refresh-canary" not in repr(prepared)
    assert "secret-canary" not in repr(prepared)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("provider_security_fingerprint", "tampered"),
        ("provider_revocation_fingerprint", "tampered"),
    ],
)
def test_revoke_preparation_fails_closed_on_configuration_fingerprint_drift(
    field: str, value: str
) -> None:
    provider = _provider()
    configuration, connection = _owned_rows(provider)
    configuration[field] = value

    assert (
        prepare_extension_oauth_revocation(
            provider=provider,
            configuration=configuration,
            connection=connection,
            token_snapshot=lambda **_values: {"refresh_token": "must-not-leak"},
            configuration_secret_value=lambda **_values: "must-not-leak",
        )
        is None
    )


def test_remote_revoke_failure_is_normalized_to_redacted_outcome() -> None:
    prepared = PreparedOAuthRevocation(
        endpoint=OAuthEndpointPermission("https://identity.example/revoke"),
        token_endpoint_permission=OAuthEndpointPermission(
            "https://identity.example/token", "client_secret_post"
        ),
        token="token-canary",
        client_id="client-canary",
        client_secret="secret-canary",
    )
    assert (
        execute_extension_oauth_revocation(
            prepared,
            revoke_token=lambda **_values: (_ for _ in ()).throw(
                RuntimeError("provider echoed token-canary")
            ),
        )
        is False
    )


def test_lifecycle_audit_persists_only_normalized_outcome_and_identifiers() -> None:
    logger = MagicMock()

    record_extension_oauth_lifecycle_audit(
        logger,
        extension_id="acme.oauth-extension",
        account_id="account-1",
        project_id="project-1",
        event="connection_disconnect",
        outcome="failed",
        resource_id="connection-1",
    )

    payload = logger.log_extension_invocation.call_args.kwargs
    assert payload["request"] == {
        "event": "connection_disconnect",
        "resource_id": "connection-1",
    }
    assert payload["response"] == {"remote_revocation": "failed"}
    assert payload["request_context"] == {"project_id": "project-1"}
    assert "token" not in repr(payload).lower()


def test_uninstall_audit_uses_durable_command_sink_with_redacted_outcome() -> None:
    logger = MagicMock()

    record_extension_oauth_uninstall_audit(
        logger,
        extension_id="acme.oauth-extension",
        account_id="account-1",
        install_id="install-1",
        plan_revision="revision-1",
        actor_type="operator",
        actor_id="operator-1",
        outcome="failed",
    )

    payload = logger.log_command_execution.call_args.kwargs
    assert payload["command_type"] == "extension_oauth_uninstall_lifecycle"
    assert payload["target"] == {
        "extension_id": "acme.oauth-extension",
        "install_id": "install-1",
    }
    assert payload["payload"] == {}
    assert payload["result"] == {"remote_revocation": "failed"}
    assert payload["status"] == "executed"
    assert "token" not in repr(payload).lower()


def test_disable_consumes_states_and_rotates_exact_install_connections_without_deleting() -> None:
    transaction_conn = MagicMock()
    cursor = transaction_conn.cursor.return_value.__enter__.return_value
    cursor.fetchall.return_value = [("install-1", "account-1")]
    cursor.rowcount = 2

    count = invalidate_install_oauth_on_disable(
        transaction_conn,
        install_id="install-1",
        extension_id="acme.oauth-extension",
        account_id="account-1",
    )

    assert count == 2
    statements = [str(call.args[0]) for call in cursor.execute.call_args_list]
    assert any("extension_oauth_authorization_states" in statement for statement in statements)
    assert any(
        "credential_revision = credential_revision + 1" in statement for statement in statements
    )
    assert all("DELETE" not in statement.upper() for statement in statements)
    assert "install_id = %s" in statements[0]
    assert cursor.execute.call_args_list[0].args[1] == (
        "install-1",
        "acme.oauth-extension",
        "account-1",
    )
    assert all("extension_install_id = %s" in statement for statement in statements[1:])


@pytest.mark.parametrize(
    ("extension_id", "enabled", "expected_calls"),
    [
        ("acme.oauth-extension", False, 1),
        ("acme.oauth-extension", True, 0),
        ("another.oauth-extension", False, 1),
    ],
)
def test_legacy_projection_applies_generic_disable_invariant(
    extension_id: str, enabled: bool, expected_calls: int
) -> None:
    from core.application.extensions.command_lifecycle_activation import (
        sync_legacy_enabled_projection,
    )

    transaction_conn = object()
    logger = MagicMock()

    @contextmanager
    def transaction():
        yield transaction_conn

    logger.extension_lifecycle_transaction.side_effect = transaction
    logger.sync_enabled_extension_record.return_value = (extension_id, [])
    with (
        patch(
            "core.application.extensions.oauth_lifecycle.resolve_install_id_for_disable",
            return_value="install-1",
        ),
        patch(
            "core.application.extensions.oauth_lifecycle.invalidate_install_oauth_on_disable"
        ) as invalidate_runtime_access,
    ):
        assert sync_legacy_enabled_projection(logger, extension_id, enabled=enabled) is True

    assert invalidate_runtime_access.call_count == expected_calls
    if expected_calls:
        invalidate_runtime_access.assert_called_once_with(
            transaction_conn,
            install_id="install-1",
            extension_id=extension_id,
            account_id=DEFAULT_ACCOUNT_ID,
        )
