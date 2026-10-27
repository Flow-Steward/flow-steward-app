from __future__ import annotations

import logging
from collections.abc import Sequence
from contextlib import contextmanager
from typing import Any

from core.application.memory.models import MemoryRepository
from core.infrastructure.db import action_log_store as action_log_store
from core.infrastructure.db import psycopg_runtime as psycopg_runtime
from core.infrastructure.db.db_pool import (
    PooledConnectionProxy,
    active_transaction_connection,
    register_transaction_after_commit,
    transaction_connection,
)
from core.infrastructure.db.memory_store_common import (
    _SEARCH_VECTOR_SQL as _SEARCH_VECTOR_SQL,
)
from core.infrastructure.db.memory_store_common import (
    VALUE_AND_SQL as VALUE_AND_SQL,
)
from core.infrastructure.db.memory_store_common import (
    _db_account_id as _db_account_id,
)
from core.infrastructure.db.memory_store_common import (
    _db_scope_id as _db_scope_id,
)
from core.infrastructure.db.memory_store_common import (
    _db_tenant_id as _db_tenant_id,
)
from core.infrastructure.db.memory_store_common import (
    _evidence_refs as _evidence_refs,
)
from core.infrastructure.db.memory_store_common import (
    _json_dumps as _json_dumps,
)
from core.infrastructure.db.memory_store_common import (
    _json_loads as _json_loads,
)
from core.infrastructure.db.memory_store_common import (
    _maybe_float as _maybe_float,
)
from core.infrastructure.db.memory_store_common import (
    _row_to_alias as _row_to_alias,
)
from core.infrastructure.db.memory_store_common import (
    _row_to_edge as _row_to_edge,
)
from core.infrastructure.db.memory_store_common import (
    _row_to_entity as _row_to_entity,
)
from core.infrastructure.db.memory_store_common import (
    _row_to_record as _row_to_record,
)
from core.infrastructure.db.memory_store_common import (
    _scope_id as _scope_id,
)
from core.infrastructure.db.memory_store_graph import MemoryGraphStoreMixin
from core.infrastructure.db.memory_store_jobs import MemoryJobStoreMixin
from core.infrastructure.db.memory_store_records import MemoryRecordStoreMixin

logger = logging.getLogger(__name__)


class PostgresMemoryRepository(
    MemoryRecordStoreMixin,
    MemoryGraphStoreMixin,
    MemoryJobStoreMixin,
    MemoryRepository,
):
    def __init__(self, *, logger_instance: Any) -> None:
        self._logger = logger_instance
        conn = getattr(logger_instance, "conn", None)
        if conn is None:
            raise RuntimeError("Memory repository requires a live Flow Steward logger connection")
        self._conn: Any = conn
        self._dsn = str(getattr(logger_instance, "dsn", "") or "").strip()
        self._auto_commit = bool(getattr(logger_instance, "auto_commit", True))
        self._transaction_depth = 0
        self._transaction_conn: Any | None = None
        self._restore_autocommit: bool | None = None
        self._post_commit_hooks: list[Any] = []

    def _current_conn(self) -> Any:
        return self._transaction_conn or self._conn

    def _can_reuse_existing_connection_for_transactions(self) -> bool:
        return (not self._auto_commit) and (not isinstance(self._conn, PooledConnectionProxy))

    def add_post_commit_hook(self, callback: Any) -> None:
        if self._transaction_depth <= 0:
            callback()
            return
        self._post_commit_hooks.append(callback)

    @contextmanager
    def transaction(self):
        managed_conn = active_transaction_connection(self._conn)
        if self._transaction_depth == 0 and managed_conn is not None:
            self._transaction_depth = 1
            self._transaction_conn = managed_conn
            self._post_commit_hooks.clear()
            try:
                with transaction_connection(self._conn):
                    yield
            except Exception:
                self._post_commit_hooks.clear()
                raise
            else:
                hooks = list(self._post_commit_hooks)
                self._post_commit_hooks.clear()
                for hook in hooks:
                    if not register_transaction_after_commit(self._conn, hook):
                        raise RuntimeError(
                            "Memory post-commit hook requires the owning managed transaction"
                        )
            finally:
                self._transaction_depth = 0
                self._transaction_conn = None
            return
        opened_here = False
        self._transaction_depth += 1
        if self._transaction_depth == 1:
            if self._can_reuse_existing_connection_for_transactions():
                self._transaction_conn = self._conn
                try:
                    self._restore_autocommit = bool(self._transaction_conn.autocommit)
                    self._transaction_conn.autocommit = False
                except Exception:
                    self._restore_autocommit = None
            else:
                if not self._dsn:
                    self._transaction_depth -= 1
                    raise RuntimeError(
                        "Memory transactions require a dedicated DSN-backed connection"
                    )
                self._transaction_conn = psycopg_runtime.connect(self._dsn)
                self._transaction_conn.autocommit = False
                opened_here = True
        try:
            yield
        except Exception:
            self._transaction_depth -= 1
            if self._transaction_depth == 0:
                try:
                    self._current_conn().rollback()
                except Exception as exc:
                    logger.debug("memory transaction rollback failed: %s", exc)
                finally:
                    if opened_here and self._transaction_conn is not None:
                        try:
                            self._transaction_conn.close()
                        except Exception as exc:
                            logger.debug("memory transaction connection close failed: %s", exc)
                    elif (
                        self._restore_autocommit is not None and self._transaction_conn is not None
                    ):
                        try:
                            self._transaction_conn.autocommit = self._restore_autocommit
                        except Exception as exc:
                            logger.debug("memory transaction autocommit restore failed: %s", exc)
                    self._transaction_conn = None
                    self._restore_autocommit = None
                    self._post_commit_hooks.clear()
            raise
        else:
            self._transaction_depth -= 1
            if self._transaction_depth == 0:
                hooks = list(self._post_commit_hooks)
                self._post_commit_hooks.clear()
                try:
                    self._current_conn().commit()
                finally:
                    if opened_here and self._transaction_conn is not None:
                        try:
                            self._transaction_conn.close()
                        except Exception as exc:
                            logger.debug("memory transaction connection close failed: %s", exc)
                    elif (
                        self._restore_autocommit is not None and self._transaction_conn is not None
                    ):
                        try:
                            self._transaction_conn.autocommit = self._restore_autocommit
                        except Exception as exc:
                            logger.debug("memory transaction autocommit restore failed: %s", exc)
                    self._transaction_conn = None
                    self._restore_autocommit = None
                for hook in hooks:
                    hook()

    def _maybe_commit(self) -> None:
        if self._auto_commit and self._transaction_depth == 0:
            self._conn.commit()

    def _release_read_transaction(self) -> None:
        """End the implicit transaction a read opens on a non-autocommit connection.

        A plain ``SELECT`` still begins a transaction, and the driver holds it
        open until someone commits or rolls back. Left open, it pins a snapshot
        and keeps whatever row locks the statement acquired — which is how a
        single job-detail read sat ``idle in transaction`` for 28 minutes and
        blocked every subsequent ``SELECT ... FOR UPDATE`` on that job. That
        stalled the worker's terminalization and every cancel attempt
        platform-wide (RD-001 / RD-002).

        Rollback rather than commit: a read has nothing to persist, and rollback
        cannot accidentally make a half-finished caller transaction durable.
        """
        if self._transaction_depth > 0:
            # This repository's own transaction() block owns the boundary.
            return
        conn = self._current_conn()
        # A managed transaction pinned by `transaction_connection()` belongs to
        # whoever opened it — typically several statements that have to commit
        # or roll back as one decision. Rolling it back from a read would
        # silently discard the caller's work, so leave it strictly alone. Its
        # own context manager closes it.
        try:
            from core.infrastructure.db.db_pool import active_transaction_connection

            if active_transaction_connection(conn) is not None:
                return
        except Exception:  # pragma: no cover - pool import is always available in runtime
            return
        # Nothing else owns a transaction here, so any that the read opened is
        # ours to close. A PooledConnectionProxy reports `autocommit=True` and
        # no-ops `rollback()`, and its cursors already clean up after
        # themselves, so this only bites on a bare non-autocommit connection.
        try:
            if getattr(conn, "autocommit", False):
                return
            conn.rollback()
        except Exception as exc:
            logger.debug("memory read transaction release failed: %s", exc)

    def _fetchone(self, sql: str, params: Sequence[Any]) -> dict[str, Any] | None:
        try:
            with self._current_conn().cursor(
                cursor_factory=psycopg_runtime.extras.RealDictCursor
            ) as cur:
                cur.execute(sql, tuple(params))
                row = cur.fetchone()
            return dict(row) if row else None
        finally:
            self._release_read_transaction()

    def _fetchall(self, sql: str, params: Sequence[Any]) -> list[dict[str, Any]]:
        try:
            with self._current_conn().cursor(
                cursor_factory=psycopg_runtime.extras.RealDictCursor
            ) as cur:
                cur.execute(sql, tuple(params))
                return [dict(row) for row in cur.fetchall()]
        finally:
            self._release_read_transaction()
