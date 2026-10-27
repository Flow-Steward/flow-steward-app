"""Shared psycopg_runtime connection-pool helpers for observability/runtime DB clients."""

from __future__ import annotations

import asyncio
import logging
import sys
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any

from core.infrastructure.config.runtime_env import runtime_env as _runtime_env

_PSYCOPG2_IMPORT_ERROR: Exception | None = None
try:
    from core.infrastructure.db import psycopg_runtime as psycopg_runtime
    from core.infrastructure.db.psycopg_runtime.pool import ThreadedConnectionPool
except Exception as exc:  # pragma: no cover - exercised in optional-dependency tests
    psycopg_runtime = None
    ThreadedConnectionPool = Any
    _PSYCOPG2_IMPORT_ERROR = exc

logger = logging.getLogger(__name__)

_POOL_LOCK = threading.Lock()
_POOLS: dict[tuple[str, int, int], ThreadedConnectionPool] = {}
_TRANSACTION_STATUS_IDLE = int(
    getattr(getattr(psycopg_runtime, "extensions", None), "TRANSACTION_STATUS_IDLE", 0)
)
_BOUND_PROXY_TRANSACTIONS: ContextVar[dict[int, _TransactionBinding] | None]


@dataclass(slots=True)
class _TransactionState:
    connection: Any
    bound_targets: tuple[Any, ...]
    owner_thread_id: int
    owner_task: asyncio.Task[Any] | None
    active: bool = True
    generation: int = 1
    rollback_only: bool = False
    rollback_cause: BaseException | None = None
    rollback_callbacks: list[Callable[[], None]] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class _TransactionBinding:
    state: _TransactionState
    generation: int


_BOUND_PROXY_TRANSACTIONS = ContextVar(
    "bound_proxy_transactions",
    default=None,
)


class _AfterCommitFrame:
    __slots__ = ("callbacks", "open")

    def __init__(self) -> None:
        self.callbacks: list[Callable[[], None]] = []
        self.open = True


_AFTER_COMMIT_CALLBACKS: ContextVar[dict[int, _AfterCommitFrame] | None] = ContextVar(
    "after_commit_callbacks",
    default=None,
)


def _require_psycopg_runtime() -> None:
    if _PSYCOPG2_IMPORT_ERROR is not None:
        raise ModuleNotFoundError("psycopg_runtime") from _PSYCOPG2_IMPORT_ERROR


def _read_int_env(name: str, default: int, *, minimum: int, maximum: int) -> int:
    raw = _runtime_env.get(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, value))


def _read_float_env(name: str, default: float, *, minimum: float, maximum: float) -> float:
    raw = _runtime_env.get(name)
    if raw is None:
        return default
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, value))


def pool_bounds() -> tuple[int, int]:
    """Resolve pool bounds from env (default: min=1, max=8)."""
    min_conn = _read_int_env("FS_DB_POOL_MIN", 1, minimum=1, maximum=64)
    max_conn = _read_int_env("FS_DB_POOL_MAX", 8, minimum=1, maximum=128)
    if max_conn < min_conn:
        max_conn = min_conn
    return min_conn, max_conn


def pool_wait_timeout() -> float:
    """Resolve how long pooled borrowers wait before fallback/error (default: 1s)."""
    return _read_float_env("FS_DB_POOL_WAIT_TIMEOUT", 1.0, minimum=0.0, maximum=30.0)


def pooling_enabled() -> bool:
    raw = str(_runtime_env.get("FS_DB_POOL_ENABLED", "1")).strip().lower()
    return raw not in {"0", "false", "no", "off"}


def get_threaded_pool(dsn: str, *, minconn: int, maxconn: int) -> ThreadedConnectionPool:
    """Return a shared ThreadedConnectionPool keyed by (dsn, min, max)."""
    _require_psycopg_runtime()
    key = (dsn, int(minconn), int(maxconn))
    with _POOL_LOCK:
        cached = _POOLS.get(key)
        if cached:
            return cached
        pool = ThreadedConnectionPool(
            minconn,
            maxconn,
            dsn,
            wait_timeout=pool_wait_timeout(),
        )
        _POOLS[key] = pool
        logger.info(
            "Initialized psycopg_runtime pool dsn_hash=%s min=%d max=%d",
            hex(abs(hash(dsn)))[:10],
            minconn,
            maxconn,
        )
        return pool


@contextmanager
def pooled_connection(
    pool: ThreadedConnectionPool,
    *,
    autocommit: bool = True,
) -> Iterator[object]:
    """Borrow one connection from a ThreadedConnectionPool."""
    _require_psycopg_runtime()
    conn = pool.getconn()
    try:
        # A defensive reset protects callers from a buggy pooled borrower that
        # returned an open transaction (for example a read-only policy lookup).
        transaction_status = getattr(conn, "get_transaction_status", None)
        if callable(transaction_status) and int(transaction_status()) != _TRANSACTION_STATUS_IDLE:
            conn.rollback()
        conn.autocommit = autocommit
        yield conn
    finally:
        close_conn = bool(getattr(conn, "closed", 0))
        pool.putconn(conn, close=close_conn)


def _connection_target(db: Any) -> Any:
    """Return the concrete connection-like object held by a DB facade."""
    return getattr(db, "conn", None) or db


def _fallback_dsn(db: Any, target: Any) -> str:
    for candidate in (db, target):
        dsn = getattr(candidate, "_dsn", None) or getattr(candidate, "dsn", None)
        if str(dsn or "").strip():
            return str(dsn).strip()
    return ""


def _current_asyncio_task() -> asyncio.Task[Any] | None:
    try:
        return asyncio.current_task()
    except RuntimeError:
        return None


def _active_transaction_state(target: Any) -> _TransactionState | None:
    binding = (_BOUND_PROXY_TRANSACTIONS.get() or {}).get(id(target))
    if binding is None:
        return None
    state = binding.state
    if not any(candidate is target for candidate in state.bound_targets):
        return None
    if state.owner_thread_id != threading.get_ident():
        raise RuntimeError("transaction context cannot be reused across threads")
    if not state.active or binding.generation != state.generation:
        raise RuntimeError("transaction context cannot be reused after its owner exited")
    if state.owner_task is not _current_asyncio_task():
        raise RuntimeError("transaction context cannot be reused across asyncio tasks")
    return state


def active_transaction_connection(db: Any) -> Any | None:
    """Return the physical connection pinned in this transaction context."""
    target = _connection_target(db)
    state = _active_transaction_state(target)
    if state is not None:
        return state.connection
    if isinstance(target, PooledConnectionProxy):
        return None
    transaction_status = getattr(target, "get_transaction_status", None)
    if callable(transaction_status):
        try:
            if int(transaction_status()) != _TRANSACTION_STATUS_IDLE:
                return target
        except Exception:
            return None
    return None


def managed_transaction_connection(db: Any) -> Any | None:
    """Return the exact connection owned by the current managed root transaction."""
    target = _connection_target(db)
    frame = (_AFTER_COMMIT_CALLBACKS.get() or {}).get(id(target))
    if frame is None or not frame.open:
        return None
    state = _active_transaction_state(target)
    return state.connection if state is not None else None


def transaction_connection_is_verifiably_idle(db: Any) -> bool:
    """Return true only when the concrete connection reports an idle transaction state."""
    target = _connection_target(db)
    transaction_status = getattr(target, "get_transaction_status", None)
    if not callable(transaction_status):
        return False
    try:
        return int(transaction_status()) == _TRANSACTION_STATUS_IDLE
    except Exception:
        return False


def transaction_after_commit_supported(db: Any) -> bool:
    """Return whether the current managed transaction can observe its root commit."""
    target = _connection_target(db)
    frame = (_AFTER_COMMIT_CALLBACKS.get() or {}).get(id(target))
    return frame is not None and frame.open


def register_transaction_after_commit(db: Any, callback: Callable[[], None]) -> bool:
    """Register work for the current managed root transaction's durable commit."""
    if not callable(callback):
        raise TypeError("transaction after-commit callback must be callable")
    target = _connection_target(db)
    frame = (_AFTER_COMMIT_CALLBACKS.get() or {}).get(id(target))
    if frame is None or not frame.open:
        return False
    frame.callbacks.append(callback)
    return True


def register_transaction_after_rollback(db: Any, callback: Callable[[], None]) -> bool:
    """Register best-effort compensation for failure of the owning transaction."""
    if not callable(callback):
        raise TypeError("transaction after-rollback callback must be callable")
    state = _active_transaction_state(_connection_target(db))
    if state is None:
        return False
    state.rollback_callbacks.append(callback)
    return True


def _bind_after_commit_frame(*targets: Any):
    frame = _AfterCommitFrame()
    bindings = dict(_AFTER_COMMIT_CALLBACKS.get() or {})
    for target in targets:
        bindings[id(target)] = frame
    return _AFTER_COMMIT_CALLBACKS.set(bindings), frame


def _run_after_commit_callbacks(frame: _AfterCommitFrame) -> list[Exception]:
    errors: list[Exception] = []
    for callback in tuple(frame.callbacks):
        try:
            callback()
        except Exception as exc:
            errors.append(exc)
    frame.callbacks.clear()
    return errors


def _raise_primary_postcommit_error(errors: list[Exception]) -> None:
    if not errors:
        return
    primary = errors[0]
    for secondary in errors[1:]:
        logger.error(
            "Additional post-commit callback or cleanup failure",
            exc_info=(secondary.__class__, secondary, secondary.__traceback__),
        )
        with suppress(Exception):
            primary.add_note(
                f"Additional post-commit failure: {secondary.__class__.__name__}: {secondary}"
            )
    raise primary


def connection_participates_in_transaction(db: Any, transaction_conn: Any) -> bool:
    """Return whether ``db`` resolves to the concrete transaction connection.

    Direct connections participate by identity. Pooled facades participate only
    while this context has pinned them to the yielded connection.
    """
    target = _connection_target(db)
    return target is transaction_conn or active_transaction_connection(target) is transaction_conn


def defer_transaction_rollback(db: Any, callback: Callable[[], None]) -> bool:
    """Run ``callback`` after the active owning transaction has rolled back.

    Returns ``False`` when the caller isn't nested in a transaction owned by
    this helper, allowing it to compensate immediately after its own scope has
    already exited.
    """
    if not callable(callback):
        raise TypeError("transaction rollback callback must be callable")
    state = _active_transaction_state(_connection_target(db))
    if state is None:
        return False
    state.rollback_only = True
    if state.rollback_cause is None:
        state.rollback_cause = sys.exc_info()[1]
    state.rollback_callbacks.append(callback)
    return True


def _bind_transaction_state(target: Any, conn: Any):
    bound_targets = (target,) if target is conn else (target, conn)
    state = _TransactionState(
        connection=conn,
        bound_targets=bound_targets,
        owner_thread_id=threading.get_ident(),
        owner_task=_current_asyncio_task(),
    )
    bound = dict(_BOUND_PROXY_TRANSACTIONS.get() or {})
    for candidate in bound_targets:
        existing = bound.get(id(candidate))
        if existing is not None and existing.state is not state:
            raise RuntimeError("connection is already bound to another transaction context")
        bound[id(candidate)] = _TransactionBinding(state=state, generation=state.generation)
    return _BOUND_PROXY_TRANSACTIONS.set(bound), state


def _deactivate_transaction_state(state: _TransactionState) -> None:
    state.active = False
    state.generation += 1


def _raise_if_rollback_only(state: _TransactionState) -> None:
    if not state.rollback_only:
        return
    if state.rollback_cause is not None:
        state.rollback_cause.add_note(
            "The owning transaction was marked rollback-only by deferred compensation."
        )
        raise state.rollback_cause
    raise RuntimeError("transaction was marked rollback-only by deferred compensation")


def _run_rollback_callbacks(
    state: _TransactionState,
    *,
    original_failure: BaseException | None,
) -> None:
    callbacks = tuple(reversed(state.rollback_callbacks))
    state.rollback_callbacks.clear()
    failures: list[BaseException] = []
    for callback in callbacks:
        try:
            callback()
        except BaseException as exc:
            failures.append(exc)
            logger.exception("Deferred transaction rollback callback failed")
    if not failures:
        return
    failure_group = BaseExceptionGroup(
        "deferred transaction rollback callbacks failed",
        failures,
    )
    if original_failure is None:
        raise failure_group
    original_failure.__dict__["transaction_rollback_failures"] = failure_group
    original_failure.add_note(
        f"{len(failures)} deferred transaction rollback callbacks failed; "
        "see transaction_rollback_failures."
    )


def _connection_transaction_status(conn: Any) -> int | None:
    transaction_status = getattr(conn, "get_transaction_status", None)
    if callable(transaction_status):
        return int(transaction_status())
    info = getattr(conn, "info", None)
    status = getattr(info, "transaction_status", None)
    return int(status) if status is not None else None


def _should_use_native_transaction(conn: Any, *, force_fallback_commit: bool) -> bool:
    transaction = getattr(conn, "transaction", None)
    if not callable(transaction):
        return False
    if force_fallback_commit or bool(getattr(conn, "autocommit", False)):
        return True
    transaction_status = _connection_transaction_status(conn)
    return transaction_status is None or transaction_status != _TRANSACTION_STATUS_IDLE


@contextmanager
def _connection_transaction(conn: Any, *, force_fallback_commit: bool) -> Iterator[None]:
    transaction = getattr(conn, "transaction", None)
    if _should_use_native_transaction(conn, force_fallback_commit=force_fallback_commit):
        with transaction():
            yield
        return

    previous_autocommit = getattr(conn, "autocommit", None)
    changed_autocommit = previous_autocommit is not None and bool(previous_autocommit)
    if changed_autocommit:
        conn.autocommit = False
    try:
        yield
    except BaseException:
        with suppress(Exception):
            conn.rollback()
        raise
    else:
        if force_fallback_commit or changed_autocommit:
            try:
                conn.commit()
            except Exception:
                with suppress(Exception):
                    conn.rollback()
                raise
    finally:
        if changed_autocommit:
            with suppress(Exception):
                conn.autocommit = previous_autocommit


@contextmanager
def _nested_transaction(state: _TransactionState) -> Iterator[Any]:
    transaction = getattr(state.connection, "transaction", None)
    if _should_use_native_transaction(state.connection, force_fallback_commit=False):
        with transaction():
            yield state.connection
        return
    yield state.connection


@contextmanager
def transaction_connection(db: Any) -> Iterator[Any]:
    """Yield one connection for an atomic transaction.

    ``PooledConnectionProxy.cursor()`` intentionally uses an autocommit
    connection for each cursor. Callers that need multiple statements to be one
    decision, such as advisory locks followed by conditional writes, use this
    helper to pin one pool connection for the complete transaction.
    """
    target = _connection_target(db)
    active_state = _active_transaction_state(target)
    if active_state is not None:
        with _nested_transaction(active_state) as conn:
            yield conn
        return

    pool = target._pool if isinstance(target, PooledConnectionProxy) else None
    if pool is not None:
        conn = None
        borrowed_from_pool = False
        binding_token = None
        callback_token = None
        state = None
        frame: _AfterCommitFrame | None = None
        committed_frame: _AfterCommitFrame | None = None
        postcommit_errors: list[Exception] = []
        failed = False
        failure: BaseException | None = None
        try:
            try:
                conn = pool.getconn()
                borrowed_from_pool = True
            except Exception:
                dsn = _fallback_dsn(db, target)
                if not dsn:
                    raise
                _require_psycopg_runtime()
                conn = psycopg_runtime.connect(dsn)
            transaction_status = getattr(conn, "get_transaction_status", None)
            if (
                callable(transaction_status)
                and int(transaction_status()) != _TRANSACTION_STATUS_IDLE
            ):
                conn.rollback()
            binding_token, state = _bind_transaction_state(target, conn)
            callback_token, frame = _bind_after_commit_frame(target, conn)
            try:
                with _connection_transaction(conn, force_fallback_commit=True):
                    yield conn
                    _raise_if_rollback_only(state)
                frame.open = False
                committed_frame = frame
            except BaseException as exc:
                frame.open = False
                failed = True
                failure = exc
                raise
            finally:
                frame.open = False
                if callback_token is not None:
                    _AFTER_COMMIT_CALLBACKS.reset(callback_token)
                if state is not None:
                    _deactivate_transaction_state(state)
                if binding_token is not None:
                    _BOUND_PROXY_TRANSACTIONS.reset(binding_token)
        finally:
            try:
                if conn is not None:
                    if borrowed_from_pool:
                        try:
                            pool.putconn(conn, close=bool(getattr(conn, "closed", 0)))
                        except Exception as exc:
                            if committed_frame is None:
                                raise
                            postcommit_errors.append(exc)
                    else:
                        try:
                            conn.close()
                        except Exception as exc:
                            if committed_frame is not None:
                                postcommit_errors.append(exc)
            finally:
                if failed and state is not None:
                    _run_rollback_callbacks(state, original_failure=failure)
        if committed_frame is not None:
            postcommit_errors.extend(_run_after_commit_callbacks(committed_frame))
            _raise_primary_postcommit_error(postcommit_errors)
        return

    binding_token, state = _bind_transaction_state(target, target)
    owns_transaction = bool(getattr(target, "autocommit", False))
    callback_token = None
    frame: _AfterCommitFrame | None = None
    committed_frame: _AfterCommitFrame | None = None
    if owns_transaction:
        callback_token, frame = _bind_after_commit_frame(target)
    failed = False
    failure: BaseException | None = None
    try:
        with _connection_transaction(target, force_fallback_commit=False):
            yield target
            _raise_if_rollback_only(state)
        if frame is not None:
            frame.open = False
            committed_frame = frame
    except BaseException as exc:
        if frame is not None:
            frame.open = False
        failed = True
        failure = exc
        raise
    finally:
        if frame is not None:
            frame.open = False
        if callback_token is not None:
            _AFTER_COMMIT_CALLBACKS.reset(callback_token)
        _deactivate_transaction_state(state)
        _BOUND_PROXY_TRANSACTIONS.reset(binding_token)
        if failed:
            _run_rollback_callbacks(state, original_failure=failure)
    if committed_frame is not None:
        _raise_primary_postcommit_error(_run_after_commit_callbacks(committed_frame))


class _PooledCursorContext:
    def __init__(
        self,
        pool: ThreadedConnectionPool,
        cursor_args: tuple,
        cursor_kwargs: dict,
        *,
        dsn: str = "",
    ):
        self._pool = pool
        self._cursor_args = cursor_args
        self._cursor_kwargs = cursor_kwargs
        self._dsn = str(dsn or "").strip()
        self._borrowed_from_pool = False
        self._conn: Any | None = None
        self._cursor: Any | None = None

    def __enter__(self):
        try:
            self._conn = self._pool.getconn()
            self._borrowed_from_pool = True
        except Exception:
            if not self._dsn:
                raise
            self._conn = psycopg_runtime.connect(self._dsn)
            self._borrowed_from_pool = False
        self._conn.autocommit = True
        self._cursor = self._conn.cursor(*self._cursor_args, **self._cursor_kwargs)
        return self._cursor.__enter__()

    def __exit__(self, exc_type, exc, tb):
        should_suppress = False
        close_conn = bool(getattr(self._conn, "closed", 0)) or exc_type is not None
        try:
            if self._cursor is not None:
                should_suppress = bool(self._cursor.__exit__(exc_type, exc, tb))
            if not close_conn and self._conn is not None:
                try:
                    txn_status = int(self._conn.get_transaction_status())
                except Exception:
                    close_conn = True
                else:
                    if txn_status != _TRANSACTION_STATUS_IDLE:
                        close_conn = True
        finally:
            if self._conn is not None:
                if self._borrowed_from_pool:
                    self._pool.putconn(self._conn, close=close_conn)
                else:
                    with suppress(Exception):
                        self._conn.close()
                self._conn = None
                self._borrowed_from_pool = False
        return should_suppress


class PooledConnectionProxy:
    """Connection-like adapter exposing `.cursor()` for pooled usage."""

    def __init__(self, pool: ThreadedConnectionPool, *, dsn: str = ""):
        self._pool = pool
        self._dsn = str(dsn or "").strip()

    def cursor(self, *args, **kwargs):
        transaction_conn = active_transaction_connection(self)
        if transaction_conn is not None:
            return transaction_conn.cursor(*args, **kwargs)
        return _PooledCursorContext(self._pool, args, kwargs, dsn=self._dsn)

    def commit(self) -> None:
        # Queries run with autocommit=True, so explicit commit is a no-op.
        return None

    def rollback(self) -> None:
        # Queries run with autocommit=True, so explicit rollback is a no-op.
        return None

    def get_transaction_status(self) -> int:
        # Proxy cursors always run with autocommit=True.
        return _TRANSACTION_STATUS_IDLE

    @property
    def autocommit(self) -> bool:
        return True

    @autocommit.setter
    def autocommit(self, _value: bool) -> None:
        # Keep pooled proxy behavior simple and stable for call sites toggling this flag.
        return None

    def close(self) -> None:
        # Do not close shared pools from call sites that expect a raw connection.
        return None

    @property
    def closed(self) -> int:
        # Match psycopg_runtime semantics: 0 means open.
        return 0
