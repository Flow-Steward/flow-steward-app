"""Cross-process serialization for extension filesystem/database lifecycle gates."""

from __future__ import annotations

import contextlib
import threading
from collections.abc import Iterator
from typing import Any

_LOCK_NAMESPACE = "platform.extension_lifecycle"
_process_lock = threading.RLock()
_thread_state = threading.local()


@contextlib.contextmanager
def extension_lifecycle_lock(*, connection: Any, blocking: bool = False) -> Iterator[bool]:
    """Try to hold the shared lifecycle lock on one pinned database session.

    The process lock covers local/non-database test runtimes. The advisory lock
    covers every application process. Nested lifecycle work in the owning thread
    is reentrant and reuses the outer database lock instead of opening a second
    pooled session that could deadlock against itself.
    """
    from core.infrastructure.db.db_pool import managed_transaction_connection

    managed_connection = (
        managed_transaction_connection(connection) if connection is not None else None
    )
    if not _process_lock.acquire(blocking=blocking):
        yield False
        return
    depth = int(getattr(_thread_state, "depth", 0) or 0)
    database_authority = getattr(_thread_state, "database_authority", None)
    try:
        same_database_authority = connection is database_authority
        if not same_database_authority and managed_connection is not None:
            same_database_authority = managed_connection is database_authority
        if not same_database_authority and database_authority is not None:
            stored_managed_connection = managed_transaction_connection(database_authority)
            same_database_authority = stored_managed_connection is not None and (
                stored_managed_connection is connection
                or stored_managed_connection is managed_connection
            )
        if depth and (connection is None or same_database_authority):
            _thread_state.depth = depth + 1
            try:
                yield True
            finally:
                _thread_state.depth = depth
            return
        if depth and database_authority is not None:
            yield False
            return
        if connection is None:
            _thread_state.depth = depth + 1
            _thread_state.database_authority = database_authority
            try:
                yield True
            finally:
                _thread_state.depth = depth
                _thread_state.database_authority = database_authority
            return
        with _connection_lifecycle_lock(
            connection,
            managed_connection=managed_connection,
            blocking=blocking,
        ) as acquired:
            if not acquired:
                yield False
                return
            _thread_state.depth = depth + 1
            _thread_state.database_authority = connection
            try:
                yield True
            finally:
                _thread_state.depth = depth
                _thread_state.database_authority = database_authority
    finally:
        _process_lock.release()


@contextlib.contextmanager
def _connection_lifecycle_lock(
    connection: Any,
    *,
    managed_connection: Any | None,
    blocking: bool,
) -> Iterator[bool]:
    from core.infrastructure.db.db_pool import (
        PooledConnectionProxy,
        pooled_connection,
    )

    if managed_connection is not None:
        with _database_lifecycle_transaction_lock(
            managed_connection, blocking=blocking
        ) as acquired:
            yield acquired
        return
    if isinstance(connection, PooledConnectionProxy):
        with (
            pooled_connection(connection._pool, autocommit=True) as lock_connection,
            _database_lifecycle_lock(lock_connection, blocking=blocking) as acquired,
        ):
            yield acquired
        return
    with _database_lifecycle_lock(connection, blocking=blocking) as acquired:
        yield acquired


@contextlib.contextmanager
def _database_lifecycle_transaction_lock(
    connection: Any, *, blocking: bool = False
) -> Iterator[bool]:
    """Acquire a lock owned by the already-managed root transaction."""
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT "
            + ("pg_advisory_xact_lock" if blocking else "pg_try_advisory_xact_lock")
            + "(hashtext(%s))",
            (_LOCK_NAMESPACE,),
        )
        row = cursor.fetchone()
    value = row[0] if isinstance(row, (tuple, list)) and row else row
    yield blocking or value is True


@contextlib.contextmanager
def _database_lifecycle_lock(connection: Any, *, blocking: bool = False) -> Iterator[bool]:
    acquired = False
    acquisition_complete = False
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT "
                + ("pg_advisory_lock" if blocking else "pg_try_advisory_lock")
                + "(hashtext(%s))",
                (_LOCK_NAMESPACE,),
            )
            row = cursor.fetchone()
        value = row[0] if isinstance(row, (tuple, list)) and row else row
        acquired = blocking or value is True
        acquisition_complete = True
        yield acquired
    except BaseException:
        if not acquisition_complete:
            # The server may have acquired the session lock even when the
            # result could not be observed. Closing is the only safe release.
            with contextlib.suppress(BaseException):
                connection.close()
        raise
    finally:
        if acquired:
            try:
                with connection.cursor() as cursor:
                    cursor.execute(
                        "SELECT pg_advisory_unlock(hashtext(%s))",
                        (_LOCK_NAMESPACE,),
                    )
                    row = cursor.fetchone()
                value = row[0] if isinstance(row, (tuple, list)) and row else row
                if value is not True:
                    raise RuntimeError("Extension lifecycle advisory lock release failed")
            except BaseException:
                # Never return a session-level advisory lock to a pool when its
                # release cannot be confirmed. Closing the session asks Postgres
                # to release the lock and makes the lifecycle failure explicit.
                with contextlib.suppress(BaseException):
                    connection.close()
                raise
