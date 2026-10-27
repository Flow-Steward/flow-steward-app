from __future__ import annotations

from unittest.mock import patch

from core.application.management import project_connection_sessions


def _connection() -> dict:
    return {
        "connection_id": "conn_123",
        "account_id": "acc_default",
        "project_id": "project_1",
        "credential_revision": 7,
    }


def test_connection_session_scope_hash_is_stable_for_sorted_scope() -> None:
    first = project_connection_sessions.connection_session_scope_hash(
        {"scopes": ["read", "write"], "subject": "alice"}
    )
    second = project_connection_sessions.connection_session_scope_hash(
        {"subject": "alice", "scopes": ["read", "write"]}
    )

    assert first == second
    assert first != "default"
    assert project_connection_sessions.connection_session_scope_hash({}) == "default"


def test_upsert_connection_session_encrypts_payload_and_uses_connection_revision() -> None:
    with (
        patch(
            "core.application.management.project_connection_sessions.encrypt_secret",
            return_value="encrypted-session",
        ) as encrypt,
        patch(
            "core.application.management.project_connection_sessions."
            "project_connection_session_store.upsert_connection_session",
            return_value={
                "session_id": "session_1",
                "expires_at": 1234.0,
                "refresh_after": 1200.0,
                "scope_hash": "scope_hash",
                "subject_key": "alice",
                "credential_revision": 7,
            },
        ) as upsert,
    ):
        result = project_connection_sessions.upsert_connection_session(
            object(),
            connection=_connection(),
            session_payload={"access_token": "token"},
            expires_at=1234.0,
            refresh_after=1200.0,
            scope={"scopes": ["read"]},
            subject_key="alice",
            metadata={"source": "test"},
        )

    encrypt.assert_called_once()
    upsert.assert_called_once()
    kwargs = upsert.call_args.kwargs
    assert kwargs["connection_id"] == "conn_123"
    assert kwargs["account_id"] == "acc_default"
    assert kwargs["project_id"] == "project_1"
    assert kwargs["credential_revision"] == 7
    assert kwargs["encrypted_session_payload"] == "encrypted-session"
    assert kwargs["subject_key"] == "alice"
    assert result["session_id"] == "session_1"


def test_get_connection_session_decrypts_valid_cached_session() -> None:
    with (
        patch(
            "core.application.management.project_connection_sessions.decrypt_secret",
            return_value='{"access_token":"token"}',
        ) as decrypt,
        patch(
            "core.application.management.project_connection_sessions."
            "project_connection_session_store.get_valid_connection_session",
            return_value={
                "encrypted_session_payload": "encrypted-session",
                "expires_at": 1234.0,
                "refresh_after": 1200.0,
                "scope_hash": "scope_hash",
                "subject_key": "alice",
                "credential_revision": 7,
            },
        ) as get_valid,
    ):
        result = project_connection_sessions.get_connection_session(
            object(),
            connection=_connection(),
            scope={"scopes": ["read"]},
            subject_key="alice",
        )

    get_valid.assert_called_once()
    decrypt.assert_called_once_with("encrypted-session")
    assert result == {
        "payload": {"access_token": "token"},
        "expires_at": 1234.0,
        "refresh_after": 1200.0,
        "scope_hash": "scope_hash",
        "subject_key": "alice",
        "credential_revision": 7,
    }
