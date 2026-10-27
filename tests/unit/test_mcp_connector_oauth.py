"""Tests for MCP connector OAuth 2.1 support."""

from __future__ import annotations

import base64
import io
import urllib.error
from contextlib import contextmanager
from copy import deepcopy
from typing import Any
from unittest.mock import MagicMock
from urllib.parse import parse_qs, quote_plus, urlparse

import pytest

from core.application.auth import (
    mcp_connector_oauth,
    mcp_connector_oauth_policy,
    mcp_connector_oauth_provider,
    mcp_connector_oauth_repository,
    mcp_connector_oauth_rpc,
    mcp_connector_oauth_tokens,
    oauth_client,
)
from core.application.auth.oauth_client import OAuthConnectionError
from core.application.management import mcp_connector_http
from core.application.management.errors import (
    ServiceUnavailableError,
    ServiceValidationError,
)
from tests.fixtures.mcp_streamable_http import modern_mcp_server


@pytest.fixture(autouse=True)
def _stable_mcp_connector_dns(monkeypatch):
    monkeypatch.setattr(
        "core.application.management.mcp_connector_http._resolve_hostname_ips",
        lambda _hostname, *, port: {"93.184.216.34"},
    )
    monkeypatch.setattr(
        "core.application.extensions.url_safety._resolve_hostname_ips",
        lambda _hostname, *, port: {"93.184.216.34"},
    )


def _connector_row(**overrides: Any) -> dict[str, Any]:
    row = {
        "connector_id": "mcp_alpha",
        "account_id": "acc_alpha",
        "project_id": "project_alpha",
        "tenant_id": "tenant_alpha",
        "display_name": "Alpha MCP",
        "transport": "streamable_http",
        "base_url": "https://mcp.example.test/public/mcp",
        "auth_mode": "oauth2",
        "headers": {},
        "oauth_metadata_jsonb": {},
        "oauth_secret_refs_jsonb": {},
        "oauth_credential_revision": 1,
        "auth_status": "not_configured",
        "enabled": True,
    }
    row.update(overrides)
    return row


def test_parse_www_authenticate_extracts_resource_metadata_and_scope() -> None:
    challenges = mcp_connector_oauth.parse_www_authenticate(
        'Bearer resource_metadata="https://mcp.example.test/.well-known/oauth-protected-resource", '
        'scope="files:read files:write", error="insufficient_scope"'
    )

    assert challenges == [
        {
            "scheme": "bearer",
            "resource_metadata": "https://mcp.example.test/.well-known/oauth-protected-resource",
            "scope": "files:read files:write",
            "error": "insufficient_scope",
        }
    ]


def test_discover_mcp_oauth_uses_challenge_metadata_and_persists(monkeypatch) -> None:
    updates: list[dict[str, Any]] = []
    row = _connector_row()

    monkeypatch.setattr(
        mcp_connector_oauth_repository.McpConnectorStore,
        "update_connector_row",
        lambda _self, _conn, _connector_id, payload, **_kwargs: updates.append(dict(payload)),
    )

    def _fetch_json(url: str, *, purpose: str, timeout_seconds: float = 15.0):
        _ = (purpose, timeout_seconds)
        if "oauth-protected-resource" in url:
            return {
                "resource": "https://mcp.example.test/public/mcp",
                "authorization_servers": ["https://auth.example.test/tenant1"],
                "scopes_supported": ["files:read"],
            }
        if url.endswith("/.well-known/oauth-authorization-server/tenant1"):
            return {
                "issuer": "https://auth.example.test/tenant1",
                "authorization_endpoint": (
                    "https://auth.example.test/authorize?audience=operator&resource=stale"
                ),
                "token_endpoint": "https://auth.example.test/token",
                "code_challenge_methods_supported": ["S256"],
                "client_id_metadata_document_supported": True,
            }
        raise OAuthConnectionError(f"unexpected URL {url}")

    discovery = mcp_connector_oauth.discover_mcp_oauth(
        object(),
        connector_row=row,
        www_authenticate=(
            'Bearer resource_metadata="https://mcp.example.test/.well-known/'
            'oauth-protected-resource", scope="files:read"'
        ),
        fetch_json=_fetch_json,
    )

    assert discovery["issuer"] == "https://auth.example.test/tenant1"
    assert discovery["challenge_scope"] == ["files:read"]
    assert updates[-1]["auth_status"] == "auth_required"
    assert "oauth_secret_refs_jsonb" not in updates[-1]


def test_discover_mcp_oauth_rejects_authorization_server_issuer_mismatch(
    monkeypatch,
) -> None:
    updates: list[dict[str, Any]] = []
    row = _connector_row()

    monkeypatch.setattr(
        mcp_connector_oauth_repository.McpConnectorStore,
        "update_connector_row",
        lambda _self, _conn, _connector_id, payload, **_kwargs: updates.append(dict(payload)),
    )

    def _fetch_json(url: str, *, purpose: str, timeout_seconds: float = 15.0):
        _ = (purpose, timeout_seconds)
        if "oauth-protected-resource" in url:
            return {
                "resource": "https://mcp.example.test/public/mcp",
                "authorization_servers": ["https://auth.example.test/tenant1"],
                "scopes_supported": ["files:read"],
            }
        if "auth.example.test" in url:
            return {
                "issuer": "https://evil.example.test",
                "authorization_endpoint": "https://evil.example.test/authorize",
                "token_endpoint": "https://evil.example.test/token",
                "code_challenge_methods_supported": ["S256"],
            }
        raise OAuthConnectionError(f"unexpected URL {url}")

    _raises_input_149_1 = object()
    with pytest.raises(OAuthConnectionError, match="issuer must match"):
        mcp_connector_oauth.discover_mcp_oauth(
            _raises_input_149_1, connector_row=row, fetch_json=_fetch_json
        )

    assert updates[-1]["auth_status"] == "discovery_required"
    assert "issuer must match" in updates[-1]["oauth_last_error"]


def test_authorization_server_accepts_only_root_trailing_slash_issuer_difference() -> None:
    metadata = {
        "issuer": "https://auth.example.test",
        "authorization_endpoint": "https://auth.example.test/authorize",
        "token_endpoint": "https://auth.example.test/token",
        "code_challenge_methods_supported": ["S256"],
    }

    assert (
        mcp_connector_oauth_policy._validate_authorization_server_metadata(
            metadata,
            expected_issuer="https://auth.example.test/",
        )
        == metadata
    )


@pytest.mark.parametrize(
    "actual_issuer",
    [
        "https://auth.example.test/tenant/",
        "https://auth.example.test/other",
        "https://auth.example.test?tenant=one",
        "https://auth.example.test#fragment",
        "http://auth.example.test/",
        "https://other.example.test/",
        "https://auth.example.test:8443/",
    ],
)
def test_authorization_server_rejects_non_root_or_authority_issuer_mismatch(
    actual_issuer: str,
) -> None:
    with pytest.raises(OAuthConnectionError, match="issuer must match"):
        mcp_connector_oauth_policy._validate_authorization_server_metadata(
            {
                "issuer": actual_issuer,
                "authorization_endpoint": (
                    "https://auth.example.test/authorize?audience=operator&resource=stale"
                ),
                "token_endpoint": "https://auth.example.test/token",
                "code_challenge_methods_supported": ["S256"],
            },
            expected_issuer="https://auth.example.test/tenant",
        )


def test_discover_mcp_oauth_rejects_cross_origin_resource(monkeypatch) -> None:
    updates: list[dict[str, Any]] = []
    row = _connector_row()

    monkeypatch.setattr(
        mcp_connector_oauth_repository.McpConnectorStore,
        "update_connector_row",
        lambda _self, _conn, _connector_id, payload, **_kwargs: updates.append(dict(payload)),
    )

    def _fetch_json(url: str, *, purpose: str, timeout_seconds: float = 15.0):
        _ = (purpose, timeout_seconds)
        if "oauth-protected-resource" in url or url == "https://mcp.example.test/prm":
            return {
                "resource": "https://graph.microsoft.com",
                "authorization_servers": ["https://auth.example.test"],
                "scopes_supported": ["files:read"],
            }
        raise AssertionError("authorization server discovery must not run for invalid resource")

    _raises_input_226_1 = object()
    with pytest.raises(OAuthConnectionError, match="must exactly match"):
        mcp_connector_oauth.discover_mcp_oauth(
            _raises_input_226_1,
            connector_row=row,
            www_authenticate='Bearer resource_metadata="https://mcp.example.test/prm"',
            fetch_json=_fetch_json,
        )

    assert updates[-1]["auth_status"] == "discovery_required"
    assert "must exactly match" in updates[-1]["oauth_last_error"]


def test_discover_mcp_oauth_rejects_same_origin_sibling_resource_path(monkeypatch) -> None:
    updates: list[dict[str, Any]] = []
    row = _connector_row(base_url="https://mcp.example.test/tenant-a/mcp")

    monkeypatch.setattr(
        mcp_connector_oauth_repository.McpConnectorStore,
        "update_connector_row",
        lambda _self, _conn, _connector_id, payload, **_kwargs: updates.append(dict(payload)),
    )

    def _fetch_json(url: str, *, purpose: str, timeout_seconds: float = 15.0):
        _ = (purpose, timeout_seconds)
        if "oauth-protected-resource" in url or url == "https://mcp.example.test/prm":
            return {
                "resource": "https://mcp.example.test/tenant-b/mcp",
                "authorization_servers": ["https://auth.example.test"],
                "scopes_supported": ["files:read"],
            }
        raise AssertionError("authorization server discovery must not run for invalid resource")

    _raises_input_258_1 = object()
    with pytest.raises(OAuthConnectionError, match="must exactly match"):
        mcp_connector_oauth.discover_mcp_oauth(
            _raises_input_258_1,
            connector_row=row,
            www_authenticate='Bearer resource_metadata="https://mcp.example.test/prm"',
            fetch_json=_fetch_json,
        )

    assert updates[-1]["auth_status"] == "discovery_required"
    assert "must exactly match" in updates[-1]["oauth_last_error"]


def test_mcp_resource_validation_requires_exact_expected_resource() -> None:
    base_url = "https://mcp.example.test/tenant-a/mcp"
    expected = "https://mcp.example.test/tenant-a/mcp"

    assert (
        mcp_connector_oauth_policy._validated_mcp_resource(
            "https://mcp.example.test/tenant-a/mcp",
            expected_resource=expected,
        )
        == expected
    )
    for invalid_resource in [
        "https://mcp.example.test",
        "https://mcp.example.test/tenant-a",
        "https://mcp.example.test/tenant-a/mcp?aud=other",
        "https://mcp.example.test/tenant-b/mcp",
    ]:
        with pytest.raises(OAuthConnectionError, match="must exactly match"):
            mcp_connector_oauth_policy._validated_mcp_resource(
                invalid_resource,
                expected_resource=base_url,
            )


def test_discover_mcp_oauth_root_well_known_accepts_origin_resource(monkeypatch) -> None:
    updates: list[dict[str, Any]] = []
    row = _connector_row()
    fetched_urls: list[str] = []

    monkeypatch.setattr(
        mcp_connector_oauth_repository.McpConnectorStore,
        "update_connector_row",
        lambda _self, _conn, _connector_id, payload, **_kwargs: updates.append(dict(payload)),
    )

    def _fetch_json(url: str, *, purpose: str, timeout_seconds: float = 15.0):
        _ = (purpose, timeout_seconds)
        fetched_urls.append(url)
        if url.endswith("oauth-protected-resource/public/mcp"):
            raise OAuthConnectionError("endpoint metadata unavailable")
        if url.endswith("oauth-protected-resource"):
            return {
                "resource": "https://mcp.example.test",
                "authorization_servers": ["https://auth.example.test"],
                "scopes_supported": ["files:read"],
            }
        if url.endswith("/.well-known/oauth-authorization-server"):
            return {
                "issuer": "https://auth.example.test",
                "authorization_endpoint": "https://auth.example.test/authorize",
                "token_endpoint": "https://auth.example.test/token",
                "code_challenge_methods_supported": ["S256"],
                "client_id_metadata_document_supported": True,
            }
        raise OAuthConnectionError(f"unexpected URL {url}")

    discovery = mcp_connector_oauth.discover_mcp_oauth(
        object(),
        connector_row=row,
        fetch_json=_fetch_json,
    )

    assert fetched_urls[:2] == [
        "https://mcp.example.test/.well-known/oauth-protected-resource/public/mcp",
        "https://mcp.example.test/.well-known/oauth-protected-resource",
    ]
    assert discovery["resource"] == "https://mcp.example.test"


def test_start_mcp_connector_oauth_cimd_stores_state_and_resource(monkeypatch) -> None:
    monkeypatch.setenv("FS_SECRET_KEY", "state-secret")
    monkeypatch.setenv("ENCRYPTION_KEY", "encryption-secret")
    monkeypatch.setattr(
        "core.application.auth.serve_auth_runtime.public_base_url",
        lambda: "https://flow.example.test",
    )
    monkeypatch.setattr(
        mcp_connector_oauth,
        "_validate_metadata_url",
        lambda url, *, purpose: url,
    )
    monkeypatch.setattr(
        mcp_connector_oauth_policy,
        "_validate_metadata_url",
        mcp_connector_oauth._validate_metadata_url,
    )
    monkeypatch.setattr(
        mcp_connector_oauth_provider,
        "_validate_metadata_url",
        mcp_connector_oauth._validate_metadata_url,
    )
    monkeypatch.setattr(
        mcp_connector_oauth,
        "_probe_mcp_oauth_www_authenticate",
        lambda _connector_row: "",
    )
    updates: list[dict[str, Any]] = []
    stored_state: dict[str, Any] = {}
    sweep_calls: list[bool] = []
    row = _connector_row()

    monkeypatch.setattr(
        mcp_connector_oauth_repository.McpConnectorStore,
        "update_connector_row",
        lambda _self, _conn, _connector_id, payload, **_kwargs: updates.append(dict(payload)),
    )
    monkeypatch.setattr(
        mcp_connector_oauth.mcp_connector_oauth_store,
        "insert_oauth_state",
        lambda _conn, **kwargs: stored_state.update(kwargs),
    )
    monkeypatch.setattr(
        mcp_connector_oauth.mcp_connector_oauth_store,
        "delete_expired_oauth_states",
        lambda _conn: sweep_calls.append(True) or 0,
    )

    def _fetch_json(url: str, *, purpose: str, timeout_seconds: float = 15.0):
        _ = (purpose, timeout_seconds)
        if "oauth-protected-resource/public/mcp" in url:
            return {
                "resource": "https://mcp.example.test/public/mcp",
                "authorization_servers": ["https://auth.example.test"],
                "scopes_supported": ["files:read"],
            }
        if url.endswith("/.well-known/oauth-authorization-server"):
            return {
                "issuer": "https://auth.example.test",
                "authorization_endpoint": (
                    "https://auth.example.test/authorize?audience=operator&resource=stale"
                ),
                "token_endpoint": "https://auth.example.test/token",
                "code_challenge_methods_supported": ["S256"],
                "client_id_metadata_document_supported": True,
            }
        raise OAuthConnectionError(f"unexpected URL {url}")

    result = mcp_connector_oauth.start_mcp_connector_oauth(
        object(),
        connector_row=row,
        fetch_json=_fetch_json,
        now=1000.0,
    )

    assert result["registration_mode"] == "client_id_metadata_document"
    assert sweep_calls == [True]
    assert "resource=https%3A%2F%2Fmcp.example.test%2Fpublic%2Fmcp" in result["authorization_url"]
    authorization_query = parse_qs(urlparse(result["authorization_url"]).query)
    assert authorization_query["audience"] == ["operator"]
    assert authorization_query["resource"] == ["https://mcp.example.test/public/mcp"]
    assert stored_state["connector_id"] == "mcp_alpha"
    assert stored_state["auth_payload"]["auth_config"]["resource"] == (
        "https://mcp.example.test/public/mcp"
    )
    assert stored_state["auth_payload"]["auth_config"]["connector_endpoint"] == (
        "https://mcp.example.test/public/mcp"
    )
    assert stored_state["auth_payload"]["connector_endpoint"] == (
        "https://mcp.example.test/public/mcp"
    )
    metadata_update = next(update for update in updates if "oauth_metadata_jsonb" in update)
    assert "discovery" in metadata_update["oauth_metadata_jsonb"]
    assert updates[-1]["oauth_credential_revision"] == 2
    assert stored_state["oauth_credential_revision"] == 2
    registration_update = next(
        update for update in updates if "registration" in update.get("oauth_metadata_jsonb", {})
    )
    assert registration_update["oauth_metadata_jsonb"]["registration"]["mode"] == (
        "client_id_metadata_document"
    )


def test_second_oauth_start_invalidates_the_first_attempt(monkeypatch) -> None:
    monkeypatch.setenv("FS_SECRET_KEY", "latest-attempt-state-key")
    monkeypatch.setenv("ENCRYPTION_KEY", "latest-attempt-encryption-key")
    discovery = {
        "issuer": "https://auth.example.test",
        "resource": "https://mcp.example.test/public/mcp",
        "authorization_server": {
            "authorization_endpoint": "https://auth.example.test/authorize",
            "token_endpoint": "https://auth.example.test/token",
        },
    }
    row = _connector_row(
        oauth_credential_revision=1,
        oauth_metadata_jsonb={
            "requested_scopes": ["catalog.read"],
            "discovery": discovery,
        },
    )
    states: list[dict[str, Any]] = []
    deleted_revisions: list[int] = []
    monkeypatch.setattr(mcp_connector_oauth, "_selected_discovery", lambda *_args: discovery)
    monkeypatch.setattr(
        mcp_connector_oauth,
        "_register_client",
        lambda *_args, **_kwargs: {
            "mode": "manual",
            "client_id": "client-id",
            "token_endpoint_auth_method": "none",
        },
    )
    monkeypatch.setattr(mcp_connector_oauth, "_locked_connector_row", lambda *_args: row)
    monkeypatch.setattr(
        mcp_connector_oauth_provider,
        "_locked_connector_row",
        mcp_connector_oauth._locked_connector_row,
    )
    monkeypatch.setattr(
        mcp_connector_oauth_repository,
        "_locked_connector_row",
        mcp_connector_oauth._locked_connector_row,
    )
    monkeypatch.setattr(
        mcp_connector_oauth_repository.McpConnectorStore,
        "update_connector_row",
        lambda _self, _conn, _connector_id, payload, **_kwargs: row.update(payload),
    )
    monkeypatch.setattr(
        mcp_connector_oauth.mcp_connector_oauth_store,
        "delete_connector_oauth_states",
        lambda *_args, **_kwargs: (
            deleted_revisions.append(row["oauth_credential_revision"]) or states.clear()
        ),
    )
    monkeypatch.setattr(
        mcp_connector_oauth.mcp_connector_oauth_store,
        "insert_oauth_state",
        lambda _conn, **kwargs: states.append(dict(kwargs)),
    )
    monkeypatch.setattr(
        mcp_connector_oauth.mcp_connector_oauth_store,
        "delete_expired_oauth_states",
        lambda *_args: 0,
    )
    monkeypatch.setattr(
        mcp_connector_oauth,
        "_oauth_callback_url",
        lambda: "https://flow.example.test/api/integrations/oauth/callback",
    )

    class ManagedConnection:
        autocommit = True

        @contextmanager
        def transaction(self):
            yield

        def cursor(self):
            raise AssertionError("all persistence calls are mocked")

    connection = ManagedConnection()

    first = mcp_connector_oauth.start_mcp_connector_oauth(
        connection, connector_row=row, now=1_000.0
    )
    first_state_hash = states[0]["state_hash"]
    second = mcp_connector_oauth.start_mcp_connector_oauth(
        connection, connector_row=row, now=1_001.0
    )

    assert row["oauth_credential_revision"] == 3
    assert deleted_revisions == [2, 3]
    assert len(states) == 1
    assert states[0]["oauth_credential_revision"] == 3
    assert states[0]["state_hash"] != first_state_hash
    assert first["authorization_url"] != second["authorization_url"]


def test_start_mcp_connector_oauth_uses_initial_401_challenge_metadata(monkeypatch) -> None:
    monkeypatch.setenv("FS_SECRET_KEY", "state-secret")
    monkeypatch.setenv("ENCRYPTION_KEY", "encryption-secret")
    monkeypatch.setattr(
        "core.application.auth.serve_auth_runtime.public_base_url",
        lambda: "https://flow.example.test",
    )
    monkeypatch.setattr(
        mcp_connector_oauth,
        "_validate_metadata_url",
        lambda url, *, purpose: url,
    )
    monkeypatch.setattr(
        mcp_connector_oauth_policy,
        "_validate_metadata_url",
        mcp_connector_oauth._validate_metadata_url,
    )
    monkeypatch.setattr(
        mcp_connector_oauth_provider,
        "_validate_metadata_url",
        mcp_connector_oauth._validate_metadata_url,
    )
    updates: list[dict[str, Any]] = []
    stored_state: dict[str, Any] = {}
    probe_calls: list[dict[str, Any]] = []
    fetched_urls: list[str] = []
    row = _connector_row()

    monkeypatch.setattr(
        mcp_connector_oauth_repository.McpConnectorStore,
        "update_connector_row",
        lambda _self, _conn, _connector_id, payload, **_kwargs: updates.append(dict(payload)),
    )
    monkeypatch.setattr(
        mcp_connector_oauth.mcp_connector_oauth_store,
        "insert_oauth_state",
        lambda _conn, **kwargs: stored_state.update(kwargs),
    )

    def _post_with_latency(*, url, headers, payload, timeout_seconds):
        probe_calls.append(
            {
                "url": url,
                "headers": headers,
                "payload": payload,
                "timeout_seconds": timeout_seconds,
            }
        )
        http_error = urllib.error.HTTPError(
            url,
            401,
            "Unauthorized",
            hdrs={
                "WWW-Authenticate": (
                    'Bearer resource_metadata="https://mcp.example.test/prm", '
                    'scope="files:read files:write"'
                )
            },
            fp=io.BytesIO(b""),
        )
        raise ServiceUnavailableError("MCP request failed with HTTP 401") from http_error

    monkeypatch.setattr(mcp_connector_oauth_rpc, "_json_rpc_post_with_latency", _post_with_latency)

    def _fetch_json(url: str, *, purpose: str, timeout_seconds: float = 15.0):
        _ = (purpose, timeout_seconds)
        fetched_urls.append(url)
        if url == "https://mcp.example.test/prm":
            return {
                "resource": "https://mcp.example.test/public/mcp",
                "authorization_servers": ["https://auth.example.test"],
                "scopes_supported": ["files:read", "files:write"],
            }
        if url.endswith("/.well-known/oauth-authorization-server"):
            return {
                "issuer": "https://auth.example.test",
                "authorization_endpoint": "https://auth.example.test/authorize",
                "token_endpoint": "https://auth.example.test/token",
                "code_challenge_methods_supported": ["S256"],
                "client_id_metadata_document_supported": True,
            }
        raise OAuthConnectionError(f"unexpected URL {url}")

    result = mcp_connector_oauth.start_mcp_connector_oauth(
        object(),
        connector_row=row,
        fetch_json=_fetch_json,
        now=1000.0,
    )

    assert probe_calls == [
        {
            "url": "https://mcp.example.test/public/mcp",
            "headers": {},
            "payload": {
                "jsonrpc": "2.0",
                "id": "oauth-discovery",
                "method": "tools/list",
                "params": {},
            },
            "timeout_seconds": 10.0,
        }
    ]
    assert fetched_urls[0] == "https://mcp.example.test/prm"
    assert result["resource"] == "https://mcp.example.test/public/mcp"
    assert result["scopes"] == ["files:read", "files:write"]
    assert "resource=https%3A%2F%2Fmcp.example.test%2Fpublic%2Fmcp" in result["authorization_url"]
    assert stored_state["auth_payload"]["auth_config"]["resource"] == (
        "https://mcp.example.test/public/mcp"
    )
    assert stored_state["auth_payload"]["auth_config"]["connector_endpoint"] == (
        "https://mcp.example.test/public/mcp"
    )
    discovery_update = next(
        update for update in updates if "discovery" in update.get("oauth_metadata_jsonb", {})
    )
    assert discovery_update["oauth_metadata_jsonb"]["discovery"]["connector_endpoint"] == (
        "https://mcp.example.test/public/mcp"
    )


def test_start_mcp_connector_oauth_rejects_stale_manual_registration_before_state(
    monkeypatch,
) -> None:
    monkeypatch.setenv("FS_SECRET_KEY", "state-secret")
    secret_values = {
        mcp_connector_oauth.MCP_OAUTH_CLIENT_ID_SECRET: "old-client",
        mcp_connector_oauth.MCP_OAUTH_CLIENT_SECRET_SECRET: "old-secret",
    }
    row = _connector_row(
        oauth_metadata_jsonb={
            "discovery": {
                "connector_endpoint": "https://mcp.example.test/public/mcp",
                "resource": "https://mcp.example.test/public/mcp",
                "issuer": "https://auth-b.example.test",
                "authorization_server": {
                    "issuer": "https://auth-b.example.test",
                    "authorization_endpoint": "https://auth-b.example.test/authorize",
                    "token_endpoint": "https://auth-b.example.test/token",
                    "code_challenge_methods_supported": ["S256"],
                },
            },
            "registration": {
                "mode": "manual",
                "issuer": "https://auth-a.example.test",
                "token_endpoint_auth_method": "client_secret_post",
            },
        },
        oauth_secret_refs_jsonb={
            "refs": {name: f"mcp_connector:mcp_alpha:{name}" for name in secret_values}
        },
    )

    monkeypatch.setattr(
        mcp_connector_oauth_provider,
        "_resolve_connector_oauth_secrets",
        lambda _conn, _row: dict(secret_values),
    )
    monkeypatch.setattr(
        mcp_connector_oauth_repository.McpConnectorStore,
        "get_connector_row",
        lambda _self, _conn, _connector_id, **_kwargs: row,
    )
    monkeypatch.setattr(
        mcp_connector_oauth_repository.McpConnectorStore,
        "update_connector_row",
        lambda _self, _conn, _connector_id, payload, **_kwargs: row.update(payload),
    )
    monkeypatch.setattr(
        mcp_connector_oauth.mcp_connector_oauth_store,
        "insert_oauth_state",
        lambda _conn, **kwargs: pytest.fail("stale registration must not create OAuth state"),
    )

    _raises_input_683_1 = object()

    def _raises_input_683_2(*args, **kwargs):
        return pytest.fail("manual stale secret must not use DCR")

    with pytest.raises(OAuthConnectionError, match="different authorization server"):
        mcp_connector_oauth.start_mcp_connector_oauth(
            _raises_input_683_1, connector_row=row, post_json=_raises_input_683_2, now=1000.0
        )

    assert row["auth_status"] == "auth_required"
    assert "authorization server" in row["oauth_last_error"]


def test_start_mcp_connector_oauth_reregisters_stale_dcr_without_reusing_secret(
    monkeypatch,
) -> None:
    monkeypatch.setenv("FS_SECRET_KEY", "state-secret")
    monkeypatch.setenv("ENCRYPTION_KEY", "encryption-secret")
    monkeypatch.setattr(
        "core.application.auth.serve_auth_runtime.public_base_url",
        lambda: "https://flow.example.test",
    )
    monkeypatch.setattr(
        mcp_connector_oauth,
        "_validate_metadata_url",
        lambda url, *, purpose: url,
    )
    monkeypatch.setattr(
        mcp_connector_oauth_policy,
        "_validate_metadata_url",
        mcp_connector_oauth._validate_metadata_url,
    )
    monkeypatch.setattr(
        mcp_connector_oauth_provider,
        "_validate_metadata_url",
        mcp_connector_oauth._validate_metadata_url,
    )
    secret_values = {
        mcp_connector_oauth.MCP_OAUTH_CLIENT_ID_SECRET: "old-dcr-client",
        mcp_connector_oauth.MCP_OAUTH_CLIENT_SECRET_SECRET: "old-dcr-secret",
        mcp_connector_oauth.MCP_OAUTH_REGISTRATION_ACCESS_TOKEN_SECRET: "old-registration-token",
    }
    saved_secrets: dict[str, Any] = {}
    deleted_refs: list[str] = []
    stored_state: dict[str, Any] = {}
    row = _connector_row(
        oauth_metadata_jsonb={
            "discovery": {
                "connector_endpoint": "https://mcp.example.test/public/mcp",
                "resource": "https://mcp.example.test/public/mcp",
                "issuer": "https://auth-b.example.test",
                "authorization_server": {
                    "issuer": "https://auth-b.example.test",
                    "authorization_endpoint": "https://auth-b.example.test/authorize",
                    "token_endpoint": "https://auth-b.example.test/token",
                    "registration_endpoint": "https://auth-b.example.test/register",
                    "client_id_metadata_document_supported": True,
                    "code_challenge_methods_supported": ["S256"],
                },
            },
            "registration": {
                "mode": "dynamic_client_registration",
                "issuer": "https://auth-a.example.test",
                "token_endpoint_auth_method": "client_secret_post",
            },
        },
        oauth_secret_refs_jsonb={
            "refs": {name: f"mcp_connector:mcp_alpha:{name}" for name in secret_values}
        },
    )

    monkeypatch.setattr(
        mcp_connector_oauth_provider,
        "_resolve_connector_oauth_secrets",
        lambda _conn, _row: dict(secret_values),
    )
    monkeypatch.setattr(
        mcp_connector_oauth_repository.McpConnectorStore,
        "get_connector_row",
        lambda _self, _conn, _connector_id, **_kwargs: row,
    )
    monkeypatch.setattr(
        mcp_connector_oauth_repository.McpConnectorStore,
        "update_connector_row",
        lambda _self, _conn, _connector_id, payload, **_kwargs: row.update(payload),
    )
    monkeypatch.setattr(
        mcp_connector_oauth.mcp_connector_oauth_store,
        "insert_oauth_state",
        lambda _conn, **kwargs: stored_state.update(kwargs),
    )

    def _save(_conn, *, account_id, connector_id, secrets_to_save, **_kwargs):
        _ = (account_id, connector_id)
        saved_secrets.update(secrets_to_save)
        return {"refs": {name: f"mcp_connector:mcp_alpha:{name}" for name in secrets_to_save}}

    monkeypatch.setattr(mcp_connector_oauth_provider, "_save_connector_oauth_secrets", _save)
    monkeypatch.setattr(
        mcp_connector_oauth.model_provider_store,
        "delete_secret_ref",
        lambda _conn, *, ref_key, **_kwargs: deleted_refs.append(ref_key),
    )
    monkeypatch.setattr(
        mcp_connector_oauth.mcp_connector_oauth_store,
        "delete_connector_oauth_states",
        lambda *_args, **_kwargs: 0,
    )

    def _post_json(url: str, payload: dict[str, Any], *, purpose: str):
        _ = purpose
        assert url == "https://auth-b.example.test/register"
        assert "old-dcr-secret" not in str(payload)
        return {
            "client_id": "new-dcr-client",
            "token_endpoint_auth_method": "none",
        }

    result = mcp_connector_oauth.start_mcp_connector_oauth(
        object(),
        connector_row=row,
        post_json=_post_json,
        now=1000.0,
    )

    assert result["registration_mode"] == "dynamic_client_registration"
    assert "client_id=new-dcr-client" in result["authorization_url"]
    assert stored_state["auth_payload"]["client_id"] == "new-dcr-client"
    assert stored_state["auth_payload"]["auth_config"]["token_endpoint"] == (
        "https://auth-b.example.test/token"
    )
    assert mcp_connector_oauth.MCP_OAUTH_CLIENT_SECRET_SECRET not in saved_secrets
    assert sorted(deleted_refs) == sorted(
        f"mcp_connector:mcp_alpha:{name}" for name in secret_values
    )
    assert row["oauth_secret_refs_jsonb"] == {
        "refs": {
            mcp_connector_oauth.MCP_OAUTH_CLIENT_ID_SECRET: (
                "mcp_connector:mcp_alpha:_oauth_client_id"
            )
        }
    }
    assert row["oauth_metadata_jsonb"]["registration"]["issuer"] == ("https://auth-b.example.test")


def test_start_rejects_discovery_result_after_concurrent_credential_revision(
    monkeypatch,
) -> None:
    monkeypatch.setenv("FS_SECRET_KEY", "discovery-race-state-key")
    monkeypatch.setenv("ENCRYPTION_KEY", "discovery-race-encryption-key")
    monkeypatch.setattr(
        "core.application.auth.serve_auth_runtime.public_base_url",
        lambda: "https://flow.example.test",
    )
    row = _connector_row(oauth_credential_revision=4, oauth_metadata_jsonb={})
    discovery_completed = False
    writes: list[dict[str, Any]] = []

    def _fetch(url: str, *, purpose: str, timeout_seconds: float = 15.0):
        nonlocal discovery_completed
        _ = (purpose, timeout_seconds)
        if "oauth-protected-resource" in url:
            return {
                "resource": "https://mcp.example.test/public/mcp",
                "authorization_servers": ["https://auth.example.test"],
            }
        discovery_completed = True
        return {
            "issuer": "https://auth.example.test",
            "authorization_endpoint": "https://auth.example.test/authorize",
            "token_endpoint": "https://auth.example.test/token",
            "code_challenge_methods_supported": ["S256"],
            "client_id_metadata_document_supported": True,
        }

    monkeypatch.setattr(
        mcp_connector_oauth,
        "_locked_connector_row",
        lambda _conn, _row: {
            **row,
            "oauth_credential_revision": 5 if discovery_completed else 4,
        },
    )
    monkeypatch.setattr(
        mcp_connector_oauth_provider,
        "_locked_connector_row",
        mcp_connector_oauth._locked_connector_row,
    )
    monkeypatch.setattr(
        mcp_connector_oauth_repository,
        "_locked_connector_row",
        mcp_connector_oauth._locked_connector_row,
    )
    monkeypatch.setattr(
        mcp_connector_oauth_repository.McpConnectorStore,
        "update_connector_row",
        lambda _self, _conn, _connector_id, payload, **_kwargs: writes.append(payload),
    )
    monkeypatch.setattr(
        mcp_connector_oauth.mcp_connector_oauth_store,
        "insert_oauth_state",
        lambda *_args, **_kwargs: pytest.fail("stale discovery must not create OAuth state"),
    )

    _raises_input_866_1 = object()
    with pytest.raises(OAuthConnectionError, match="credential revision changed"):
        mcp_connector_oauth.start_mcp_connector_oauth(
            _raises_input_866_1, connector_row=row, fetch_json=_fetch
        )

    assert writes == []


def test_start_rejects_dcr_result_after_concurrent_credential_revision(monkeypatch) -> None:
    monkeypatch.setenv("FS_SECRET_KEY", "dcr-race-state-key")
    monkeypatch.setenv("ENCRYPTION_KEY", "dcr-race-encryption-key")
    monkeypatch.setattr(
        "core.application.auth.serve_auth_runtime.public_base_url",
        lambda: "https://flow.example.test",
    )
    row = _connector_row(
        oauth_credential_revision=7,
        oauth_metadata_jsonb={
            "discovery": {
                "connector_endpoint": "https://mcp.example.test/public/mcp",
                "resource": "https://mcp.example.test/public/mcp",
                "issuer": "https://auth.example.test",
                "authorization_server": {
                    "issuer": "https://auth.example.test",
                    "authorization_endpoint": "https://auth.example.test/authorize",
                    "token_endpoint": "https://auth.example.test/token",
                    "registration_endpoint": "https://auth.example.test/register",
                    "code_challenge_methods_supported": ["S256"],
                },
            }
        },
    )
    dcr_completed = False
    cleanup = MagicMock()
    monkeypatch.setattr(
        mcp_connector_oauth_provider, "_delete_dynamic_client_registration", cleanup
    )

    def _post_json(*_args, **_kwargs):
        nonlocal dcr_completed
        dcr_completed = True
        return {
            "client_id": "stale-dcr-client",
            "client_secret": "stale-dcr-secret",
            "registration_client_uri": "https://auth.example.test/register/stale-client",
            "registration_access_token": "registration-cleanup-token",
        }

    monkeypatch.setattr(
        mcp_connector_oauth,
        "_locked_connector_row",
        lambda _conn, _row: {
            **row,
            "oauth_credential_revision": 8 if dcr_completed else 7,
        },
    )
    monkeypatch.setattr(
        mcp_connector_oauth_provider,
        "_locked_connector_row",
        mcp_connector_oauth._locked_connector_row,
    )
    monkeypatch.setattr(
        mcp_connector_oauth_repository,
        "_locked_connector_row",
        mcp_connector_oauth._locked_connector_row,
    )
    monkeypatch.setattr(
        mcp_connector_oauth_provider,
        "_save_connector_oauth_secrets",
        lambda *_args, **_kwargs: pytest.fail("stale DCR must not write secret refs"),
    )
    monkeypatch.setattr(
        mcp_connector_oauth_repository.McpConnectorStore,
        "update_connector_row",
        lambda *_args, **_kwargs: pytest.fail("stale DCR must not update connector metadata"),
    )
    monkeypatch.setattr(
        mcp_connector_oauth.mcp_connector_oauth_store,
        "insert_oauth_state",
        lambda *_args, **_kwargs: pytest.fail("stale DCR must not create OAuth state"),
    )

    _raises_input_938_1 = object()
    with pytest.raises(OAuthConnectionError, match="credential revision changed"):
        mcp_connector_oauth.start_mcp_connector_oauth(
            _raises_input_938_1, connector_row=row, post_json=_post_json
        )

    cleanup.assert_called_once_with(
        "https://auth.example.test/register/stale-client",
        "registration-cleanup-token",
    )


@pytest.mark.parametrize("failure_point", ["save", "update", "commit"])
def test_dcr_compensates_remote_client_when_local_persistence_fails(
    monkeypatch,
    failure_point: str,
) -> None:
    discovery = {
        "issuer": "https://auth.example.test",
        "resource": "https://mcp.example.test/public/mcp",
        "connector_endpoint": "https://mcp.example.test/public/mcp",
        "authorization_server": {
            "registration_endpoint": "https://auth.example.test/register",
        },
    }
    row = _connector_row(oauth_metadata_jsonb={"discovery": discovery})
    cleanup = MagicMock()
    monkeypatch.setattr(
        mcp_connector_oauth_provider, "_delete_dynamic_client_registration", cleanup
    )
    monkeypatch.setattr(
        mcp_connector_oauth_provider,
        "_oauth_callback_url",
        lambda: "https://flow.example.test/api/integrations/oauth/callback",
    )
    monkeypatch.setattr(
        mcp_connector_oauth_provider,
        "_locked_connector_row",
        lambda _conn, _row: deepcopy(row),
    )
    monkeypatch.setattr(
        mcp_connector_oauth_repository,
        "_locked_connector_row",
        mcp_connector_oauth_provider._locked_connector_row,
    )
    monkeypatch.setattr(
        mcp_connector_oauth_provider,
        "_resolve_connector_oauth_secrets",
        lambda *_args, **_kwargs: {},
    )

    def _save(*_args, **_kwargs):
        if failure_point == "save":
            raise RuntimeError("secret save failed")
        return {"refs": {"_oauth_client_id": "secret-ref"}}

    def _update(*_args, **_kwargs):
        if failure_point == "update":
            raise RuntimeError("metadata update failed")

    monkeypatch.setattr(mcp_connector_oauth_provider, "_save_connector_oauth_secrets", _save)
    monkeypatch.setattr(mcp_connector_oauth_provider, "_update_connector_oauth", _update)
    monkeypatch.setattr(
        mcp_connector_oauth_repository,
        "_update_connector_oauth",
        mcp_connector_oauth_provider._update_connector_oauth,
    )

    class CommitFailingConnection:
        def __init__(self) -> None:
            self.transaction_count = 0

        @contextmanager
        def transaction(self):
            self.transaction_count += 1
            transaction_number = self.transaction_count
            yield self
            if transaction_number == 1:
                raise RuntimeError("owning commit failed")

    conn: object = CommitFailingConnection() if failure_point == "commit" else object()

    with pytest.raises(RuntimeError, match="failed"):
        mcp_connector_oauth_provider._register_client(
            conn,
            connector_row=row,
            discovery=discovery,
            scopes=["records:read"],
            post_json=lambda *_args, **_kwargs: {
                "client_id": "orphan-candidate",
                "client_secret": "local-secret",
                "registration_client_uri": ("https://auth.example.test/register/orphan-candidate"),
                "registration_access_token": "registration-cleanup-token",
            },
        )

    cleanup.assert_called_once_with(
        "https://auth.example.test/register/orphan-candidate",
        "registration-cleanup-token",
    )


def test_serialized_registration_reuses_single_dynamic_client(monkeypatch) -> None:
    monkeypatch.setattr(
        "core.application.auth.serve_auth_runtime.public_base_url",
        lambda: "https://flow.example.test",
    )
    row = _connector_row(
        oauth_metadata_jsonb={
            "discovery": {
                "issuer": "https://auth.example.test",
                "resource": "https://mcp.example.test/public/mcp",
                "connector_endpoint": "https://mcp.example.test/public/mcp",
                "authorization_server": {
                    "registration_endpoint": "https://auth.example.test/register",
                },
            }
        }
    )
    values: dict[str, str] = {}
    registrations: list[dict[str, Any]] = []
    cleanup = MagicMock()
    monkeypatch.setattr(
        mcp_connector_oauth_provider, "_delete_dynamic_client_registration", cleanup
    )
    monkeypatch.setattr(
        mcp_connector_oauth_provider,
        "_locked_connector_row",
        lambda _conn, _row: deepcopy(row),
    )
    monkeypatch.setattr(
        mcp_connector_oauth_repository,
        "_locked_connector_row",
        mcp_connector_oauth_provider._locked_connector_row,
    )
    monkeypatch.setattr(
        mcp_connector_oauth_provider,
        "_resolve_connector_oauth_secrets",
        lambda *_args, **_kwargs: dict(values),
    )

    def _save(*_args, secrets_to_save, **_kwargs):
        values.update(secrets_to_save)
        return {"refs": {name: f"mcp_connector:mcp_alpha:{name}" for name in secrets_to_save}}

    monkeypatch.setattr(mcp_connector_oauth_provider, "_save_connector_oauth_secrets", _save)
    monkeypatch.setattr(
        mcp_connector_oauth_provider,
        "_update_connector_oauth",
        lambda _conn, _connector_id, *, updates, **_kwargs: row.update(updates),
    )
    monkeypatch.setattr(
        mcp_connector_oauth_repository,
        "_update_connector_oauth",
        mcp_connector_oauth_provider._update_connector_oauth,
    )

    def _post_json(_url, payload, **_kwargs):
        registrations.append(dict(payload))
        return {
            "client_id": "shared-client",
            "client_secret": "shared-secret",
            "registration_client_uri": "https://auth.example.test/register/shared-client",
            "registration_access_token": "registration-management-token",
        }

    discovery = deepcopy(row["oauth_metadata_jsonb"]["discovery"])
    first = mcp_connector_oauth_provider._register_client(
        object(),
        connector_row=row,
        discovery=discovery,
        scopes=["records:read"],
        post_json=_post_json,
    )
    second = mcp_connector_oauth_provider._register_client(
        object(),
        connector_row=row,
        discovery=discovery,
        scopes=["records:read"],
        post_json=_post_json,
    )

    assert len(registrations) == 1
    assert first["client_id"] == second["client_id"] == "shared-client"
    assert values[mcp_connector_oauth.MCP_OAUTH_CLIENT_SECRET_SECRET] == "shared-secret"
    cleanup.assert_not_called()


def test_dcr_payload_includes_application_type(monkeypatch) -> None:
    monkeypatch.setattr(
        mcp_connector_oauth_provider,
        "_oauth_callback_url",
        lambda: "https://flow.example.test/api/integrations/oauth/callback",
    )

    web_payload = mcp_connector_oauth_provider._client_registration_payload(
        _connector_row(),
        scopes=["files:read"],
    )

    assert web_payload["application_type"] == "web"

    monkeypatch.setattr(
        mcp_connector_oauth_provider,
        "_oauth_callback_url",
        lambda: "http://localhost:3000/api/integrations/oauth/callback",
    )

    native_payload = mcp_connector_oauth_provider._client_registration_payload(
        _connector_row(),
        scopes=["files:read"],
    )

    assert native_payload["application_type"] == "native"


def test_cimd_client_metadata_document_is_generic_and_connector_id_based(monkeypatch) -> None:
    monkeypatch.setattr(
        "core.application.auth.serve_auth_runtime.public_base_url",
        lambda: "https://flow.example.test",
    )

    document = mcp_connector_oauth.mcp_client_metadata_document(
        _connector_row(display_name="Tenant Private Name"),
    )

    assert document["client_id"] == (
        "https://flow.example.test/api/mcp/oauth/client-metadata/mcp_alpha.json"
    )
    assert document["client_name"] == "Flow Steward MCP Client"
    assert "Tenant Private Name" not in str(document)


def test_handle_mcp_oauth_callback_rejects_replayed_state(monkeypatch) -> None:
    monkeypatch.setenv("FS_SECRET_KEY", "state-secret")
    state = oauth_client._build_state_token(
        {
            "v": 1,
            "type": mcp_connector_oauth.MCP_OAUTH_STATE_TYPE,
            "connector_id": "mcp_alpha",
            "account_id": "acc_alpha",
            "exp": 1600,
        }
    )
    monkeypatch.setattr(
        mcp_connector_oauth.mcp_connector_oauth_store,
        "consume_oauth_state",
        lambda _conn, *, state_hash: None,
    )
    sweep_calls: list[bool] = []
    monkeypatch.setattr(
        mcp_connector_oauth.mcp_connector_oauth_store,
        "delete_expired_oauth_states",
        lambda _conn: sweep_calls.append(True) or 0,
    )

    _raises_input_1171_1 = object()
    with pytest.raises(OAuthConnectionError, match="expired or was already used"):
        mcp_connector_oauth.handle_mcp_oauth_callback(
            _raises_input_1171_1, state=state, code="auth-code", now=1000.0
        )
    assert sweep_calls == [True]


def test_mcp_oauth_callback_context_returns_only_validated_state_lineage(monkeypatch) -> None:
    monkeypatch.setenv("FS_SECRET_KEY", "state-secret")
    state_payload = {
        "v": 1,
        "type": mcp_connector_oauth.MCP_OAUTH_STATE_TYPE,
        "connector_id": "mcp_alpha",
        "account_id": "acc_alpha",
        "project_id": "project_alpha",
        "tenant_id": "tenant_alpha",
        "oauth_credential_revision": 3,
        "exp": 1600,
    }
    state = oauth_client._build_state_token(state_payload)
    stored_row = {
        **state_payload,
        "return_path": "/registry/mcp/mcp_alpha?project_id=project_alpha",
    }
    monkeypatch.setattr(
        mcp_connector_oauth.mcp_connector_oauth_store,
        "get_oauth_state",
        lambda _conn, *, state_hash: stored_row,
    )

    context = mcp_connector_oauth.mcp_oauth_callback_context(
        object(),
        state=state,
        fallback_return_path="/settings/integrations",
        now=1000.0,
    )

    assert context == {
        "return_path": "/registry/mcp/mcp_alpha?project_id=project_alpha",
        "connector_id": "mcp_alpha",
        "account_id": "acc_alpha",
        "project_id": "project_alpha",
        "tenant_id": "tenant_alpha",
    }


@pytest.mark.parametrize(
    ("mismatch", "state_value", "stored_value"),
    [
        ("project", "project_other", "project_alpha"),
        ("tenant", "tenant_other", "tenant_alpha"),
        ("revision", 2, 1),
    ],
)
def test_handle_mcp_oauth_callback_rejects_signed_vs_stored_lineage_before_exchange(
    monkeypatch,
    mismatch,
    state_value,
    stored_value,
) -> None:
    monkeypatch.setenv("FS_SECRET_KEY", "state-secret")
    state_payload = {
        "v": 1,
        "type": mcp_connector_oauth.MCP_OAUTH_STATE_TYPE,
        "connector_id": "mcp_alpha",
        "account_id": "acc_alpha",
        "project_id": "project_alpha",
        "tenant_id": "tenant_alpha",
        "oauth_credential_revision": 1,
        "exp": 1600,
    }
    state_row = {
        "connector_id": "mcp_alpha",
        "account_id": "acc_alpha",
        "project_id": "project_alpha",
        "tenant_id": "tenant_alpha",
        "oauth_credential_revision": 1,
    }
    key = {
        "project": "project_id",
        "tenant": "tenant_id",
        "revision": "oauth_credential_revision",
    }[mismatch]
    state_payload[key] = state_value
    state_row[key] = stored_value
    state = oauth_client._build_state_token(state_payload)

    monkeypatch.setattr(
        mcp_connector_oauth.mcp_connector_oauth_store,
        "consume_oauth_state",
        lambda _conn, *, state_hash: state_row,
    )
    monkeypatch.setattr(
        mcp_connector_oauth_repository.McpConnectorStore,
        "get_connector_row",
        lambda *_args, **_kwargs: pytest.fail("stored lineage mismatch must fail before lookup"),
    )

    _raises_input_1272_1 = object()

    def _raises_input_1272_2(*args, **kwargs):
        return pytest.fail("token exchange must not run")

    with pytest.raises(OAuthConnectionError, match="does not match"):
        mcp_connector_oauth.handle_mcp_oauth_callback(
            _raises_input_1272_1,
            state=state,
            code="auth-code",
            now=1000.0,
            post_form=_raises_input_1272_2,
        )


def test_handle_mcp_oauth_callback_rejects_current_connector_revision_before_exchange(
    monkeypatch,
) -> None:
    monkeypatch.setenv("FS_SECRET_KEY", "state-secret")
    state_payload = {
        "v": 1,
        "type": mcp_connector_oauth.MCP_OAUTH_STATE_TYPE,
        "connector_id": "mcp_alpha",
        "account_id": "acc_alpha",
        "project_id": "project_alpha",
        "tenant_id": "tenant_alpha",
        "oauth_credential_revision": 1,
        "exp": 1600,
    }
    state = oauth_client._build_state_token(state_payload)
    state_row = {
        **state_payload,
        "code_verifier_encrypted": "unused",
        "redirect_uri": "https://flow.example.test/api/integrations/oauth/callback",
        "auth_payload_jsonb": {},
    }
    connector = _connector_row(oauth_credential_revision=2)
    monkeypatch.setattr(
        mcp_connector_oauth.mcp_connector_oauth_store,
        "consume_oauth_state",
        lambda _conn, *, state_hash: state_row,
    )
    monkeypatch.setattr(
        mcp_connector_oauth_repository.McpConnectorStore,
        "get_connector_row",
        lambda _self, _conn, _connector_id, **_kwargs: connector,
    )
    monkeypatch.setattr(
        mcp_connector_oauth,
        "_resolve_connector_oauth_secrets",
        lambda *_args, **_kwargs: pytest.fail("stale revision must fail before secret lookup"),
    )

    _raises_input_1320_1 = object()

    def _raises_input_1320_2(*args, **kwargs):
        return pytest.fail("token exchange must not run")

    with pytest.raises(OAuthConnectionError, match="credentials changed"):
        mcp_connector_oauth.handle_mcp_oauth_callback(
            _raises_input_1320_1,
            state=state,
            code="auth-code",
            now=1000.0,
            post_form=_raises_input_1320_2,
        )


def test_handle_mcp_oauth_callback_rejects_changed_endpoint_before_exchange(
    monkeypatch,
) -> None:
    monkeypatch.setenv("FS_SECRET_KEY", "state-secret")
    state = oauth_client._build_state_token(
        {
            "v": 1,
            "type": mcp_connector_oauth.MCP_OAUTH_STATE_TYPE,
            "connector_id": "mcp_alpha",
            "account_id": "acc_alpha",
            "project_id": "project_alpha",
            "tenant_id": "tenant_alpha",
            "oauth_credential_revision": 1,
            "connector_endpoint": "https://mcp.example.test/public/mcp",
            "resource": "https://mcp.example.test",
            "issuer": "https://auth.example.test",
            "exp": 1600,
        }
    )
    connector = _connector_row(
        base_url="https://mcp-new.example.test/public/mcp",
        oauth_metadata_jsonb={},
        auth_status="auth_required",
    )
    state_row = {
        "connector_id": "mcp_alpha",
        "account_id": "acc_alpha",
        "project_id": "project_alpha",
        "tenant_id": "tenant_alpha",
        "oauth_credential_revision": 1,
        "code_verifier_encrypted": "unused",
        "redirect_uri": "https://flow.example.test/api/integrations/oauth/callback",
        "return_path": "/registry/mcp/mcp_alpha?project_id=project_alpha",
        "auth_payload_jsonb": {
            "auth_config": {
                "authorization_endpoint": "https://auth.example.test/authorize",
                "token_endpoint": "https://auth.example.test/token",
                "connector_endpoint": "https://mcp.example.test/public/mcp",
                "resource": "https://mcp.example.test",
                "issuer": "https://auth.example.test",
                "token_endpoint_auth_method": "none",
            },
            "client_id": "client-1",
            "scopes": ["files:read"],
        },
    }

    monkeypatch.setattr(
        mcp_connector_oauth.mcp_connector_oauth_store,
        "consume_oauth_state",
        lambda _conn, *, state_hash: state_row,
    )
    monkeypatch.setattr(
        mcp_connector_oauth_repository.McpConnectorStore,
        "get_connector_row",
        lambda _self, _conn, _connector_id, **_kwargs: connector,
    )
    monkeypatch.setattr(
        mcp_connector_oauth_repository.McpConnectorStore,
        "update_connector_row",
        lambda _self, _conn, _connector_id, payload, **_kwargs: connector.update(payload),
    )

    _raises_input_1393_1 = object()

    def _raises_input_1393_2(*args, **kwargs):
        return pytest.fail("token exchange must not run")

    with pytest.raises(OAuthConnectionError, match="endpoint changed"):
        mcp_connector_oauth.handle_mcp_oauth_callback(
            _raises_input_1393_1,
            state=state,
            code="auth-code",
            now=1000.0,
            post_form=_raises_input_1393_2,
        )

    assert connector["auth_status"] == "auth_required"
    assert "endpoint changed" in connector["oauth_last_error"]


def test_handle_mcp_oauth_callback_rejects_stale_registration_before_token_post(
    monkeypatch,
) -> None:
    monkeypatch.setenv("FS_SECRET_KEY", "state-secret")
    state = oauth_client._build_state_token(
        {
            "v": 1,
            "type": mcp_connector_oauth.MCP_OAUTH_STATE_TYPE,
            "connector_id": "mcp_alpha",
            "account_id": "acc_alpha",
            "project_id": "project_alpha",
            "tenant_id": "tenant_alpha",
            "oauth_credential_revision": 1,
            "connector_endpoint": "https://mcp.example.test/public/mcp",
            "resource": "https://mcp.example.test/public/mcp",
            "issuer": "https://auth-b.example.test",
            "exp": 1600,
        }
    )
    connector = _connector_row(
        oauth_metadata_jsonb={
            "discovery": {
                "connector_endpoint": "https://mcp.example.test/public/mcp",
                "resource": "https://mcp.example.test/public/mcp",
                "issuer": "https://auth-b.example.test",
                "authorization_server": {
                    "issuer": "https://auth-b.example.test",
                    "authorization_endpoint": "https://auth-b.example.test/authorize",
                    "token_endpoint": "https://auth-b.example.test/token",
                    "code_challenge_methods_supported": ["S256"],
                },
            },
            "registration": {
                "mode": "manual",
                "issuer": "https://auth-a.example.test",
                "token_endpoint_auth_method": "client_secret_post",
            },
        },
        oauth_secret_refs_jsonb={
            "refs": {
                mcp_connector_oauth.MCP_OAUTH_CLIENT_ID_SECRET: (
                    "mcp_connector:mcp_alpha:_oauth_client_id"
                ),
                mcp_connector_oauth.MCP_OAUTH_CLIENT_SECRET_SECRET: (
                    "mcp_connector:mcp_alpha:_oauth_client_secret"
                ),
            }
        },
        auth_status="auth_required",
    )
    state_row = {
        "connector_id": "mcp_alpha",
        "account_id": "acc_alpha",
        "project_id": "project_alpha",
        "tenant_id": "tenant_alpha",
        "oauth_credential_revision": 1,
        "code_verifier_encrypted": "unused",
        "redirect_uri": "https://flow.example.test/api/integrations/oauth/callback",
        "return_path": "/registry/mcp/mcp_alpha?project_id=project_alpha",
        "auth_payload_jsonb": {
            "auth_config": {
                "authorization_endpoint": "https://auth-b.example.test/authorize",
                "token_endpoint": "https://auth-b.example.test/token",
                "connector_endpoint": "https://mcp.example.test/public/mcp",
                "resource": "https://mcp.example.test/public/mcp",
                "issuer": "https://auth-b.example.test",
                "token_endpoint_auth_method": "client_secret_post",
            },
            "client_id": "old-client",
            "scopes": ["files:read"],
        },
    }

    monkeypatch.setattr(
        mcp_connector_oauth.mcp_connector_oauth_store,
        "consume_oauth_state",
        lambda _conn, *, state_hash: state_row,
    )
    monkeypatch.setattr(
        mcp_connector_oauth_repository.McpConnectorStore,
        "get_connector_row",
        lambda _self, _conn, _connector_id, **_kwargs: connector,
    )
    monkeypatch.setattr(
        mcp_connector_oauth_repository.McpConnectorStore,
        "update_connector_row",
        lambda _self, _conn, _connector_id, payload, **_kwargs: connector.update(payload),
    )
    monkeypatch.setattr(
        mcp_connector_oauth,
        "_resolve_connector_oauth_secrets",
        lambda _conn, _row: pytest.fail("stale registration must not resolve old secrets"),
    )

    _raises_input_1500_1 = object()

    def _raises_input_1500_2(*args, **kwargs):
        return pytest.fail("token exchange must not run")

    with pytest.raises(OAuthConnectionError, match="different authorization server"):
        mcp_connector_oauth.handle_mcp_oauth_callback(
            _raises_input_1500_1,
            state=state,
            code="auth-code",
            now=1000.0,
            post_form=_raises_input_1500_2,
        )

    assert connector["auth_status"] == "auth_required"
    assert "authorization server" in connector["oauth_last_error"]


def test_mcp_connector_rpc_post_injects_oauth_bearer(monkeypatch) -> None:
    clients: list[Any] = []
    row = _connector_row()

    monkeypatch.setattr(
        mcp_connector_oauth_rpc,
        "ensure_valid_mcp_connector_access_token",
        lambda _conn, *, connector_row, force_refresh=False: "access-token",
    )

    class _Client:
        def __init__(self, *, url, timeout_seconds, headers_provider, **_kwargs):
            self.url = url
            self.timeout_seconds = timeout_seconds
            self.headers_provider = headers_provider
            self.headers: list[dict[str, str]] = []
            self.requests: list[tuple[str, dict[str, Any] | None, str]] = []
            self.close_calls = 0
            clients.append(self)

        def request(self, method, params, *, name=""):
            self.headers.append(self.headers_provider())
            self.requests.append((method, params, name))
            return {"jsonrpc": "2.0", "id": 1, "result": {"ok": True}}, 12

        def close(self):
            self.close_calls += 1

    monkeypatch.setattr(mcp_connector_oauth_rpc, "StreamableHttpMcpClient", _Client)
    monkeypatch.setattr(
        mcp_connector_oauth_repository.McpConnectorStore,
        "get_connector_row",
        lambda _self, _conn, _connector_id, **_kwargs: row,
    )

    payload, latency = mcp_connector_oauth.mcp_connector_rpc_post_with_latency(
        object(),
        connector_row=row,
        payload={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        timeout_seconds=5.0,
    )

    assert payload["result"] == {"ok": True}
    assert latency == 12
    assert len(clients) == 1
    assert clients[0].url == "https://mcp.example.test/public/mcp"
    assert clients[0].timeout_seconds == 5.0
    assert clients[0].headers == [{"Authorization": "Bearer access-token"}]
    assert clients[0].requests == [("tools/list", None, "")]
    assert clients[0].close_calls == 1


def test_mcp_connector_rpc_context_reuses_one_authenticated_short_lived_client(
    monkeypatch,
) -> None:
    clients: list[Any] = []
    row = _connector_row()

    monkeypatch.setattr(
        mcp_connector_oauth_rpc,
        "ensure_valid_mcp_connector_access_token",
        lambda _conn, *, connector_row, force_refresh=False: "access-token",
    )

    class _Client:
        def __init__(self, *, headers_provider, **_kwargs):
            self.headers_provider = headers_provider
            self.requests: list[tuple[str, dict[str, Any] | None, str]] = []
            self.close_calls = 0
            clients.append(self)

        def request(self, method, params, *, name=""):
            assert self.headers_provider() == {"Authorization": "Bearer access-token"}
            self.requests.append((method, params, name))
            return {
                "jsonrpc": "2.0",
                "id": len(self.requests),
                "result": {"tools": []},
            }, len(self.requests)

        def close(self):
            self.close_calls += 1

    monkeypatch.setattr(mcp_connector_oauth_rpc, "StreamableHttpMcpClient", _Client)
    monkeypatch.setattr(
        mcp_connector_oauth_repository.McpConnectorStore,
        "get_connector_row",
        lambda _self, _conn, _connector_id, **_kwargs: row,
    )

    with mcp_connector_oauth.mcp_connector_rpc_context(
        object(),
        connector_row=row,
        timeout_seconds=5.0,
    ) as rpc_post:
        first, first_latency = rpc_post(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
        )
        second, second_latency = rpc_post(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/list",
                "params": {"cursor": "page-2"},
            }
        )
        assert clients[0].close_calls == 0

    assert first["result"] == {"tools": []}
    assert second["result"] == {"tools": []}
    assert (first_latency, second_latency) == (1, 2)
    assert len(clients) == 1
    assert clients[0].requests == [
        ("tools/list", {}, ""),
        ("tools/list", {"cursor": "page-2"}, ""),
    ]
    assert clients[0].close_calls == 1


def test_mcp_connector_rpc_post_threads_generated_transport_headers(monkeypatch) -> None:
    captured: dict[str, Any] = {}

    class _Client:
        def __init__(self, **_kwargs):
            pass

        def request(self, method, params, *, name="", generated_transport_headers=None):
            captured.update(
                method=method,
                params=params,
                name=name,
                generated_transport_headers=generated_transport_headers,
            )
            return {"jsonrpc": "2.0", "id": 1, "result": {"ok": True}}, 1

        def close(self):
            pass

    monkeypatch.setattr(mcp_connector_oauth_rpc, "StreamableHttpMcpClient", _Client)

    response = mcp_connector_oauth.mcp_connector_rpc_post(
        object(),
        connector_row={**_connector_row(), "auth_mode": "none", "headers": {}},
        payload={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": " catalog.read ", "arguments": {}},
        },
        timeout_seconds=5.0,
        generated_transport_headers={"Mcp-Param-Tenant": "trusted"},
    )

    assert response["result"] == {"ok": True}
    assert captured == {
        "method": "tools/call",
        "params": {"name": " catalog.read ", "arguments": {}},
        "name": " catalog.read ",
        "generated_transport_headers": {"Mcp-Param-Tenant": "trusted"},
    }


def test_mcp_connector_rpc_post_uses_same_protocol_client_path_without_oauth(
    monkeypatch,
) -> None:
    clients: list[Any] = []
    row = _connector_row(
        auth_mode="bearer",
        headers={"Authorization": "Bearer configured-token"},
    )

    class _Client:
        def __init__(self, *, headers_provider, **_kwargs):
            self.headers_provider = headers_provider
            self.headers: list[dict[str, str]] = []
            self.close_calls = 0
            clients.append(self)

        def request(self, method, params, *, name=""):
            self.headers.append(self.headers_provider())
            assert (method, params, name) == (
                "tools/call",
                {"name": "catalog.read", "arguments": {}},
                "catalog.read",
            )
            return {"jsonrpc": "2.0", "id": 2, "result": {"ok": True}}, 9

        def close(self):
            self.close_calls += 1

    monkeypatch.setattr(mcp_connector_oauth_rpc, "StreamableHttpMcpClient", _Client)
    monkeypatch.setattr(
        mcp_connector_oauth_repository.McpConnectorStore,
        "get_connector_row",
        lambda _self, _conn, _connector_id, **_kwargs: row,
    )

    payload, latency = mcp_connector_oauth.mcp_connector_rpc_post_with_latency(
        object(),
        connector_row=row,
        payload={
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "catalog.read", "arguments": {}},
        },
        timeout_seconds=6.0,
    )

    assert payload["result"] == {"ok": True}
    assert latency == 9
    assert len(clients) == 1
    assert clients[0].headers == [{"Authorization": "Bearer configured-token"}]
    assert clients[0].close_calls == 1


@pytest.mark.parametrize("era", ["modern", "legacy"])
def test_mcp_connector_rpc_post_refreshes_401_on_same_client_and_era(
    monkeypatch,
    era: str,
) -> None:
    clients: list[Any] = []
    force_refreshes: list[bool] = []
    discoveries: list[str] = []
    latest = _connector_row()

    def _token(_conn, *, connector_row, force_refresh=False):
        assert connector_row is latest
        force_refreshes.append(force_refresh)
        return "fresh-access" if force_refresh else "stale-access"

    first_error = ServiceUnavailableError("MCP request failed with HTTP 401")
    first_error.__cause__ = urllib.error.HTTPError(
        "https://mcp.example.test/public/mcp",
        401,
        "Unauthorized",
        {"WWW-Authenticate": 'Bearer resource_metadata="https://mcp.example.test/meta"'},
        None,
    )

    class _Client:
        def __init__(self, *, headers_provider, **_kwargs):
            self.era = era
            self.headers_provider = headers_provider
            self.headers: list[dict[str, str]] = []
            self.request_calls = 0
            self.close_calls = 0
            clients.append(self)

        def request(self, method, params, *, name=""):
            assert (method, params, name) == ("tools/list", None, "")
            self.request_calls += 1
            self.headers.append(self.headers_provider())
            if self.request_calls == 1:
                raise first_error
            return {"jsonrpc": "2.0", "id": 1, "result": {"tools": []}}, 14

        def close(self):
            self.close_calls += 1

    monkeypatch.setattr(mcp_connector_oauth_rpc, "StreamableHttpMcpClient", _Client)
    monkeypatch.setattr(
        mcp_connector_oauth_rpc,
        "ensure_valid_mcp_connector_access_token",
        _token,
    )
    monkeypatch.setattr(
        mcp_connector_oauth_rpc,
        "discover_mcp_oauth",
        lambda _conn, *, connector_row, www_authenticate: discoveries.append(www_authenticate),
    )
    monkeypatch.setattr(
        mcp_connector_oauth_repository.McpConnectorStore,
        "get_connector_row",
        lambda _self, _conn, _connector_id, **_kwargs: latest,
    )

    payload, latency = mcp_connector_oauth.mcp_connector_rpc_post_with_latency(
        object(),
        connector_row=latest,
        payload={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        timeout_seconds=5.0,
    )

    assert payload["result"] == {"tools": []}
    assert latency == 14
    assert len(clients) == 1
    assert clients[0].era == era
    assert clients[0].request_calls == 2
    assert clients[0].headers == [
        {"Authorization": "Bearer stale-access"},
        {"Authorization": "Bearer fresh-access"},
    ]
    assert clients[0].close_calls == 1
    assert force_refreshes == [False, True]
    assert discoveries == ['Bearer resource_metadata="https://mcp.example.test/meta"']


def test_modern_fixture_oauth_401_retries_without_renegotiating_era(monkeypatch) -> None:
    stale_authorization = "Bearer fixture-stale-access"
    fresh_authorization = "Bearer fixture-fresh-access"
    force_refreshes: list[bool] = []
    discoveries: list[str] = []
    monkeypatch.setenv("FS_ALLOW_PRIVATE_REMOTE_URLS", "1")

    def _token(_conn, *, connector_row, force_refresh=False):
        force_refreshes.append(force_refresh)
        return "fixture-fresh-access" if force_refresh else "fixture-stale-access"

    monkeypatch.setattr(
        mcp_connector_oauth_rpc,
        "ensure_valid_mcp_connector_access_token",
        _token,
    )
    monkeypatch.setattr(
        mcp_connector_oauth_rpc,
        "discover_mcp_oauth",
        lambda _conn, *, connector_row, www_authenticate: discoveries.append(www_authenticate),
    )

    with modern_mcp_server(
        unauthorized_once_for="tools/list",
        accepted_authorizations=[stale_authorization, fresh_authorization],
    ) as fixture:
        row = _connector_row(base_url=fixture.url)
        monkeypatch.setattr(
            mcp_connector_oauth_repository.McpConnectorStore,
            "get_connector_row",
            lambda _self, _conn, _connector_id, **_kwargs: row,
        )

        payload, _latency = mcp_connector_oauth.mcp_connector_rpc_post_with_latency(
            object(),
            connector_row=row,
            payload={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
            timeout_seconds=2.0,
        )

    assert payload["result"]["tools"][0]["name"] == "browser_navigate"
    post_records = [record for record in fixture.requests if record.payload is not None]
    assert [record.payload["method"] for record in post_records] == [
        "server/discover",
        "tools/list",
        "tools/list",
    ]
    assert [record.headers.get("authorization") for record in post_records] == [
        stale_authorization,
        stale_authorization,
        fresh_authorization,
    ]
    assert force_refreshes == [False, False, True]
    assert discoveries == ['Bearer error="invalid_token"']
    assert fixture.violations == []
    assert fixture.sessions == []
    assert fixture.delete_count == 0


def test_mcp_connector_rpc_post_preserves_403_insufficient_scope_behavior(
    monkeypatch,
) -> None:
    clients: list[Any] = []
    recorded_updates: list[dict[str, Any]] = []
    row = _connector_row(oauth_metadata_jsonb={"discovery": {"issuer": "https://auth.test"}})
    scope_error = ServiceUnavailableError("MCP request failed with HTTP 403")
    scope_error.__cause__ = urllib.error.HTTPError(
        "https://mcp.example.test/public/mcp",
        403,
        "Forbidden",
        {
            "WWW-Authenticate": (
                'Bearer error="insufficient_scope", scope="files:read files:write", '
                'resource_metadata="https://mcp.example.test/meta"'
            )
        },
        None,
    )

    class _Client:
        def __init__(self, *, headers_provider, **_kwargs):
            self.headers_provider = headers_provider
            self.close_calls = 0
            clients.append(self)

        def request(self, method, params, *, name=""):
            assert self.headers_provider()["Authorization"] == "Bearer access-token"
            raise scope_error

        def close(self):
            self.close_calls += 1

    monkeypatch.setattr(mcp_connector_oauth_rpc, "StreamableHttpMcpClient", _Client)
    monkeypatch.setattr(
        mcp_connector_oauth_repository.McpConnectorStore,
        "get_connector_row",
        lambda _self, _conn, _connector_id, **_kwargs: row,
    )
    monkeypatch.setattr(
        mcp_connector_oauth_rpc,
        "ensure_valid_mcp_connector_access_token",
        lambda _conn, *, connector_row, force_refresh=False: "access-token",
    )
    monkeypatch.setattr(
        mcp_connector_oauth_provider,
        "_update_connector_oauth",
        lambda _conn, _connector_id, *, updates, **_kwargs: recorded_updates.append(dict(updates)),
    )
    monkeypatch.setattr(
        mcp_connector_oauth_repository,
        "_update_connector_oauth",
        mcp_connector_oauth_provider._update_connector_oauth,
    )
    monkeypatch.setattr(
        mcp_connector_oauth_rpc,
        "_update_connector_oauth",
        mcp_connector_oauth_provider._update_connector_oauth,
    )

    _raises_input_1920_1 = object()
    with pytest.raises(
        ServiceUnavailableError,
        match="requires reconnect with additional OAuth scopes",
    ):
        mcp_connector_oauth.mcp_connector_rpc_post_with_latency(
            _raises_input_1920_1,
            connector_row=row,
            payload={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            timeout_seconds=5.0,
        )

    assert len(clients) == 1
    assert clients[0].close_calls == 1
    assert recorded_updates[-1]["auth_status"] == "auth_required"
    assert recorded_updates[-1]["status"] == "degraded"
    assert recorded_updates[-1]["oauth_last_error"] == "insufficient_scope"
    assert recorded_updates[-1]["oauth_metadata_jsonb"]["last_scope_challenge"]["scope"] == [
        "files:read",
        "files:write",
    ]


def test_stale_insufficient_scope_response_cannot_downgrade_reconnected_connector(
    monkeypatch,
) -> None:
    stale_row = _connector_row(
        oauth_credential_revision=4,
        oauth_metadata_jsonb={"discovery": {"issuer": "https://old-auth.example.test"}},
    )
    scope_error = ServiceUnavailableError("MCP request failed with HTTP 403")
    scope_error.__cause__ = urllib.error.HTTPError(
        stale_row["base_url"],
        403,
        "Forbidden",
        {"WWW-Authenticate": 'Bearer error="insufficient_scope", scope="files:write"'},
        None,
    )

    class _Client:
        def __init__(self, **_kwargs):
            pass

        def request(self, *_args, **_kwargs):
            raise scope_error

        def close(self):
            pass

    writes: list[dict[str, Any]] = []
    monkeypatch.setattr(mcp_connector_oauth_rpc, "StreamableHttpMcpClient", _Client)
    monkeypatch.setattr(
        mcp_connector_oauth_rpc,
        "ensure_valid_mcp_connector_access_token",
        lambda *_args, **_kwargs: "stale-access",
    )
    monkeypatch.setattr(
        mcp_connector_oauth_provider,
        "_locked_connector_row",
        lambda _conn, row: {**row, "oauth_credential_revision": 5},
    )
    monkeypatch.setattr(
        mcp_connector_oauth_repository,
        "_locked_connector_row",
        mcp_connector_oauth_provider._locked_connector_row,
    )
    monkeypatch.setattr(
        mcp_connector_oauth_rpc,
        "_locked_connector_row",
        mcp_connector_oauth_provider._locked_connector_row,
    )
    monkeypatch.setattr(
        mcp_connector_oauth_provider,
        "_update_connector_oauth",
        lambda *_args, **kwargs: writes.append(dict(kwargs.get("updates") or {})),
    )
    monkeypatch.setattr(
        mcp_connector_oauth_repository,
        "_update_connector_oauth",
        mcp_connector_oauth_provider._update_connector_oauth,
    )
    monkeypatch.setattr(
        mcp_connector_oauth_rpc,
        "_update_connector_oauth",
        mcp_connector_oauth_provider._update_connector_oauth,
    )

    _raises_input_1986_1 = object()
    with pytest.raises(OAuthConnectionError, match="credential revision changed"):
        mcp_connector_oauth.mcp_connector_rpc_post_with_latency(
            _raises_input_1986_1,
            connector_row=stale_row,
            payload={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            timeout_seconds=5.0,
        )

    assert writes == []


def test_mcp_connector_rpc_post_does_not_expose_credentials_after_retry_failure(
    monkeypatch,
) -> None:
    attempts = 0
    row = _connector_row()
    error = ServiceUnavailableError("MCP request failed with HTTP 401")
    error.__cause__ = urllib.error.HTTPError(
        row["base_url"],
        401,
        "Unauthorized",
        {"WWW-Authenticate": 'Bearer error="invalid_token"'},
        None,
    )

    class _Client:
        def __init__(self, *, headers_provider, **_kwargs):
            self.headers_provider = headers_provider

        def request(self, method, params, *, name=""):
            nonlocal attempts
            attempts += 1
            self.headers_provider()
            raise error

        def close(self):
            return None

    monkeypatch.setattr(mcp_connector_oauth_rpc, "StreamableHttpMcpClient", _Client)
    monkeypatch.setattr(
        mcp_connector_oauth_rpc,
        "ensure_valid_mcp_connector_access_token",
        lambda _conn, *, connector_row, force_refresh=False: (
            "fresh-secret-token" if force_refresh else "stale-secret-token"
        ),
    )
    monkeypatch.setattr(mcp_connector_oauth_rpc, "discover_mcp_oauth", lambda *args, **kwargs: {})
    monkeypatch.setattr(
        mcp_connector_oauth_repository.McpConnectorStore,
        "get_connector_row",
        lambda _self, _conn, _connector_id, **_kwargs: row,
    )

    _raises_input_2039_1 = object()
    with pytest.raises(ServiceUnavailableError) as exc_info:
        mcp_connector_oauth.mcp_connector_rpc_post_with_latency(
            _raises_input_2039_1,
            connector_row=row,
            payload={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            timeout_seconds=5.0,
        )

    assert attempts == 2
    message = str(exc_info.value)
    assert "stale-secret-token" not in message
    assert "fresh-secret-token" not in message
    assert "Authorization" not in message


@pytest.mark.parametrize("allow_private", [False, True])
def test_public_http_oauth_connector_is_rejected_before_credentials_or_rpc(
    monkeypatch,
    allow_private: bool,
) -> None:
    row = _connector_row(base_url="http://mcp.example.test/public/mcp")
    if allow_private:
        monkeypatch.setenv("FS_ALLOW_PRIVATE_REMOTE_URLS", "1")
    else:
        monkeypatch.delenv("FS_ALLOW_PRIVATE_REMOTE_URLS", raising=False)
    monkeypatch.setattr(
        mcp_connector_oauth_rpc,
        "ensure_valid_mcp_connector_access_token",
        lambda *_args, **_kwargs: pytest.fail("OAuth credentials must not be resolved"),
    )
    monkeypatch.setattr(
        mcp_connector_oauth_rpc,
        "StreamableHttpMcpClient",
        lambda **_kwargs: pytest.fail("HTTP client must not be created"),
    )

    _raises_input_2075_1 = object()
    with pytest.raises(ServiceValidationError, match=r"Credential-bearing.*https"):
        mcp_connector_oauth.mcp_connector_rpc_post_with_latency(
            _raises_input_2075_1,
            connector_row=row,
            payload={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            timeout_seconds=5.0,
        )


def test_private_http_oauth_connector_requires_explicit_development_override(
    monkeypatch,
) -> None:

    row = _connector_row(base_url="http://127.0.0.1:8200/mcp")
    monkeypatch.delenv("FS_ALLOW_PRIVATE_REMOTE_URLS", raising=False)
    with pytest.raises(ServiceValidationError, match=r"Credential-bearing.*https"):
        mcp_connector_http._connector_rpc_url(row)

    monkeypatch.setenv("FS_ALLOW_PRIVATE_REMOTE_URLS", "1")
    assert mcp_connector_http._connector_rpc_url(row) == row["base_url"]


@pytest.mark.parametrize("allow_private", [False, True])
def test_public_https_oauth_connector_is_permitted(
    monkeypatch,
    allow_private: bool,
) -> None:

    if allow_private:
        monkeypatch.setenv("FS_ALLOW_PRIVATE_REMOTE_URLS", "1")
    else:
        monkeypatch.delenv("FS_ALLOW_PRIVATE_REMOTE_URLS", raising=False)
    row = _connector_row(base_url="https://mcp.example.test/public/mcp")

    assert mcp_connector_http._connector_rpc_url(row) == row["base_url"]


def test_mcp_connector_rpc_post_reuses_execution_client_until_scope_exit(
    monkeypatch,
) -> None:
    from core.application.management.mcp_connector_client import mcp_execution_scope

    clients: list[Any] = []
    row = _connector_row(auth_mode="none")

    class _Client:
        def __init__(self, *, headers_provider, **_kwargs):
            self.headers_provider = headers_provider
            self.request_calls = 0
            self.close_calls = 0
            clients.append(self)

        def request(self, method, params, *, name=""):
            self.request_calls += 1
            assert self.headers_provider() == {}
            return {"jsonrpc": "2.0", "id": 1, "result": {"tools": []}}, 1

        def close(self):
            self.close_calls += 1

    monkeypatch.setattr(mcp_connector_oauth_rpc, "StreamableHttpMcpClient", _Client)
    monkeypatch.setattr(
        mcp_connector_oauth_repository.McpConnectorStore,
        "get_connector_row",
        lambda _self, _conn, _connector_id, **_kwargs: row,
    )

    with mcp_execution_scope("acc_alpha", "job-1"):
        for _ in range(2):
            mcp_connector_oauth.mcp_connector_rpc_post_with_latency(
                object(),
                connector_row=row,
                payload={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                timeout_seconds=5.0,
                runtime_context={"account_id": "acc_alpha", "job_id": "job-1"},
            )
        assert len(clients) == 1
        assert clients[0].request_calls == 2
        assert clients[0].close_calls == 0

    assert clients[0].close_calls == 1


def test_retained_client_fails_closed_when_connector_is_deleted_between_requests(
    monkeypatch,
) -> None:
    from core.application.management.mcp_connector_client import mcp_execution_scope

    row = _connector_row(
        auth_mode="bearer",
        headers={"Authorization": "Bearer configured-token"},
    )
    outbound_headers: list[dict[str, str]] = []
    latest_rows = [row, None]

    class _Client:
        def __init__(self, *, headers_provider, **_kwargs):
            self.headers_provider = headers_provider

        def request(self, method, params, *, name=""):
            _ = (method, params, name)
            outbound_headers.append(self.headers_provider())
            return {"jsonrpc": "2.0", "id": 1, "result": {"tools": []}}, 1

        def close(self):
            return None

    monkeypatch.setattr(mcp_connector_oauth_rpc, "StreamableHttpMcpClient", _Client)
    monkeypatch.setattr(
        mcp_connector_oauth_repository.McpConnectorStore,
        "get_connector_row",
        lambda _self, _conn, _connector_id, **_kwargs: latest_rows.pop(0),
    )

    with mcp_execution_scope("acc_alpha", "job-1"):
        mcp_connector_oauth.mcp_connector_rpc_post_with_latency(
            object(),
            connector_row=row,
            payload={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            timeout_seconds=5.0,
            runtime_context={"account_id": "acc_alpha", "job_id": "job-1"},
        )
        _raises_input_2199_1 = object()
        with pytest.raises(ServiceValidationError, match="no longer exists"):
            mcp_connector_oauth.mcp_connector_rpc_post_with_latency(
                _raises_input_2199_1,
                connector_row=row,
                payload={"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                timeout_seconds=5.0,
                runtime_context={"account_id": "acc_alpha", "job_id": "job-1"},
            )

    assert outbound_headers == [{"Authorization": "Bearer configured-token"}]


def test_retained_execution_client_refreshes_401_through_its_existing_provider(
    monkeypatch,
) -> None:
    from core.application.management.mcp_connector_client import mcp_execution_scope

    clients: list[Any] = []
    force_refreshes: list[bool] = []
    row = _connector_row()
    auth_error = ServiceUnavailableError("MCP request failed with HTTP 401")
    auth_error.__cause__ = urllib.error.HTTPError(
        row["base_url"],
        401,
        "Unauthorized",
        {"WWW-Authenticate": 'Bearer error="invalid_token"'},
        None,
    )

    class _Client:
        def __init__(self, *, headers_provider, **_kwargs):
            self.headers_provider = headers_provider
            self.headers: list[dict[str, str]] = []
            self.request_calls = 0
            self.close_calls = 0
            clients.append(self)

        def request(self, method, params, *, name=""):
            self.request_calls += 1
            self.headers.append(self.headers_provider())
            if self.request_calls == 2:
                raise auth_error
            return {"jsonrpc": "2.0", "id": 1, "result": {"tools": []}}, 1

        def close(self):
            self.close_calls += 1

    def _token(_conn, *, connector_row, force_refresh=False):
        force_refreshes.append(force_refresh)
        return "fresh-access" if force_refresh else "stale-access"

    monkeypatch.setattr(mcp_connector_oauth_rpc, "StreamableHttpMcpClient", _Client)
    monkeypatch.setattr(
        mcp_connector_oauth_rpc,
        "ensure_valid_mcp_connector_access_token",
        _token,
    )
    monkeypatch.setattr(mcp_connector_oauth_rpc, "discover_mcp_oauth", lambda *args, **kwargs: {})
    monkeypatch.setattr(
        mcp_connector_oauth_repository.McpConnectorStore,
        "get_connector_row",
        lambda _self, _conn, _connector_id, **_kwargs: row,
    )

    with mcp_execution_scope("acc_alpha", "job-1"):
        for _ in range(2):
            mcp_connector_oauth.mcp_connector_rpc_post_with_latency(
                object(),
                connector_row=row,
                payload={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                timeout_seconds=5.0,
                runtime_context={"account_id": "acc_alpha", "job_id": "job-1"},
            )
        assert len(clients) == 1
        assert clients[0].request_calls == 3
        assert clients[0].headers == [
            {"Authorization": "Bearer stale-access"},
            {"Authorization": "Bearer stale-access"},
            {"Authorization": "Bearer fresh-access"},
        ]
        assert force_refreshes == [False, False, True]
        assert clients[0].close_calls == 0

    assert clients[0].close_calls == 1


def test_retained_client_rejects_endpoint_rebind_before_sending_new_token(
    monkeypatch,
) -> None:
    from core.application.management.mcp_connector_client import mcp_execution_scope

    row_a = _connector_row(base_url="https://mcp-a.example.test/public/mcp")
    row_b = _connector_row(base_url="https://mcp-b.example.test/public/mcp")
    outbound: list[tuple[str, str]] = []
    token_rows: list[str] = []
    current_row = [row_a]
    close_binding_errors: list[str] = []

    def _token(_conn, *, connector_row, force_refresh=False):
        _ = force_refresh
        endpoint = connector_row["base_url"]
        token_rows.append(endpoint)
        return "token-b" if endpoint == row_b["base_url"] else "token-a"

    class _Client:
        def __init__(self, *, url, headers_provider, **_kwargs):
            self.url = url
            self.headers_provider = headers_provider
            self.request_calls = 0

        def request(self, method, params, *, name=""):
            _ = (method, params, name)
            self.request_calls += 1
            authorization = self.headers_provider()["Authorization"]
            outbound.append((self.url, authorization))
            return {"jsonrpc": "2.0", "id": 1, "result": {"tools": []}}, 1

        def close(self):
            try:
                authorization = self.headers_provider()["Authorization"]
            except ServiceValidationError as exc:
                close_binding_errors.append(str(exc))
                return
            outbound.append((f"DELETE {self.url}", authorization))

    monkeypatch.setattr(mcp_connector_oauth_rpc, "StreamableHttpMcpClient", _Client)
    monkeypatch.setattr(mcp_connector_oauth_rpc, "ensure_valid_mcp_connector_access_token", _token)
    monkeypatch.setattr(
        mcp_connector_oauth_repository.McpConnectorStore,
        "get_connector_row",
        lambda _self, _conn, _connector_id, **_kwargs: current_row[0],
    )

    with mcp_execution_scope("acc_alpha", "job-1"):
        mcp_connector_oauth.mcp_connector_rpc_post_with_latency(
            object(),
            connector_row=row_a,
            payload={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            timeout_seconds=5.0,
            runtime_context={"account_id": "acc_alpha", "job_id": "job-1"},
        )
        current_row[0] = row_b
        _raises_input_2341_1 = object()
        with pytest.raises(ServiceValidationError, match="binding no longer matches"):
            mcp_connector_oauth.mcp_connector_rpc_post_with_latency(
                _raises_input_2341_1,
                connector_row=row_a,
                payload={"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                timeout_seconds=5.0,
                runtime_context={"account_id": "acc_alpha", "job_id": "job-1"},
            )

    assert outbound == [(row_a["base_url"], "Bearer token-a")]
    assert close_binding_errors == [
        "MCP retained client binding no longer matches the connector endpoint or auth mode"
    ]
    assert row_b["base_url"] not in token_rows
    assert all(authorization != "Bearer token-b" for _url, authorization in outbound)


def test_mcp_connector_rpc_post_complete_identity_without_scope_is_short_lived(
    monkeypatch,
) -> None:
    clients: list[Any] = []

    class _Client:
        def __init__(self, **_kwargs):
            self.close_calls = 0
            clients.append(self)

        def request(self, method, params, *, name=""):
            return {"jsonrpc": "2.0", "id": 1, "result": {"tools": []}}, 1

        def close(self):
            self.close_calls += 1

    monkeypatch.setattr(mcp_connector_oauth_rpc, "StreamableHttpMcpClient", _Client)

    mcp_connector_oauth.mcp_connector_rpc_post_with_latency(
        object(),
        connector_row=_connector_row(auth_mode="none"),
        payload={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        timeout_seconds=5.0,
        runtime_context={"account_id": "acc_alpha", "job_id": "job-1"},
    )

    assert len(clients) == 1
    assert clients[0].close_calls == 1


def test_typed_inactive_scope_message_drift_still_selects_short_lived_client(
    monkeypatch,
) -> None:
    clients: list[Any] = []

    class _Client:
        def __init__(self, **_kwargs):
            self.close_calls = 0
            clients.append(self)

        def request(self, method, params, *, name=""):
            return {"jsonrpc": "2.0", "id": 1, "result": {"tools": []}}, 1

        def close(self):
            self.close_calls += 1

    def _inactive_scope(**_kwargs):
        raise mcp_connector_oauth_rpc.McpExecutionScopeInactiveError(
            "inactive policy copy changed completely"
        )

    monkeypatch.setattr(mcp_connector_oauth_rpc, "StreamableHttpMcpClient", _Client)
    monkeypatch.setattr(
        mcp_connector_oauth_rpc,
        "get_execution_mcp_client",
        _inactive_scope,
    )

    mcp_connector_oauth.mcp_connector_rpc_post_with_latency(
        object(),
        connector_row=_connector_row(auth_mode="none"),
        payload={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        timeout_seconds=5.0,
        runtime_context={"account_id": "acc_alpha", "job_id": "job-1"},
    )

    assert len(clients) == 1
    assert clients[0].close_calls == 1


def test_mcp_connector_rpc_post_rejects_cross_account_runtime_identity(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        mcp_connector_oauth_rpc,
        "StreamableHttpMcpClient",
        lambda **_kwargs: pytest.fail("cross-account request must not create a client"),
    )

    _raises_input_2437_1 = object()
    _raises_input_2437_2 = _connector_row(account_id="acc_alpha", auth_mode="none")
    with pytest.raises(ServiceValidationError, match="account"):
        mcp_connector_oauth.mcp_connector_rpc_post_with_latency(
            _raises_input_2437_1,
            connector_row=_raises_input_2437_2,
            payload={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            timeout_seconds=5.0,
            runtime_context={"account_id": "acc_beta", "job_id": "job-1"},
        )


def test_mcp_connector_rpc_post_rejects_runtime_job_that_mismatches_active_scope(
    monkeypatch,
) -> None:
    from core.application.management.mcp_connector_client import mcp_execution_scope

    monkeypatch.setattr(
        mcp_connector_oauth_rpc,
        "StreamableHttpMcpClient",
        lambda **_kwargs: pytest.fail("scope mismatch must not create a short-lived client"),
    )

    _raises_input_2458_1 = object()
    _raises_input_2458_2 = _connector_row(account_id="acc_alpha", auth_mode="none")
    with (
        mcp_execution_scope("acc_alpha", "job-active"),
        pytest.raises(ServiceValidationError, match="active execution scope"),
    ):
        mcp_connector_oauth.mcp_connector_rpc_post_with_latency(
            _raises_input_2458_1,
            connector_row=_raises_input_2458_2,
            payload={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            timeout_seconds=5.0,
            runtime_context={"account_id": "acc_alpha", "job_id": "job-other"},
        )


def test_refresh_uses_preserved_discovery_metadata(monkeypatch) -> None:
    outer_conn = object()
    lock_conn = object()
    resolve_conns: list[object] = []
    secret_values = {
        mcp_connector_oauth.MCP_OAUTH_ACCESS_TOKEN_SECRET: "expired-access",
        mcp_connector_oauth.MCP_OAUTH_REFRESH_TOKEN_SECRET: "refresh-token",
        mcp_connector_oauth.MCP_OAUTH_EXPIRES_AT_SECRET: "1000",
        mcp_connector_oauth.MCP_OAUTH_GRANTED_SCOPES_SECRET: '["files:read"]',
        mcp_connector_oauth.MCP_OAUTH_CLIENT_ID_SECRET: "client-1",
        mcp_connector_oauth.MCP_OAUTH_CLIENT_SECRET_SECRET: "  secret-1  ",
    }
    row = _connector_row(
        oauth_metadata_jsonb={
            "discovery": {
                "resource": "https://mcp.example.test/public/mcp",
                "authorization_server": {
                    "authorization_endpoint": "https://auth.example.test/authorize",
                    "token_endpoint": "https://auth.example.test/token",
                    "code_challenge_methods_supported": ["S256"],
                },
            },
            "registration": {
                "mode": "manual",
                "token_endpoint_auth_method": "client_secret_post",
            },
        },
        oauth_secret_refs_jsonb={
            "refs": {name: f"mcp_connector:mcp_alpha:{name}" for name in secret_values}
        },
    )

    monkeypatch.setattr(
        mcp_connector_oauth_repository.McpConnectorStore,
        "get_connector_row",
        lambda _self, _conn, _connector_id, **_kwargs: (
            pytest.fail("refresh must read latest connector through lock connection")
            if _conn is not lock_conn
            else row
        ),
    )
    monkeypatch.setattr(
        mcp_connector_oauth_repository.McpConnectorStore,
        "update_connector_row",
        lambda _self, _conn, _connector_id, payload, **_kwargs: (
            pytest.fail("refresh must update connector through lock connection")
            if _conn is not lock_conn
            else row.update(payload)
        ),
    )
    monkeypatch.setattr(
        mcp_connector_oauth_tokens,
        "_resolve_connector_oauth_secrets",
        lambda _conn, _row: resolve_conns.append(_conn) or dict(secret_values),
    )

    def _save(_conn, *, account_id, connector_id, secrets_to_save, **_kwargs):
        assert _conn is lock_conn
        _ = (account_id, connector_id)
        secret_values.update({name: str(value) for name, value in secrets_to_save.items()})
        return {"refs": {name: f"mcp_connector:mcp_alpha:{name}" for name in secrets_to_save}}

    monkeypatch.setattr(mcp_connector_oauth_tokens, "_save_connector_oauth_secrets", _save)

    @contextmanager
    def _refresh_lock(_conn, *, lock_name):
        assert _conn is outer_conn
        assert lock_name == "mcp_connector_oauth_refresh:acc_alpha:mcp_alpha"
        yield True, lock_conn

    monkeypatch.setattr(
        mcp_connector_oauth.mcp_connector_oauth_store,
        "refresh_advisory_lock",
        _refresh_lock,
    )

    def _post_form(url, data, *, purpose, basic_auth=None):
        _ = (purpose, basic_auth)
        assert url == "https://auth.example.test/token"
        assert data["grant_type"] == "refresh_token"
        assert data["refresh_token"] == "refresh-token"
        assert data["resource"] == "https://mcp.example.test/public/mcp"
        assert data["client_secret"] == "  secret-1  "
        return {
            "access_token": "fresh-access",
            "refresh_token": "fresh-refresh",
            "expires_in": 3600,
            "token_type": "Bearer",
            "scope": "files:read",
        }

    token = mcp_connector_oauth.ensure_valid_mcp_connector_access_token(
        outer_conn,
        connector_row=row,
        now=2000.0,
        post_form=_post_form,
    )

    assert token == "fresh-access"
    assert secret_values[mcp_connector_oauth.MCP_OAUTH_ACCESS_TOKEN_SECRET] == "fresh-access"
    assert "discovery" in row["oauth_metadata_jsonb"]
    assert "registration" in row["oauth_metadata_jsonb"]
    assert row["oauth_metadata_jsonb"]["token"]["granted_scopes"] == ["files:read"]
    assert row["oauth_metadata_jsonb"]["token"]["connector_endpoint"] == (
        "https://mcp.example.test/public/mcp"
    )
    assert row["oauth_credential_revision"] == 1
    assert resolve_conns == [outer_conn, lock_conn, lock_conn]


def test_cached_access_token_allows_prm_resource_different_from_endpoint(monkeypatch) -> None:
    secret_values = {
        mcp_connector_oauth.MCP_OAUTH_ACCESS_TOKEN_SECRET: "still-valid-access",
        mcp_connector_oauth.MCP_OAUTH_REFRESH_TOKEN_SECRET: "refresh-token",
        mcp_connector_oauth.MCP_OAUTH_EXPIRES_AT_SECRET: "9999",
    }
    row = _connector_row(
        base_url="https://mcp.example.test/public/mcp",
        oauth_metadata_jsonb={
            "discovery": {
                "connector_endpoint": "https://mcp.example.test/public/mcp",
                "resource": "https://mcp.example.test",
                "issuer": "https://auth.example.test",
                "authorization_server": {
                    "authorization_endpoint": "https://auth.example.test/authorize",
                    "token_endpoint": "https://auth.example.test/token",
                    "code_challenge_methods_supported": ["S256"],
                },
            },
            "token": {
                "connector_endpoint": "https://mcp.example.test/public/mcp",
                "resource": "https://mcp.example.test",
                "issuer": "https://auth.example.test",
            },
        },
    )
    monkeypatch.setattr(
        mcp_connector_oauth_repository.McpConnectorStore,
        "update_connector_row",
        lambda _self, _conn, _connector_id, payload, **_kwargs: pytest.fail(
            "valid binding must not mark auth_required"
        ),
    )
    monkeypatch.setattr(
        mcp_connector_oauth_tokens,
        "_resolve_connector_oauth_secrets",
        lambda _conn, _row: dict(secret_values),
    )

    token = mcp_connector_oauth.ensure_valid_mcp_connector_access_token(
        object(),
        connector_row=row,
        now=2000.0,
        post_form=lambda *args, **kwargs: pytest.fail("refresh must not run"),
    )

    assert token == "still-valid-access"


def test_cached_access_token_rejects_changed_endpoint_before_bearer(monkeypatch) -> None:
    secret_values = {
        mcp_connector_oauth.MCP_OAUTH_ACCESS_TOKEN_SECRET: "still-valid-access",
        mcp_connector_oauth.MCP_OAUTH_REFRESH_TOKEN_SECRET: "refresh-token",
        mcp_connector_oauth.MCP_OAUTH_EXPIRES_AT_SECRET: "9999",
    }
    row = _connector_row(
        base_url="https://mcp-new.example.test/public/mcp",
        oauth_metadata_jsonb={
            "discovery": {
                "connector_endpoint": "https://mcp.example.test/public/mcp",
                "resource": "https://mcp.example.test/public/mcp",
                "issuer": "https://auth.example.test",
                "authorization_server": {
                    "authorization_endpoint": "https://auth.example.test/authorize",
                    "token_endpoint": "https://auth.example.test/token",
                    "code_challenge_methods_supported": ["S256"],
                },
            },
            "token": {
                "connector_endpoint": "https://mcp.example.test/public/mcp",
                "resource": "https://mcp.example.test/public/mcp",
                "issuer": "https://auth.example.test",
            },
        },
    )
    monkeypatch.setattr(
        mcp_connector_oauth_repository.McpConnectorStore,
        "update_connector_row",
        lambda _self, _conn, _connector_id, payload, **_kwargs: row.update(payload),
    )
    monkeypatch.setattr(
        mcp_connector_oauth_tokens,
        "_resolve_connector_oauth_secrets",
        lambda _conn, _row: dict(secret_values),
    )

    _raises_input_2667_1 = object()

    def _raises_input_2667_2(*args, **kwargs):
        return pytest.fail("refresh must not run")

    with pytest.raises(OAuthConnectionError, match="different resource"):
        mcp_connector_oauth.ensure_valid_mcp_connector_access_token(
            _raises_input_2667_1, connector_row=row, now=2000.0, post_form=_raises_input_2667_2
        )

    assert row["auth_status"] == "auth_required"
    assert "different resource" in row["oauth_last_error"]


def test_refresh_rejects_issuer_rebinding_before_using_refresh_token(monkeypatch) -> None:
    secret_values = {
        mcp_connector_oauth.MCP_OAUTH_ACCESS_TOKEN_SECRET: "expired-access",
        mcp_connector_oauth.MCP_OAUTH_REFRESH_TOKEN_SECRET: "refresh-token",
        mcp_connector_oauth.MCP_OAUTH_EXPIRES_AT_SECRET: "1000",
        mcp_connector_oauth.MCP_OAUTH_GRANTED_SCOPES_SECRET: '["files:read"]',
        mcp_connector_oauth.MCP_OAUTH_CLIENT_ID_SECRET: "client-1",
        mcp_connector_oauth.MCP_OAUTH_CLIENT_SECRET_SECRET: "secret-1",
    }
    row = _connector_row(
        oauth_metadata_jsonb={
            "discovery": {
                "resource": "https://mcp.example.test/public/mcp",
                "issuer": "https://auth-new.example.test",
                "authorization_server": {
                    "authorization_endpoint": "https://auth-new.example.test/authorize",
                    "token_endpoint": "https://auth-new.example.test/token",
                    "code_challenge_methods_supported": ["S256"],
                },
            },
            "registration": {
                "mode": "manual",
                "issuer": "https://auth.example.test",
                "token_endpoint_auth_method": "client_secret_post",
            },
            "token": {
                "resource": "https://mcp.example.test/public/mcp",
                "issuer": "https://auth.example.test",
            },
        },
        oauth_secret_refs_jsonb={
            "refs": {name: f"mcp_connector:mcp_alpha:{name}" for name in secret_values}
        },
    )
    monkeypatch.setattr(
        mcp_connector_oauth_repository.McpConnectorStore,
        "update_connector_row",
        lambda _self, _conn, _connector_id, payload, **_kwargs: row.update(payload),
    )
    monkeypatch.setattr(
        mcp_connector_oauth_tokens,
        "_resolve_connector_oauth_secrets",
        lambda _conn, _row: dict(secret_values),
    )

    _raises_input_2724_1 = object()

    def _raises_input_2724_2(*args, **kwargs):
        return pytest.fail("refresh must not run")

    with pytest.raises(OAuthConnectionError, match="different authorization server"):
        mcp_connector_oauth.ensure_valid_mcp_connector_access_token(
            _raises_input_2724_1, connector_row=row, now=2000.0, post_form=_raises_input_2724_2
        )

    assert row["auth_status"] == "auth_required"
    assert "authorization server" in row["oauth_last_error"]


def test_revoke_skips_poisoned_discovery_revocation_endpoint(monkeypatch) -> None:
    secret_values = {
        mcp_connector_oauth.MCP_OAUTH_ACCESS_TOKEN_SECRET: "access-token",
        mcp_connector_oauth.MCP_OAUTH_REFRESH_TOKEN_SECRET: "refresh-token",
        mcp_connector_oauth.MCP_OAUTH_CLIENT_ID_SECRET: "client-1",
        mcp_connector_oauth.MCP_OAUTH_CLIENT_SECRET_SECRET: "secret-1",
        mcp_connector_oauth.MCP_OAUTH_REGISTRATION_ACCESS_TOKEN_SECRET: "registration-token",
    }
    deleted_refs: list[str] = []
    row = _connector_row(
        oauth_metadata_jsonb={
            "discovery": {
                "connector_endpoint": "https://mcp.example.test/public/mcp",
                "resource": "https://mcp.example.test/public/mcp",
                "issuer": "https://attacker-auth.example.test",
                "authorization_server": {
                    "issuer": "https://attacker-auth.example.test",
                    "authorization_endpoint": "https://attacker-auth.example.test/authorize",
                    "token_endpoint": "https://attacker-auth.example.test/token",
                    "revocation_endpoint": "https://attacker-auth.example.test/revoke",
                    "code_challenge_methods_supported": ["S256"],
                },
            },
            "registration": {
                "mode": "manual",
                "issuer": "https://auth.example.test",
                "token_endpoint_auth_method": "client_secret_post",
            },
            "token": {
                "connector_endpoint": "https://mcp.example.test/public/mcp",
                "resource": "https://mcp.example.test/public/mcp",
                "issuer": "https://auth.example.test",
            },
        },
        oauth_secret_refs_jsonb={
            "refs": {name: f"mcp_connector:mcp_alpha:{name}" for name in secret_values}
        },
        oauth_connected_identity="alice@example.test",
        oauth_last_error="stale failure",
    )
    deleted_states: list[str] = []

    monkeypatch.setattr(
        mcp_connector_oauth,
        "_resolve_connector_oauth_secrets",
        lambda _conn, _row: dict(secret_values),
    )
    monkeypatch.setattr(
        mcp_connector_oauth_provider,
        "_resolve_connector_oauth_secrets",
        mcp_connector_oauth._resolve_connector_oauth_secrets,
    )
    monkeypatch.setattr(
        mcp_connector_oauth,
        "_post_form",
        lambda *args, **kwargs: pytest.fail("poisoned revocation endpoint must not receive token"),
    )
    monkeypatch.setattr(
        mcp_connector_oauth.model_provider_store,
        "delete_secret_ref",
        lambda _conn, *, ref_key, **_kwargs: deleted_refs.append(ref_key),
    )
    monkeypatch.setattr(
        mcp_connector_oauth_repository.McpConnectorStore,
        "update_connector_row",
        lambda _self, _conn, _connector_id, payload, **_kwargs: row.update(payload),
    )
    monkeypatch.setattr(
        mcp_connector_oauth.mcp_connector_oauth_store,
        "delete_connector_oauth_states",
        lambda _conn, *, connector_id, **_kwargs: deleted_states.append(connector_id),
    )

    mcp_connector_oauth.revoke_mcp_connector_oauth(object(), connector_row=row)

    assert sorted(deleted_refs) == sorted(
        f"mcp_connector:mcp_alpha:{name}"
        for name in (
            mcp_connector_oauth.MCP_OAUTH_ACCESS_TOKEN_SECRET,
            mcp_connector_oauth.MCP_OAUTH_REFRESH_TOKEN_SECRET,
        )
    )
    assert deleted_states == ["mcp_alpha"]
    assert row["auth_status"] == "auth_required"
    assert set(row["oauth_secret_refs_jsonb"]["refs"]) == {
        mcp_connector_oauth.MCP_OAUTH_CLIENT_ID_SECRET,
        mcp_connector_oauth.MCP_OAUTH_CLIENT_SECRET_SECRET,
        mcp_connector_oauth.MCP_OAUTH_REGISTRATION_ACCESS_TOKEN_SECRET,
    }
    assert "token" not in row["oauth_metadata_jsonb"]
    assert row["oauth_metadata_jsonb"]["registration"]["mode"] == "manual"
    assert row["oauth_connected_identity"] == ""
    assert row["oauth_last_error"] == ""
    assert row["oauth_credential_revision"] == 2

    row["oauth_metadata_jsonb"]["discovery"] = {
        "issuer": "https://auth.example.test",
        "authorization_server": {},
    }
    registration = mcp_connector_oauth_provider._register_client(
        object(),
        connector_row=row,
        discovery={
            "issuer": "https://auth.example.test",
            "authorization_server": {},
        },
        scopes=["files:read"],
        post_json=lambda *_args, **_kwargs: pytest.fail(
            "a new authorization must reuse the preserved manual credentials"
        ),
    )
    assert registration == {
        "mode": "manual",
        "client_id": "client-1",
        "client_secret": "secret-1",
        "token_endpoint_auth_method": "client_secret_basic",
    }


def test_connector_delete_revoke_purges_every_oauth_secret_ref(monkeypatch) -> None:
    secret_names = (
        mcp_connector_oauth.MCP_OAUTH_ACCESS_TOKEN_SECRET,
        mcp_connector_oauth.MCP_OAUTH_REFRESH_TOKEN_SECRET,
        mcp_connector_oauth.MCP_OAUTH_CLIENT_ID_SECRET,
        mcp_connector_oauth.MCP_OAUTH_CLIENT_SECRET_SECRET,
        mcp_connector_oauth.MCP_OAUTH_REGISTRATION_ACCESS_TOKEN_SECRET,
    )
    refs = {name: f"mcp_connector:mcp_alpha:{name}" for name in secret_names}
    deleted_refs: list[str] = []
    row = _connector_row(oauth_secret_refs_jsonb={"refs": refs})
    monkeypatch.setattr(
        mcp_connector_oauth,
        "_resolve_connector_oauth_secrets",
        lambda _conn, _row: {},
    )
    monkeypatch.setattr(
        mcp_connector_oauth.model_provider_store,
        "delete_secret_ref",
        lambda _conn, *, ref_key, **_kwargs: deleted_refs.append(ref_key),
    )
    monkeypatch.setattr(
        mcp_connector_oauth.mcp_connector_oauth_store,
        "delete_connector_oauth_states",
        lambda *_args, **_kwargs: 0,
    )
    monkeypatch.setattr(
        mcp_connector_oauth_repository.McpConnectorStore,
        "update_connector_row",
        lambda *_args, **_kwargs: pytest.fail(
            "connector delete already cleared row state and must only purge refs"
        ),
    )

    mcp_connector_oauth.prepare_mcp_connector_oauth_delete_locked(
        object(),
        connector_row=row,
    )

    assert sorted(deleted_refs) == sorted(refs.values())


def test_connector_delete_revoke_surfaces_local_secret_delete_failure(monkeypatch) -> None:
    ref_key = "mcp_connector:mcp_alpha:oauth_client_secret"
    row = _connector_row(
        oauth_secret_refs_jsonb={
            "refs": {mcp_connector_oauth.MCP_OAUTH_CLIENT_SECRET_SECRET: ref_key}
        }
    )
    monkeypatch.setattr(
        mcp_connector_oauth,
        "_resolve_connector_oauth_secrets",
        lambda _conn, _row: {},
    )
    monkeypatch.setattr(
        mcp_connector_oauth.model_provider_store,
        "delete_secret_ref",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("injected local secret delete failure")
        ),
    )

    _raises_input_2912_1 = object()
    with pytest.raises(RuntimeError, match="injected local secret delete failure"):
        mcp_connector_oauth.revoke_mcp_connector_oauth(
            _raises_input_2912_1, connector_row=row, clear_connector_state=False
        )


def test_save_credentials_is_scoped_write_only_and_invalidates_pending_tokens(
    monkeypatch,
) -> None:
    monkeypatch.setenv("ENCRYPTION_KEY", "credential-secret-key")
    row = _connector_row(
        oauth_credential_revision=4,
        oauth_metadata_jsonb={
            "requested_scopes": ["files:read"],
            "authorization_parameters": {"prompt": "consent"},
            "refresh_token_required": True,
            "discovery": {"issuer": "https://auth.example.test"},
            "registration": {"mode": "dynamic", "issuer": "https://auth.example.test"},
            "token": {"resource": "https://mcp.example.test/public/mcp"},
        },
        oauth_secret_refs_jsonb={
            "refs": {
                name: f"mcp_connector:mcp_alpha:{name}"
                for name in (
                    mcp_connector_oauth.MCP_OAUTH_ACCESS_TOKEN_SECRET,
                    mcp_connector_oauth.MCP_OAUTH_REFRESH_TOKEN_SECRET,
                    mcp_connector_oauth.MCP_OAUTH_CLIENT_ID_SECRET,
                    mcp_connector_oauth.MCP_OAUTH_CLIENT_SECRET_SECRET,
                )
            }
        },
        oauth_connected_identity="alice@example.test",
    )
    deleted_refs: list[tuple[str, str, str, str]] = []
    deleted_states: list[tuple[str, str, str, str]] = []
    writes: list[dict[str, str]] = []

    monkeypatch.setattr(
        mcp_connector_oauth_repository.McpConnectorStore,
        "get_connector_row",
        lambda _self, _conn, _connector_id, **_kwargs: row,
    )
    monkeypatch.setattr(
        mcp_connector_oauth_repository.McpConnectorStore,
        "update_connector_row",
        lambda _self, _conn, _connector_id, payload, **_kwargs: row.update(payload),
    )
    monkeypatch.setattr(
        mcp_connector_oauth.model_provider_store,
        "delete_secret_ref",
        lambda _conn, *, account_id, project_id, tenant_id, ref_key: deleted_refs.append(
            (account_id, project_id, tenant_id, ref_key)
        ),
    )
    monkeypatch.setattr(
        mcp_connector_oauth.model_provider_store,
        "upsert_secret_ref",
        lambda _conn, **kwargs: writes.append(dict(kwargs)),
    )
    monkeypatch.setattr(
        mcp_connector_oauth.mcp_connector_oauth_store,
        "delete_connector_oauth_states",
        lambda _conn, *, connector_id, account_id, project_id, tenant_id: deleted_states.append(
            (connector_id, account_id, project_id, tenant_id)
        ),
    )

    result = mcp_connector_oauth.save_mcp_connector_oauth_credentials(
        object(),
        connector_row=row,
        client_id=" client-1 ",
        client_secret="  opaque secret  ",
    )

    assert result == {"saved": True, "has_client_id": True, "has_client_secret": True}
    assert "client-1" not in str(result)
    assert "opaque secret" not in str(result)
    assert deleted_states == [("mcp_alpha", "acc_alpha", "project_alpha", "tenant_alpha")]
    assert {item[-1] for item in deleted_refs} == {
        f"mcp_connector:mcp_alpha:{name}"
        for name in (
            mcp_connector_oauth.MCP_OAUTH_ACCESS_TOKEN_SECRET,
            mcp_connector_oauth.MCP_OAUTH_REFRESH_TOKEN_SECRET,
            mcp_connector_oauth.MCP_OAUTH_CLIENT_ID_SECRET,
            mcp_connector_oauth.MCP_OAUTH_CLIENT_SECRET_SECRET,
        )
    }
    assert all(item[:3] == ("acc_alpha", "project_alpha", "tenant_alpha") for item in deleted_refs)
    assert {
        write["ref_key"]: mcp_connector_oauth.decrypt_secret(write["encrypted_value"])
        for write in writes
    } == {
        "mcp_connector:mcp_alpha:_oauth_client_id": "client-1",
        "mcp_connector:mcp_alpha:_oauth_client_secret": "  opaque secret  ",
    }
    assert all(
        (write["account_id"], write["project_id"], write["tenant_id"])
        == ("acc_alpha", "project_alpha", "tenant_alpha")
        for write in writes
    )
    assert row["oauth_credential_revision"] == 5
    assert row["oauth_connected_identity"] == ""
    assert row["oauth_metadata_jsonb"]["requested_scopes"] == ["files:read"]
    assert row["oauth_metadata_jsonb"]["authorization_parameters"] == {"prompt": "consent"}
    assert row["oauth_metadata_jsonb"]["refresh_token_required"] is True
    assert "token" not in row["oauth_metadata_jsonb"]
    assert row["oauth_metadata_jsonb"]["registration"]["mode"] == "manual"
    assert row["oauth_last_error"] == ("MCP OAuth client credentials changed; reconnect required")


def test_scope_change_discards_dynamic_registration_but_preserves_manual_credentials(
    monkeypatch,
) -> None:
    deleted: list[str] = []
    monkeypatch.setattr(
        mcp_connector_oauth.model_provider_store,
        "delete_secret_ref",
        lambda _conn, *, ref_key, **_kwargs: deleted.append(ref_key),
    )
    monkeypatch.setattr(
        mcp_connector_oauth.mcp_connector_oauth_store,
        "delete_connector_oauth_states",
        lambda *_args, **_kwargs: None,
    )
    dynamic = _connector_row(
        oauth_metadata_jsonb={
            "requested_scopes": ["files:read"],
            "discovery": {"issuer": "https://auth.example.test"},
            "registration": {"mode": "dynamic", "client_id": "old-client"},
        },
        oauth_secret_refs_jsonb={
            "refs": {
                mcp_connector_oauth.MCP_OAUTH_CLIENT_ID_SECRET: "dynamic-client-id-ref",
                mcp_connector_oauth.MCP_OAUTH_CLIENT_SECRET_SECRET: "dynamic-secret-ref",
                mcp_connector_oauth.MCP_OAUTH_REGISTRATION_ACCESS_TOKEN_SECRET: (
                    "dynamic-registration-token-ref"
                ),
            }
        },
    )

    dynamic_reset = mcp_connector_oauth.mcp_connector_oauth_reset_updates(
        object(),
        connector_row=dynamic,
        auth_status="auth_required",
        preserve_client_credentials=True,
    )

    assert dynamic_reset["oauth_secret_refs_jsonb"] == {}
    assert "registration" not in dynamic_reset["oauth_metadata_jsonb"]
    assert dynamic_reset["oauth_metadata_jsonb"]["discovery"]["issuer"] == (
        "https://auth.example.test"
    )
    assert set(deleted) == {
        "dynamic-client-id-ref",
        "dynamic-secret-ref",
        "dynamic-registration-token-ref",
    }

    deleted.clear()
    manual = deepcopy(dynamic)
    manual["oauth_metadata_jsonb"]["registration"] = {"mode": "manual"}
    manual_reset = mcp_connector_oauth.mcp_connector_oauth_reset_updates(
        object(),
        connector_row=manual,
        auth_status="auth_required",
        preserve_client_credentials=True,
    )
    assert set(manual_reset["oauth_secret_refs_jsonb"]["refs"]) == {
        mcp_connector_oauth.MCP_OAUTH_CLIENT_ID_SECRET,
        mcp_connector_oauth.MCP_OAUTH_CLIENT_SECRET_SECRET,
        mcp_connector_oauth.MCP_OAUTH_REGISTRATION_ACCESS_TOKEN_SECRET,
    }
    assert manual_reset["oauth_metadata_jsonb"]["registration"] == {"mode": "manual"}
    assert deleted == []


@pytest.mark.parametrize(
    ("client_id", "client_secret"),
    [("", "secret"), ("   ", "secret"), ("client", "")],
)
def test_save_credentials_requires_both_values_without_writes(
    monkeypatch,
    client_id,
    client_secret,
) -> None:
    monkeypatch.setattr(
        mcp_connector_oauth.model_provider_store,
        "upsert_secret_ref",
        lambda *_args, **_kwargs: pytest.fail("invalid credentials must not write secrets"),
    )
    _raises_input_3105_1 = object()
    _raises_input_3105_2 = _connector_row()
    with pytest.raises(OAuthConnectionError, match="required"):
        mcp_connector_oauth.save_mcp_connector_oauth_credentials(
            _raises_input_3105_1,
            connector_row=_raises_input_3105_2,
            client_id=client_id,
            client_secret=client_secret,
        )


def test_save_credentials_rolls_back_all_oauth_mutations_when_final_update_fails(
    monkeypatch,
) -> None:
    monkeypatch.setenv("ENCRYPTION_KEY", "encryption-secret")
    old_refs = {
        name: f"mcp_connector:mcp_alpha:{name}"
        for name in (
            mcp_connector_oauth.MCP_OAUTH_ACCESS_TOKEN_SECRET,
            mcp_connector_oauth.MCP_OAUTH_REFRESH_TOKEN_SECRET,
            mcp_connector_oauth.MCP_OAUTH_CLIENT_ID_SECRET,
            mcp_connector_oauth.MCP_OAUTH_CLIENT_SECRET_SECRET,
        )
    }
    row = _connector_row(
        oauth_metadata_jsonb={"token": {"issuer": "https://auth.example.test"}},
        oauth_secret_refs_jsonb={"refs": old_refs},
        oauth_credential_revision=4,
    )
    database = {
        "connector": deepcopy(row),
        "secrets": {ref_key: f"old:{name}" for name, ref_key in old_refs.items()},
        "states": ["pending-state"],
    }
    original = deepcopy(database)

    class AtomicConnection:
        autocommit = True

        @contextmanager
        def transaction(self):
            snapshot = deepcopy(database)
            try:
                yield
            except Exception:
                database.clear()
                database.update(snapshot)
                raise

    connection = AtomicConnection()
    monkeypatch.setattr(
        mcp_connector_oauth_repository,
        "_latest_connector_row",
        lambda *_args, **_kwargs: database["connector"],
    )
    monkeypatch.setattr(
        mcp_connector_oauth.model_provider_store,
        "delete_secret_ref",
        lambda _conn, *, ref_key, **_kwargs: database["secrets"].pop(ref_key),
    )
    monkeypatch.setattr(
        mcp_connector_oauth.model_provider_store,
        "upsert_secret_ref",
        lambda _conn, *, ref_key, encrypted_value, **_kwargs: database["secrets"].__setitem__(
            ref_key, encrypted_value
        ),
    )
    monkeypatch.setattr(
        mcp_connector_oauth.mcp_connector_oauth_store,
        "delete_connector_oauth_states",
        lambda _conn, **_kwargs: database["states"].clear(),
    )

    def _fail_final_update(_self, _conn, _connector_id, payload, **_kwargs):
        database["connector"].update(payload)
        raise RuntimeError("injected connector update failure")

    monkeypatch.setattr(
        mcp_connector_oauth_repository.McpConnectorStore,
        "update_connector_row",
        _fail_final_update,
    )

    with pytest.raises(RuntimeError, match="injected connector update failure"):
        mcp_connector_oauth.save_mcp_connector_oauth_credentials(
            connection,
            connector_row=row,
            client_id="replacement-client",
            client_secret="replacement-secret",
        )

    assert database == original
    assert connection.autocommit is True


def test_save_credentials_rejects_stale_revision_before_secret_writes(monkeypatch) -> None:
    row = _connector_row(oauth_credential_revision=4)
    monkeypatch.setattr(
        mcp_connector_oauth,
        "_locked_connector_row",
        lambda _conn, _row: {**row, "oauth_credential_revision": 5},
    )
    monkeypatch.setattr(
        mcp_connector_oauth.model_provider_store,
        "upsert_secret_ref",
        lambda *_args, **_kwargs: pytest.fail("stale save must not write secret refs"),
    )

    _raises_input_3211_1 = object()
    with pytest.raises(OAuthConnectionError, match="credential revision changed"):
        mcp_connector_oauth.save_mcp_connector_oauth_credentials(
            _raises_input_3211_1,
            connector_row=row,
            client_id="replacement-client",
            client_secret="replacement-secret",
        )


@pytest.mark.parametrize("provider_fails", [False, True])
def test_save_credentials_revokes_old_grant_only_after_local_commit(
    monkeypatch,
    provider_fails: bool,
) -> None:
    monkeypatch.setenv("ENCRYPTION_KEY", "replacement-credential-test-key")
    refs = {
        name: f"mcp_connector:mcp_alpha:{name}"
        for name in (
            mcp_connector_oauth.MCP_OAUTH_ACCESS_TOKEN_SECRET,
            mcp_connector_oauth.MCP_OAUTH_REFRESH_TOKEN_SECRET,
            mcp_connector_oauth.MCP_OAUTH_CLIENT_ID_SECRET,
            mcp_connector_oauth.MCP_OAUTH_CLIENT_SECRET_SECRET,
        )
    }
    values = {
        mcp_connector_oauth.MCP_OAUTH_ACCESS_TOKEN_SECRET: "old-access",
        mcp_connector_oauth.MCP_OAUTH_REFRESH_TOKEN_SECRET: "old-refresh",
        mcp_connector_oauth.MCP_OAUTH_CLIENT_ID_SECRET: "old-client",
        mcp_connector_oauth.MCP_OAUTH_CLIENT_SECRET_SECRET: "old-secret",
    }
    row = _connector_row(
        oauth_credential_revision=4,
        oauth_metadata_jsonb={
            "discovery": {
                "issuer": "https://auth.example.test",
                "authorization_server": {
                    "issuer": "https://auth.example.test",
                    "revocation_endpoint": "https://auth.example.test/revoke",
                },
            },
            "registration": {
                "mode": "manual",
                "issuer": "https://auth.example.test",
                "token_endpoint_auth_method": "client_secret_post",
            },
            "token": {"issuer": "https://auth.example.test"},
        },
        oauth_secret_refs_jsonb={"refs": refs},
        oauth_connected_identity="alice@example.test",
    )
    events: list[str] = []

    class ManagedConnection:
        autocommit = True

        @contextmanager
        def transaction(self):
            yield
            events.append("db-commit")

    connection = ManagedConnection()
    monkeypatch.setattr(
        mcp_connector_oauth,
        "_locked_connector_row",
        lambda _conn, _row: row,
    )
    monkeypatch.setattr(
        mcp_connector_oauth,
        "_resolve_connector_oauth_secrets",
        lambda _conn, _row: dict(values),
    )

    def _post_form(url, data, *, purpose, basic_auth=None):
        assert url == "https://auth.example.test/revoke"
        assert data["token"] == "old-refresh"
        assert data["client_id"] == "old-client"
        assert data["client_secret"] == "old-secret"
        assert basic_auth is None
        events.append("provider-revoke")
        if provider_fails:
            raise OAuthConnectionError("provider unavailable")
        return {}

    monkeypatch.setattr(mcp_connector_oauth, "_post_form", _post_form)
    monkeypatch.setattr(
        mcp_connector_oauth.model_provider_store,
        "delete_secret_ref",
        lambda _conn, *, ref_key, **_kwargs: events.append(f"delete:{ref_key}"),
    )
    monkeypatch.setattr(
        mcp_connector_oauth.model_provider_store,
        "upsert_secret_ref",
        lambda _conn, *, ref_key, **_kwargs: events.append(f"save:{ref_key}"),
    )
    monkeypatch.setattr(
        mcp_connector_oauth.mcp_connector_oauth_store,
        "delete_connector_oauth_states",
        lambda *_args, **_kwargs: events.append("delete-states"),
    )
    monkeypatch.setattr(
        mcp_connector_oauth_repository.McpConnectorStore,
        "update_connector_row",
        lambda _self, _conn, _connector_id, payload, **_kwargs: row.update(payload),
    )

    result = mcp_connector_oauth.save_mcp_connector_oauth_credentials(
        connection,
        connector_row=row,
        client_id="new-client",
        client_secret="new-secret",
    )

    assert result["saved"] is True
    assert events[-2:] == ["db-commit", "provider-revoke"]
    assert events[-1] == "provider-revoke"
    assert any(event.startswith("delete:") for event in events[:-1])
    assert row["oauth_credential_revision"] == 5


def test_disconnect_rejects_stale_revision_before_local_mutations(monkeypatch) -> None:
    row = _connector_row(oauth_credential_revision=4)
    monkeypatch.setattr(
        mcp_connector_oauth,
        "_locked_connector_row",
        lambda _conn, _row: {**row, "oauth_credential_revision": 5},
    )
    monkeypatch.setattr(
        mcp_connector_oauth.model_provider_store,
        "delete_secret_ref",
        lambda *_args, **_kwargs: pytest.fail("stale disconnect must not delete secrets"),
    )

    _raises_input_3343_1 = object()
    with pytest.raises(OAuthConnectionError, match="credential revision changed"):
        mcp_connector_oauth.revoke_mcp_connector_oauth(_raises_input_3343_1, connector_row=row)


def test_disconnect_rolls_back_all_local_mutations_when_final_update_fails(monkeypatch) -> None:
    ref_key = "mcp_connector:mcp_alpha:_oauth_access_token"
    row = _connector_row(
        oauth_credential_revision=4,
        oauth_metadata_jsonb={
            "discovery": {
                "authorization_server": {"revocation_endpoint": "https://auth.example.test/revoke"}
            },
            "token": {"issuer": "https://auth.example.test"},
        },
        oauth_secret_refs_jsonb={
            "refs": {mcp_connector_oauth.MCP_OAUTH_ACCESS_TOKEN_SECRET: ref_key}
        },
    )
    database = {
        "connector": deepcopy(row),
        "secrets": {ref_key: "encrypted-old-access"},
        "states": ["pending-state"],
    }
    original = deepcopy(database)

    class AtomicConnection:
        autocommit = True

        @contextmanager
        def transaction(self):
            snapshot = deepcopy(database)
            try:
                yield
            except Exception:
                database.clear()
                database.update(snapshot)
                raise

    connection = AtomicConnection()
    monkeypatch.setattr(
        mcp_connector_oauth,
        "_locked_connector_row",
        lambda *_args, **_kwargs: database["connector"],
    )
    monkeypatch.setattr(
        mcp_connector_oauth,
        "_resolve_connector_oauth_secrets",
        lambda *_args, **_kwargs: {mcp_connector_oauth.MCP_OAUTH_ACCESS_TOKEN_SECRET: "old-access"},
    )
    remote_revoke = MagicMock()
    monkeypatch.setattr(mcp_connector_oauth, "_post_form", remote_revoke)
    monkeypatch.setattr(
        mcp_connector_oauth.model_provider_store,
        "delete_secret_ref",
        lambda _conn, *, ref_key, **_kwargs: database["secrets"].pop(ref_key),
    )
    monkeypatch.setattr(
        mcp_connector_oauth.mcp_connector_oauth_store,
        "delete_connector_oauth_states",
        lambda *_args, **_kwargs: database["states"].clear(),
    )

    def _fail_update(_self, _conn, _connector_id, payload, **_kwargs):
        database["connector"].update(payload)
        raise RuntimeError("injected disconnect update failure")

    monkeypatch.setattr(
        mcp_connector_oauth_repository.McpConnectorStore,
        "update_connector_row",
        _fail_update,
    )

    with pytest.raises(RuntimeError, match="injected disconnect update failure"):
        mcp_connector_oauth.revoke_mcp_connector_oauth(connection, connector_row=row)

    assert database == original
    remote_revoke.assert_not_called()


def test_first_start_binds_unbound_manual_credentials_to_discovered_issuer(
    monkeypatch,
) -> None:
    monkeypatch.setenv("FS_SECRET_KEY", "state-secret")
    monkeypatch.setenv("ENCRYPTION_KEY", "encryption-secret")
    monkeypatch.setattr(
        mcp_connector_oauth,
        "_oauth_callback_url",
        lambda: "https://flow.example.test/api/integrations/oauth/callback",
    )
    monkeypatch.setattr(
        mcp_connector_oauth_provider, "_oauth_callback_url", mcp_connector_oauth._oauth_callback_url
    )
    monkeypatch.setattr(
        mcp_connector_oauth,
        "_validate_metadata_url",
        lambda url, *, purpose: url,
    )
    monkeypatch.setattr(
        mcp_connector_oauth_policy,
        "_validate_metadata_url",
        mcp_connector_oauth._validate_metadata_url,
    )
    monkeypatch.setattr(
        mcp_connector_oauth_provider,
        "_validate_metadata_url",
        mcp_connector_oauth._validate_metadata_url,
    )
    monkeypatch.setattr(
        mcp_connector_oauth,
        "_probe_mcp_oauth_www_authenticate",
        lambda _connector_row: "",
    )
    row = _connector_row(
        oauth_metadata_jsonb={
            "requested_scopes": ["files:read"],
            "authorization_parameters": {"prompt": "consent"},
            "refresh_token_required": True,
        }
    )
    secret_values: dict[str, str] = {}
    stored_state: dict[str, Any] = {}
    persistence_events: list[tuple[str, str]] = []

    def _update(_self, _conn, _connector_id, payload, **_kwargs):
        row.update(payload)
        registration = payload.get("oauth_metadata_jsonb", {}).get("registration", {})
        persistence_events.append(("connector", str(registration.get("issuer") or "")))

    def _insert_state(_conn, **kwargs):
        stored_state.update(kwargs)
        persistence_events.append(("state", kwargs["auth_payload"]["auth_config"]["issuer"]))

    monkeypatch.setattr(
        mcp_connector_oauth_repository.McpConnectorStore,
        "get_connector_row",
        lambda _self, _conn, _connector_id, **_kwargs: row,
    )
    monkeypatch.setattr(
        mcp_connector_oauth_repository.McpConnectorStore,
        "update_connector_row",
        _update,
    )
    monkeypatch.setattr(
        mcp_connector_oauth.mcp_connector_oauth_store,
        "delete_connector_oauth_states",
        lambda _conn, **_kwargs: 0,
    )
    monkeypatch.setattr(
        mcp_connector_oauth.mcp_connector_oauth_store,
        "insert_oauth_state",
        _insert_state,
    )

    def _save(_conn, *, connector_id, secrets_to_save, **_kwargs):
        secret_values.update(
            {
                name: str(value)
                for name, value in secrets_to_save.items()
                if value is not None and str(value) != ""
            }
        )
        return {"refs": {name: f"mcp_connector:{connector_id}:{name}" for name in secret_values}}

    monkeypatch.setattr(mcp_connector_oauth, "_save_connector_oauth_secrets", _save)
    monkeypatch.setattr(
        mcp_connector_oauth_provider,
        "_save_connector_oauth_secrets",
        mcp_connector_oauth._save_connector_oauth_secrets,
    )
    monkeypatch.setattr(
        mcp_connector_oauth,
        "_resolve_connector_oauth_secrets",
        lambda _conn, _row: dict(secret_values),
    )
    monkeypatch.setattr(
        mcp_connector_oauth_provider,
        "_resolve_connector_oauth_secrets",
        mcp_connector_oauth._resolve_connector_oauth_secrets,
    )

    saved = mcp_connector_oauth.save_mcp_connector_oauth_credentials(
        object(),
        connector_row=row,
        client_id="manual-client",
        client_secret="opaque-secret",
    )

    assert saved == {"saved": True, "has_client_id": True, "has_client_secret": True}
    assert row["oauth_metadata_jsonb"]["registration"]["issuer"] == ""
    assert row["oauth_credential_revision"] == 2
    assert row["oauth_last_error"] == ""

    def _fetch_json(url: str, *, purpose: str, timeout_seconds: float = 15.0):
        _ = (purpose, timeout_seconds)
        if "oauth-protected-resource/public/mcp" in url:
            return {
                "resource": "https://mcp.example.test/public/mcp",
                "authorization_servers": ["https://auth.example.test"],
                "scopes_supported": ["files:read"],
            }
        if url.endswith("/.well-known/oauth-authorization-server"):
            return {
                "issuer": "https://auth.example.test",
                "authorization_endpoint": "https://auth.example.test/authorize",
                "token_endpoint": "https://auth.example.test/token",
                "code_challenge_methods_supported": ["S256"],
            }
        raise OAuthConnectionError(f"unexpected URL {url}")

    result = mcp_connector_oauth.start_mcp_connector_oauth(
        object(),
        connector_row=row,
        fetch_json=_fetch_json,
        post_json=lambda *_args, **_kwargs: pytest.fail(
            "stored manual credentials must not use dynamic registration"
        ),
        now=1000.0,
    )

    assert result["registration_mode"] == "manual"
    assert "client_id=manual-client" in result["authorization_url"]
    assert "scope=files%3Aread" in result["authorization_url"]
    assert "prompt=consent" in result["authorization_url"]
    assert row["oauth_metadata_jsonb"]["registration"] == {
        "mode": "manual",
        "issuer": "https://auth.example.test",
        "token_endpoint_auth_method": "client_secret_basic",
    }
    assert stored_state["oauth_credential_revision"] == 3
    assert stored_state["auth_payload"]["client_id"] == "manual-client"
    assert stored_state["auth_payload"]["auth_config"]["issuer"] == ("https://auth.example.test")
    assert persistence_events[-3:] == [
        ("connector", "https://auth.example.test"),
        ("connector", ""),
        ("state", "https://auth.example.test"),
    ]
    assert "opaque-secret" not in str(stored_state)


def test_first_discovery_rebinds_manual_credentials_to_post_only_token_auth(
    monkeypatch,
) -> None:
    row = _connector_row(
        oauth_metadata_jsonb={
            "registration": {
                "mode": "manual",
                "issuer": "",
                "token_endpoint_auth_method": "client_secret_basic",
            }
        }
    )
    discovery = {
        "issuer": "https://auth.example.test",
        "authorization_server": {
            "issuer": "https://auth.example.test",
            "authorization_endpoint": "https://auth.example.test/authorize",
            "token_endpoint": "https://auth.example.test/token",
            "token_endpoint_auth_methods_supported": ["client_secret_post"],
            "code_challenge_methods_supported": ["S256"],
        },
    }
    monkeypatch.setattr(
        mcp_connector_oauth_provider,
        "_resolve_connector_oauth_secrets",
        lambda _conn, _row: {
            mcp_connector_oauth.MCP_OAUTH_CLIENT_ID_SECRET: "manual-client",
            mcp_connector_oauth.MCP_OAUTH_CLIENT_SECRET_SECRET: "manual-secret",
        },
    )
    persisted: list[dict[str, Any]] = []
    monkeypatch.setattr(
        mcp_connector_oauth_repository.McpConnectorStore,
        "update_connector_row",
        lambda _self, _conn, _connector_id, payload, **_kwargs: persisted.append(payload),
    )

    registration = mcp_connector_oauth_provider._register_client(
        object(),
        connector_row=row,
        discovery=discovery,
        scopes=["files:read"],
        post_json=lambda *_args, **_kwargs: pytest.fail(
            "stored manual credentials must not use dynamic registration"
        ),
    )

    assert registration["token_endpoint_auth_method"] == "client_secret_post"
    assert persisted[-1]["oauth_metadata_jsonb"]["registration"] == {
        "mode": "manual",
        "issuer": "https://auth.example.test",
        "token_endpoint_auth_method": "client_secret_post",
    }
    captured: dict[str, Any] = {}

    def _post_form(url, data, *, purpose, basic_auth=None):
        captured.update(url=url, data=data, purpose=purpose, basic_auth=basic_auth)
        return {"access_token": "provider-token", "token_type": "Bearer"}

    mcp_connector_oauth_tokens._exchange_token(
        auth_config={
            "token_endpoint": "https://auth.example.test/token",
            "token_endpoint_auth_method": registration["token_endpoint_auth_method"],
            "resource": "https://mcp.example.test/public/mcp",
        },
        data={"grant_type": "authorization_code", "code": "provider-code"},
        client_id=registration["client_id"],
        client_secret=registration["client_secret"],
        post_form=_post_form,
    )

    assert captured["basic_auth"] is None
    assert captured["data"]["client_id"] == "manual-client"
    assert captured["data"]["client_secret"] == "manual-secret"


def test_start_reports_structured_manual_credentials_requirement(monkeypatch) -> None:
    row = _connector_row()
    monkeypatch.setattr(
        mcp_connector_oauth_provider,
        "_resolve_connector_oauth_secrets",
        lambda _conn, _row: {},
    )

    _raises_input_3643_1 = object()
    with pytest.raises(
        mcp_connector_oauth.McpOAuthManualCredentialsRequired,
        match="client credentials",
    ) as exc_info:
        mcp_connector_oauth_provider._register_client(
            _raises_input_3643_1,
            connector_row=row,
            discovery={"issuer": "https://auth.example.test", "authorization_server": {}},
            scopes=["catalog.read"],
        )

    assert exc_info.value.reason_code == "manual_credentials_required"


def test_oauth_status_exposes_the_exact_runtime_redirect_uri(monkeypatch) -> None:
    redirect_uri = "https://flow.example.test/api/integrations/oauth/callback"
    monkeypatch.setattr(mcp_connector_oauth_tokens, "_oauth_callback_url", lambda: redirect_uri)
    monkeypatch.setattr(
        mcp_connector_oauth_tokens,
        "_resolve_connector_oauth_secrets",
        lambda _conn, _row: {},
    )

    result = mcp_connector_oauth.mcp_connector_oauth_status(
        object(),
        connector_row=_connector_row(auth_status="auth_required"),
    )

    assert result["redirect_uri"] == redirect_uri


def test_post_form_http_error_does_not_expose_response_body_secrets(monkeypatch) -> None:
    monkeypatch.setattr(
        mcp_connector_oauth_provider,
        "_validate_metadata_url",
        lambda url, *, purpose: url,
    )

    def _raise_http_error(_request, *, timeout_seconds, purpose, require_private=False):
        _ = (timeout_seconds, purpose, require_private)
        raise urllib.error.HTTPError(
            "https://auth.example.test/token",
            400,
            "Bad Request",
            hdrs=None,
            fp=io.BytesIO(
                b'{"error":"invalid_grant","error_description":"refresh_token=leaked-refresh&client_secret=leaked-secret"}'
            ),
        )

    monkeypatch.setattr(
        mcp_connector_oauth_provider.url_safety, "open_pinned_url", _raise_http_error
    )

    with pytest.raises(OAuthConnectionError) as exc_info:
        mcp_connector_oauth_provider._post_form(
            "https://auth.example.test/token",
            {
                "grant_type": "refresh_token",
                "refresh_token": "submitted-refresh",
                "client_secret": "submitted-secret",
            },
            purpose="MCP OAuth token endpoint",
        )

    message = str(exc_info.value)
    assert message == "MCP OAuth token endpoint returned HTTP 400 (invalid_grant)"
    assert "leaked-refresh" not in message
    assert "leaked-secret" not in message
    assert "submitted-refresh" not in message
    assert "submitted-secret" not in message


def test_post_form_basic_auth_uses_form_encoded_opaque_credentials(monkeypatch) -> None:
    captured: dict[str, Any] = {}
    client_id = " client:+%雪 "
    client_secret = " secret:+%雪 "

    def _open(request, *, timeout_seconds, purpose, require_private=False):
        _ = (timeout_seconds, purpose, require_private)
        captured["request"] = request
        return io.BytesIO(b"{}")

    monkeypatch.setattr(mcp_connector_oauth_provider.url_safety, "open_pinned_url", _open)

    mcp_connector_oauth_provider._post_form(
        "https://auth.example.test/token",
        {"grant_type": "authorization_code"},
        purpose="MCP OAuth token endpoint",
        basic_auth=(client_id, client_secret),
    )

    request = captured["request"]
    expected = base64.b64encode(
        f"{quote_plus(client_id, safe='')}:{quote_plus(client_secret, safe='')}".encode()
    ).decode("ascii")
    assert request.get_header("Authorization") == f"Basic {expected}"
    assert request.data == b"grant_type=authorization_code"


def test_revoke_uses_stored_basic_auth_method_without_normalizing_secret(
    monkeypatch,
) -> None:
    client_secret = " secret:+%雪 "
    secret_values = {
        mcp_connector_oauth.MCP_OAUTH_REFRESH_TOKEN_SECRET: "refresh-token",
        mcp_connector_oauth.MCP_OAUTH_CLIENT_ID_SECRET: "client-id",
        mcp_connector_oauth.MCP_OAUTH_CLIENT_SECRET_SECRET: client_secret,
    }
    row = _connector_row(
        oauth_metadata_jsonb={
            "discovery": {
                "issuer": "https://auth.example.test",
                "resource": "https://mcp.example.test/public/mcp",
                "authorization_server": {
                    "issuer": "https://auth.example.test",
                    "authorization_endpoint": "https://auth.example.test/authorize",
                    "token_endpoint": "https://auth.example.test/token",
                    "revocation_endpoint": "https://auth.example.test/revoke",
                    "code_challenge_methods_supported": ["S256"],
                },
            },
            "registration": {
                "mode": "manual",
                "issuer": "https://auth.example.test",
                "token_endpoint_auth_method": "client_secret_basic",
            },
        },
        oauth_secret_refs_jsonb={
            "refs": {name: f"mcp_connector:mcp_alpha:{name}" for name in secret_values}
        },
    )
    posted: dict[str, Any] = {}
    monkeypatch.setattr(
        mcp_connector_oauth,
        "_resolve_connector_oauth_secrets",
        lambda _conn, _row: dict(secret_values),
    )
    monkeypatch.setattr(
        mcp_connector_oauth,
        "_post_form",
        lambda url, data, *, purpose, basic_auth=None: (
            posted.update(
                url=url,
                data=data,
                purpose=purpose,
                basic_auth=basic_auth,
            )
            or {}
        ),
    )
    monkeypatch.setattr(
        mcp_connector_oauth.model_provider_store,
        "delete_secret_ref",
        lambda *_args, **_kwargs: None,
    )

    mcp_connector_oauth.execute_prepared_mcp_connector_revocation(
        mcp_connector_oauth._prepare_mcp_connector_grant_revocation(
            object(),
            connector_row=row,
        )
    )

    assert posted == {
        "url": "https://auth.example.test/revoke",
        "data": {"token": "refresh-token"},
        "purpose": "MCP OAuth revocation endpoint",
        "basic_auth": ("client-id", client_secret),
    }


def test_refresh_failure_persists_sanitized_error(monkeypatch) -> None:
    secret_values = {
        mcp_connector_oauth.MCP_OAUTH_ACCESS_TOKEN_SECRET: "expired-access",
        mcp_connector_oauth.MCP_OAUTH_REFRESH_TOKEN_SECRET: "refresh-token",
        mcp_connector_oauth.MCP_OAUTH_EXPIRES_AT_SECRET: "1000",
        mcp_connector_oauth.MCP_OAUTH_GRANTED_SCOPES_SECRET: '["files:read"]',
        mcp_connector_oauth.MCP_OAUTH_CLIENT_ID_SECRET: "client-1",
        mcp_connector_oauth.MCP_OAUTH_CLIENT_SECRET_SECRET: "secret-1",
    }
    row = _connector_row(
        oauth_metadata_jsonb={
            "discovery": {
                "resource": "https://mcp.example.test/public/mcp",
                "authorization_server": {
                    "authorization_endpoint": "https://auth.example.test/authorize",
                    "token_endpoint": "https://auth.example.test/token",
                    "code_challenge_methods_supported": ["S256"],
                },
            },
            "registration": {
                "mode": "manual",
                "token_endpoint_auth_method": "client_secret_post",
            },
        },
        oauth_secret_refs_jsonb={
            "refs": {name: f"mcp_connector:mcp_alpha:{name}" for name in secret_values}
        },
    )

    monkeypatch.setattr(
        mcp_connector_oauth_repository.McpConnectorStore,
        "get_connector_row",
        lambda _self, _conn, _connector_id, **_kwargs: row,
    )
    monkeypatch.setattr(
        mcp_connector_oauth_repository.McpConnectorStore,
        "update_connector_row",
        lambda _self, _conn, _connector_id, payload, **_kwargs: row.update(payload),
    )
    monkeypatch.setattr(
        mcp_connector_oauth_tokens,
        "_resolve_connector_oauth_secrets",
        lambda _conn, _row: dict(secret_values),
    )

    @contextmanager
    def _refresh_lock(_conn, *, lock_name):
        _ = (_conn, lock_name)
        yield True, _conn

    monkeypatch.setattr(
        mcp_connector_oauth.mcp_connector_oauth_store,
        "refresh_advisory_lock",
        _refresh_lock,
    )

    def _post_form(url, data, *, purpose, basic_auth=None):
        _ = (url, data, purpose, basic_auth)
        raise OAuthConnectionError(
            "MCP OAuth token endpoint returned HTTP 400: "
            "refresh_token=leaked-refresh&client_secret=leaked-secret"
        )

    _raises_input_3879_1 = object()
    with pytest.raises(OAuthConnectionError) as exc_info:
        mcp_connector_oauth.ensure_valid_mcp_connector_access_token(
            _raises_input_3879_1, connector_row=row, now=2000.0, post_form=_post_form
        )

    for message in (str(exc_info.value), row["oauth_last_error"]):
        assert "leaked-refresh" not in message
        assert "leaked-secret" not in message
        assert "refresh_token=[redacted]" in message
        assert "client_secret=[redacted]" in message
    assert row["auth_status"] == "refresh_failed"


def test_public_cleartext_oauth_endpoint_stays_rejected_with_dev_override(monkeypatch) -> None:
    monkeypatch.setenv("FS_ALLOW_PRIVATE_REMOTE_URLS", "1")
    monkeypatch.setattr(
        mcp_connector_oauth_provider.url_safety,
        "_resolve_hostname_ips",
        lambda _hostname, *, port: {"93.184.216.34"},
    )
    with pytest.raises(OAuthConnectionError, match="private addresses"):
        mcp_connector_oauth_policy._validate_metadata_url(
            "http://oauth.example.test/token", purpose="OAuth token endpoint"
        )


def test_private_cleartext_oauth_endpoint_is_allowed_only_with_dev_override(monkeypatch) -> None:
    monkeypatch.setenv("FS_ALLOW_PRIVATE_REMOTE_URLS", "1")
    monkeypatch.setattr(
        mcp_connector_oauth_provider.url_safety,
        "_resolve_hostname_ips",
        lambda _hostname, *, port: {"10.0.0.9"},
    )
    assert (
        mcp_connector_oauth_policy._validate_metadata_url(
            "http://oauth.internal.test/token", purpose="OAuth token endpoint"
        )
        == "http://oauth.internal.test/token"
    )


@pytest.mark.parametrize(
    ("advertised", "expected"),
    [
        (None, "client_secret_basic"),
        (["client_secret_basic"], "client_secret_basic"),
        (["client_secret_basic", "client_secret_post"], "client_secret_post"),
    ],
)
def test_manual_credentials_choose_supported_token_auth_method(
    monkeypatch, advertised, expected
) -> None:
    server: dict[str, Any] = {}
    if advertised is not None:
        server["token_endpoint_auth_methods_supported"] = advertised
    row = _connector_row(
        oauth_metadata_jsonb={
            "discovery": {
                "issuer": "https://auth.example.test",
                "authorization_server": server,
            }
        }
    )
    updates: dict[str, Any] = {}
    monkeypatch.setattr(
        mcp_connector_oauth,
        "mcp_connector_oauth_reset_updates",
        lambda *_args, **_kwargs: {"oauth_credential_revision": 2},
    )
    monkeypatch.setattr(
        mcp_connector_oauth,
        "_save_connector_oauth_secrets",
        lambda *_args, **_kwargs: {"refs": {"_oauth_client_id": "client-ref"}},
    )
    monkeypatch.setattr(
        mcp_connector_oauth,
        "_update_connector_oauth",
        lambda *_args, **kwargs: updates.update(kwargs["updates"]),
    )
    mcp_connector_oauth._save_mcp_connector_oauth_credentials(
        object(), connector_row=row, client_id="client", client_secret="secret"
    )
    assert updates["oauth_metadata_jsonb"]["registration"]["token_endpoint_auth_method"] == expected


def test_manual_credentials_reject_unsupported_token_auth_methods() -> None:
    row = _connector_row(
        oauth_metadata_jsonb={
            "discovery": {
                "authorization_server": {
                    "token_endpoint_auth_methods_supported": ["private_key_jwt"]
                }
            }
        }
    )
    _raises_input_3977_1 = object()
    with pytest.raises(OAuthConnectionError, match="does not support"):
        mcp_connector_oauth._save_mcp_connector_oauth_credentials(
            _raises_input_3977_1, connector_row=row, client_id="client", client_secret="secret"
        )


def test_token_response_revision_mismatch_writes_no_secret_refs(monkeypatch) -> None:
    row = _connector_row(oauth_credential_revision=3)
    monkeypatch.setattr(
        mcp_connector_oauth_tokens,
        "_locked_connector_row",
        lambda _conn, _row: {**row, "oauth_credential_revision": 4},
    )
    monkeypatch.setattr(
        mcp_connector_oauth_tokens,
        "_save_connector_oauth_secrets",
        lambda *_args, **_kwargs: pytest.fail("stale exchange must not persist secrets"),
    )
    _raises_input_3995_1 = object()
    with pytest.raises(OAuthConnectionError, match="credential revision changed"):
        mcp_connector_oauth_tokens._store_token_response(
            _raises_input_3995_1,
            connector_row=row,
            auth_config={"connector_endpoint": row["base_url"]},
            token_response={"access_token": "stale-access"},
            granted_scope_fallback=[],
            expected_credential_revision=3,
        )


def test_token_response_missing_live_row_writes_no_secret_refs(monkeypatch) -> None:
    row = _connector_row(oauth_credential_revision=3)

    class _Cursor:
        description = ()

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, *_args, **_kwargs):
            return None

        def fetchone(self):
            return None

    class _Connection:
        def cursor(self):
            return _Cursor()

    monkeypatch.setattr(
        mcp_connector_oauth_tokens,
        "_save_connector_oauth_secrets",
        lambda *_args, **_kwargs: pytest.fail("deleted connector must not retain tokens"),
    )
    _raises_input_4033_1 = _Connection()
    with pytest.raises(OAuthConnectionError, match="no longer exists"):
        mcp_connector_oauth_tokens._store_token_response(
            _raises_input_4033_1,
            connector_row=row,
            auth_config={"connector_endpoint": row["base_url"]},
            token_response={"access_token": "stale-access"},
            granted_scope_fallback=[],
            expected_credential_revision=3,
        )


def test_refresh_cannot_restore_tokens_after_concurrent_revision_change(monkeypatch) -> None:
    row = _connector_row(
        base_url="https://mcp.example.test/mcp",
        oauth_credential_revision=3,
        oauth_metadata_jsonb={
            "discovery": {
                "issuer": "https://auth.example.test",
                "resource": "https://mcp.example.test/mcp",
                "authorization_server": {"token_endpoint": "https://auth.example.test/token"},
            },
            "registration": {
                "mode": "manual",
                "issuer": "https://auth.example.test",
                "token_endpoint_auth_method": "client_secret_basic",
            },
            "token": {
                "connector_endpoint": "https://mcp.example.test/mcp",
                "resource": "https://mcp.example.test/mcp",
                "issuer": "https://auth.example.test",
            },
        },
    )
    values = {
        mcp_connector_oauth.MCP_OAUTH_CLIENT_ID_SECRET: "client",
        mcp_connector_oauth.MCP_OAUTH_CLIENT_SECRET_SECRET: "secret",
        mcp_connector_oauth.MCP_OAUTH_REFRESH_TOKEN_SECRET: "refresh",
        mcp_connector_oauth.MCP_OAUTH_EXPIRES_AT_SECRET: "1",
    }
    monkeypatch.setattr(
        mcp_connector_oauth_tokens,
        "_resolve_connector_oauth_secrets",
        lambda _conn, _row: dict(values),
    )
    monkeypatch.setattr(
        mcp_connector_oauth_repository.McpConnectorStore,
        "get_connector_row",
        lambda *_args, **_kwargs: dict(row),
    )
    monkeypatch.setattr(
        mcp_connector_oauth_repository,
        "_locked_connector_row",
        lambda _conn, _row: dict(row),
    )
    monkeypatch.setattr(
        mcp_connector_oauth_tokens,
        "_locked_connector_row",
        mcp_connector_oauth_repository._locked_connector_row,
    )
    monkeypatch.setattr(
        mcp_connector_oauth_tokens,
        "_save_connector_oauth_secrets",
        lambda *_args, **_kwargs: pytest.fail("stale refresh must not persist secrets"),
    )

    @contextmanager
    def _refresh_lock(conn, *, lock_name):
        _ = lock_name
        yield True, conn

    monkeypatch.setattr(
        mcp_connector_oauth.mcp_connector_oauth_store,
        "refresh_advisory_lock",
        _refresh_lock,
    )

    def _post_form(*_args, **_kwargs):
        row["oauth_credential_revision"] = 4
        return {"access_token": "stale-access", "refresh_token": "stale-refresh"}

    _raises_input_4108_1 = object()
    with pytest.raises(OAuthConnectionError, match="credential revision changed"):
        mcp_connector_oauth.ensure_valid_mcp_connector_access_token(
            _raises_input_4108_1,
            connector_row=row,
            force_refresh=True,
            now=1000,
            post_form=_post_form,
        )


def test_refresh_rejects_new_credentials_before_contacting_stale_token_endpoint(
    monkeypatch,
) -> None:
    old_row = _connector_row(
        oauth_credential_revision=3,
        oauth_metadata_jsonb={
            "discovery": {
                "issuer": "https://old-auth.example.test",
                "resource": "https://mcp.example.test/public/mcp",
                "connector_endpoint": "https://mcp.example.test/public/mcp",
                "authorization_server": {"token_endpoint": "https://old-auth.example.test/token"},
            },
            "registration": {
                "mode": "manual",
                "issuer": "https://old-auth.example.test",
                "token_endpoint_auth_method": "client_secret_post",
            },
            "token": {
                "connector_endpoint": "https://mcp.example.test/public/mcp",
                "resource": "https://mcp.example.test/public/mcp",
                "issuer": "https://old-auth.example.test",
            },
        },
    )
    new_row = deepcopy(old_row)
    new_row["oauth_credential_revision"] = 4
    new_row["oauth_metadata_jsonb"]["discovery"]["issuer"] = "https://new-auth.example.test"
    new_row["oauth_metadata_jsonb"]["discovery"]["authorization_server"] = {
        "token_endpoint": "https://new-auth.example.test/token"
    }
    new_row["oauth_metadata_jsonb"]["registration"]["issuer"] = "https://new-auth.example.test"
    new_row["oauth_metadata_jsonb"]["token"]["issuer"] = "https://new-auth.example.test"
    old_values = {
        mcp_connector_oauth.MCP_OAUTH_CLIENT_ID_SECRET: "old-client",
        mcp_connector_oauth.MCP_OAUTH_CLIENT_SECRET_SECRET: "old-secret",
        mcp_connector_oauth.MCP_OAUTH_REFRESH_TOKEN_SECRET: "old-refresh",
        mcp_connector_oauth.MCP_OAUTH_EXPIRES_AT_SECRET: "1",
    }
    new_values = {
        mcp_connector_oauth.MCP_OAUTH_CLIENT_ID_SECRET: "new-client",
        mcp_connector_oauth.MCP_OAUTH_CLIENT_SECRET_SECRET: "new-secret",
        mcp_connector_oauth.MCP_OAUTH_REFRESH_TOKEN_SECRET: "new-refresh",
        mcp_connector_oauth.MCP_OAUTH_EXPIRES_AT_SECRET: "1",
    }
    monkeypatch.setattr(
        mcp_connector_oauth_tokens,
        "_resolve_connector_oauth_secrets",
        lambda _conn, row: dict(
            new_values if row.get("oauth_credential_revision") == 4 else old_values
        ),
    )
    monkeypatch.setattr(
        mcp_connector_oauth_repository.McpConnectorStore,
        "get_connector_row",
        lambda *_args, **_kwargs: deepcopy(new_row),
    )

    @contextmanager
    def _refresh_lock(conn, *, lock_name):
        _ = lock_name
        yield True, conn

    monkeypatch.setattr(
        mcp_connector_oauth.mcp_connector_oauth_store,
        "refresh_advisory_lock",
        _refresh_lock,
    )
    provider_calls: list[dict[str, Any]] = []

    def _post_form(url, data, **kwargs):
        provider_calls.append({"url": url, "data": data, **kwargs})
        return {"access_token": "must-not-store"}

    _raises_input_4187_1 = object()
    with pytest.raises(OAuthConnectionError, match="credential revision changed"):
        mcp_connector_oauth.ensure_valid_mcp_connector_access_token(
            _raises_input_4187_1,
            connector_row=old_row,
            force_refresh=True,
            now=1000,
            post_form=_post_form,
        )

    assert provider_calls == []


def test_mcp_provider_error_consumes_state_and_records_only_safe_error(monkeypatch) -> None:
    monkeypatch.setenv("FS_SECRET_KEY", "state-secret")
    payload = {
        "v": 1,
        "type": "mcp_connector",
        "connector_id": "mcp_alpha",
        "account_id": "acc_alpha",
        "project_id": "project_alpha",
        "tenant_id": "tenant_alpha",
        "oauth_credential_revision": 2,
        "exp": 2000,
    }
    state = oauth_client._build_state_token(payload)
    stored = {**payload, "return_path": "/registry/mcp/mcp_alpha?project_id=project_alpha"}
    consumed = [False]
    monkeypatch.setattr(
        mcp_connector_oauth.mcp_connector_oauth_store,
        "consume_oauth_state",
        lambda *_args, **_kwargs: (
            None if consumed[0] else (consumed.__setitem__(0, True) or stored)
        ),
    )
    monkeypatch.setattr(
        mcp_connector_oauth_repository.McpConnectorStore,
        "get_connector_row",
        lambda *_args, **_kwargs: _connector_row(oauth_credential_revision=2),
    )
    marked: dict[str, Any] = {}
    monkeypatch.setattr(
        mcp_connector_oauth,
        "_mark_auth_problem_if_current",
        lambda _conn, _row, **kwargs: marked.update(
            {key: value for key, value in kwargs.items() if key != "expected_credential_revision"}
        ),
    )
    result = mcp_connector_oauth.handle_mcp_oauth_provider_error(
        object(), state=state, fallback_return_path="/settings/integrations", now=1000
    )
    assert result["return_path"] == "/registry/mcp/mcp_alpha?project_id=project_alpha"
    assert marked == {
        "auth_status": "auth_required",
        "message": "OAuth authorization was denied or failed; start authorization again.",
    }
    _raises_input_4242_1 = object()
    with pytest.raises(OAuthConnectionError, match="already used"):
        mcp_connector_oauth.handle_mcp_oauth_provider_error(
            _raises_input_4242_1,
            state=state,
            fallback_return_path="/settings/integrations",
            now=1000,
        )


def test_stale_provider_error_cannot_downgrade_reconnected_connector(monkeypatch) -> None:
    monkeypatch.setenv("FS_SECRET_KEY", "state-secret")
    payload = {
        "v": 1,
        "type": "mcp_connector",
        "connector_id": "mcp_alpha",
        "account_id": "acc_alpha",
        "project_id": "project_alpha",
        "tenant_id": "tenant_alpha",
        "oauth_credential_revision": 2,
        "exp": 2000,
    }
    state = oauth_client._build_state_token(payload)
    monkeypatch.setattr(
        mcp_connector_oauth.mcp_connector_oauth_store,
        "consume_oauth_state",
        lambda *_args, **_kwargs: {**payload, "return_path": "/registry/mcp/mcp_alpha"},
    )
    monkeypatch.setattr(
        mcp_connector_oauth_repository,
        "_locked_connector_row",
        lambda _conn, row: {**row, "oauth_credential_revision": 3},
    )
    writes: list[dict[str, Any]] = []
    monkeypatch.setattr(
        mcp_connector_oauth_repository,
        "_update_connector_oauth",
        lambda *_args, **kwargs: writes.append(dict(kwargs.get("updates") or {})),
    )

    _raises_input_4278_1 = object()
    with pytest.raises(OAuthConnectionError, match="credential revision changed"):
        mcp_connector_oauth.handle_mcp_oauth_provider_error(
            _raises_input_4278_1,
            state=state,
            fallback_return_path="/settings/integrations",
            now=1000,
        )

    assert writes == []


def test_oauth_http_body_reads_are_bounded_and_never_echo_oversized_content(
    monkeypatch,
) -> None:
    secret_marker = "must-not-echo-provider-secret"

    class _OversizedResponse:
        def __init__(self):
            self.headers: dict[str, str] = {}
            self.read_sizes: list[int] = []

        def read(self, size: int) -> bytes:
            self.read_sizes.append(size)
            return (secret_marker.encode() * size)[:size]

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    response = _OversizedResponse()
    monkeypatch.setattr(mcp_connector_oauth_provider, "_MAX_OAUTH_HTTP_RESPONSE_BYTES", 16)
    monkeypatch.setattr(
        mcp_connector_oauth_provider.url_safety,
        "open_pinned_url",
        lambda *_args, **_kwargs: response,
    )

    with pytest.raises(OAuthConnectionError) as captured:
        mcp_connector_oauth_provider._fetch_json(
            "https://auth.example.test/metadata",
            purpose="OAuth metadata",
        )

    assert response.read_sizes == [17]
    assert "size limit" in str(captured.value)
    assert secret_marker not in str(captured.value)

    error_response = _OversizedResponse()
    error_response.code = 400
    message = mcp_connector_oauth_provider._oauth_http_error_message(
        "OAuth token endpoint", error_response
    )
    assert error_response.read_sizes == [17]
    assert message == "OAuth token endpoint returned HTTP 400"
    assert secret_marker not in message
