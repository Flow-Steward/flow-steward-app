from __future__ import annotations

from unittest.mock import patch

from core.application.management import project_connection_secrets
from core.infrastructure.security import connection_secret_store


class _Cursor:
    def __init__(self, row=None) -> None:
        self.row = row
        self.description = [("encrypted_value",)]
        self.calls: list[tuple[str, object]] = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def execute(self, sql, params=None):
        self.calls.append((str(sql), params))

    def fetchone(self):
        return self.row


class _Conn:
    def __init__(self, row=None) -> None:
        self.cursor_obj = _Cursor(row=row)

    def cursor(self):
        return self.cursor_obj


def test_connection_secret_ref_map_uses_connection_boundary() -> None:
    refs = connection_secret_store.connection_secret_ref_map(
        "conn_123",
        {"api_token": "secret", "": "ignored", "blank": None},
    )

    assert refs == {"refs": {"api_token": "project_connection:conn_123:api_token"}}


def test_save_connection_secrets_writes_encrypted_secret_refs() -> None:
    conn = _Conn()
    with (
        patch(
            "core.infrastructure.security.connection_secret_store.encrypt_secret",
            side_effect=lambda value: f"enc:{value}",
        ),
        patch(
            "core.infrastructure.security.connection_secret_store.model_provider_store.upsert_secret_ref"
        ) as upsert,
    ):
        refs = connection_secret_store.save_connection_secrets(
            conn,
            account_id="acc_default",
            connection_id="conn_123",
            secrets={"api_token": "top-secret"},
        )

    assert refs == {"refs": {"api_token": "project_connection:conn_123:api_token"}}
    upsert.assert_called_once_with(
        conn,
        account_id="acc_default",
        ref_key="project_connection:conn_123:api_token",
        encrypted_value="enc:top-secret",
    )


def test_resolve_connection_secrets_decrypts_refs_by_account() -> None:
    conn = _Conn(row=("encrypted-token",))
    with patch(
        "core.infrastructure.security.connection_secret_store.decrypt_secret",
        return_value="plain-token",
    ) as decrypt:
        values = connection_secret_store.resolve_connection_secrets(
            conn,
            account_id="acc_default",
            secret_refs={"refs": {"api_token": "project_connection:conn_123:api_token"}},
        )

    assert values == {"api_token": "plain-token"}
    decrypt.assert_called_once_with("encrypted-token")
    sql, params = conn.cursor_obj.calls[-1]
    assert "FROM secret_refs" in sql
    assert params == ("acc_default", "project_connection:conn_123:api_token")


def test_secret_ref_names_preserves_legacy_list_normalization() -> None:
    assert project_connection_secrets.secret_ref_names(["api_token", "", "refresh_token"]) == [
        "api_token",
        "refresh_token",
    ]


def test_resolve_connection_secrets_skips_internal_oauth_refs_by_default() -> None:
    conn = _Conn(row=("encrypted-token",))
    with patch(
        "core.infrastructure.security.connection_secret_store.decrypt_secret",
        return_value="plain-token",
    ) as decrypt:
        values = project_connection_secrets.resolve_connection_secrets(
            conn,
            account_id="acc_default",
            secret_refs={
                "refs": {
                    "access_token": "project_connection:conn_123:access_token",
                    "_oauth_refresh_token": "project_connection:conn_123:_oauth_refresh_token",
                }
            },
        )

    assert values == {"access_token": "plain-token"}
    assert decrypt.call_count == 1
    assert len(conn.cursor_obj.calls) == 1


def test_resolve_connection_secrets_can_include_internal_oauth_refs() -> None:
    conn = _Conn(row=("encrypted-token",))
    with patch(
        "core.infrastructure.security.connection_secret_store.decrypt_secret",
        return_value="plain-token",
    ) as decrypt:
        values = project_connection_secrets.resolve_connection_secrets(
            conn,
            account_id="acc_default",
            secret_refs={"refs": {"_oauth_refresh_token": "project_connection:conn_123:rt"}},
            include_internal=True,
        )

    assert values == {"_oauth_refresh_token": "plain-token"}
    assert decrypt.call_count == 1
