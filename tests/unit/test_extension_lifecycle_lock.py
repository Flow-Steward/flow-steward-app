from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import pytest

from core.infrastructure.db import db_pool
from core.infrastructure.extension_runtime.lifecycle_lock import (
    extension_lifecycle_lock,
)


class _Cursor:
    def __init__(self, *, rows: list[object], calls: list[tuple[str, object]]) -> None:
        self._rows = rows
        self._calls = calls

    def __enter__(self) -> _Cursor:
        return self

    def __exit__(self, *_args: object) -> bool:
        return False

    def execute(self, sql: str, params: object = None) -> None:
        self._calls.append((" ".join(sql.split()), params))

    def fetchone(self) -> object:
        return self._rows.pop(0)


class _Connection:
    def __init__(self, *rows: object) -> None:
        self.rows = list(rows)
        self.calls: list[tuple[str, object]] = []
        self.closed = False

    def cursor(self) -> _Cursor:
        return _Cursor(rows=self.rows, calls=self.calls)

    def close(self) -> None:
        self.closed = True


class _ForbiddenConnection:
    def cursor(self) -> Iterator[Any]:
        raise AssertionError("a reentrant lifecycle lock must reuse the outer database lock")


class _InterruptedUnlockConnection(_Connection):
    def cursor(self) -> _Cursor:
        cursor = super().cursor()
        original_execute = cursor.execute

        def _execute(sql: str, params: object = None) -> None:
            if "pg_advisory_unlock" in sql:
                raise KeyboardInterrupt("unlock interrupted")
            original_execute(sql, params)

        cursor.execute = _execute  # type: ignore[method-assign]
        return cursor


class _InterruptedAcquireConnection(_Connection):
    def cursor(self) -> _Cursor:
        cursor = super().cursor()

        def _fetchone() -> object:
            raise KeyboardInterrupt("acquire result interrupted")

        cursor.fetchone = _fetchone  # type: ignore[method-assign]
        return cursor


def test_lifecycle_lock_acquires_and_releases_database_advisory_lock() -> None:
    connection = _Connection((True,), (True,))

    with extension_lifecycle_lock(connection=connection) as acquired:
        assert acquired is True
        assert connection.calls == [
            (
                "SELECT pg_try_advisory_lock(hashtext(%s))",
                ("platform.extension_lifecycle",),
            )
        ]

    assert connection.calls == [
        (
            "SELECT pg_try_advisory_lock(hashtext(%s))",
            ("platform.extension_lifecycle",),
        ),
        (
            "SELECT pg_advisory_unlock(hashtext(%s))",
            ("platform.extension_lifecycle",),
        ),
    ]


def test_lifecycle_lock_can_wait_for_database_advisory_lock() -> None:
    connection = _Connection((None,), (True,))

    with extension_lifecycle_lock(connection=connection, blocking=True) as acquired:
        assert acquired is True

    assert connection.calls[0][0] == "SELECT pg_advisory_lock(hashtext(%s))"
    assert connection.calls[1][0] == "SELECT pg_advisory_unlock(hashtext(%s))"


def test_lifecycle_lock_is_reentrant_without_acquiring_a_second_database_lock() -> None:
    connection = _Connection((True,), (True,))

    with (
        extension_lifecycle_lock(connection=connection) as outer_acquired,
        extension_lifecycle_lock(connection=connection) as nested_acquired,
    ):
        assert outer_acquired is True
        assert nested_acquired is True

    assert len(connection.calls) == 2


def test_lifecycle_lock_rejects_a_different_nested_database_authority() -> None:
    outer_connection = _Connection((True,), (True,))
    nested_connection = _ForbiddenConnection()

    with extension_lifecycle_lock(connection=outer_connection) as outer_acquired:
        assert outer_acquired is True
        with extension_lifecycle_lock(connection=nested_connection) as nested_acquired:
            assert nested_acquired is False

    assert len(outer_connection.calls) == 2


def test_local_outer_lifecycle_lock_acquires_database_lock_for_nested_database_work() -> None:
    connection = _Connection((True,), (True,))

    with extension_lifecycle_lock(connection=None) as outer_acquired:
        assert outer_acquired is True
        with extension_lifecycle_lock(connection=connection) as nested_acquired:
            assert nested_acquired is True
            assert connection.calls == [
                (
                    "SELECT pg_try_advisory_lock(hashtext(%s))",
                    ("platform.extension_lifecycle",),
                )
            ]

    assert connection.calls == [
        (
            "SELECT pg_try_advisory_lock(hashtext(%s))",
            ("platform.extension_lifecycle",),
        ),
        (
            "SELECT pg_advisory_unlock(hashtext(%s))",
            ("platform.extension_lifecycle",),
        ),
    ]


def test_local_outer_lifecycle_lock_rejects_nested_database_work_when_lock_is_unavailable() -> None:
    connection = _Connection((False,))

    with extension_lifecycle_lock(connection=None) as outer_acquired:
        assert outer_acquired is True
        with extension_lifecycle_lock(connection=connection) as nested_acquired:
            assert nested_acquired is False

    assert connection.calls == [
        (
            "SELECT pg_try_advisory_lock(hashtext(%s))",
            ("platform.extension_lifecycle",),
        )
    ]


def test_lifecycle_lock_reports_database_advisory_lock_unavailable_without_unlock() -> None:
    connection = _Connection((False,))

    with extension_lifecycle_lock(connection=connection) as acquired:
        assert acquired is False

    assert connection.calls == [
        (
            "SELECT pg_try_advisory_lock(hashtext(%s))",
            ("platform.extension_lifecycle",),
        )
    ]


def test_lifecycle_lock_pins_one_pooled_session_for_acquire_and_release(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pool = object()
    proxy = db_pool.PooledConnectionProxy(pool)
    lock_connection = _Connection((True,), (True,))
    calls: list[tuple[object, bool]] = []

    @contextmanager
    def _pooled_connection(candidate_pool, *, autocommit):
        calls.append((candidate_pool, autocommit))
        yield lock_connection

    monkeypatch.setattr(db_pool, "pooled_connection", _pooled_connection)

    with extension_lifecycle_lock(connection=proxy) as acquired:
        assert acquired is True

    assert calls == [(pool, True)]
    assert len(lock_connection.calls) == 2


def test_lifecycle_lock_uses_bound_managed_transaction_until_outer_commit() -> None:
    transaction_connection = _Connection((True,))
    transaction_connection.autocommit = True
    transaction_connection.closed = 0
    transaction_connection.get_transaction_status = lambda: 0  # type: ignore[attr-defined]
    transaction_connection.commit = lambda: None  # type: ignore[attr-defined]
    transaction_connection.rollback = lambda: None  # type: ignore[attr-defined]
    fallback_lock_connection = _Connection((True,), (True,))

    class _Pool:
        def __init__(self) -> None:
            self.borrowed: list[_Connection] = []

        def getconn(self) -> _Connection:
            connection = transaction_connection if not self.borrowed else fallback_lock_connection
            self.borrowed.append(connection)
            return connection

        def putconn(self, _connection: _Connection, *, close: bool = False) -> None:
            assert close is False

    pool = _Pool()
    proxy = db_pool.PooledConnectionProxy(pool)
    foreign_pool = type(
        "_ForeignPool",
        (),
        {"getconn": lambda self: (_ for _ in ()).throw(AssertionError("foreign borrow"))},
    )()
    foreign_proxy = db_pool.PooledConnectionProxy(foreign_pool)

    with (
        db_pool.transaction_connection(proxy),
        extension_lifecycle_lock(connection=proxy) as acquired,
    ):
        assert acquired is True
        with extension_lifecycle_lock(connection=transaction_connection) as nested_acquired:
            assert nested_acquired is True
        with extension_lifecycle_lock(connection=foreign_proxy) as foreign_acquired:
            assert foreign_acquired is False

    assert pool.borrowed == [transaction_connection]
    assert transaction_connection.calls == [
        (
            "SELECT pg_try_advisory_xact_lock(hashtext(%s))",
            ("platform.extension_lifecycle",),
        )
    ]


def test_lifecycle_lock_recognizes_managed_direct_transaction_before_first_statement() -> None:
    connection = _Connection((True,))
    connection.autocommit = True
    connection.closed = 0
    connection.get_transaction_status = lambda: 0  # type: ignore[attr-defined]
    connection.commit = lambda: None  # type: ignore[attr-defined]
    connection.rollback = lambda: None  # type: ignore[attr-defined]

    with (
        db_pool.transaction_connection(connection),
        extension_lifecycle_lock(connection=connection) as acquired,
    ):
        assert acquired is True

    assert connection.calls == [
        (
            "SELECT pg_try_advisory_xact_lock(hashtext(%s))",
            ("platform.extension_lifecycle",),
        )
    ]


def test_session_lock_reentry_accepts_its_later_bound_transaction_connection() -> None:
    session_lock_connection = _Connection((True,), (True,))
    transaction_connection = _Connection()
    transaction_connection.autocommit = True
    transaction_connection.closed = 0
    transaction_connection.get_transaction_status = lambda: 0  # type: ignore[attr-defined]
    transaction_connection.commit = lambda: None  # type: ignore[attr-defined]
    transaction_connection.rollback = lambda: None  # type: ignore[attr-defined]

    class _Pool:
        def __init__(self) -> None:
            self.available = [session_lock_connection, transaction_connection]

        def getconn(self) -> _Connection:
            return self.available.pop(0)

        def putconn(self, _connection: _Connection, *, close: bool = False) -> None:
            assert close is False

    proxy = db_pool.PooledConnectionProxy(_Pool())

    with extension_lifecycle_lock(connection=proxy) as outer_acquired:
        assert outer_acquired is True
        with (
            db_pool.transaction_connection(proxy) as bound_connection,
            extension_lifecycle_lock(connection=bound_connection) as nested_acquired,
        ):
            assert nested_acquired is True

    assert session_lock_connection.calls == [
        (
            "SELECT pg_try_advisory_lock(hashtext(%s))",
            ("platform.extension_lifecycle",),
        ),
        (
            "SELECT pg_advisory_unlock(hashtext(%s))",
            ("platform.extension_lifecycle",),
        ),
    ]
    assert transaction_connection.calls == []


def test_lifecycle_lock_closes_session_when_unlock_cannot_be_confirmed() -> None:
    connection = _Connection((True,), (False,))

    with (
        pytest.raises(RuntimeError, match="advisory lock release failed"),
        extension_lifecycle_lock(connection=connection) as acquired,
    ):
        assert acquired is True

    assert connection.closed is True


def test_lifecycle_lock_closes_session_when_acquisition_result_is_unknown() -> None:
    connection = _InterruptedAcquireConnection()

    with (
        pytest.raises(KeyboardInterrupt, match="acquire result interrupted"),
        extension_lifecycle_lock(connection=connection),
    ):
        pass

    assert connection.closed is True


def test_lifecycle_lock_closes_session_when_unlock_is_interrupted() -> None:
    connection = _InterruptedUnlockConnection((True,))

    with (
        pytest.raises(KeyboardInterrupt, match="unlock interrupted"),
        extension_lifecycle_lock(connection=connection) as acquired,
    ):
        assert acquired is True

    assert connection.closed is True


def test_unavailable_database_lock_does_not_grant_nested_false_ownership() -> None:
    outer_connection = _Connection((False,))
    nested_connection = _Connection((False,))

    with extension_lifecycle_lock(connection=outer_connection) as outer_acquired:
        assert outer_acquired is False
        with extension_lifecycle_lock(connection=nested_connection) as nested_acquired:
            assert nested_acquired is False

    assert len(outer_connection.calls) == 1
    assert len(nested_connection.calls) == 1


def test_lifecycle_lock_reports_process_lock_unavailable_to_another_thread() -> None:
    observed: list[bool] = []

    with extension_lifecycle_lock(connection=None) as acquired:
        assert acquired is True

        thread = threading.Thread(
            target=lambda: _record_lock_result(observed),
            daemon=True,
        )
        thread.start()
        thread.join(timeout=1)

        assert not thread.is_alive()
        assert observed == [False]


def _record_lock_result(observed: list[bool]) -> None:
    with extension_lifecycle_lock(connection=None) as acquired:
        observed.append(acquired)
