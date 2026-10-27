from __future__ import annotations

from typing import Any

import pytest

from core.application.auth import (
    mcp_connector_oauth,
    mcp_connector_oauth_policy,
    mcp_connector_oauth_repository,
    mcp_connector_oauth_tokens,
)
from core.application.auth.oauth_client import OAuthConnectionError
from core.application.management.errors import ServiceValidationError
from core.application.management.mcp_connector_oauth_contract import _oauth_metadata_from_config


def _row(**overrides: Any) -> dict[str, Any]:
    value = {
        "connector_id": "mcp_alpha",
        "account_id": "acc_alpha",
        "project_id": "project_alpha",
        "tenant_id": "tenant_alpha",
        "auth_mode": "oauth2",
        "base_url": "https://mcp.example.test/mcp",
        "oauth_metadata_jsonb": {},
        "oauth_secret_refs_jsonb": {},
        "oauth_credential_revision": 1,
    }
    value.update(overrides)
    return value


def test_oauth_metadata_persists_generic_scopes_parameters_and_refresh_requirement() -> None:
    metadata = _oauth_metadata_from_config(
        {
            "requested_scopes": ["mail.read", "mail.write"],
            "authorization_parameters": {"prompt": "consent", "access_type": "offline"},
            "refresh_token_required": True,
        }
    )

    assert metadata == {
        "requested_scopes": ["mail.read", "mail.write"],
        "authorization_parameters": {"prompt": "consent", "access_type": "offline"},
        "refresh_token_required": True,
    }


@pytest.mark.parametrize(
    "config",
    [
        {"requested_scopes": "mail.read"},
        {"requested_scopes": ["mail.read", "mail.read"]},
        {"authorization_parameters": ["prompt=consent"]},
        {"authorization_parameters": {"state": "attacker"}},
        {"authorization_parameters": {"CLIENT_SECRET": "attacker"}},
        {"authorization_parameters": {"access_token": "attacker"}},
        {"authorization_parameters": {"refresh_token": "attacker"}},
        {"authorization_parameters": {"code": "attacker"}},
        {"authorization_parameters": {"code_verifier": "attacker"}},
        {"authorization_parameters": {"client_assertion": "attacker"}},
        {"authorization_parameters": {"client_assertion_type": "attacker"}},
        {"authorization_parameters": {"prompt": {"nested": True}}},
        {"refresh_token_required": "true"},
    ],
)
def test_oauth_metadata_rejects_ambiguous_or_protocol_owned_values(config: dict[str, Any]) -> None:
    with pytest.raises(ServiceValidationError):
        _oauth_metadata_from_config(config)


@pytest.mark.parametrize(
    "reserved_key",
    [
        "client_secret",
        "client-secret",
        "clientSecret",
        "CLIENT SECRET",
        "authorization",
        "api_key",
        "apiKey",
        "password",
        "secret",
        "token",
        "id_token",
        "idToken",
        "access-token",
        "refreshToken",
        "bearerToken",
        "auth-token",
        "x-api-key",
        "privateKey",
        "clientCredential",
        "oauth_credentials",
        "bearerAuth",
        "dbPasswd",
    ],
)
def test_oauth_metadata_rejects_credential_parameter_aliases(reserved_key: str) -> None:
    with pytest.raises(ServiceValidationError, match="invalid or reserved key") as exc_info:
        _oauth_metadata_from_config({"authorization_parameters": {reserved_key: "must-not-appear"}})

    assert "must-not-appear" not in str(exc_info.value)


def test_oauth_metadata_keeps_public_authorization_parameters() -> None:
    parameters = {
        "access_type": "offline",
        "prompt": "consent",
        "include_granted_scopes": "true",
        "login_hint": "operator@example.test",
        "audience": "catalog",
        "authorization_details": "catalog.read",
        # Dropbox asks for a refresh token this way; the name holds a marker, the value no secret.
        "token_access_type": "offline",
    }

    assert _oauth_metadata_from_config({"authorization_parameters": parameters}) == {
        "authorization_parameters": parameters
    }


def test_start_scope_selection_never_expands_to_advertised_capabilities() -> None:
    discovery = {
        "challenge_scope": [],
        "scopes_supported": ["files:read", "files:write", "admin:all"],
    }

    assert mcp_connector_oauth_policy._scopes_for_start(
        discovery,
        ["files:read", "configured:only"],
    ) == ["files:read", "configured:only"]


def test_start_scope_selection_falls_back_to_scopes_supported_when_nothing_is_set() -> None:
    discovery = {"challenge_scope": [], "scopes_supported": ["mcp:read", "mcp:write"]}

    assert mcp_connector_oauth_policy._scopes_for_start(discovery, []) == ["mcp:read", "mcp:write"]
    assert mcp_connector_oauth_policy._scopes_for_start({"challenge_scope": []}, []) == []


def test_start_scope_selection_combines_challenge_and_configured_scopes() -> None:
    discovery = {
        "challenge_scope": ["challenge:required", "shared"],
        "scopes_supported": ["admin:all"],
    }

    assert mcp_connector_oauth_policy._scopes_for_start(
        discovery,
        ["shared", "configured:only"],
    ) == ["challenge:required", "shared", "configured:only"]


def test_store_token_response_requires_refresh_token_when_metadata_demands_it(monkeypatch) -> None:
    row = _row(oauth_metadata_jsonb={"refresh_token_required": True})
    monkeypatch.setattr(
        mcp_connector_oauth_tokens,
        "_save_connector_oauth_secrets",
        lambda *_args, **_kwargs: {"refs": {}},
    )

    with pytest.raises(OAuthConnectionError, match="refresh_token"):
        mcp_connector_oauth_tokens._store_token_response(
            object(),
            connector_row=row,
            auth_config={"connector_endpoint": row["base_url"]},
            token_response={"access_token": "access", "expires_in": 3600},
            granted_scope_fallback=[],
            expected_credential_revision=int(row["oauth_credential_revision"]),
            now=1000,
        )


def test_store_token_response_retains_existing_refresh_token_on_rotation(monkeypatch) -> None:
    row = _row(oauth_metadata_jsonb={"refresh_token_required": True})
    updates: list[dict[str, Any]] = []
    monkeypatch.setattr(
        mcp_connector_oauth_tokens,
        "_save_connector_oauth_secrets",
        lambda *_args, **kwargs: {
            "refs": {key: f"ref:{key}" for key, value in kwargs["secrets_to_save"].items() if value}
        },
    )
    monkeypatch.setattr(
        mcp_connector_oauth_repository,
        "_update_connector_oauth",
        lambda *_args, **kwargs: updates.append(kwargs["updates"]),
    )
    monkeypatch.setattr(
        mcp_connector_oauth_tokens,
        "_update_connector_oauth",
        mcp_connector_oauth_repository._update_connector_oauth,
    )
    monkeypatch.setattr(
        mcp_connector_oauth_repository,
        "_latest_connector_row",
        lambda *_args, **_kwargs: row,
    )
    monkeypatch.setattr(
        mcp_connector_oauth_tokens,
        "_latest_connector_row",
        mcp_connector_oauth_repository._latest_connector_row,
    )
    monkeypatch.setattr(
        mcp_connector_oauth_repository.McpConnectorStore,
        "get_connector_row",
        lambda *_args, **_kwargs: {
            **row,
            "oauth_metadata_jsonb": updates[-1]["oauth_metadata_jsonb"],
        },
    )

    mcp_connector_oauth_tokens._store_token_response(
        object(),
        connector_row=row,
        auth_config={"connector_endpoint": row["base_url"]},
        token_response={"access_token": "new-access", "expires_in": 3600},
        granted_scope_fallback=[],
        existing_refresh_token="old-refresh",
        expected_credential_revision=int(row["oauth_credential_revision"]),
        now=1000,
    )

    saved = updates[-1]["oauth_secret_refs_jsonb"]["refs"]
    assert mcp_connector_oauth.MCP_OAUTH_REFRESH_TOKEN_SECRET in saved
