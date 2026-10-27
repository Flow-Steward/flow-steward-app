from __future__ import annotations

import asyncio
import sys
import threading
import time
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

pytest.importorskip("core.infrastructure.db.psycopg_runtime")


class _FakeCursor:
    def __init__(self):
        self.rowcount = 0

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def execute(self, *_args, **_kwargs):
        return None

    def fetchone(self):
        return (None,)

    def fetchall(self):
        return []


class _FakeConn:
    def __init__(self):
        self.autocommit = False
        self.closed = 0
        self.close_calls = 0
        self.cursor_calls = []
        self.transaction_status = 0
        self.commit_calls = 0
        self.rollback_calls = 0

    def cursor(self, *args, **kwargs):
        self.cursor_calls.append((args, kwargs))
        return _FakeCursor()

    def get_transaction_status(self):
        return self.transaction_status

    def close(self):
        self.close_calls += 1
        self.closed = 1

    def commit(self):
        self.commit_calls += 1

    def rollback(self):
        self.rollback_calls += 1


class _FakePool:
    def __init__(self):
        self.conn = _FakeConn()
        self.put_calls = []

    def getconn(self):
        return self.conn

    def putconn(self, conn, close=False):
        self.put_calls.append((conn, close))


def test_pool_bounds_reads_env_and_clamps():
    from core.infrastructure.db.db_pool import pool_bounds

    with patch.dict(
        "os.environ",
        {
            "FS_DB_POOL_MIN": "0",
            "FS_DB_POOL_MAX": "999",
        },
        clear=False,
    ):
        min_conn, max_conn = pool_bounds()

    assert min_conn == 1
    assert max_conn == 128


def test_pool_wait_timeout_reads_env_and_clamps():
    from core.infrastructure.db.db_pool import pool_wait_timeout

    with patch.dict(
        "os.environ",
        {
            "FS_DB_POOL_WAIT_TIMEOUT": "999",
        },
        clear=False,
    ):
        assert pool_wait_timeout() == 30.0


def test_threaded_connection_pool_waits_for_returned_connection():
    from core.infrastructure.db.psycopg_runtime.pool import ThreadedConnectionPool

    conns = [_FakeConn()]
    with patch(
        "core.infrastructure.db.psycopg_runtime.pool.connect",
        side_effect=lambda _dsn: conns.pop(0),
    ):
        pool = ThreadedConnectionPool(1, 1, "postgresql://example", wait_timeout=1.0)

    first = pool.getconn()
    acquired: list[_FakeConn] = []

    def _borrow_second() -> None:
        acquired.append(pool.getconn())

    thread = threading.Thread(target=_borrow_second)
    thread.start()
    time.sleep(0.05)
    assert acquired == []

    pool.putconn(first)
    thread.join(timeout=1.0)

    assert acquired == [first]
    pool.putconn(acquired[0])


def test_threaded_connection_pool_raises_after_wait_timeout():
    from core.infrastructure.db.psycopg_runtime.pool import PoolError, ThreadedConnectionPool

    conns = [_FakeConn()]
    with patch(
        "core.infrastructure.db.psycopg_runtime.pool.connect",
        side_effect=lambda _dsn: conns.pop(0),
    ):
        pool = ThreadedConnectionPool(1, 1, "postgresql://example", wait_timeout=0.01)

    first = pool.getconn()
    with pytest.raises(PoolError, match="connection pool exhausted"):
        pool.getconn()

    pool.putconn(first)


def test_threaded_connection_pool_discards_inherited_connections_after_fork():
    from core.infrastructure.db.psycopg_runtime.pool import ThreadedConnectionPool

    parent_conn = _FakeConn()
    child_conn = _FakeConn()
    conns = [parent_conn, child_conn]
    with patch(
        "core.infrastructure.db.psycopg_runtime.pool.connect",
        side_effect=lambda _dsn: conns.pop(0),
    ):
        pool = ThreadedConnectionPool(1, 1, "postgresql://example", wait_timeout=1.0)
        borrowed = pool.getconn()
        pool.putconn(borrowed)

        # Simulate the PID change seen by an object inherited into a forked
        # agent process. The child must never reuse the parent's DB socket.
        pool._owner_pid = -1
        inherited_replacement = pool.getconn()

    assert inherited_replacement is child_conn
    # Closing a copied psycopg object in the child can send a PostgreSQL
    # terminate packet over the shared socket and break the parent's session.
    assert parent_conn.close_calls == 0


def test_pooled_connection_proxy_borrows_and_returns_connection():
    from core.infrastructure.db.db_pool import PooledConnectionProxy

    pool = _FakePool()
    proxy = PooledConnectionProxy(pool)

    with proxy.cursor() as cur:
        assert isinstance(cur, _FakeCursor)

    assert pool.conn.autocommit is True
    assert len(pool.put_calls) == 1
    assert pool.put_calls[0][1] is False


def test_transaction_connection_binds_proxy_cursors_and_rolls_back_as_one_unit():
    from core.application.extensions.install import DBExtensionInstallStore
    from core.infrastructure.db.db_pool import PooledConnectionProxy, transaction_connection

    class _CountingPool(_FakePool):
        def __init__(self):
            super().__init__()
            self.get_calls = 0

        def getconn(self):
            self.get_calls += 1
            if self.get_calls > 1:
                raise AssertionError("proxy borrowed a second autocommit connection")
            return self.conn

    pool = _CountingPool()
    proxy = PooledConnectionProxy(pool)

    def _raises_operation_184():
        assert transaction_conn is pool.conn
        assert DBExtensionInstallStore(proxy).participates_in_transaction(transaction_conn) is True
        with proxy.cursor():
            pass
        with proxy.cursor():
            raise RuntimeError("fault between lifecycle writes")

    with (
        pytest.raises(RuntimeError, match="fault between lifecycle writes"),
        transaction_connection(proxy) as transaction_conn,
    ):
        _raises_operation_184()

    assert pool.get_calls == 1
    assert len(pool.conn.cursor_calls) == 2
    assert pool.conn.commit_calls == 0
    assert pool.conn.rollback_calls == 1
    assert pool.conn.autocommit is False
    assert pool.put_calls == [(pool.conn, False)]


def test_transaction_connection_ignores_unrelated_private_pool_attribute():
    from core.infrastructure.db.db_pool import transaction_connection

    class _UnrelatedPool:
        def getconn(self):
            raise AssertionError("an arbitrary _pool attribute is not a pooled connection proxy")

    conn = _FakeConn()
    conn._pool = _UnrelatedPool()

    with transaction_connection(conn) as transaction_conn:
        assert transaction_conn is conn

    assert conn.commit_calls == 0
    assert conn.rollback_calls == 0


def test_nested_pooled_transaction_reuses_one_connection_and_outer_owns_rollback():
    from core.infrastructure.db.db_pool import PooledConnectionProxy, transaction_connection

    class _CountingPool(_FakePool):
        def __init__(self):
            super().__init__()
            self.get_calls = 0

        def getconn(self):
            self.get_calls += 1
            return self.conn

    pool = _CountingPool()
    proxy = PooledConnectionProxy(pool)

    with (
        pytest.raises(RuntimeError, match="nested transaction failed"),
        transaction_connection(proxy) as outer_conn,
        transaction_connection(proxy) as inner_conn,
    ):
        assert inner_conn is outer_conn
        raise RuntimeError("nested transaction failed")

    assert pool.get_calls == 1
    assert pool.conn.commit_calls == 0
    assert pool.conn.rollback_calls == 1
    assert pool.put_calls == [(pool.conn, False)]


def test_transaction_binding_rejects_async_child_task_while_owner_is_active():
    from core.infrastructure.db.db_pool import PooledConnectionProxy, transaction_connection

    proxy = PooledConnectionProxy(_FakePool())

    async def _exercise_child_task() -> None:
        async def _child() -> None:
            with (
                pytest.raises(RuntimeError, match="transaction context cannot be reused"),
                transaction_connection(proxy),
            ):
                pass

        with transaction_connection(proxy):
            await asyncio.create_task(_child())

    asyncio.run(_exercise_child_task())


def test_transaction_binding_rejects_async_child_task_after_owner_exit():
    from core.infrastructure.db.db_pool import PooledConnectionProxy, transaction_connection

    proxy = PooledConnectionProxy(_FakePool())

    async def _exercise_stale_child_context() -> None:
        child_started = asyncio.Event()
        inspect_binding = asyncio.Event()

        async def _child() -> None:
            child_started.set()
            await inspect_binding.wait()
            with (
                pytest.raises(RuntimeError, match="transaction context cannot be reused"),
                transaction_connection(proxy),
            ):
                pass

        with transaction_connection(proxy):
            child = asyncio.create_task(_child())
            await child_started.wait()

        inspect_binding.set()
        await child

    asyncio.run(_exercise_stale_child_context())


def test_caught_inner_failure_marks_owner_transaction_rollback_only():
    import contextlib

    from core.infrastructure.db.db_pool import defer_transaction_rollback, transaction_connection

    events: list[str] = []

    class _DirectConnection:
        autocommit = True
        closed = 0

        def __init__(self):
            self.depth = 0

        @contextlib.contextmanager
        def transaction(self):
            self.depth += 1
            depth = self.depth
            events.append("outer_begin" if depth == 1 else "savepoint_begin")
            try:
                yield
            except BaseException:
                events.append("outer_rollback" if depth == 1 else "savepoint_rollback")
                raise
            else:
                events.append("outer_commit" if depth == 1 else "savepoint_release")
            finally:
                self.depth -= 1

    conn = _DirectConnection()

    def _raises_operation_327():
        try:
            with transaction_connection(conn):
                raise RuntimeError("caught inner failure")
        except RuntimeError:
            assert defer_transaction_rollback(conn, lambda: events.append("compensate"))
            events.append("inner_failure_caught")

    with (
        pytest.raises(RuntimeError, match="caught inner failure"),
        transaction_connection(conn),
    ):
        _raises_operation_327()

    assert events == [
        "outer_begin",
        "savepoint_begin",
        "savepoint_rollback",
        "inner_failure_caught",
        "outer_rollback",
        "compensate",
    ]


def test_deferred_compensations_run_lifo_and_attach_every_failure():
    from core.infrastructure.db.db_pool import defer_transaction_rollback, transaction_connection

    conn = _FakeConn()
    events: list[str] = []

    def _restore_first() -> None:
        events.append("first")
        raise ValueError("first compensation failed")

    def _restore_second() -> None:
        events.append("second")
        raise LookupError("second compensation failed")

    def _raises_operation_362():
        assert defer_transaction_rollback(conn, _restore_first)
        assert defer_transaction_rollback(conn, _restore_second)
        raise RuntimeError("owner failed")

    with (
        pytest.raises(RuntimeError, match="owner failed") as caught,
        transaction_connection(conn),
    ):
        _raises_operation_362()

    assert events == ["second", "first"]
    failures = caught.value.transaction_rollback_failures
    assert isinstance(failures, ExceptionGroup)
    assert [str(failure) for failure in failures.exceptions] == [
        "second compensation failed",
        "first compensation failed",
    ]
    assert any(
        "2 deferred transaction rollback callbacks failed" in note
        for note in caught.value.__notes__
    )


def test_deferred_compensations_collect_base_exceptions_without_replacing_owner_exit():
    from core.infrastructure.db.db_pool import defer_transaction_rollback, transaction_connection

    conn = _FakeConn()
    events: list[str] = []
    owner_exit = SystemExit("owner requested shutdown")

    def _restore_first() -> None:
        events.append("first")
        raise ValueError("first compensation failed")

    def _restore_second() -> None:
        events.append("second")
        raise asyncio.CancelledError("second compensation cancelled")

    def _raises_operation_398():
        assert defer_transaction_rollback(conn, _restore_first)
        assert defer_transaction_rollback(conn, _restore_second)
        raise owner_exit

    with (
        pytest.raises(SystemExit) as caught,
        transaction_connection(conn),
    ):
        _raises_operation_398()

    assert caught.value is owner_exit
    assert events == ["second", "first"]
    failures = caught.value.transaction_rollback_failures
    assert isinstance(failures, BaseExceptionGroup)
    assert [type(failure) for failure in failures.exceptions] == [
        asyncio.CancelledError,
        ValueError,
    ]
    assert [str(failure) for failure in failures.exceptions] == [
        "second compensation cancelled",
        "first compensation failed",
    ]


def test_direct_idle_non_autocommit_native_connection_success_stays_caller_owned():
    import contextlib

    from core.infrastructure.db.db_pool import transaction_connection

    events: list[str] = []

    class _DirectConnection(_FakeConn):
        @contextlib.contextmanager
        def transaction(self):
            events.append("transaction_begin")
            try:
                yield
            except BaseException:
                events.append("transaction_rollback")
                raise
            else:
                events.append("transaction_commit")

    conn = _DirectConnection()
    conn.autocommit = False
    conn.transaction_status = 0

    with transaction_connection(conn) as transaction_conn:
        assert transaction_conn is conn

    assert events == []
    assert conn.commit_calls == 0
    assert conn.rollback_calls == 0


def test_direct_active_non_autocommit_native_connection_success_uses_savepoint():
    import contextlib

    from core.infrastructure.db.db_pool import transaction_connection

    events: list[str] = []

    class _DirectConnection(_FakeConn):
        @contextlib.contextmanager
        def transaction(self):
            events.append("savepoint_begin")
            try:
                yield
            except BaseException:
                events.append("savepoint_rollback")
                raise
            else:
                events.append("savepoint_release")

    conn = _DirectConnection()
    conn.autocommit = False
    conn.transaction_status = 2

    with transaction_connection(conn) as transaction_conn:
        assert transaction_conn is conn

    assert events == ["savepoint_begin", "savepoint_release"]
    assert conn.commit_calls == 0
    assert conn.rollback_calls == 0


def test_direct_nested_install_rolls_back_savepoint_and_outer_transaction():
    import contextlib

    from core.application.extensions.command_service import ExtensionCommandService
    from core.application.management.command_models import ExtensionReleaseInstallInput
    from core.infrastructure.db.db_pool import transaction_connection

    events: list[str] = []

    class _DirectConnection:
        def __init__(self):
            self._autocommit = True
            self.autocommit_assignments = 0
            self.depth = 0
            self.closed = 0

        @property
        def autocommit(self):
            return self._autocommit

        @autocommit.setter
        def autocommit(self, value):
            self.autocommit_assignments += 1
            self._autocommit = bool(value)

        @contextlib.contextmanager
        def transaction(self):
            self.depth += 1
            depth = self.depth
            events.append("outer_begin" if depth == 1 else "savepoint_begin")
            try:
                yield
            except BaseException:
                events.append("outer_rollback" if depth == 1 else "savepoint_rollback")
                raise
            else:
                events.append("outer_commit" if depth == 1 else "savepoint_release")
            finally:
                self.depth -= 1

        def commit(self):
            raise AssertionError("commit() is illegal inside a psycopg transaction context")

        def rollback(self):
            raise AssertionError("rollback() is illegal inside a psycopg transaction context")

    conn = _DirectConnection()
    logger = type("_Logger", (), {"conn": conn, "db_store": None, "dsn": ""})()
    service = ExtensionCommandService(
        get_logger=lambda: logger,
        get_install_store=lambda: object(),
        get_runtime_store=lambda: object(),
    )
    service._install._install_release = lambda *_args, **_kwargs: (_ for _ in ()).throw(
        RuntimeError("nested install failed")
    )

    _raises_input_539_1 = ExtensionReleaseInstallInput(release_id="candidate")
    with (
        pytest.raises(RuntimeError, match="nested install failed"),
        transaction_connection(conn),
    ):
        service._install.install_release(_raises_input_539_1)

    assert events == [
        "outer_begin",
        "savepoint_begin",
        "savepoint_rollback",
        "outer_rollback",
    ]
    assert conn.autocommit_assignments == 0


def test_direct_nested_install_rolls_back_system_exit():
    import contextlib

    from core.application.extensions.command_service import ExtensionCommandService
    from core.application.management.command_models import ExtensionReleaseInstallInput
    from core.infrastructure.db.db_pool import transaction_connection

    events: list[str] = []

    class _DirectConnection:
        autocommit = True
        closed = 0

        def __init__(self):
            self.depth = 0

        @contextlib.contextmanager
        def transaction(self):
            self.depth += 1
            depth = self.depth
            events.append("outer_begin" if depth == 1 else "savepoint_begin")
            try:
                yield
            except BaseException:
                events.append("outer_rollback" if depth == 1 else "savepoint_rollback")
                raise
            else:
                events.append("outer_commit" if depth == 1 else "savepoint_release")
            finally:
                self.depth -= 1

    conn = _DirectConnection()
    logger = type("_Logger", (), {"conn": conn, "db_store": None, "dsn": ""})()
    service = ExtensionCommandService(
        get_logger=lambda: logger,
        get_install_store=lambda: object(),
        get_runtime_store=lambda: object(),
    )
    owner_exit = SystemExit("stop install")
    service._install._install_release = lambda *_args, **_kwargs: (_ for _ in ()).throw(owner_exit)

    _raises_input_595_1 = ExtensionReleaseInstallInput(release_id="candidate")
    with (
        pytest.raises(SystemExit) as caught,
        transaction_connection(conn),
    ):
        service._install.install_release(_raises_input_595_1)

    assert caught.value is owner_exit
    assert events == [
        "outer_begin",
        "savepoint_begin",
        "savepoint_rollback",
        "outer_rollback",
    ]


def test_transaction_binding_rejects_copied_context_in_another_thread():
    from contextvars import copy_context

    from core.infrastructure.db.db_pool import (
        PooledConnectionProxy,
        active_transaction_connection,
        transaction_connection,
    )

    proxy = PooledConnectionProxy(_FakePool())
    errors: list[BaseException] = []

    with transaction_connection(proxy):
        copied_context = copy_context()

        def _read_copied_binding() -> None:
            try:
                copied_context.run(active_transaction_connection, proxy)
            except BaseException as exc:
                errors.append(exc)

        thread = threading.Thread(target=_read_copied_binding)
        thread.start()
        thread.join(timeout=1)

    assert not thread.is_alive()
    assert len(errors) == 1
    assert isinstance(errors[0], RuntimeError)
    assert str(errors[0]) == "transaction context cannot be reused across threads"


def test_transaction_connection_rolls_back_existing_non_autocommit_connection_on_error():
    from core.infrastructure.db.db_pool import transaction_connection

    conn = _FakeConn()
    assert conn.autocommit is False

    with (
        pytest.raises(RuntimeError, match="direct transaction failed"),
        transaction_connection(conn) as transaction_conn,
    ):
        assert transaction_conn is conn
        raise RuntimeError("direct transaction failed")

    assert conn.commit_calls == 0
    assert conn.rollback_calls == 1
    assert conn.autocommit is False


def test_db_extension_participants_require_the_exact_yielded_transaction_connection():
    from core.application.extensions.install import DBExtensionInstallStore
    from core.application.extensions.runtime_store_db import DBExtensionRuntimeStore
    from core.application.registry.db_store import RegistryDBStore

    transaction_conn = object()
    different_conn = object()
    install_store = DBExtensionInstallStore(transaction_conn)
    runtime_store = DBExtensionRuntimeStore(transaction_conn)
    registry_store = RegistryDBStore("")
    registry_store._set_tx_conn(transaction_conn)

    assert install_store.participates_in_transaction(transaction_conn) is True
    assert runtime_store.participates_in_transaction(transaction_conn) is True
    assert registry_store.participates_in_transaction(transaction_conn) is True
    assert install_store.participates_in_transaction(different_conn) is False
    assert runtime_store.participates_in_transaction(different_conn) is False
    assert registry_store.participates_in_transaction(different_conn) is False


def test_nested_transaction_connection_reuses_the_outer_pooled_connection():
    from core.infrastructure.db.db_pool import PooledConnectionProxy, transaction_connection

    class _CountingPool(_FakePool):
        def __init__(self):
            super().__init__()
            self.get_calls = 0

        def getconn(self):
            self.get_calls += 1
            if self.get_calls > 1:
                raise AssertionError("nested transaction borrowed another connection")
            return self.conn

    pool = _CountingPool()
    proxy = PooledConnectionProxy(pool)

    with transaction_connection(proxy) as outer, transaction_connection(proxy) as inner:
        assert inner is outer
        with proxy.cursor():
            pass

    assert pool.get_calls == 1
    assert pool.conn.commit_calls == 1
    assert pool.conn.rollback_calls == 0


def test_pooled_connection_proxy_discards_connection_after_exception():
    from core.infrastructure.db.db_pool import PooledConnectionProxy

    pool = _FakePool()
    proxy = PooledConnectionProxy(pool)

    with pytest.raises(RuntimeError, match="boom"), proxy.cursor():
        raise RuntimeError("boom")

    assert len(pool.put_calls) == 1
    assert pool.put_calls[0][1] is True


def test_pooled_connection_proxy_discards_non_idle_connection():
    from core.infrastructure.db.db_pool import PooledConnectionProxy

    pool = _FakePool()
    pool.conn.transaction_status = 1
    proxy = PooledConnectionProxy(pool)

    with proxy.cursor():
        pass

    assert len(pool.put_calls) == 1
    assert pool.put_calls[0][1] is True


def test_pooled_connection_proxy_falls_back_to_direct_connection_when_pool_is_exhausted():
    from core.infrastructure.db.db_pool import PooledConnectionProxy

    class _ExhaustedPool:
        def __init__(self):
            self.put_calls = []

        def getconn(self):
            raise RuntimeError("connection pool exhausted")

        def putconn(self, conn, close=False):
            self.put_calls.append((conn, close))

    pool = _ExhaustedPool()
    fallback_conn = _FakeConn()

    with patch(
        "core.infrastructure.db.db_pool.psycopg_runtime.connect", return_value=fallback_conn
    ) as mock_connect:
        proxy = PooledConnectionProxy(pool, dsn="postgresql://example")
        with proxy.cursor() as cur:
            assert isinstance(cur, _FakeCursor)

    mock_connect.assert_called_once_with("postgresql://example")
    assert pool.put_calls == []
    assert fallback_conn.close_calls == 1


def test_platform_logger_uses_shared_pool_when_enabled():
    from core.infrastructure.db.db_pool import PooledConnectionProxy
    from tests.support.platform_test_store import PlatformTestStore as PlatformLogger

    fake_pool = _FakePool()
    with (
        patch("core.infrastructure.db.platform_logger.pooling_enabled", return_value=True),
        patch("core.infrastructure.db.platform_logger.pool_bounds", return_value=(1, 8)),
        patch("core.infrastructure.db.platform_logger.get_threaded_pool", return_value=fake_pool),
        patch("core.infrastructure.db.platform_logger.psycopg_runtime.connect") as mock_connect,
    ):
        bl = PlatformLogger("postgresql://example")

    assert isinstance(bl.conn, PooledConnectionProxy)
    assert bl.conn._dsn == "postgresql://example"
    mock_connect.assert_not_called()


def test_platform_logger_falls_back_to_direct_connection_when_pool_size_is_one():
    from tests.support.platform_test_store import PlatformTestStore as PlatformLogger

    fake_conn = _FakeConn()
    with (
        patch("core.infrastructure.db.platform_logger.pooling_enabled", return_value=True),
        patch("core.infrastructure.db.platform_logger.pool_bounds", return_value=(1, 1)),
        patch(
            "core.infrastructure.db.platform_logger.psycopg_runtime.connect", return_value=fake_conn
        ) as mock_connect,
    ):
        bl = PlatformLogger("postgresql://example")

    assert bl.conn is fake_conn
    assert bl._direct_conn is fake_conn
    assert fake_conn.autocommit is True
    mock_connect.assert_called_once()
