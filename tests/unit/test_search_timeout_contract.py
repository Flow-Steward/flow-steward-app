from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from core.application.search.search_postgres_common import _execute_rows
from core.application.search.search_timeout import (
    SearchQueryTimeoutError,
    search_timeout_budget,
)


class _QueryCanceledError(RuntimeError):
    sqlstate = "57014"


class _Cursor:
    def __init__(self, *, cancel_query: bool = False) -> None:
        self.cancel_query = cancel_query
        self.calls: list[tuple[str, tuple[object, ...]]] = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, sql: str, params=()) -> None:
        self.calls.append((sql, tuple(params)))
        if self.cancel_query and sql.startswith("SELECT"):
            raise _QueryCanceledError("canceling statement due to statement timeout")

    def fetchall(self):
        return [{"id": "row-1"}]


def test_execute_rows_applies_and_resets_postgres_statement_timeout() -> None:
    cursor = _Cursor()
    conn = MagicMock()
    conn.cursor.return_value = cursor

    with search_timeout_budget(10.0):
        rows = _execute_rows(conn, "SELECT 1", ())

    assert rows == [{"id": "row-1"}]
    assert cursor.calls[0][0].startswith("SET statement_timeout = ")
    assert cursor.calls[1] == ("SELECT 1", ())
    assert cursor.calls[2] == ("SET statement_timeout = 0", ())


def test_execute_rows_maps_postgres_cancellation_to_search_timeout() -> None:
    cursor = _Cursor(cancel_query=True)
    conn = MagicMock()
    conn.cursor.return_value = cursor

    with pytest.raises(SearchQueryTimeoutError), search_timeout_budget(10.0):
        _execute_rows(conn, "SELECT pg_sleep(30)", ())

    assert cursor.calls[-1] == ("SET statement_timeout = 0", ())
