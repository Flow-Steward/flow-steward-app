from __future__ import annotations

from contextlib import contextmanager
from unittest.mock import MagicMock
from urllib.parse import parse_qs, urlparse

import pytest

from core.application.auth import oauth_client, oauth_client_tokens


def _auth_config() -> dict:
    return {
        "type": "oauth2",
        "grant": "authorization_code",
        "pkce": "required",
        "authorization_endpoint": "https://idp.example.test/authorize",
        "token_endpoint": "https://idp.example.test/token",
        "revocation_endpoint": "https://idp.example.test/revoke",
        "scopes": {"required": ["read"], "optional": ["write"]},
        "client_credentials": {
            "client_id": {"source": "platform_secret"},
            "client_secret": {"source": "platform_secret", "optional": True},
        },
        "refresh": {"rotates_refresh_token": False},
        "token_placement": "secrets",
        "secret_names": {"access_token": "access_token"},
    }


def _connection() -> dict:
    return {
        "connection_id": "conn_oauth_1",
        "account_id": "acc_alpha",
        "project_id": "project_alpha",
        "connection_type": "example_oauth_account",
        "credential_revision": 3,
        "connection_config": {"oauth_client_id": "client-123"},
        "secret_refs": {},
    }


@pytest.fixture(autouse=True)
def _default_locked_connection(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "core.infrastructure.db.project_connection_store.get_project_connection_row_for_update",
        lambda _conn, _connection_id: _connection(),
    )


class _FakeResponse:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_exc_info):
        return False

    def read(self) -> bytes:
        return self.payload


def test_http_post_form_uses_pinned_url_opener(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict = {}

    def _open_pinned_url(request, *, timeout_seconds, purpose):
        captured["url"] = request.full_url
        captured["body"] = request.data
        captured["timeout_seconds"] = timeout_seconds
        captured["purpose"] = purpose
        return _FakeResponse(b'{"access_token":"access-1"}')

    monkeypatch.setattr(oauth_client.url_safety, "open_pinned_url", _open_pinned_url)

    result = oauth_client._http_post_form(
        "https://idp.example.test/token",
        {"grant_type": "refresh_token", "client_id": "client-1"},
        timeout_seconds=3,
    )

    assert result == {"access_token": "access-1"}
    assert captured["url"] == "https://idp.example.test/token"
    assert captured["body"] == b"grant_type=refresh_token&client_id=client-1"
    assert captured["timeout_seconds"] == 3
    assert captured["purpose"] == "OAuth token endpoint"


def test_http_post_form_rejects_plain_http_without_dev_escape_hatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("FS_ALLOW_PRIVATE_REMOTE_URLS", raising=False)

    with pytest.raises(oauth_client.OAuthConnectionError, match="must use https"):
        oauth_client._http_post_form("http://idp.example.test/token", {"client_id": "client-1"})


def test_http_post_form_rejects_private_metadata_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("FS_ALLOW_PRIVATE_REMOTE_URLS", raising=False)

    with pytest.raises(oauth_client.OAuthConnectionError, match="not allowed"):
        oauth_client._http_post_form(
            "https://169.254.169.254/latest/meta-data/token",
            {"client_id": "client-1", "client_secret": "secret"},
        )


def test_oauth_revocation_capture_defers_provider_network_until_explicit_execution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    post_form = MagicMock(return_value={})
    monkeypatch.setattr(
        oauth_client, "oauth_auth_config_for_connection", lambda *_args: _auth_config()
    )
    monkeypatch.setattr(
        oauth_client,
        "_resolve_oauth_secret_values",
        lambda *_args: {
            oauth_client.OAUTH_REFRESH_TOKEN_SECRET: "refresh-canary"  # pragma: allowlist secret
        },
    )
    monkeypatch.setattr(
        oauth_client,
        "_client_credential_value",
        lambda **kwargs: (
            "client-canary" if kwargs["credential_name"] == "client_id" else "secret-canary"
        ),
    )
    monkeypatch.setattr(oauth_client, "_http_post_form", post_form)

    prepared = oauth_client.prepare_connection_oauth_revocation(
        object(),
        connection=_connection(),
    )

    post_form.assert_not_called()
    assert prepared is not None
    assert oauth_client.execute_prepared_oauth_revocation(prepared) is True
    post_form.assert_called_once_with(
        "https://idp.example.test/revoke",
        {
            "token": "refresh-canary",
            "client_id": "client-canary",
            "client_secret": "secret-canary",  # pragma: allowlist secret
        },
        purpose="OAuth revocation endpoint",
    )


def test_oauth_callback_uses_trusted_public_base_and_allows_explicit_loopback_dev(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "core.application.auth.serve_auth_runtime.public_base_url",
        lambda: "http://127.0.0.1:8080",
    )
    monkeypatch.setattr(oauth_client.url_safety, "_allow_private_remote_urls", lambda: True)

    assert oauth_client.oauth_callback_url() == (
        "http://127.0.0.1:8080/api/integrations/oauth/callback"
    )


def test_oauth_callback_rejects_non_loopback_http_even_in_private_url_dev_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "core.application.auth.serve_auth_runtime.public_base_url",
        lambda: "http://flow.example.test",
    )
    monkeypatch.setattr(oauth_client.url_safety, "_allow_private_remote_urls", lambda: True)

    with pytest.raises(oauth_client.OAuthConnectionError, match="must use https"):
        oauth_client.oauth_callback_url()


def test_start_oauth_authorization_stores_pkce_and_returns_authorization_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stored: dict = {}
    monkeypatch.setenv("FS_SECRET_KEY", "state-secret")
    monkeypatch.setattr(oauth_client, "encrypt_secret", lambda value: f"enc:{value}")
    monkeypatch.setattr(
        "core.infrastructure.db.project_connection_oauth_store.replace_connection_oauth_state",
        lambda _conn, **kwargs: stored.update(kwargs),
    )

    result = oauth_client.start_oauth_authorization(
        object(),
        connection=_connection(),
        auth_config=_auth_config(),
        redirect_uri="https://flow.example.test/api/integrations/oauth/callback",
        optional_scopes=["write", "admin"],
        return_path="/projects/project_alpha/connections/conn_oauth_1",
        now=1000.0,
    )

    parsed = urlparse(result["authorization_url"])
    query = parse_qs(parsed.query)
    assert parsed.scheme == "https"
    assert query["response_type"] == ["code"]
    assert query["client_id"] == ["client-123"]
    assert query["scope"] == ["read write"]
    assert query["code_challenge_method"] == ["S256"]
    assert "code_verifier" not in query
    assert query["state"][0]
    assert stored["state_hash"] == oauth_client._state_hash(query["state"][0])
    assert stored["code_verifier_encrypted"].startswith("enc:")
    assert stored["auth_payload"]["scopes"] == ["read", "write"]
    assert stored["expires_at"] == 1600.0


def test_start_oauth_authorization_replaces_prior_live_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    live_states: dict[str, dict] = {}
    token_exchange_calls: list[str] = []
    monkeypatch.setenv("FS_SECRET_KEY", "state-secret")
    monkeypatch.setattr(oauth_client, "encrypt_secret", lambda value: f"enc:{value}")

    def replace_state(_conn, **kwargs):
        live_states.clear()
        live_states[kwargs["state_hash"]] = dict(kwargs)

    monkeypatch.setattr(
        "core.infrastructure.db.project_connection_oauth_store.replace_connection_oauth_state",
        replace_state,
    )
    monkeypatch.setattr(
        "core.infrastructure.db.project_connection_oauth_store.consume_oauth_state",
        lambda _conn, *, state_hash: live_states.pop(state_hash, None),
    )

    first = oauth_client.start_oauth_authorization(
        object(),
        connection=_connection(),
        auth_config=_auth_config(),
        redirect_uri="https://flow.example.test/api/integrations/oauth/callback",
        now=1000.0,
    )
    second = oauth_client.start_oauth_authorization(
        object(),
        connection=_connection(),
        auth_config=_auth_config(),
        redirect_uri="https://flow.example.test/api/integrations/oauth/callback",
        now=1001.0,
    )

    first_state = parse_qs(urlparse(first["authorization_url"]).query)["state"][0]
    second_state = parse_qs(urlparse(second["authorization_url"]).query)["state"][0]
    assert oauth_client._state_hash(first_state) not in live_states
    assert oauth_client._state_hash(second_state) in live_states

    _raises_input_257_1 = object()

    def _raises_input_257_2(**_kwargs):
        return token_exchange_calls.append("called") or {}

    with pytest.raises(oauth_client.OAuthConnectionError, match="already used"):
        oauth_client.handle_oauth_callback(
            _raises_input_257_1,
            state=first_state,
            code="stale-auth-code",
            now=1002.0,
            token_exchange=_raises_input_257_2,
        )
    assert token_exchange_calls == []


def test_handle_oauth_callback_consumes_state_and_exchanges_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    monkeypatch.setenv("FS_SECRET_KEY", "state-secret")
    state = oauth_client._build_state_token(
        {
            "v": 1,
            "nonce": "nonce",
            "connection_id": "conn_oauth_1",
            "account_id": "acc_alpha",
            "project_id": "project_alpha",
            "exp": 1600,
        }
    )
    monkeypatch.setattr(oauth_client, "decrypt_secret", lambda value: "verifier-123")
    monkeypatch.setattr(
        "core.infrastructure.db.project_connection_store.get_project_connection_row_for_update",
        lambda _conn, _connection_id: events.append("lock_connection") or _connection(),
    )

    def consume_state(_conn, *, state_hash):
        events.append("consume_state")
        return {
            "state_hash": state_hash,
            "connection_id": "conn_oauth_1",
            "account_id": "acc_alpha",
            "project_id": "project_alpha",
            "code_verifier_encrypted": "encrypted-verifier",
            "redirect_uri": "https://flow.example.test/api/integrations/oauth/callback",
            "return_path": "/projects/project_alpha/connections/conn_oauth_1",
            "auth_payload_jsonb": {
                "auth_config": _auth_config(),
                "client_id": "client-123",
                "scopes": ["read"],
            },
        }

    monkeypatch.setattr(
        "core.infrastructure.db.project_connection_oauth_store.consume_oauth_state",
        consume_state,
    )
    monkeypatch.setattr(
        "core.infrastructure.db.project_connection_store.get_project_connection_row",
        lambda _conn, _connection_id: _connection(),
    )

    def _exchange(**kwargs):
        events.append("token_exchange")
        assert kwargs["code"] == "auth-code"
        assert kwargs["code_verifier"] == "verifier-123"
        assert kwargs["client_id"] == "client-123"
        return {"access_token": "access-1", "refresh_token": "refresh-1", "expires_in": 3600}

    monkeypatch.setattr(
        oauth_client,
        "store_oauth_token_response",
        lambda _conn, **kwargs: (
            events.append("store_tokens") or {**_connection(), "credential_revision": 4}
        ),
    )

    result = oauth_client.handle_oauth_callback(
        object(),
        state=state,
        code="auth-code",
        now=1000.0,
        token_exchange=_exchange,
    )

    assert result["status"] == "connected"
    assert events == [
        "lock_connection",
        "consume_state",
        "token_exchange",
        "lock_connection",
        "store_tokens",
    ]
    assert result["connection_id"] == "conn_oauth_1"
    assert result["return_path"] == "/projects/project_alpha/connections/conn_oauth_1"


def test_callback_does_not_store_tokens_after_connection_revision_changes_during_exchange(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FS_SECRET_KEY", "state-secret")
    state = oauth_client._build_state_token(
        {
            "v": 1,
            "nonce": "nonce",
            "connection_id": "conn_oauth_1",
            "account_id": "acc_alpha",
            "project_id": "project_alpha",
            "exp": 1600,
        }
    )
    connections = [_connection(), {**_connection(), "credential_revision": 4}]
    monkeypatch.setattr(
        "core.infrastructure.db.project_connection_store.get_project_connection_row_for_update",
        lambda _conn, _connection_id: connections.pop(0),
    )
    monkeypatch.setattr(
        "core.infrastructure.db.project_connection_oauth_store.consume_oauth_state",
        lambda _conn, *, state_hash: {
            "state_hash": state_hash,
            "connection_id": "conn_oauth_1",
            "account_id": "acc_alpha",
            "project_id": "project_alpha",
            "code_verifier_encrypted": "encrypted-verifier",
            "redirect_uri": "https://flow.example.test/api/integrations/oauth/callback",
            "auth_payload_jsonb": {
                "auth_config": _auth_config(),
                "client_id": "client-123",
                "scopes": ["read"],
            },
        },
    )
    monkeypatch.setattr(oauth_client, "decrypt_secret", lambda _value: "verifier")
    store_tokens = MagicMock()
    monkeypatch.setattr(oauth_client, "store_oauth_token_response", store_tokens)

    _raises_input_389_1 = object()
    with pytest.raises(oauth_client.OAuthConnectionError, match="connection changed"):
        oauth_client.handle_oauth_callback(
            _raises_input_389_1,
            state=state,
            code="auth-code",
            now=1000.0,
            token_exchange=lambda **_kwargs: {
                "access_token": "must-not-persist",
                "refresh_token": "must-not-persist",
            },
        )

    store_tokens.assert_not_called()


def test_handle_oauth_callback_rejects_replayed_state(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FS_SECRET_KEY", "state-secret")
    state = oauth_client._build_state_token(
        {
            "v": 1,
            "nonce": "nonce",
            "connection_id": "conn_oauth_1",
            "account_id": "acc_alpha",
            "project_id": "project_alpha",
            "exp": 1600,
        }
    )
    monkeypatch.setattr(
        "core.infrastructure.db.project_connection_oauth_store.consume_oauth_state",
        lambda _conn, *, state_hash: None,
    )

    _raises_input_421_1 = object()
    with pytest.raises(oauth_client.OAuthConnectionError, match="already used"):
        oauth_client.handle_oauth_callback(
            _raises_input_421_1, state=state, code="auth-code", now=1000.0
        )


def test_provider_error_callback_consumes_state_and_uses_stored_return_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FS_SECRET_KEY", "state-secret")
    state = oauth_client._build_state_token(
        {
            "v": 1,
            "nonce": "nonce",
            "connection_id": "conn_oauth_1",
            "account_id": "acc_alpha",
            "project_id": "project_alpha",
            "exp": 1600,
        }
    )
    events: list[str] = []

    def consume(_conn, *, state_hash):
        events.append(f"consume:{state_hash}")
        return {
            "connection_id": "conn_oauth_1",
            "account_id": "acc_alpha",
            "project_id": "project_alpha",
            "return_path": "/projects/project_alpha/connections/conn_oauth_1",
        }

    monkeypatch.setattr(
        "core.infrastructure.db.project_connection_oauth_store.consume_oauth_state",
        consume,
    )
    monkeypatch.setattr(
        "core.infrastructure.db.project_connection_store.get_project_connection_row_for_update",
        lambda _conn, _connection_id: events.append("lock_connection") or _connection(),
    )

    result = oauth_client.handle_oauth_provider_error(
        object(),
        state=state,
        provider_error="access_denied",
        now=1000.0,
    )

    assert events == ["lock_connection", f"consume:{oauth_client._state_hash(state)}"]
    assert result["status"] == "pending"
    assert result["return_path"] == "/projects/project_alpha/connections/conn_oauth_1"


@pytest.mark.parametrize(
    ("provider_error", "expected_reason"),
    [
        ("access_denied", "authorization_cancelled"),
        ("server_error", "oauth_failed"),
        ("temporarily_unavailable", "oauth_failed"),
        ("invalid_request", "oauth_failed"),
    ],
)
def test_generic_callback_maps_only_access_denied_to_user_cancellation(
    provider_error: str,
    expected_reason: str,
) -> None:
    assert oauth_client.oauth_provider_error_reason(provider_error) == expected_reason


def test_handle_oauth_callback_rejects_expired_state(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FS_SECRET_KEY", "state-secret")
    state = oauth_client._build_state_token(
        {
            "v": 1,
            "nonce": "nonce",
            "connection_id": "conn_oauth_1",
            "account_id": "acc_alpha",
            "project_id": "project_alpha",
            "exp": 999,
        }
    )

    _raises_input_500_1 = object()
    with pytest.raises(oauth_client.OAuthConnectionError, match="expired"):
        oauth_client.handle_oauth_callback(
            _raises_input_500_1, state=state, code="auth-code", now=1000.0
        )


def test_handle_oauth_callback_rejects_state_connection_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FS_SECRET_KEY", "state-secret")
    state = oauth_client._build_state_token(
        {
            "v": 1,
            "nonce": "nonce",
            "connection_id": "conn_oauth_1",
            "account_id": "acc_alpha",
            "project_id": "project_alpha",
            "exp": 1600,
        }
    )
    monkeypatch.setattr(
        "core.infrastructure.db.project_connection_oauth_store.consume_oauth_state",
        lambda _conn, *, state_hash: {
            "state_hash": state_hash,
            "connection_id": "conn_oauth_2",
            "account_id": "acc_alpha",
            "project_id": "project_alpha",
            "code_verifier_encrypted": "encrypted-verifier",
        },
    )

    _raises_input_529_1 = object()
    with pytest.raises(oauth_client.OAuthConnectionError, match="does not match"):
        oauth_client.handle_oauth_callback(
            _raises_input_529_1, state=state, code="auth-code", now=1000.0
        )


def test_store_oauth_token_response_writes_internal_refs_and_bumps_revision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    saved_updates: dict = {}
    monkeypatch.setattr(
        "core.application.management.project_connection_secrets.save_connection_secrets",
        lambda _conn, **kwargs: {
            "refs": {
                secret_name: f"project_connection:conn_oauth_1:{secret_name}"
                for secret_name in kwargs["secrets"]
            }
        },
    )
    monkeypatch.setattr(
        "core.infrastructure.db.project_connection_store.update_project_connection_row",
        lambda _conn, _connection_id, updates: saved_updates.update(updates),
    )
    monkeypatch.setattr(
        "core.infrastructure.db.project_connection_store.get_project_connection_row",
        lambda _conn, _connection_id: None,
    )
    monkeypatch.setattr(
        "core.application.management.project_connection_sessions.invalidate_connection_sessions",
        lambda _conn, *, connection_id: 1,
    )

    oauth_client.store_oauth_token_response(
        object(),
        connection=_connection(),
        auth_config=_auth_config(),
        token_response={
            "access_token": "access-1",
            "refresh_token": "refresh-1",
            "expires_in": 60,
            "scope": "read write",
            "token_type": "Bearer",
        },
        granted_scope_fallback=["read"],
        now=1000.0,
    )

    refs = saved_updates["secret_refs"]["refs"]
    assert refs["access_token"] == "project_connection:conn_oauth_1:access_token"
    assert refs["_oauth_refresh_token"] == "project_connection:conn_oauth_1:_oauth_refresh_token"
    assert refs["_oauth_expires_at"] == "project_connection:conn_oauth_1:_oauth_expires_at"
    assert saved_updates["credential_revision"] == 4
    assert saved_updates["health_status"] == "healthy"
    assert saved_updates["connection_config"]["oauth"]["granted_scopes"] == ["read", "write"]
    assert saved_updates["connection_config"]["oauth"]["lifecycle_status"] == "connected"


def test_incremental_callback_without_refresh_token_preserves_existing_refresh_ref(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    saved_updates: dict = {}
    saved_values: dict = {}
    connection = _connection()
    connection["secret_refs"] = {
        "refs": {
            "_oauth_refresh_token": "project_connection:conn_oauth_1:existing-refresh",
        }
    }

    def save_secrets(_conn, **kwargs):
        saved_values.update(kwargs["secrets"])
        return {
            "refs": {name: f"project_connection:conn_oauth_1:{name}" for name in kwargs["secrets"]}
        }

    monkeypatch.setattr(
        "core.application.management.project_connection_secrets.save_connection_secrets",
        save_secrets,
    )
    monkeypatch.setattr(
        "core.infrastructure.db.project_connection_store.update_project_connection_row",
        lambda _conn, _connection_id, updates: saved_updates.update(updates),
    )
    monkeypatch.setattr(
        "core.infrastructure.db.project_connection_store.get_project_connection_row",
        lambda *_args: None,
    )
    monkeypatch.setattr(
        "core.application.management.project_connection_sessions.invalidate_connection_sessions",
        lambda *_args, **_kwargs: 1,
    )

    oauth_client.store_oauth_token_response(
        object(),
        connection=connection,
        auth_config=_auth_config(),
        token_response={"access_token": "new-access", "scope": "read"},
        granted_scope_fallback=[],
        now=1000.0,
    )

    assert "_oauth_refresh_token" not in saved_values
    assert saved_updates["secret_refs"]["refs"]["_oauth_refresh_token"] == (
        "project_connection:conn_oauth_1:existing-refresh"
    )


def test_ensure_valid_oauth_access_token_refreshes_under_advisory_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    outer_conn = object()
    lock_conn = object()
    refreshed_connection = {**_connection(), "credential_revision": 4}
    store_calls: list[dict] = []
    resolve_conns: list[object] = []
    values = [
        {
            "access_token": "old-access",
            "_oauth_refresh_token": "refresh-1",
            "_oauth_expires_at": "1010",
            "_oauth_granted_scopes": '["read"]',
        },
        {
            "access_token": "old-access",
            "_oauth_refresh_token": "refresh-1",
            "_oauth_expires_at": "1010",
            "_oauth_granted_scopes": '["read"]',
        },
        {
            "access_token": "new-access",
            "_oauth_refresh_token": "refresh-2",
            "_oauth_expires_at": "5000",
            "_oauth_granted_scopes": '["read"]',
        },
    ]
    monkeypatch.setattr(
        oauth_client_tokens,
        "_resolve_oauth_secret_values",
        lambda _conn, _connection: resolve_conns.append(_conn) or values.pop(0),
    )

    @contextmanager
    def _refresh_lock(_conn, *, lock_name):
        assert _conn is outer_conn
        assert lock_name == "project_connection_oauth_refresh:conn_oauth_1"
        yield True, lock_conn

    monkeypatch.setattr(
        "core.infrastructure.db.project_connection_oauth_store.refresh_advisory_lock",
        _refresh_lock,
    )
    monkeypatch.setattr(
        "core.infrastructure.db.project_connection_store.get_project_connection_row",
        lambda _conn, _connection_id: (
            pytest.fail("refresh must read latest connection through lock connection")
            if _conn is not lock_conn
            else _connection()
        ),
    )

    def _store(_conn, **kwargs):
        if _conn is not lock_conn:
            pytest.fail("refresh must store token response through lock connection")
        store_calls.append(kwargs)
        return refreshed_connection

    monkeypatch.setattr(
        oauth_client_tokens,
        "store_oauth_token_response",
        _store,
    )

    result = oauth_client.ensure_valid_oauth_access_token(
        outer_conn,
        connection=_connection(),
        auth_config=_auth_config(),
        now=1000.0,
        http_post_form=lambda _url, _data: {
            "access_token": "new-access",
            "refresh_token": "refresh-2",
            "expires_in": 3600,
        },
    )

    assert result == {"access_token": "new-access"}
    assert store_calls[0]["bump_credential_revision"] is False
    assert resolve_conns == [outer_conn, lock_conn, lock_conn]


@pytest.mark.parametrize(
    "transition",
    ("disconnect", "client_id_replacement", "extension_disable"),
)
def test_refresh_discards_response_after_destructive_connection_transition(
    monkeypatch: pytest.MonkeyPatch,
    transition: str,
) -> None:
    outer_conn = object()
    lock_conn = object()
    initial = {
        **_connection(),
        "extension_id": "fixture.extension-oauth",
        "extension_install_id": "install-fixture",
        "enabled": True,
        "runtime_status": "ready",
        "connection_config": {
            "oauth": {
                "lifecycle_status": "connected",
            }
        },
        "secret_refs": {
            "refs": {
                "access_token": "access-ref",
                "_oauth_refresh_token": "refresh-ref",
            }
        },
    }
    transitioned = {
        **initial,
        "connection_config": {
            "oauth": {
                **initial["connection_config"]["oauth"],
                "lifecycle_status": (
                    "disconnected"
                    if transition == "disconnect"
                    else "reconnect_required"
                    if transition == "client_id_replacement"
                    else "connected"
                ),
            }
        },
        "credential_revision": (
            initial["credential_revision"]
            if transition == "disconnect"
            else initial["credential_revision"] + 1
        ),
        "secret_refs": {}
        if transition in {"disconnect", "client_id_replacement"}
        else initial["secret_refs"],
    }
    transition_committed = False
    values = {
        "access_token": "expired-access",
        "_oauth_refresh_token": "refresh-token",
        "_oauth_expires_at": "1",
        "_oauth_granted_scopes": '["read"]',
    }

    monkeypatch.setattr(oauth_client_tokens, "_resolve_oauth_secret_values", lambda *_args: values)
    monkeypatch.setattr(
        oauth_client_tokens,
        "_client_credential_value",
        lambda **kwargs: (
            "client-before" if kwargs["credential_name"] == "client_id" else "secret-before"
        ),
    )
    monkeypatch.setattr(
        oauth_client_tokens,
        "_provider_credential_generation",
        lambda *_args, **_kwargs: (
            2 if transition_committed and transition == "client_id_replacement" else 1
        ),
    )

    @contextmanager
    def refresh_lock(_conn, *, lock_name):
        assert lock_name == "project_connection_oauth_refresh:conn_oauth_1"
        yield True, lock_conn

    monkeypatch.setattr(
        "core.infrastructure.db.project_connection_oauth_store.refresh_advisory_lock",
        refresh_lock,
    )
    monkeypatch.setattr(
        "core.infrastructure.db.project_connection_store.get_project_connection_row",
        lambda _conn, _connection_id: initial,
    )
    locked_rows: list[str] = []

    def lock_latest(_conn, connection_id):
        assert _conn is lock_conn
        locked_rows.append(connection_id)
        return transitioned

    monkeypatch.setattr(
        "core.infrastructure.db.project_connection_store.get_project_connection_row_for_update",
        lock_latest,
    )
    store_tokens = MagicMock(side_effect=AssertionError("stale refresh must not persist tokens"))
    monkeypatch.setattr(oauth_client_tokens, "store_oauth_token_response", store_tokens)
    degraded = MagicMock()
    reconnect = MagicMock()
    unavailable = MagicMock()
    monkeypatch.setattr(oauth_client_tokens, "_mark_oauth_degraded", degraded)
    monkeypatch.setattr(oauth_client_tokens, "_mark_oauth_reconnect_required", reconnect)
    monkeypatch.setattr(oauth_client_tokens, "_mark_oauth_provider_unavailable", unavailable)

    def token_endpoint(_url, _data):
        nonlocal transition_committed
        transition_committed = True
        return {"access_token": "stale-access", "expires_in": 3600}

    _raises_input_829_1 = _auth_config()
    with pytest.raises(oauth_client.OAuthConnectionError, match="stale"):
        oauth_client.ensure_valid_oauth_access_token(
            outer_conn,
            connection=initial,
            auth_config=_raises_input_829_1,
            now=1000.0,
            http_post_form=token_endpoint,
        )

    assert locked_rows == ["conn_oauth_1"]
    store_tokens.assert_not_called()
    degraded.assert_not_called()
    reconnect.assert_not_called()
    unavailable.assert_not_called()


def test_store_oauth_token_response_keeps_revision_for_token_refresh(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    saved_updates: dict = {}
    invalidations: list[str] = []
    monkeypatch.setattr(
        "core.application.management.project_connection_secrets.save_connection_secrets",
        lambda _conn, **kwargs: {
            "refs": {
                secret_name: f"project_connection:conn_oauth_1:{secret_name}"
                for secret_name in kwargs["secrets"]
            }
        },
    )
    monkeypatch.setattr(
        "core.infrastructure.db.project_connection_store.update_project_connection_row",
        lambda _conn, _connection_id, updates: saved_updates.update(updates),
    )
    monkeypatch.setattr(
        "core.infrastructure.db.project_connection_store.get_project_connection_row",
        lambda _conn, _connection_id: None,
    )
    monkeypatch.setattr(
        "core.application.management.project_connection_sessions.invalidate_connection_sessions",
        lambda _conn, *, connection_id: invalidations.append(connection_id),
    )

    oauth_client.store_oauth_token_response(
        object(),
        connection=_connection(),
        auth_config=_auth_config(),
        token_response={"access_token": "access-2", "expires_in": 60},
        granted_scope_fallback=["read"],
        existing_refresh_token="refresh-1",
        bump_credential_revision=False,
        now=1000.0,
    )

    assert saved_updates["credential_revision"] == 3
    assert invalidations == []


def test_oauth_connection_status_redacts_tokens(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        oauth_client_tokens,
        "oauth_auth_config_for_connection",
        lambda _conn, _connection: _auth_config(),
    )
    monkeypatch.setattr(
        oauth_client_tokens,
        "_resolve_oauth_secret_values",
        lambda _conn, _connection: {
            "access_token": "secret-access",
            "_oauth_refresh_token": "secret-refresh",
            "_oauth_expires_at": "4102444800",
            "_oauth_granted_scopes": '["read","write"]',
        },
    )

    status = oauth_client.oauth_connection_status(object(), connection=_connection())

    assert status["status"] == "connected"
    assert status["granted_scopes"] == ["read", "write"]
    assert status["has_refresh_token"] is True
    assert "secret-access" not in str(status)
    assert "secret-refresh" not in str(status)


def test_oauth_status_for_an_invalid_legacy_extension_id_is_stable_not_oauth() -> None:
    status = oauth_client.oauth_connection_status(
        object(),
        connection={
            "connection_id": "conn-invalid-extension",
            "extension_id": "acme_showcase",
        },
    )

    assert status == {
        "oauth": False,
        "status": "not_oauth",
        "connection_id": "conn-invalid-extension",
    }


def test_oauth_status_for_legacy_single_segment_extension_with_type_is_not_oauth() -> None:
    status = oauth_client.oauth_connection_status(
        object(),
        connection={
            "connection_id": "conn-legacy-notifier",
            "extension_id": "notifier",
            "connection_type": "notifier",
        },
    )

    assert status == {
        "oauth": False,
        "status": "not_oauth",
        "connection_id": "conn-legacy-notifier",
    }


@pytest.mark.parametrize("lifecycle", ["pending", "disconnected", "reconnect_required"])
def test_explicit_non_connected_lifecycle_blocks_runtime_injection(
    monkeypatch: pytest.MonkeyPatch,
    lifecycle: str,
) -> None:
    monkeypatch.setattr(
        oauth_client_tokens,
        "oauth_auth_config_for_connection",
        lambda _conn, _connection: _auth_config(),
    )
    connection = _connection()
    connection["connection_config"] = {"oauth": {"lifecycle_status": lifecycle}}

    _raises_input_959_1 = object()
    with pytest.raises(oauth_client.OAuthConnectionError, match=lifecycle):
        oauth_client.resolve_oauth_secrets_for_runtime(_raises_input_959_1, connection=connection)


def test_connected_lifecycle_blocks_runtime_when_required_scope_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    auth_config = _auth_config()
    auth_config["scopes"] = {"required": ["messages.modify"]}
    monkeypatch.setattr(
        oauth_client_tokens,
        "oauth_auth_config_for_connection",
        lambda _conn, _connection: auth_config,
    )
    monkeypatch.setattr(
        oauth_client_tokens,
        "_resolve_oauth_secret_values",
        lambda *_args: {
            "access_token": "access",
            "_oauth_refresh_token": "refresh",
            "_oauth_granted_scopes": '["profile"]',
        },
    )
    connection = _connection()
    connection["runtime_status"] = "ready"
    connection["connection_config"] = {"oauth": {"lifecycle_status": "connected"}}

    _raises_input_986_1 = object()
    with pytest.raises(oauth_client.OAuthConnectionError, match="required scopes"):
        oauth_client.resolve_oauth_secrets_for_runtime(_raises_input_986_1, connection=connection)


def test_connected_lifecycle_status_is_independent_from_degraded_health(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        oauth_client_tokens,
        "oauth_auth_config_for_connection",
        lambda _conn, _connection: _auth_config(),
    )
    monkeypatch.setattr(
        oauth_client_tokens,
        "_resolve_oauth_secret_values",
        lambda _conn, _connection: {
            "access_token": "access",
            "_oauth_refresh_token": "refresh",
            "_oauth_expires_at": "4102444800",
            "_oauth_granted_scopes": '["read"]',
        },
    )
    connection = _connection()
    connection.update({"health_status": "degraded", "runtime_status": "degraded"})
    connection["connection_config"] = {"oauth": {"lifecycle_status": "connected"}}

    status = oauth_client.oauth_connection_status(object(), connection=connection)

    assert status["status"] == "connected"
    assert status["health_status"] == "degraded"
    assert status["runtime_status"] == "degraded"


def test_connected_lifecycle_without_refresh_token_projects_reconnect_required(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        oauth_client_tokens,
        "oauth_auth_config_for_connection",
        lambda _conn, _connection: _auth_config(),
    )
    monkeypatch.setattr(
        oauth_client_tokens,
        "_resolve_oauth_secret_values",
        lambda _conn, _connection: {
            "access_token": "access",
            "_oauth_expires_at": "4102444800",
            "_oauth_granted_scopes": '["read"]',
        },
    )
    connection = _connection()
    connection["connection_config"] = {"oauth": {"lifecycle_status": "connected"}}

    assert oauth_client.oauth_connection_status(object(), connection=connection)["status"] == (
        "reconnect_required"
    )


@pytest.mark.parametrize(
    ("error_code", "expected_reconnect", "expected_unavailable"),
    [
        ("invalid_grant", True, False),
        ("invalid_client", False, True),
        ("temporarily_unavailable", False, False),
    ],
)
def test_refresh_failure_classification_preserves_or_invalidates_grant(
    monkeypatch: pytest.MonkeyPatch,
    error_code: str,
    expected_reconnect: bool,
    expected_unavailable: bool,
) -> None:
    outer_conn = object()
    lock_conn = object()
    connection = {
        **_connection(),
        "secret_refs": {
            "refs": {"_oauth_refresh_token": "project_connection:conn_oauth_1:refresh"}
        },
    }
    values = {
        "access_token": "expired",
        "_oauth_refresh_token": "refresh",
        "_oauth_expires_at": "1",
        "_oauth_granted_scopes": '["read"]',
    }

    monkeypatch.setattr(oauth_client_tokens, "_resolve_oauth_secret_values", lambda *_args: values)

    @contextmanager
    def refresh_lock(_conn, *, lock_name):
        yield True, lock_conn

    monkeypatch.setattr(
        "core.infrastructure.db.project_connection_oauth_store.refresh_advisory_lock",
        refresh_lock,
    )
    monkeypatch.setattr(
        "core.infrastructure.db.project_connection_store.get_project_connection_row",
        lambda *_args: connection,
    )
    reconnects: list[dict] = []
    degraded: list[str] = []
    unavailable: list[str] = []
    monkeypatch.setattr(
        oauth_client_tokens,
        "_mark_oauth_reconnect_required",
        lambda *_args, **kwargs: reconnects.append(kwargs),
    )
    monkeypatch.setattr(
        oauth_client_tokens,
        "_mark_oauth_degraded",
        lambda *_args: degraded.append("degraded"),
    )
    monkeypatch.setattr(
        oauth_client_tokens,
        "_mark_oauth_provider_unavailable",
        lambda *_args: unavailable.append("unavailable"),
    )

    def token_endpoint(_url, _data):
        raise oauth_client.OAuthTokenEndpointError(error_code=error_code, status_code=400)

    _raises_input_1109_1 = _auth_config()
    with pytest.raises(oauth_client.OAuthTokenEndpointError):
        oauth_client.ensure_valid_oauth_access_token(
            outer_conn,
            connection=connection,
            auth_config=_raises_input_1109_1,
            now=1000.0,
            http_post_form=token_endpoint,
        )

    assert bool(reconnects) is expected_reconnect
    assert bool(degraded) is (error_code == "temporarily_unavailable")
    assert bool(unavailable) is expected_unavailable
    if expected_reconnect:
        assert reconnects[0]["delete_invalid_refresh_token"] is True


def test_provider_configuration_failure_blocks_runtime_and_invalidates_sessions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    updates: dict = {}
    invalidated: list[str] = []
    connection = _connection()
    connection["connection_config"] = {"oauth": {"lifecycle_status": "connected"}}

    monkeypatch.setattr(
        "core.infrastructure.db.project_connection_store.update_project_connection_row",
        lambda _conn, _id, values: updates.update(values),
    )
    monkeypatch.setattr(
        "core.application.management.project_connection_sessions.invalidate_connection_sessions",
        lambda _conn, **kwargs: invalidated.append(kwargs["connection_id"]),
    )

    oauth_client_tokens._mark_oauth_provider_unavailable(
        object(),
        connection,
        "OAuth provider configuration is unavailable",
    )

    assert updates["health_status"] == "degraded"
    assert updates["runtime_status"] == "unavailable"
    assert "connection_config" not in updates
    assert invalidated == ["conn_oauth_1"]


def test_disconnect_clears_grant_before_persisting_lifecycle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    transaction_conn = object()
    connection = _connection()
    connection["connection_config"] = {
        "oauth": {
            "lifecycle_status": "connected",
            "verified_email": "person@example.test",
            "expires_at": 123,
        }
    }

    @contextmanager
    def transaction(_conn):
        yield transaction_conn

    monkeypatch.setattr("core.infrastructure.db.db_pool.transaction_connection", transaction)
    monkeypatch.setattr(
        "core.infrastructure.db.project_connection_store.get_project_connection_row_for_update",
        lambda _conn, _connection_id: events.append("lock_connection") or connection,
    )
    monkeypatch.setattr(
        oauth_client,
        "revoke_connection_oauth_tokens",
        lambda *_args, **kwargs: events.append(f"revoke:{kwargs['delete_local']}"),
    )
    monkeypatch.setattr(
        "core.application.management.project_connection_secrets.delete_connection_secret_refs",
        lambda *_args, **_kwargs: events.append("delete_secrets"),
    )
    monkeypatch.setattr(
        "core.application.management.project_connection_sessions.invalidate_connection_sessions",
        lambda *_args, **_kwargs: events.append("invalidate_sessions"),
    )
    monkeypatch.setattr(
        "core.infrastructure.db.project_connection_oauth_store.invalidate_connection_oauth_states",
        lambda *_args, **_kwargs: events.append("invalidate_oauth_states"),
    )

    def update(_conn, _connection_id, updates):
        events.append("update_lifecycle")
        assert updates["secret_refs"] == {}
        assert updates["health_status"] == "unknown"
        assert updates["runtime_status"] == "unavailable"
        assert updates["connection_config"]["oauth"] == {
            "lifecycle_status": "disconnected",
            "verified_email": "person@example.test",
        }

    monkeypatch.setattr(
        "core.infrastructure.db.project_connection_store.update_project_connection_row",
        update,
    )

    result = oauth_client.disconnect_connection_oauth(object(), connection=connection)

    assert events == [
        "revoke:False",
        "lock_connection",
        "invalidate_oauth_states",
        "delete_secrets",
        "invalidate_sessions",
        "update_lifecycle",
    ]
    assert result["verified_email"] == "person@example.test"


def test_disconnect_invalidates_pending_state_before_stale_callback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    live_states = {
        "stale-state-hash": {
            "connection_id": "conn_oauth_1",
            "account_id": "acc_alpha",
            "project_id": "project_alpha",
        }
    }
    lifecycle_updates: dict = {}
    token_exchange_calls: list[str] = []
    transaction_conn = object()
    connection = _connection()
    connection["connection_config"] = {"oauth": {"lifecycle_status": "pending"}}

    @contextmanager
    def transaction(_conn):
        yield transaction_conn

    monkeypatch.setattr("core.infrastructure.db.db_pool.transaction_connection", transaction)
    monkeypatch.setattr(
        "core.infrastructure.db.project_connection_store.get_project_connection_row_for_update",
        lambda _conn, _connection_id: connection,
    )
    monkeypatch.setattr(
        oauth_client,
        "revoke_connection_oauth_tokens",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        "core.application.management.project_connection_secrets.delete_connection_secret_refs",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        "core.application.management.project_connection_sessions.invalidate_connection_sessions",
        lambda *_args, **_kwargs: None,
    )

    def invalidate_states(_conn, *, connection_id):
        assert _conn is transaction_conn
        assert connection_id == "conn_oauth_1"
        live_states.clear()

    monkeypatch.setattr(
        "core.infrastructure.db.project_connection_oauth_store.invalidate_connection_oauth_states",
        invalidate_states,
    )
    monkeypatch.setattr(
        "core.infrastructure.db.project_connection_oauth_store.consume_oauth_state",
        lambda _conn, *, state_hash: live_states.pop(state_hash, None),
    )
    monkeypatch.setattr(
        "core.infrastructure.db.project_connection_store.update_project_connection_row",
        lambda _conn, _connection_id, updates: lifecycle_updates.update(updates),
    )

    result = oauth_client.disconnect_connection_oauth(object(), connection=connection)

    assert result["status"] == "disconnected"
    assert lifecycle_updates["connection_config"]["oauth"]["lifecycle_status"] == "disconnected"
    assert live_states == {}

    monkeypatch.setenv("FS_SECRET_KEY", "state-secret")
    stale_state = oauth_client._build_state_token(
        {
            "v": 1,
            "nonce": "nonce",
            "connection_id": "conn_oauth_1",
            "account_id": "acc_alpha",
            "project_id": "project_alpha",
            "exp": 1600,
        }
    )
    _raises_input_1297_1 = object()

    def _raises_input_1297_2(**_kwargs):
        return token_exchange_calls.append("called") or {}

    with pytest.raises(oauth_client.OAuthConnectionError, match="already used"):
        oauth_client.handle_oauth_callback(
            _raises_input_1297_1,
            state=stale_state,
            code="stale-auth-code",
            now=1000.0,
            token_exchange=_raises_input_1297_2,
        )
    assert token_exchange_calls == []


def test_replace_oauth_state_locks_connection_before_state_rows() -> None:
    from core.infrastructure.db import project_connection_oauth_store

    db = MagicMock()
    cur = db.cursor.return_value.__enter__.return_value
    cur.rowcount = 1

    project_connection_oauth_store.replace_connection_oauth_state(
        db,
        state_hash="state-hash",
        connection_id="conn_oauth_1",
        account_id="acc_alpha",
        project_id="project_alpha",
        code_verifier_encrypted="encrypted-verifier",
        redirect_uri="https://flow.example.test/api/integrations/oauth/callback",
        return_path="/projects/project_alpha/connections/conn_oauth_1",
        auth_payload={},
        expires_at=1600.0,
    )

    statements = [" ".join(str(call.args[0]).split()) for call in cur.execute.call_args_list]
    assert "FROM project_connections" in statements[0]
    assert "FOR UPDATE" in statements[0]
    assert "UPDATE project_connection_oauth_states" in statements[1]
    assert "INSERT INTO project_connection_oauth_states" in statements[2]
