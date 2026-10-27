from __future__ import annotations

import threading
from typing import Any

import pytest
from psycopg.errors import CheckViolation

from core.application.auth.user_admin import repository
from core.application.management.errors import ServiceConflictError
from core.infrastructure.db import psycopg_runtime
from tests._db_env import resolve_test_db_dsn


class _Logger:
    def __init__(self, conn: Any) -> None:
        self.conn = conn

    def _table_exists(self, _name: str) -> bool:
        return True


def _connection() -> Any:
    dsn = str(resolve_test_db_dsn() or "").strip()
    if not dsn:
        pytest.skip("FS_DB_DSN is required for PostgreSQL transaction verification")
    conn = psycopg_runtime.connect(dsn)
    conn.autocommit = True
    return conn


def _seed_temp_auth_tables(conn: Any, *, reject_delete: bool = False) -> None:
    delete_constraint = ", CHECK (deleted_at IS NULL)" if reject_delete else ""
    with conn.cursor() as cur:
        cur.execute(
            f"""
            CREATE TEMP TABLE users (
                id text PRIMARY KEY,
                username text NOT NULL,
                is_active boolean NOT NULL DEFAULT true,
                deleted_at timestamptz,
                deleted_by text,
                updated_at timestamptz NOT NULL DEFAULT now()
                {delete_constraint}
            ) ON COMMIT PRESERVE ROWS
            """  # nosec B608 - the optional clause is a test-owned constant.
        )
        cur.execute(
            """
            CREATE TEMP TABLE roles (
                id text PRIMARY KEY,
                key text NOT NULL
            ) ON COMMIT PRESERVE ROWS
            """
        )
        cur.execute(
            """
            CREATE TEMP TABLE user_roles (
                user_id text NOT NULL,
                role_id text NOT NULL
            ) ON COMMIT PRESERVE ROWS
            """
        )
        cur.execute(
            """
            CREATE TEMP TABLE auth_sessions (
                user_id text NOT NULL,
                revoked boolean NOT NULL DEFAULT false,
                updated_at timestamptz NOT NULL DEFAULT now()
            ) ON COMMIT PRESERVE ROWS
            """
        )
        cur.execute(
            """
            CREATE TEMP TABLE personal_access_tokens (
                user_id text NOT NULL,
                revoked boolean NOT NULL DEFAULT false,
                updated_at timestamptz NOT NULL DEFAULT now()
            ) ON COMMIT PRESERVE ROWS
            """
        )
        cur.execute(
            """
            INSERT INTO users (id, username) VALUES
                ('u_target', 'target@example.com'),
                ('u_peer', 'peer@example.com')
            """
        )
        cur.execute("INSERT INTO auth_sessions (user_id) VALUES ('u_target')")
        cur.execute("INSERT INTO personal_access_tokens (user_id) VALUES ('u_target')")


def _revocation_state(conn: Any) -> tuple[bool, bool, bool]:
    with conn.cursor() as cur:
        cur.execute("SELECT revoked FROM auth_sessions WHERE user_id = 'u_target'")
        session_revoked = bool(cur.fetchone()[0])
        cur.execute("SELECT revoked FROM personal_access_tokens WHERE user_id = 'u_target'")
        token_revoked = bool(cur.fetchone()[0])
        cur.execute("SELECT deleted_at IS NOT NULL FROM users WHERE id = 'u_target'")
        deleted = bool(cur.fetchone()[0])
    return session_revoked, token_revoked, deleted


@pytest.mark.integration
def test_delete_auth_user_rolls_back_revocations_when_soft_delete_fails() -> None:
    conn = _connection()
    try:
        _seed_temp_auth_tables(conn, reject_delete=True)
        logger = _Logger(conn)

        with pytest.raises(CheckViolation):
            repository.delete_auth_user(
                logger,
                user_id="u_target",
                deleted_by="admin@example.com",
            )

        assert _revocation_state(conn) == (False, False, False)
    finally:
        conn.close()


@pytest.mark.integration
@pytest.mark.parametrize("operation", ["delete", "active_update"])
def test_user_lifecycle_lock_is_held_for_entire_conditional_mutation(
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    actor = _connection()
    observer = _connection()
    entered_revoke = threading.Event()
    release_revoke = threading.Event()
    errors: list[BaseException] = []
    worker: threading.Thread | None = None
    original_revoke = repository._revoke_user_auth_material

    def blocking_revoke(cur: Any, bl: Any, user_id: str) -> int:
        entered_revoke.set()
        assert release_revoke.wait(timeout=10)
        return original_revoke(cur, bl, user_id)

    monkeypatch.setattr(repository, "_revoke_user_auth_material", blocking_revoke)

    try:
        _seed_temp_auth_tables(actor)

        def mutate() -> None:
            try:
                if operation == "delete":
                    repository.delete_auth_user(
                        _Logger(actor),
                        user_id="u_target",
                        deleted_by="admin@example.com",
                    )
                else:
                    repository.update_auth_user(
                        _Logger(actor),
                        user_id="u_target",
                        existing_user={"username": "target@example.com"},
                        display_name=None,
                        is_active=False,
                        password_hash_value=None,
                        must_change_password=None,
                        roles=None,
                        requested_account_ids=None,
                        primary_account_id=None,
                        revoke_sessions=True,
                    )
            except BaseException as exc:  # pragma: no cover - asserted in caller thread
                errors.append(exc)

        worker = threading.Thread(target=mutate, daemon=True)
        worker.start()
        assert entered_revoke.wait(timeout=10)

        with observer.cursor() as cur:
            cur.execute(
                "SELECT pg_try_advisory_xact_lock(hashtext(%s))",
                ("flow-steward:user-lifecycle",),
            )
            acquired = bool(cur.fetchone()[0])
        assert acquired is False

        release_revoke.set()
        worker.join(timeout=10)
        assert not worker.is_alive()
        assert errors == []

        with observer.cursor() as cur:
            cur.execute(
                "SELECT pg_try_advisory_xact_lock(hashtext(%s))",
                ("flow-steward:user-lifecycle",),
            )
            assert bool(cur.fetchone()[0]) is True
    finally:
        release_revoke.set()
        if worker is not None:
            worker.join(timeout=10)
        observer.close()
        actor.close()


@pytest.mark.integration
@pytest.mark.parametrize("guard", ["last_user", "last_active_admin"])
def test_delete_auth_user_enforces_lifecycle_guards_in_postgres(guard: str) -> None:
    conn = _connection()
    try:
        _seed_temp_auth_tables(conn)
        with conn.cursor() as cur:
            if guard == "last_user":
                cur.execute("DELETE FROM users WHERE id = 'u_peer'")
            else:
                cur.execute("INSERT INTO roles (id, key) VALUES ('r_admin', 'admin')")
                cur.execute(
                    "INSERT INTO user_roles (user_id, role_id) VALUES ('u_target', 'r_admin')"
                )

        expected = "last user" if guard == "last_user" else "last active administrator"
        logger = _Logger(conn)
        with pytest.raises(ServiceConflictError, match=expected):
            repository.delete_auth_user(
                logger,
                user_id="u_target",
                deleted_by="admin@example.com",
            )

        assert _revocation_state(conn) == (False, False, False)
    finally:
        conn.close()
