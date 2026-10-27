from __future__ import annotations

import sys
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from core.application.registry.db_store import RegistryDBStore


def test_search_entities_filters_trashed_rows_when_deleted_at_exists():
    db = RegistryDBStore("postgres://unused")
    db._column_exists = lambda table, column: table == "registry_agents" and column == "deleted_at"
    executed: list[tuple[str, tuple]] = []

    class _Cursor:
        def execute(self, sql, params=None):
            executed.append((str(sql), tuple(params or ())))

        def fetchall(self):
            return [
                {
                    "entity_id": "composer",
                    "display_name": "Developer",
                    "description": "Builds features",
                    "enabled": True,
                }
            ]

    @contextmanager
    def _cursor():
        yield _Cursor()

    db._cursor = _cursor

    rows = db.search_entities("dev", entity_type="agent")

    assert len(rows) == 1
    assert rows[0]["entity_type"] == "agent"
    sql, params = executed[0]
    assert "deleted_at IS NULL" in sql
    assert "COALESCE(description, '') ILIKE %s" in sql
    assert params == ("%dev%", "%dev%", "%dev%")


def test_search_entities_keeps_compatibility_when_deleted_at_missing():
    db = RegistryDBStore("postgres://unused")
    db._column_exists = lambda _table, _column: False
    executed: list[tuple[str, tuple]] = []

    class _Cursor:
        def execute(self, sql, params=None):
            executed.append((str(sql), tuple(params or ())))

        def fetchall(self):
            return []

    @contextmanager
    def _cursor():
        yield _Cursor()

    db._cursor = _cursor

    rows = db.search_entities("proofreader", entity_type="workflow")

    assert rows == []
    sql, _params = executed[0]
    assert "deleted_at IS NULL" not in sql


def test_search_entities_projects_optional_account_scope_for_transport_filtering():
    db = RegistryDBStore("postgres://unused")
    db._column_exists = lambda table, column: table == "registry_agents" and column == "account_id"
    executed: list[str] = []

    class _Cursor:
        def execute(self, sql, params=None):
            executed.append(str(sql))

        def fetchall(self):
            return [
                {
                    "entity_id": "composer",
                    "display_name": "Developer",
                    "description": "Builds features",
                    "enabled": True,
                    "account_id": "account-A",
                }
            ]

    @contextmanager
    def _cursor():
        yield _Cursor()

    db._cursor = _cursor

    rows = db.search_entities("dev", entity_type="agent")

    assert rows[0]["account_id"] == "account-A"
    assert "account_id" in executed[0]


def test_acquire_conn_falls_back_to_direct_connection_when_pool_borrow_fails(monkeypatch):
    db = RegistryDBStore("postgres://unused")

    class _ExhaustedPool:
        def getconn(self):
            raise RuntimeError("connection pool exhausted")

    fallback_conn = MagicMock()
    fallback_conn.closed = 0
    db._pool = _ExhaustedPool()
    db._conn = None

    monkeypatch.setattr(
        "core.application.registry.db_store.psycopg_runtime.connect", lambda _dsn: fallback_conn
    )

    conn, from_pool = db._acquire_conn()

    assert conn is fallback_conn
    assert from_pool is False
    assert conn.autocommit is True

    db._release_conn(conn, from_pool)
    fallback_conn.close.assert_called_once()


def test_release_conn_does_not_close_shared_non_pool_connection():
    db = RegistryDBStore("postgres://unused")
    shared_conn = MagicMock()
    db._conn = shared_conn

    db._release_conn(shared_conn, from_pool=False)

    shared_conn.close.assert_not_called()
