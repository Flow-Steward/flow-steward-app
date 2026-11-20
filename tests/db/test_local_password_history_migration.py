"""Exercise password history storage on the disposable audit database."""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[2]
MIGRATION = ROOT / "db/migrations/20261120160100_local_password_history.sql"


def _password_history(conn, user_id) -> list[str]:
    return [
        row[0]
        for row in conn.execute(
            """
            SELECT password_hash FROM public.auth_password_history
            WHERE user_id = %s ORDER BY id DESC
            """,
            (user_id,),
        ).fetchall()
    ]


def test_password_history_migration_keeps_ten_prior_hashes(disposable_db_conn):
    conn = disposable_db_conn
    migration = MIGRATION.read_text(encoding="utf-8")
    user_id = uuid4()
    hashes = [uuid4().hex for _ in range(13)]
    with conn.transaction(force_rollback=True):
        conn.execute("DROP TRIGGER trg_users_archive_previous_password ON public.users")
        conn.execute("DROP TABLE public.auth_password_history")
        conn.execute(
            "INSERT INTO public.users (id, username, password_hash) VALUES (%s, %s, %s)",
            (user_id, f"{user_id}@example.invalid", hashes[0]),
        )
        conn.execute(migration)
        assert _password_history(conn, user_id) == []

        for password_hash in hashes[1:]:
            conn.execute(
                "UPDATE public.users SET password_hash = %s WHERE id = %s", (password_hash, user_id)
            )
        expected = list(reversed(hashes[-11:-1]))
        assert _password_history(conn, user_id) == expected

        conn.execute(
            "UPDATE public.users SET password_hash = %s WHERE id = %s", (hashes[-1], user_id)
        )
        assert _password_history(conn, user_id) == expected

        with conn.transaction(force_rollback=True):
            conn.execute(
                "UPDATE public.users SET password_hash = %s WHERE id = %s",
                (uuid4().hex, user_id),
            )
            assert _password_history(conn, user_id) == [hashes[-1], *expected[:9]]
        assert _password_history(conn, user_id) == expected

        conn.execute(migration)
        assert _password_history(conn, user_id) == expected

        conn.execute("DELETE FROM public.users WHERE id = %s", (user_id,))
        assert _password_history(conn, user_id) == []
