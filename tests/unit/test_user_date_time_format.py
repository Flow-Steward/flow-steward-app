"""A user's date pattern and clock: validated on save, defaulted when unset or unknown."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from core.application.settings.date_time_format import (
    DATE_FORMATS,
    DEFAULT_DATE_FORMAT,
    DEFAULT_TIME_FORMAT,
    normalize_date_format,
    normalize_time_format,
    stored_date_format,
    stored_time_format,
)
from core.entrypoints.api.auth_users_preferences import (
    save_date_time_format,
    session_date_time_format,
)


class _Cursor:
    def __init__(self, row: Any = None) -> None:
        self.row = row
        self.executed: list[tuple[str, tuple[Any, ...]]] = []

    def __enter__(self) -> _Cursor:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def execute(self, query: str, params: tuple[Any, ...]) -> None:
        self.executed.append((query, params))

    def fetchone(self) -> Any:
        return self.row


class _Conn:
    def __init__(self, cursor: _Cursor) -> None:
        self._cursor = cursor
        self.commits = 0

    def cursor(self, **_kwargs: Any) -> _Cursor:
        return self._cursor

    def commit(self) -> None:
        self.commits += 1


class _Request:
    def __init__(self, body: Any) -> None:
        self._body = body

    async def json(self) -> Any:
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


def _save(body: Any, bl: Any) -> Any:
    return asyncio.run(
        save_date_time_format(
            _Request(body),  # type: ignore[arg-type]
            "user-1",
            get_logger_only=lambda: bl,
            err_invalid_json_body="invalid-json",
            err_database_not_configured="db-missing",
        )
    )


def test_every_offered_date_pattern_is_accepted_and_others_are_refused() -> None:
    for pattern in DATE_FORMATS:
        assert normalize_date_format(pattern) == pattern
    assert normalize_date_format("") == ""
    assert normalize_time_format("12H") == "12h"
    with pytest.raises(ValueError, match="date format"):
        normalize_date_format("%Y-%m-%d")
    with pytest.raises(ValueError, match="time format"):
        normalize_time_format("am/pm")


def test_an_unset_or_unknown_stored_value_reads_as_the_default() -> None:
    assert stored_date_format("") == DEFAULT_DATE_FORMAT
    assert stored_date_format("Europe/Vilnius") == DEFAULT_DATE_FORMAT
    assert stored_date_format("MM/DD/YYYY") == "MM/DD/YYYY"
    assert stored_time_format(None) == DEFAULT_TIME_FORMAT
    assert stored_time_format("12h") == "12h"


def test_the_session_reads_the_users_stored_choice() -> None:
    cursor = _Cursor(row=("MM/DD/YYYY", "12h"))
    bl = SimpleNamespace(conn=_Conn(cursor))

    assert session_date_time_format({"id": "user-1"}, lambda: bl) == ("MM/DD/YYYY", "12h")
    assert cursor.executed[0][1] == ("user-1",)
    assert session_date_time_format({"id": ""}, lambda: bl) == (
        DEFAULT_DATE_FORMAT,
        DEFAULT_TIME_FORMAT,
    )


def test_a_database_without_the_columns_still_gives_the_session_the_default() -> None:
    class _Broken:
        @property
        def conn(self) -> Any:
            raise RuntimeError('column "date_format" does not exist')

    assert session_date_time_format({"id": "user-1"}, _Broken) == (
        DEFAULT_DATE_FORMAT,
        DEFAULT_TIME_FORMAT,
    )


def test_saving_stores_the_choice_and_an_empty_one_as_null() -> None:
    cursor = _Cursor(row=("DD.MM.YYYY", "12h"))
    conn = _Conn(cursor)
    bl = SimpleNamespace(conn=conn)

    saved = _save({"date_format": "DD.MM.YYYY", "time_format": "12h"}, bl)
    assert saved == {"status": "ok", "date_format": "DD.MM.YYYY", "time_format": "12h"}
    assert cursor.executed[-2] == (
        "UPDATE users SET date_format = %s, time_format = %s WHERE id = %s",
        ("DD.MM.YYYY", "12h", "user-1"),
    )
    assert conn.commits == 1

    cursor.row = (None, None)
    reset = _save({"date_format": "", "time_format": ""}, bl)
    assert reset == {
        "status": "ok",
        "date_format": DEFAULT_DATE_FORMAT,
        "time_format": DEFAULT_TIME_FORMAT,
    }
    assert cursor.executed[-2][1] == (None, None, "user-1")


def test_saving_one_choice_keeps_the_other() -> None:
    cursor = _Cursor(row=("MM/DD/YYYY", "12h"))
    bl = SimpleNamespace(conn=_Conn(cursor))

    saved = _save({"time_format": "12h"}, bl)

    assert cursor.executed[-2] == (
        "UPDATE users SET time_format = %s WHERE id = %s",
        ("12h", "user-1"),
    )
    assert saved["date_format"] == "MM/DD/YYYY"
    before = len(cursor.executed)
    _save({}, bl)
    assert len(cursor.executed) == before + 1  # nothing to store: only the read


def test_saving_refuses_an_unknown_pattern_bad_json_and_a_missing_database() -> None:
    bl = SimpleNamespace(conn=_Conn(_Cursor()))

    unknown = _save({"date_format": "YYYY/DD/MM"}, bl)
    assert unknown.status_code == 422
    assert _save(ValueError("not json"), bl).status_code == 400
    assert _save({"time_format": "24h"}, None).status_code == 503
