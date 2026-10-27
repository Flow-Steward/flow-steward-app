"""Simple threaded connection pool compatible with older call sites."""

from __future__ import annotations

import os
import threading
import time
from contextlib import suppress
from typing import Any

from ._connection import connect


class PoolError(Exception):
    """Raised when the pool cannot provide a connection."""


class ThreadedConnectionPool:
    """A tiny thread-safe connection pool with psycopg2-like methods."""

    def __init__(self, minconn: int, maxconn: int, dsn: str, *, wait_timeout: float = 0.5):
        self._minconn = int(minconn)
        self._maxconn = int(maxconn)
        self._dsn = str(dsn)
        self._wait_timeout = max(0.0, float(wait_timeout))
        self._owner_pid = os.getpid()
        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)
        self._free: list[Any] = []
        self._borrowed: set[int] = set()
        self._inherited_connections: list[Any] = []
        if self._maxconn < self._minconn:
            raise ValueError("maxconn must be >= minconn")
        for _ in range(self._minconn):
            self._free.append(connect(self._dsn))

    def _total(self) -> int:
        return len(self._free) + len(self._borrowed)

    def _ensure_current_process(self) -> None:
        """Replace connections and synchronization inherited through ``fork``.

        PostgreSQL connections are process-owned.  Reusing a parent's socket in
        a forked agent can corrupt the protocol stream or wait forever for a
        response consumed by the parent.  Thread locks are unsafe to inherit as
        well because their owning thread does not exist in the child.
        """
        current_pid = os.getpid()
        if current_pid == self._owner_pid:
            return

        # Keep the copied connection objects alive until the child exits. A
        # normal psycopg ``close()`` may send PostgreSQL's terminate message on
        # the shared socket and invalidate the parent's still-live session.
        self._inherited_connections.extend(self._free)
        self._owner_pid = current_pid
        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)
        self._free = []
        self._borrowed = set()
        for _ in range(self._minconn):
            self._free.append(connect(self._dsn))

    def getconn(self):
        self._ensure_current_process()
        deadline = time.monotonic() + self._wait_timeout
        with self._cond:
            while True:
                if self._free:
                    conn = self._free.pop()
                    self._borrowed.add(id(conn))
                    return conn
                if self._total() < self._maxconn:
                    conn = connect(self._dsn)
                    self._borrowed.add(id(conn))
                    return conn
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise PoolError("connection pool exhausted")
                self._cond.wait(timeout=remaining)

    def putconn(self, conn, close: bool = False):
        if os.getpid() != self._owner_pid:
            self._ensure_current_process()
            with suppress(Exception):
                conn.close()
            return
        with self._cond:
            self._borrowed.discard(id(conn))
            if close:
                with suppress(Exception):
                    conn.close()
                self._cond.notify()
                return
            if not bool(getattr(conn, "closed", 0)):
                self._free.append(conn)
            self._cond.notify()

    def closeall(self) -> None:
        self._ensure_current_process()
        with self._cond:
            all_connections = list(self._free)
            self._free.clear()
            self._borrowed.clear()
            self._cond.notify_all()
        for conn in all_connections:
            with suppress(Exception):
                conn.close()
