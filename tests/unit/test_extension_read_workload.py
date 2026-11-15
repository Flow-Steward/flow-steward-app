"""Installed-extension reads must do work proportional to the requested data."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager

import pytest

from core.infrastructure.db.logger_extensions_accounts_query_mixin import (
    PlatformLoggerExtensionsAccountsQueryMixin,
)


class _Cursor:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self._cursor = connection.cursor()

    def execute(self, sql: str, params: tuple = ()) -> None:
        self._cursor.execute(sql.replace("%s", "?"), params)

    def fetchall(self) -> list[dict]:
        return [dict(row) for row in self._cursor.fetchall()]

    def fetchone(self) -> dict | None:
        row = self._cursor.fetchone()
        return dict(row) if row is not None else None


class _Connection:
    def __init__(self) -> None:
        self.db = sqlite3.connect(":memory:")
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            CREATE TABLE extension_bundles (
                id TEXT, extension_key TEXT, kind TEXT, latest_version TEXT, account_id TEXT
            );
            CREATE TABLE extension_versions (bundle_id TEXT, version TEXT);
            CREATE TABLE enabled_extensions (bundle_id TEXT, enabled BOOLEAN, config_jsonb TEXT);
            INSERT INTO extension_bundles VALUES
                ('a', 'acme.alpha', 'service_extension', '1.0.0', 'account-a'),
                ('b', 'acme.beta', 'service_extension', '1.0.0', 'account-a'),
                ('c', 'acme.alpha', 'service_extension', '2.0.0', 'account-b');
        """)

    @contextmanager
    def cursor(self, **_kwargs):
        yield _Cursor(self.db)


class _Lookup(PlatformLoggerExtensionsAccountsQueryMixin):
    """Real SQL joins/filters with small columns instead of unrelated JSON fixtures."""

    def __init__(self) -> None:
        self.conn = _Connection()
        self.normalized: list[str] = []

    @staticmethod
    def _table_exists(_table: str) -> bool:
        return True

    @staticmethod
    def _column_exists(_table: str, _column: str) -> bool:
        return True

    @staticmethod
    def _extension_base_select_columns() -> list[str]:
        return ["eb.extension_key", "eb.kind", "ee.enabled"]

    @staticmethod
    def _extension_version_select_columns() -> list[str]:
        return []

    def _normalize_extension_row(self, row: dict) -> dict:
        self.normalized.append(row["extension_key"])
        return row


@pytest.fixture
def lookup():
    store = _Lookup()
    yield store
    store.conn.db.close()


def test_single_extension_lookup_does_not_materialize_other_packages(lookup) -> None:
    row = lookup.get_extension(" acme.beta ", account_id="account-a")

    assert row is not None
    assert row["extension_key"] == "acme.beta"
    assert lookup.normalized == ["acme.beta"]


def test_single_extension_lookup_is_account_scoped(lookup) -> None:
    row = lookup.get_extension("acme.alpha", account_id=" account-b ")

    assert row is not None
    assert row["account_id"] == "account-b"
    assert len(lookup.normalized) == 1
    assert lookup.get_extension("acme.beta", account_id="account-b") is None
    assert len(lookup.normalized) == 1


def test_single_extension_lookup_binds_untrusted_key(lookup) -> None:
    assert lookup.get_extension("acme.alpha' OR 1=1 --", account_id="account-a") is None
    assert lookup.normalized == []


def test_single_extension_lookup_retains_unscoped_and_disabled_reads(lookup) -> None:
    row = lookup.get_extension("acme.beta")
    assert row is not None
    assert row["account_id"] == "account-a"
    assert not row["enabled"]
    assert lookup.normalized == ["acme.beta"]


def test_blank_lookup_does_no_row_work(lookup) -> None:
    assert lookup.get_extension(" ") is None
    assert lookup.normalized == []
