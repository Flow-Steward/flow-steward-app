from __future__ import annotations

from contextlib import contextmanager

import pytest

from core.infrastructure.db import db_pool
from core.infrastructure.extension_runtime.extension_operation_lock import extension_operation_lock


class _Cursor:
    def __init__(self, rows, calls):
        self.rows, self.calls = rows, calls

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, sql, params=None):
        self.calls.append((" ".join(sql.split()), params))

    def fetchone(self):
        return self.rows.pop(0)


class _Connection:
    def __init__(self, *rows):
        self.rows = list(rows)
        self.calls = []
        self.closed = False

    def cursor(self):
        return _Cursor(self.rows, self.calls)

    def close(self):
        self.closed = True


def _extension():
    return {
        "extension_key": "fixture.spreadsheets",
        "action_definitions": [
            {
                "action_id": "append_rows",
                "contract": {"serialization": {"key_fields": ["sheet_id"]}},
            }
        ],
    }


def _lock(connection):
    return extension_operation_lock(
        connection=connection,
        extension=_extension(),
        action_id="append_rows",
        input_payload={"sheet_id": "sheet-1"},
        runtime_context={"extension_install_id": "install-1"},
    )


def test_extension_operation_lock_acquires_and_releases_exact_declared_key():
    connection = _Connection((True,), (True,))
    with _lock(connection) as acquired:
        assert acquired is True
        assert len(connection.calls) == 1
    assert "pg_advisory_unlock" in connection.calls[1][0]
    assert connection.calls[0][1] == connection.calls[1][1]
    assert (
        "fixture.spreadsheets:install-1:append_rows:sheet_id=sheet-1" in connection.calls[0][1][0]
    )


def test_extension_operation_lock_fails_closed_on_contention_or_missing_authority():
    connection = _Connection((False,))
    with _lock(connection) as acquired:
        assert acquired is False
    with _lock(None) as acquired:
        assert acquired is False


def test_declared_serialization_fails_closed_when_install_authority_is_missing():
    with extension_operation_lock(
        connection=_Connection(),
        extension=_extension(),
        action_id="append_rows",
        input_payload={"sheet_id": "sheet-1"},
        runtime_context={},
    ) as acquired:
        assert acquired is False


def test_extension_operation_lock_pins_one_pool_session(monkeypatch: pytest.MonkeyPatch):
    pool = object()
    proxy = db_pool.PooledConnectionProxy(pool)
    lock_connection = _Connection((True,), (True,))
    calls = []

    @contextmanager
    def pooled(candidate_pool, *, autocommit):
        calls.append((candidate_pool, autocommit))
        yield lock_connection

    monkeypatch.setattr(db_pool, "pooled_connection", pooled)
    with _lock(proxy) as acquired:
        assert acquired is True
    assert calls == [(pool, True)]


def test_extension_operation_lock_does_not_swallow_process_interrupts():
    class InterruptingConnection:
        closed = False

        def cursor(self):
            raise KeyboardInterrupt

        def close(self):
            self.closed = True

    connection = InterruptingConnection()

    with pytest.raises(KeyboardInterrupt), _lock(connection):
        pass


def test_extension_operation_lock_does_not_swallow_pool_interrupts(
    monkeypatch: pytest.MonkeyPatch,
):
    pool = object()
    proxy = db_pool.PooledConnectionProxy(pool)

    class InterruptingPoolContext:
        def __enter__(self):
            raise KeyboardInterrupt

        def __exit__(self, *_args):
            return False

    monkeypatch.setattr(
        db_pool,
        "pooled_connection",
        lambda *_args, **_kwargs: InterruptingPoolContext(),
    )

    with pytest.raises(KeyboardInterrupt), _lock(proxy):
        pass


def test_undeclared_action_has_no_serialization_side_effect():
    with extension_operation_lock(
        connection=None,
        extension=_extension(),
        action_id="read_rows",
        input_payload={},
        runtime_context={},
    ) as acquired:
        assert acquired is True
