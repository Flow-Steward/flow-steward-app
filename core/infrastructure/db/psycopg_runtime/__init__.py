"""Canonical psycopg3 runtime surface used by DB adapters and scripts."""

from __future__ import annotations

from psycopg import OperationalError

from . import errorcodes, extensions, extras, pool, sql
from ._connection import Binary, connect
from .extras import RealDictCursor
from .pool import PoolError, ThreadedConnectionPool

__all__ = [
    "Binary",
    "OperationalError",
    "PoolError",
    "RealDictCursor",
    "ThreadedConnectionPool",
    "connect",
    "errorcodes",
    "extensions",
    "extras",
    "pool",
    "sql",
]
