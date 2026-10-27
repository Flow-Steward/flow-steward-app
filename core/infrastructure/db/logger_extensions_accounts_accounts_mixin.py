# mypy: ignore-errors
from __future__ import annotations

import re
import uuid
from typing import Any

from core.infrastructure.db import psycopg_runtime as psycopg_runtime
from core.infrastructure.db.logger_extensions_accounts_common import (
    ACCOUNT_ID_IS_REQUIRED_MESSAGE,
    SQL_AND,
    _as_dict,
    _coerce_bool,
    _default_account_id,
)


class PlatformLoggerAccountsMixin:
    def list_accounts(
        self,
        *,
        include_inactive: bool = True,
        search: str = "",
        limit: int = 200,
        offset: int = 0,
    ) -> list[dict]:
        if not self._table_exists("accounts"):
            default_account = _default_account_id()
            return [
                {
                    "account_id": default_account,
                    "display_name": "Default Account",
                    "is_active": True,
                }
            ]
        where: list[str] = []
        params: list[Any] = []
        if not bool(include_inactive):
            where.append("is_active = true")
        normalized_search = str(search or "").strip()
        if normalized_search:
            account_expr = self._accounts_identity_expr()
            if self._column_exists("accounts", "account_key"):
                where.append(
                    f"({account_expr}::text ILIKE %s OR COALESCE(account_key, '') ILIKE %s "
                    "OR COALESCE(display_name, '') ILIKE %s)"
                )
                like = f"%{normalized_search}%"
                params.extend([like, like, like])
            else:
                where.append(
                    f"({account_expr}::text ILIKE %s OR COALESCE(display_name, '') ILIKE %s)"
                )
                like = f"%{normalized_search}%"
                params.extend([like, like])
        where_sql = f"WHERE {SQL_AND.join(where)}" if where else ""
        account_expr = self._accounts_identity_expr()
        with self.conn.cursor(cursor_factory=psycopg_runtime.extras.RealDictCursor) as cur:
            cur.execute(
                f"""
                SELECT {account_expr}::text AS account_id, display_name, is_active, created_at, updated_at
                FROM accounts
                {where_sql}
                ORDER BY {account_expr}
                LIMIT %s OFFSET %s
                """,
                (*params, max(1, min(int(limit or 200), 2000)), max(0, int(offset or 0))),
            )
            rows = cur.fetchall()
        return [self._normalize_account_row(dict(row)) for row in rows]

    def get_account(self, account_id: str) -> dict | None:
        normalized_account_id = str(account_id or "").strip()
        if not normalized_account_id:
            return None
        if not self._table_exists("accounts"):
            default_account = _default_account_id()
            if normalized_account_id != default_account:
                return None
            return {
                "account_id": default_account,
                "display_name": "Default Account",
                "is_active": True,
            }
        where_sql, params = self._account_lookup_filter(normalized_account_id)
        account_expr = self._accounts_identity_expr()
        with self.conn.cursor(cursor_factory=psycopg_runtime.extras.RealDictCursor) as cur:
            cur.execute(
                f"""
                SELECT {account_expr}::text AS account_id, display_name, is_active, created_at, updated_at
                FROM accounts
                WHERE {where_sql}
                LIMIT 1
                """,
                params,
            )
            row = cur.fetchone()
        return self._normalize_account_row(dict(row)) if row else None

    def is_account_active(self, account_id: str) -> bool:
        normalized_account_id = str(account_id or "").strip()
        if not normalized_account_id:
            return False
        if not self._table_exists("accounts"):
            # Compatibility fallback: account table unavailable means we cannot
            # verify account lifecycle state, so keep existing behavior.
            return True
        where_sql, params = self._account_lookup_filter(normalized_account_id)
        with self.conn.cursor(cursor_factory=psycopg_runtime.extras.RealDictCursor) as cur:
            cur.execute(
                f"""
                SELECT is_active
                FROM accounts
                WHERE {where_sql}
                LIMIT 1
                """,
                params,
            )
            row = cur.fetchone()
        if not row:
            return False
        return _coerce_bool(_as_dict(row).get("is_active"), False)

    def create_account(
        self,
        *,
        account_id: str,
        display_name: str = "",
        is_active: bool = True,
    ) -> dict | None:
        if not self._table_exists("accounts"):
            return None
        normalized_account_id = str(account_id or "").strip()
        if not normalized_account_id:
            raise ValueError(ACCOUNT_ID_IS_REQUIRED_MESSAGE)
        if not re.match(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{1,127}$", normalized_account_id):
            raise ValueError("account_id must match [A-Za-z0-9][A-Za-z0-9_.:-]{1,127}")
        normalized_display_name = str(display_name or "").strip() or normalized_account_id
        account_column = self._accounts_identity_column()
        with self.conn.cursor(cursor_factory=psycopg_runtime.extras.RealDictCursor) as cur:
            if account_column == "id":
                try:
                    account_uuid = str(uuid.UUID(normalized_account_id))
                except (AttributeError, TypeError, ValueError):
                    account_uuid = ""
                if account_uuid:
                    cur.execute(
                        """
                        INSERT INTO accounts (id, account_key, display_name, is_active)
                        VALUES (%s, %s, %s, %s)
                        ON CONFLICT DO NOTHING
                        RETURNING id::text AS account_id, display_name, is_active, created_at, updated_at
                        """,
                        (
                            account_uuid,
                            normalized_account_id,
                            normalized_display_name,
                            bool(is_active),
                        ),
                    )
                else:
                    cur.execute(
                        """
                        INSERT INTO accounts (account_key, display_name, is_active)
                        VALUES (%s, %s, %s)
                        ON CONFLICT (tenant_id, account_key) DO NOTHING
                        RETURNING id::text AS account_id, display_name, is_active, created_at, updated_at
                        """,
                        (normalized_account_id, normalized_display_name, bool(is_active)),
                    )
            else:
                cur.execute(
                    """
                    INSERT INTO accounts (account_id, display_name, is_active)
                    VALUES (%s, %s, %s)
                    ON CONFLICT (account_id) DO NOTHING
                    RETURNING account_id, display_name, is_active, created_at, updated_at
                    """,
                    (normalized_account_id, normalized_display_name, bool(is_active)),
                )
            row = cur.fetchone()
            if not row:
                raise ValueError(f"Account '{normalized_account_id}' already exists")
        return self._normalize_account_row(dict(row))

    def update_account(
        self,
        account_id: str,
        *,
        display_name: str,
    ) -> dict | None:
        if not self._table_exists("accounts"):
            return None
        normalized_account_id = str(account_id or "").strip()
        if not normalized_account_id:
            raise ValueError(ACCOUNT_ID_IS_REQUIRED_MESSAGE)
        normalized_display_name = str(display_name or "").strip()
        if not normalized_display_name:
            raise ValueError("display_name is required")
        where_sql, params = self._account_lookup_filter(normalized_account_id)
        account_expr = self._accounts_identity_expr()
        with self.conn.cursor(cursor_factory=psycopg_runtime.extras.RealDictCursor) as cur:
            cur.execute(
                f"""
                UPDATE accounts
                SET display_name = %s,
                    updated_at = now()
                WHERE {where_sql}
                RETURNING {account_expr}::text AS account_id, display_name, is_active, created_at, updated_at
                """,
                (normalized_display_name, *params),
            )
            row = cur.fetchone()
        return self._normalize_account_row(dict(row)) if row else None

    def set_account_active(
        self,
        account_id: str,
        *,
        is_active: bool,
    ) -> dict | None:
        if not self._table_exists("accounts"):
            return None
        normalized_account_id = str(account_id or "").strip()
        if not normalized_account_id:
            raise ValueError(ACCOUNT_ID_IS_REQUIRED_MESSAGE)
        if normalized_account_id in {_default_account_id(), "acc_default"} and not bool(is_active):
            raise ValueError("Default account cannot be disabled")
        where_sql, params = self._account_lookup_filter(normalized_account_id)
        account_expr = self._accounts_identity_expr()
        with self.conn.cursor(cursor_factory=psycopg_runtime.extras.RealDictCursor) as cur:
            cur.execute(
                f"""
                UPDATE accounts
                SET is_active = %s, updated_at = now()
                WHERE {where_sql}
                RETURNING {account_expr}::text AS account_id, display_name, is_active, created_at, updated_at
                """,
                (bool(is_active), *params),
            )
            row = cur.fetchone()
        return self._normalize_account_row(dict(row)) if row else None
