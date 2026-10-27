"""Reusable helpers for encrypted secret storage migration."""

from __future__ import annotations

from core.infrastructure.db import psycopg_runtime as psycopg_runtime
from core.infrastructure.db import system_settings_store

PLAINTEXT_SECRET_BACKFILL_SETTINGS_KEY = (
    "plaintext_secret_storage_backfill"  # pragma: allowlist secret
)


def _backfill_secret_table(
    table_name: str,
    *,
    conn,
    table_exists,
    encrypt_secret_value,
) -> int:
    if not table_exists(table_name):
        return 0
    with conn.cursor(cursor_factory=psycopg_runtime.extras.RealDictCursor) as cur:
        cur.execute(
            f"""
            SELECT id, secret_value_ciphertext
            FROM {table_name}
            WHERE active = true
              AND COALESCE(key_version, '') ILIKE 'plain%%'
            ORDER BY id
            """,
        )
        rows = cur.fetchall() or []
    for row in rows:
        ciphertext, nonce_text, key_version = encrypt_secret_value(
            str((row or {}).get("secret_value_ciphertext") or "")
        )
        with conn.cursor() as cur:
            cur.execute(
                f"""
                UPDATE {table_name}
                   SET secret_value_ciphertext = %s,
                       secret_value_nonce = %s,
                       key_version = %s,
                       updated_at = now()
                 WHERE id = %s
                """,
                (ciphertext, nonce_text, key_version, str((row or {}).get("id") or "").strip()),
            )
    return len(rows)


def _plaintext_secret_backfill_completed(conn) -> bool:
    try:
        row = system_settings_store.get_system_setting(
            conn=conn,
            setting_key=PLAINTEXT_SECRET_BACKFILL_SETTINGS_KEY,
        )
    except Exception:
        return False
    value = row.get("setting_value_jsonb") if isinstance(row, dict) else {}
    return bool(isinstance(value, dict) and value.get("completed") is True)


def _mark_plaintext_secret_backfill_completed(conn, *, plaintext_rows_seen: int) -> None:
    try:
        system_settings_store.save_system_setting(
            conn=conn,
            setting_key=PLAINTEXT_SECRET_BACKFILL_SETTINGS_KEY,
            value={
                "completed": True,
                "plaintext_rows_seen": int(plaintext_rows_seen or 0),
            },
            updated_by="secret_storage_backfill",
        )
    except Exception:
        return


def backfill_plaintext_secret_storage(
    *,
    conn,
    table_exists,
    column_exists,
    encrypt_secret_value,
) -> None:
    _ = column_exists
    if _plaintext_secret_backfill_completed(conn):
        return
    plaintext_rows_seen = _backfill_secret_table(
        "project_secrets",
        conn=conn,
        table_exists=table_exists,
        encrypt_secret_value=encrypt_secret_value,
    )
    plaintext_rows_seen += _backfill_secret_table(
        "provider_secrets",
        conn=conn,
        table_exists=table_exists,
        encrypt_secret_value=encrypt_secret_value,
    )
    _mark_plaintext_secret_backfill_completed(
        conn,
        plaintext_rows_seen=plaintext_rows_seen,
    )
