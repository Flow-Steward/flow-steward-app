"""Authenticator persistence operations performed within the caller's transaction."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any


def active_factor_exists(cur: Any, user_id: str) -> bool:
    cur.execute(
        "SELECT 1 FROM auth_totp_factors WHERE user_id = %s AND secret_enc IS NOT NULL",
        (user_id,),
    )
    return cur.fetchone() is not None


def lock_active_factor(cur: Any, user_id: str) -> tuple[str, int | None] | None:
    cur.execute(
        """
        SELECT secret_enc, last_used_step
        FROM auth_totp_factors
        WHERE user_id = %s AND secret_enc IS NOT NULL
        FOR UPDATE
        """,
        (user_id,),
    )
    row = cur.fetchone()
    if row is None:
        return None
    if isinstance(row, dict):
        return row["secret_enc"], row["last_used_step"]
    return row[0], row[1]


def advance_totp_step(cur: Any, user_id: str, step: int) -> bool:
    cur.execute(
        """
        UPDATE auth_totp_factors SET last_used_step = %s
        WHERE user_id = %s AND (last_used_step IS NULL OR last_used_step < %s)
        """,
        (step, user_id, step),
    )
    return cur.rowcount == 1


def store_pending_factor(cur: Any, user_id: str, secret_enc: str, expires_at: datetime) -> None:
    cur.execute(
        """
        INSERT INTO auth_totp_factors (user_id, pending_secret_enc, pending_expires_at)
        VALUES (%s, %s, %s)
        ON CONFLICT (user_id) DO UPDATE
        SET pending_secret_enc = EXCLUDED.pending_secret_enc,
            pending_expires_at = EXCLUDED.pending_expires_at
        """,
        (user_id, secret_enc, expires_at),
    )


def lock_pending_factor(cur: Any, user_id: str, now: datetime) -> str | None:
    cur.execute(
        """
        SELECT pending_secret_enc
        FROM auth_totp_factors
        WHERE user_id = %s AND pending_expires_at > %s
        FOR UPDATE
        """,
        (user_id, now),
    )
    row = cur.fetchone()
    if row is None:
        return None
    value = row["pending_secret_enc"] if isinstance(row, dict) else row[0]
    return str(value) if value else None


def activate_pending_factor(cur: Any, user_id: str, secret_enc: str, step: int) -> None:
    cur.execute(
        """
        UPDATE auth_totp_factors
        SET secret_enc = %s,
            last_used_step = %s,
            pending_secret_enc = NULL,
            pending_expires_at = NULL
        WHERE user_id = %s
        """,
        (secret_enc, step, user_id),
    )


def replace_recovery_codes(cur: Any, user_id: str, code_hashes: Sequence[str]) -> None:
    cur.execute("DELETE FROM auth_mfa_recovery_codes WHERE user_id = %s", (user_id,))
    cur.executemany(
        "INSERT INTO auth_mfa_recovery_codes (user_id, code_hash) VALUES (%s, %s)",
        [(user_id, code_hash) for code_hash in code_hashes],
    )


def consume_recovery_code(cur: Any, user_id: str, code_hash: str) -> bool:
    cur.execute(
        """
        DELETE FROM auth_mfa_recovery_codes AS code
        USING auth_totp_factors AS factor
        WHERE code.user_id = %s AND code.code_hash = %s
          AND factor.user_id = code.user_id AND factor.secret_enc IS NOT NULL
        """,
        (user_id, code_hash),
    )
    return cur.rowcount == 1


def delete_factor(cur: Any, user_id: str) -> None:
    cur.execute("DELETE FROM auth_totp_factors WHERE user_id = %s", (user_id,))
    cur.execute("DELETE FROM auth_mfa_recovery_codes WHERE user_id = %s", (user_id,))


def clear_pending_factor(cur: Any, user_id: str) -> None:
    cur.execute(
        """
        UPDATE auth_totp_factors
        SET pending_secret_enc = NULL, pending_expires_at = NULL
        WHERE user_id = %s AND pending_secret_enc IS NOT NULL
        """,
        (user_id,),
    )


def clear_reset_pending(cur: Any, user_id: str) -> None:
    cur.execute("UPDATE users SET authenticator_reset_pending = false WHERE id = %s", (user_id,))


def lock_admin_target(cur: Any, user_id: str) -> tuple[str, str, bool, bool] | None:
    cur.execute(
        """
        SELECT username, account_id::text, authenticator_required, authenticator_reset_pending
        FROM users
        WHERE id = %s AND deleted_at IS NULL AND password_hash <> ''
        FOR UPDATE
        """,
        (user_id,),
    )
    return cur.fetchone()


def admin_target_membership_ids(cur: Any, user_id: str) -> list[str]:
    # The user lock is acquired before reading memberships so we see committed account edits.
    cur.execute(
        "SELECT account_id::text FROM user_account_memberships WHERE user_id = %s",
        (user_id,),
    )
    return [row[0] for row in cur.fetchall()]


def mark_reset_pending(cur: Any, user_id: str) -> None:
    cur.execute("UPDATE users SET authenticator_reset_pending = true WHERE id = %s", (user_id,))


def set_authenticator_required(cur: Any, user_id: str, required: bool) -> None:
    cur.execute(
        "UPDATE users SET authenticator_required = %s WHERE id = %s",
        (required, user_id),
    )


def active_cookie_session_id(
    cur: Any, user_id: str, token_hash: str, *, lock: bool = False
) -> str | None:
    query = """
        SELECT id
        FROM auth_sessions
        WHERE user_id = %s AND session_token_hash = %s AND revoked = false AND expires_at > now()
    """
    if lock:
        query += " FOR UPDATE"
    cur.execute(query, (user_id, token_hash))
    row = cur.fetchone()
    if row is None:
        return None
    return str(row["id"] if isinstance(row, dict) else row[0])


def revoke_sessions(cur: Any, user_id: str, *, keep_session_id: str | None = None) -> int:
    cur.execute(
        """
        UPDATE auth_sessions
        SET revoked = true, updated_at = now()
        WHERE user_id = %s AND id IS DISTINCT FROM %s AND revoked = false
        """,
        (user_id, keep_session_id),
    )
    return cur.rowcount
