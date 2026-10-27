from __future__ import annotations

from typing import Any

import psycopg
import pytest

from core.infrastructure.db import psycopg_runtime
from core.infrastructure.db.db_pool import transaction_connection
from tests._db_env import resolve_test_db_dsn


def _postgres_connection() -> Any:
    dsn = str(resolve_test_db_dsn() or "").strip()
    if not dsn:
        pytest.skip("FS_DB_DSN is required for PostgreSQL transaction verification")
    return psycopg.connect(dsn, prepare_threshold=None)


def _transaction_status(conn: Any) -> int:
    return int(conn.info.transaction_status)


@pytest.mark.integration
def test_direct_idle_non_autocommit_success_remains_caller_owned() -> None:
    conn = _postgres_connection()
    try:
        conn.autocommit = False
        assert _transaction_status(conn) == psycopg_runtime.extensions.TRANSACTION_STATUS_IDLE

        with transaction_connection(conn):
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
            assert (
                _transaction_status(conn) == psycopg_runtime.extensions.TRANSACTION_STATUS_INTRANS
            )

        assert _transaction_status(conn) == psycopg_runtime.extensions.TRANSACTION_STATUS_INTRANS
        conn.rollback()
        assert _transaction_status(conn) == psycopg_runtime.extensions.TRANSACTION_STATUS_IDLE
    finally:
        conn.close()


@pytest.mark.integration
def test_direct_idle_non_autocommit_failure_returns_connection_to_idle() -> None:
    conn = _postgres_connection()
    try:
        conn.autocommit = False
        assert _transaction_status(conn) == psycopg_runtime.extensions.TRANSACTION_STATUS_IDLE

        def _raises_operation_52():
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
            assert (
                _transaction_status(conn) == psycopg_runtime.extensions.TRANSACTION_STATUS_INTRANS
            )
            raise RuntimeError("idle owner failed")

        with (
            pytest.raises(RuntimeError, match="idle owner failed"),
            transaction_connection(conn),
        ):
            _raises_operation_52()

        assert _transaction_status(conn) == psycopg_runtime.extensions.TRANSACTION_STATUS_IDLE
    finally:
        conn.close()


@pytest.mark.integration
def test_direct_active_non_autocommit_success_uses_savepoint() -> None:
    conn = _postgres_connection()
    observer = _postgres_connection()
    try:
        conn.autocommit = False
        observer.autocommit = True
        with conn.cursor() as cur:
            cur.execute("SELECT 1")
        assert _transaction_status(conn) == psycopg_runtime.extensions.TRANSACTION_STATUS_INTRANS

        with transaction_connection(conn):
            with observer.cursor() as cur:
                cur.execute(
                    "SELECT query FROM pg_stat_activity WHERE pid = %s",
                    (conn.info.backend_pid,),
                )
                row = cur.fetchone()
            assert row is not None
            assert str(row[0]).upper().startswith("SAVEPOINT ")

        assert _transaction_status(conn) == psycopg_runtime.extensions.TRANSACTION_STATUS_INTRANS
        conn.rollback()
    finally:
        observer.close()
        conn.close()


@pytest.mark.integration
def test_direct_active_non_autocommit_failure_rolls_back_only_savepoint() -> None:
    conn = _postgres_connection()
    try:
        conn.autocommit = False
        with conn.cursor() as cur:
            cur.execute("CREATE TEMP TABLE tx_helper_savepoint_probe (value integer NOT NULL)")
            cur.execute("INSERT INTO tx_helper_savepoint_probe (value) VALUES (1)")
        assert _transaction_status(conn) == psycopg_runtime.extensions.TRANSACTION_STATUS_INTRANS

        def _raises_operation_106():
            with conn.cursor() as cur:
                cur.execute("INSERT INTO tx_helper_savepoint_probe (value) VALUES (2)")
            raise RuntimeError("savepoint owner failed")

        with (
            pytest.raises(RuntimeError, match="savepoint owner failed"),
            transaction_connection(conn),
        ):
            _raises_operation_106()

        assert _transaction_status(conn) == psycopg_runtime.extensions.TRANSACTION_STATUS_INTRANS
        with conn.cursor() as cur:
            cur.execute("SELECT value FROM tx_helper_savepoint_probe ORDER BY value")
            assert cur.fetchall() == [(1,)]
        conn.rollback()
        assert _transaction_status(conn) == psycopg_runtime.extensions.TRANSACTION_STATUS_IDLE
    finally:
        conn.close()
