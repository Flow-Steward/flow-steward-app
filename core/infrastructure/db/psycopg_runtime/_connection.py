"""Connection adapters that normalize cursor kwargs for psycopg3 APIs."""

from __future__ import annotations

from typing import Any

import psycopg
from psycopg.rows import dict_row

from .extras import RealDictCursor


class ConnectionAdapter:
    """Wrap psycopg3 connections with the repository cursor signature."""

    def __init__(self, conn: Any):
        self._conn = conn

    def __enter__(self) -> ConnectionAdapter:
        self._conn.__enter__()
        return self

    def __exit__(self, exc_type, exc, tb):
        return self._conn.__exit__(exc_type, exc, tb)

    def cursor(self, *args, **kwargs):
        cursor_factory = kwargs.pop("cursor_factory", None)
        if cursor_factory is RealDictCursor:
            kwargs.setdefault("row_factory", dict_row)
        return self._conn.cursor(*args, **kwargs)

    @property
    def autocommit(self) -> bool:
        return bool(getattr(self._conn, "autocommit", False))

    @autocommit.setter
    def autocommit(self, value: bool) -> None:
        self._conn.autocommit = bool(value)

    @property
    def read_only(self) -> bool | None:
        return getattr(self._conn, "read_only", None)

    @read_only.setter
    def read_only(self, value: bool | None) -> None:
        self._conn.read_only = value

    @property
    def isolation_level(self) -> Any:
        return getattr(self._conn, "isolation_level", None)

    @isolation_level.setter
    def isolation_level(self, value: Any) -> None:
        self._conn.isolation_level = value

    def get_transaction_status(self) -> int:
        return int(self._conn.info.transaction_status)

    @property
    def closed(self) -> int:
        return 1 if bool(getattr(self._conn, "closed", False)) else 0

    def __getattr__(self, name: str) -> Any:
        return getattr(self._conn, name)


def connect(*args, **kwargs) -> ConnectionAdapter:
    kwargs.setdefault("prepare_threshold", None)
    return ConnectionAdapter(psycopg.connect(*args, **kwargs))


def binary(value: bytes | bytearray | memoryview) -> Any:
    return psycopg.Binary(value)


Binary = binary
