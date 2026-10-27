"""Transaction-scoped advisory lock helpers for OAuth token refresh."""

from __future__ import annotations

import contextlib
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from core.infrastructure.db import psycopg_runtime


def _lock_result(row: Any) -> bool:
    if isinstance(row, dict):
        return bool(next(iter(row.values()), False))
    if isinstance(row, (tuple, list)) and row:
        return bool(row[0])
    return False


def _connection_target(db: Any) -> Any:
    try:
        target = vars(db).get("conn")
    except TypeError:
        target = None
    return target or db


def _fallback_dsn(db: Any, target: Any) -> str:
    for candidate in (db, target):
        dsn = getattr(candidate, "_dsn", None) or getattr(candidate, "dsn", None)
        if str(dsn or "").strip():
            return str(dsn).strip()
    return ""


@contextmanager
def _transaction_connection(db: Any) -> Iterator[Any]:
    target = _connection_target(db)
    pool = getattr(target, "_pool", None)
    if pool is not None:
        conn = None
        borrowed_from_pool = False
        previous_autocommit = None
        try:
            try:
                conn = pool.getconn()
                borrowed_from_pool = True
            except Exception:
                dsn = _fallback_dsn(db, target)
                if not dsn:
                    raise
                conn = psycopg_runtime.connect(dsn)
            previous_autocommit = getattr(conn, "autocommit", None)
            conn.autocommit = False
            yield conn
        finally:
            if conn is not None:
                if previous_autocommit is not None:
                    with contextlib.suppress(Exception):
                        conn.autocommit = previous_autocommit
                if borrowed_from_pool:
                    pool.putconn(conn, close=bool(getattr(conn, "closed", 0)))
                else:
                    with contextlib.suppress(Exception):
                        conn.close()
        return

    previous_autocommit = getattr(target, "autocommit", None)
    if previous_autocommit is not None:
        with contextlib.suppress(Exception):
            target.autocommit = False
    try:
        yield target
    finally:
        if previous_autocommit is not None:
            with contextlib.suppress(Exception):
                target.autocommit = previous_autocommit


def _commit_transaction(conn: Any) -> None:
    commit = getattr(conn, "commit", None)
    if callable(commit):
        try:
            commit()
            return
        except Exception:
            rollback = getattr(conn, "rollback", None)
            if callable(rollback):
                with contextlib.suppress(Exception):
                    rollback()
            raise


def _rollback_transaction(conn: Any) -> None:
    rollback = getattr(conn, "rollback", None)
    if callable(rollback):
        with contextlib.suppress(Exception):
            rollback()


@contextmanager
def advisory_xact_lock(db: Any, *, lock_name: str) -> Iterator[tuple[bool, Any]]:
    """Try to acquire a transaction-scoped advisory lock on one pinned connection.

    The yielded connection must be used for the refresh critical section. This is
    deliberate: pooled `cursor()` proxies may borrow a different PostgreSQL
    session per statement, while `pg_try_advisory_xact_lock` is held only by the
    transaction on the connection that acquired it.
    """

    with _transaction_connection(db) as lock_conn:
        try:
            with lock_conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT pg_try_advisory_xact_lock(
                        ('x' || substr(md5(%s), 1, 16))::bit(64)::bigint
                    )
                    """,
                    (str(lock_name),),
                )
                acquired = _lock_result(cur.fetchone())
            yield acquired, lock_conn
        except Exception:
            _rollback_transaction(lock_conn)
            raise
        else:
            _commit_transaction(lock_conn)
