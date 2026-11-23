"""Check the local authentication schema on the disposable audit database."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest

ROOT = Path(__file__).resolve().parents[2]
MIGRATION = ROOT / "db/migrations/20261120160000_local_auth_credentials.sql"
AUTHENTICATOR_POLICY_MIGRATION = ROOT / "db/migrations/20261122120000_user_authenticator_policy.sql"
LEGACY_CREATED_AT = datetime(2024, 1, 1, tzinfo=UTC)
LEGACY_UPDATED_AT = datetime(2024, 2, 1, tzinfo=UTC)


def test_local_auth_migration_backfills_password_age_and_is_idempotent(disposable_db_conn):
    conn = disposable_db_conn
    migration = MIGRATION.read_text(encoding="utf-8")
    user_id = uuid4()
    with conn.transaction(force_rollback=True):
        conn.execute("DROP TABLE public.auth_mfa_recovery_codes, public.auth_totp_factors")
        conn.execute("DROP TRIGGER trg_users_password_changed ON public.users")
        conn.execute("ALTER TABLE public.users DROP COLUMN password_changed_at")
        original_hash = uuid4().hex
        conn.execute(
            """
            INSERT INTO public.users (id, username, password_hash, created_at, updated_at)
            VALUES (%s, %s, %s, %s, %s)
            """,
            (
                user_id,
                f"{user_id}@example.invalid",
                original_hash,
                LEGACY_CREATED_AT,
                LEGACY_UPDATED_AT,
            ),
        )

        conn.execute(migration)
        assert conn.execute(
            "SELECT password_changed_at, updated_at FROM public.users WHERE id = %s", (user_id,)
        ).fetchone() == (LEGACY_CREATED_AT, LEGACY_UPDATED_AT)

        conn.execute(
            "UPDATE public.users SET password_hash = %s, display_name = %s WHERE id = %s",
            (original_hash, "Updated profile", user_id),
        )
        assert conn.execute(
            "SELECT password_changed_at FROM public.users WHERE id = %s", (user_id,)
        ).fetchone() == (LEGACY_CREATED_AT,)

        changed_at = conn.execute(
            "UPDATE public.users SET password_hash = %s WHERE id = %s RETURNING password_changed_at",
            (uuid4().hex, user_id),
        ).fetchone()[0]
        assert changed_at > LEGACY_CREATED_AT

        conn.execute(
            "INSERT INTO public.auth_totp_factors (user_id) VALUES (%s)",
            (user_id,),
        )
        conn.execute(
            "INSERT INTO public.auth_mfa_recovery_codes (user_id, code_hash) VALUES (%s, %s)",
            (user_id, uuid4().hex),
        )
        conn.execute(migration)
        assert conn.execute(
            "SELECT password_changed_at FROM public.users WHERE id = %s", (user_id,)
        ).fetchone() == (changed_at,)
        assert conn.execute(
            "SELECT count(*) FROM public.auth_mfa_recovery_codes WHERE user_id = %s", (user_id,)
        ).fetchone() == (1,)

        conn.execute("DELETE FROM public.users WHERE id = %s", (user_id,))
        assert conn.execute(
            "SELECT count(*) FROM public.auth_totp_factors WHERE user_id = %s", (user_id,)
        ).fetchone() == (0,)
        assert conn.execute(
            "SELECT count(*) FROM public.auth_mfa_recovery_codes WHERE user_id = %s", (user_id,)
        ).fetchone() == (0,)


def test_fresh_user_has_password_age(disposable_db_conn):
    conn = disposable_db_conn
    user_id = uuid4()
    with conn.transaction(force_rollback=True):
        changed_with_creation = conn.execute(
            """
            INSERT INTO public.users (id, username, password_hash)
            VALUES (%s, %s, %s)
            RETURNING password_changed_at = created_at
            """,
            (user_id, f"{user_id}@example.invalid", uuid4().hex),
        ).fetchone()
        assert changed_with_creation == (True,)


def test_authenticator_policy_migration_defaults_and_preserves_independent_flags(
    disposable_db_conn,
):
    conn = disposable_db_conn
    migration = AUTHENTICATOR_POLICY_MIGRATION.read_text(encoding="utf-8")
    user_id = uuid4()
    with conn.transaction(force_rollback=True):
        conn.execute(
            "ALTER TABLE public.users DROP COLUMN authenticator_required, "
            "DROP COLUMN authenticator_reset_pending"
        )
        conn.execute(
            "INSERT INTO public.users (id, username, password_hash) VALUES (%s, %s, %s)",
            (user_id, f"{user_id}@example.invalid", uuid4().hex),
        )

        conn.execute(migration)
        read_policy = "SELECT authenticator_required, authenticator_reset_pending FROM public.users WHERE id = %s"
        assert conn.execute(read_policy, (user_id,)).fetchone() == (False, False)

        conn.execute(
            "UPDATE public.users SET authenticator_reset_pending = true WHERE id = %s",
            (user_id,),
        )
        conn.execute(migration)
        assert conn.execute(read_policy, (user_id,)).fetchone() == (False, True)


@pytest.mark.parametrize("password_expired", [False, True])
def test_password_change_caps_only_sessions_using_an_expired_password(
    disposable_db_conn, password_expired
):
    conn = disposable_db_conn
    user_id = uuid4()
    now = datetime.now(UTC)
    password_changed_at = now - timedelta(days=366 if password_expired else 364)
    original_session_expiry = now + timedelta(hours=1)
    active_session_id, revoked_session_id = uuid4(), uuid4()
    verified_secret = uuid4().hex
    with conn.transaction(force_rollback=True):
        conn.execute(
            """
            INSERT INTO public.users (id, username, password_hash, password_changed_at)
            VALUES (%s, %s, %s, %s)
            """,
            (user_id, f"{user_id}@example.invalid", uuid4().hex, password_changed_at),
        )
        conn.execute(
            """
            INSERT INTO public.auth_sessions (id, user_id, session_token_hash, expires_at, revoked)
            VALUES (%s, %s, %s, %s, false), (%s, %s, %s, %s, true)
            """,
            (
                active_session_id,
                user_id,
                uuid4().hex,
                original_session_expiry,
                revoked_session_id,
                user_id,
                uuid4().hex,
                original_session_expiry,
            ),
        )
        conn.execute(
            """
            INSERT INTO public.auth_totp_factors
                (user_id, secret_enc, pending_secret_enc, pending_expires_at, last_used_step)
            VALUES (%s, %s, %s, %s, 1)
            """,
            (user_id, verified_secret, uuid4().hex, original_session_expiry),
        )
        conn.execute(
            "UPDATE public.users SET password_hash = %s WHERE id = %s", (uuid4().hex, user_id)
        )

        assert conn.execute(
            """
            SELECT secret_enc, pending_secret_enc, pending_expires_at, last_used_step
            FROM public.auth_totp_factors WHERE user_id = %s
            """,
            (user_id,),
        ).fetchone() == (verified_secret, None, None, 1)

        expected_active_expiry = (
            password_changed_at + timedelta(days=365)
            if password_expired
            else original_session_expiry
        )
        assert set(
            conn.execute(
                "SELECT id, expires_at, revoked FROM public.auth_sessions WHERE user_id = %s",
                (user_id,),
            ).fetchall()
        ) == {
            (active_session_id, expected_active_expiry, False),
            (revoked_session_id, original_session_expiry, True),
        }
