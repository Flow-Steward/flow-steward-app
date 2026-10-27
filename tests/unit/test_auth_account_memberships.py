from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from uuid import UUID

from core.application.auth.account_memberships import (
    enrich_user_payload_with_account_memberships,
    list_user_account_memberships,
)


class _Cursor:
    def __init__(self, *, membership_rows: list[dict[str, Any]] | None = None) -> None:
        self.executed_queries: list[str] = []
        self._membership_rows = [dict(row) for row in (membership_rows or [])]
        self._next_fetchall: list[Any] = []
        self._next_fetchone: Any = None

    def __enter__(self) -> _Cursor:
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        return False

    def execute(self, query: str, _params: Any = None) -> None:
        normalized = " ".join(str(query).split())
        self.executed_queries.append(normalized)
        self._next_fetchall = []
        self._next_fetchone = None
        if "FROM user_account_memberships uam" in normalized:
            self._next_fetchall = list(self._membership_rows)
            return
        if "FROM accounts WHERE account_id =" in normalized:
            self._next_fetchone = {
                "account_id": "acc_default",
                "display_name": "Main account",
                "is_active": True,
            }

    def fetchone(self) -> Any:
        value = self._next_fetchone
        self._next_fetchone = None
        return value

    def fetchall(self) -> list[Any]:
        value = list(self._next_fetchall)
        self._next_fetchall = []
        return value


class _Conn:
    def __init__(self, cursor: _Cursor | None = None) -> None:
        self._cursor = cursor or _Cursor()

    def cursor(self, **_kwargs: Any) -> _Cursor:
        return self._cursor


def test_enrich_user_payload_uses_accounts_table_when_membership_table_missing() -> None:
    bl = SimpleNamespace(
        conn=_Conn(),
        _table_exists=lambda table: table == "accounts",
    )

    payload = enrich_user_payload_with_account_memberships(
        bl,
        {
            "id": 17,
            "username": "dev@example.com",
            "account_id": "acc_default",
        },
    )

    assert payload is not None
    assert payload["account_display_name"] == "Main account"
    assert payload["account_memberships"] == [
        {
            "account_id": "acc_default",
            "display_name": "Main account",
            "is_active": True,
            "is_default": True,
        }
    ]


def test_list_user_account_memberships_joins_accounts_by_id_when_account_id_column_missing() -> None:
    account_id = "00000000-0000-0000-0000-000000000002"
    cursor = _Cursor(
        membership_rows=[
            {
                "account_id": account_id,
                "display_name": "Main account",
                "is_active": True,
                "is_default": True,
            }
        ]
    )
    bl = SimpleNamespace(
        conn=_Conn(cursor),
        _table_exists=lambda table: table in {"accounts", "user_account_memberships"},
        _column_exists=lambda table, column: table == "accounts" and column == "id",
    )

    rows = list_user_account_memberships(bl, "00000000-0000-0000-0000-000000000010")

    assert rows == [
        {
            "account_id": account_id,
            "display_name": "Main account",
            "is_active": True,
            "is_default": True,
        }
    ]
    assert any(
        "JOIN accounts a ON a.id = uam.account_id" in query
        for query in cursor.executed_queries
    )


def test_list_user_account_memberships_normalizes_uuid_account_ids() -> None:
    account_id = UUID("00000000-0000-0000-0000-000000000002")
    cursor = _Cursor(
        membership_rows=[
            {
                "account_id": account_id,
                "display_name": "Main account",
                "is_active": True,
                "is_default": True,
            }
        ]
    )
    bl = SimpleNamespace(
        conn=_Conn(cursor),
        _table_exists=lambda table: table in {"accounts", "user_account_memberships"},
        _column_exists=lambda table, column: table == "accounts" and column == "id",
    )

    rows = list_user_account_memberships(bl, UUID("00000000-0000-0000-0000-000000000010"))

    assert rows == [
        {
            "account_id": "00000000-0000-0000-0000-000000000002",
            "display_name": "Main account",
            "is_active": True,
            "is_default": True,
        }
    ]
