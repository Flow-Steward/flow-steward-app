"""PostgreSQL repository for the first-administrator invariant."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager
from typing import Any


class PostgresFirstAdminRepository:
    def __init__(
        self,
        *,
        logger: Any,
        seed_default_roles: Callable[[Any], None],
        transaction_connection_factory: Callable[[Any], AbstractContextManager[Any]],
    ) -> None:
        self._logger = logger
        self._seed_default_roles = seed_default_roles
        self._transaction_connection_factory = transaction_connection_factory

    def create_if_empty(
        self, *, email: str, password_hash: str, display_name: str
    ) -> dict[str, Any] | None:
        with self._transaction_connection_factory(self._logger) as connection:
            self._seed_default_roles(self._logger)
            with connection.cursor() as cur:
                cur.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                    ("flow-steward:first-admin",),
                )
                cur.execute("SELECT COUNT(*) FROM users")
                row = cur.fetchone()
                if int((row[0] if row else 0) or 0) > 0:
                    return None
                cur.execute(
                    """
                    INSERT INTO users (username, password_hash, display_name, is_active)
                    VALUES (%s,%s,%s,true)
                    RETURNING id, username, display_name
                    """,
                    (email, password_hash, display_name),
                )
                user_row = cur.fetchone()
                user_id = str(user_row[0])
                cur.execute("SELECT id FROM roles WHERE key = 'admin' LIMIT 1")
                role_row = cur.fetchone()
                if role_row:
                    cur.execute(
                        """
                        INSERT INTO user_roles (user_id, role_id)
                        VALUES (%s,%s)
                        ON CONFLICT (user_id, role_id) DO NOTHING
                        """,
                        (user_id, str(role_row[0])),
                    )
                cur.execute(
                    """
                    SELECT id
                    FROM accounts
                    WHERE is_active = true
                    ORDER BY (account_key = %s) DESC, created_at, id
                    LIMIT 1
                    """,
                    ("default",),
                )
                account_row = cur.fetchone()
                if not account_row:
                    raise RuntimeError(
                        "Cannot create the first administrator without an active account"
                    )
                cur.execute(
                    """
                    INSERT INTO user_account_memberships (user_id, account_id, is_default)
                    VALUES (%s,%s,true)
                    ON CONFLICT (user_id, account_id)
                    DO UPDATE SET is_default = true
                    """,
                    (user_id, str(account_row[0])),
                )
                return {
                    "id": user_id,
                    "username": str(user_row[1]),
                    "display_name": str(user_row[2] or display_name),
                }
