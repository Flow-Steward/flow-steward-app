from __future__ import annotations

import pytest

from core.infrastructure.db import oauth_refresh_lock
from core.infrastructure.db.oauth_refresh_lock import advisory_xact_lock


class _Cursor:
    def __init__(self, conn):
        self.conn = conn

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def execute(self, sql, params=None):
        self.conn.statements.append((sql, params))

    def fetchone(self):
        return (True,)


class _FailingExecuteCursor(_Cursor):
    def execute(self, sql, params=None):
        super().execute(sql, params)
        raise RuntimeError("lock select failed")


class _Connection:
    closed = 0

    def __init__(self):
        self.autocommit = True
        self.commits = 0
        self.rollbacks = 0
        self.closed_called = False
        self.statements = []

    def cursor(self):
        return _Cursor(self)

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        self.closed_called = True


class _FailingExecuteConnection(_Connection):
    def cursor(self):
        return _FailingExecuteCursor(self)


class _Pool:
    def __init__(self, conn):
        self.conn = conn
        self.returned = []

    def getconn(self):
        return self.conn

    def putconn(self, conn, *, close=False):
        self.returned.append((conn, close))


class _ExhaustedPool:
    def getconn(self):
        raise RuntimeError("pool exhausted")


class _PooledProxy:
    def __init__(self, pool, *, dsn=""):
        self._pool = pool
        self.dsn = dsn


class _LoggerWrapper:
    def __init__(self, conn):
        self.conn = conn


def test_advisory_xact_lock_uses_one_pooled_transaction_connection() -> None:
    conn = _Connection()
    pool = _Pool(conn)
    proxy = _PooledProxy(pool)

    with advisory_xact_lock(proxy, lock_name="oauth-refresh") as (acquired, lock_conn):
        assert acquired is True
        assert lock_conn is conn
        assert conn.autocommit is False

    sql = " ".join(statement for statement, _params in conn.statements)
    assert "pg_try_advisory_xact_lock" in sql
    assert "pg_advisory_unlock" not in sql
    assert conn.commits == 1
    assert conn.rollbacks == 0
    assert conn.autocommit is True
    assert pool.returned == [(conn, False)]


def test_advisory_xact_lock_uses_explicit_logger_connection_target() -> None:
    conn = _Connection()
    wrapper = _LoggerWrapper(conn)

    with advisory_xact_lock(wrapper, lock_name="oauth-refresh") as (acquired, lock_conn):
        assert acquired is True
        assert lock_conn is conn

    assert conn.commits == 1
    assert conn.autocommit is True


def test_advisory_xact_lock_falls_back_to_logger_dsn_when_pool_is_exhausted(
    monkeypatch,
) -> None:
    fallback_conn = _Connection()
    connected_dsns: list[str] = []

    def _connect(dsn: str):
        connected_dsns.append(dsn)
        return fallback_conn

    monkeypatch.setattr(oauth_refresh_lock.psycopg_runtime, "connect", _connect)
    proxy = _PooledProxy(_ExhaustedPool(), dsn="postgresql://fallback")

    with advisory_xact_lock(proxy, lock_name="oauth-refresh") as (acquired, lock_conn):
        assert acquired is True
        assert lock_conn is fallback_conn

    assert connected_dsns == ["postgresql://fallback"]
    assert fallback_conn.commits == 1
    assert fallback_conn.rollbacks == 0
    assert fallback_conn.autocommit is True
    assert fallback_conn.closed_called is True


def test_advisory_xact_lock_rolls_back_on_critical_section_error() -> None:
    conn = _Connection()
    pool = _Pool(conn)
    proxy = _PooledProxy(pool)

    with (
        pytest.raises(RuntimeError, match="boom"),
        advisory_xact_lock(
            proxy,
            lock_name="oauth-refresh",
        ) as (_acquired, _lock_conn),
    ):
        raise RuntimeError("boom")

    assert conn.commits == 0
    assert conn.rollbacks == 1
    assert conn.autocommit is True
    assert pool.returned == [(conn, False)]


def test_advisory_xact_lock_rolls_back_when_acquisition_select_fails() -> None:
    conn = _FailingExecuteConnection()
    pool = _Pool(conn)
    proxy = _PooledProxy(pool)

    with (
        pytest.raises(RuntimeError, match="lock select failed"),
        advisory_xact_lock(
            proxy,
            lock_name="oauth-refresh",
        ) as (_acquired, _lock_conn),
    ):
        pytest.fail("lock body must not run after acquisition failure")

    assert conn.commits == 0
    assert conn.rollbacks == 1
    assert conn.autocommit is True
    assert pool.returned == [(conn, False)]
