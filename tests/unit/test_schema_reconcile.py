"""Tests for idempotent canonical-schema reconcile (schema drift self-heal)."""

from __future__ import annotations

import pytest

from core.infrastructure.db import schema_state
from core.infrastructure.db.schema_state import (
    _ALREADY_EXISTS_SQLSTATES,
    reconcile_canonical_schema,
    split_sql_statements,
)


class TestSplitSqlStatements:
    def test_splits_simple_statements(self):
        out = split_sql_statements("CREATE TABLE a(x int); CREATE TABLE b(y int);")
        assert len(out) == 2
        assert out[0].startswith("CREATE TABLE a")
        assert out[1].startswith("CREATE TABLE b")

    def test_keeps_dollar_quoted_function_body_whole(self):
        sql = (
            "CREATE FUNCTION f() RETURNS int AS $$ BEGIN RETURN 1; END; $$ LANGUAGE plpgsql;"
            " SELECT 2;"
        )
        out = split_sql_statements(sql)
        assert len(out) == 2
        assert "BEGIN RETURN 1; END;" in out[0]
        assert out[1] == "SELECT 2"

    def test_keeps_tagged_dollar_quote_whole(self):
        sql = "CREATE FUNCTION g() RETURNS text AS $body$ SELECT 'a;b'; $body$ LANGUAGE sql; SELECT 1;"
        out = split_sql_statements(sql)
        assert len(out) == 2
        assert "$body$ SELECT 'a;b'; $body$" in out[0]

    def test_ignores_semicolons_in_strings_and_comments(self):
        sql = "INSERT INTO x VALUES ('a;b'); -- trailing; comment\nSELECT 2;"
        out = split_sql_statements(sql)
        assert len(out) == 2
        assert out[0] == "INSERT INTO x VALUES ('a;b')"

    def test_ignores_semicolon_in_block_comment(self):
        sql = "SELECT 1 /* a; b */ ; SELECT 2;"
        out = split_sql_statements(sql)
        assert len(out) == 2

    def test_drops_empty_statements(self):
        assert split_sql_statements("  ;\n;  ;") == []

    def test_canonical_schema_splits_with_balanced_dollar_quotes(self):
        stmts = split_sql_statements(schema_state.read_canonical_schema_sql())
        assert stmts, "canonical schema should split into statements"
        # No statement may contain an unbalanced $$ (would mean a body was split).
        assert all(s.count("$$") % 2 == 0 for s in stmts)
        # No statement should merge two CREATE TABLEs (over-merge sign).
        assert all(s.upper().count("CREATE TABLE ") <= 1 for s in stmts)


class _FakeCursor:
    def __init__(self, conn):
        self._conn = conn

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, stmt):
        self._conn.executed.append(stmt)
        err = self._conn.errors.get(stmt.strip())
        if err is not None:
            raise err


class _FakeError(Exception):
    def __init__(self, sqlstate):
        super().__init__(f"err {sqlstate}")
        self.sqlstate = sqlstate


class _FakeConn:
    def __init__(self, errors=None):
        self.autocommit = False
        self.executed: list[str] = []
        self.errors = errors or {}

    def cursor(self):
        return _FakeCursor(self)

    def close(self):
        return None


class TestReconcileLogic:
    def test_tolerated_sqlstates_cover_known_already_exists(self):
        for state in ("42P07", "42710", "42P16", "42701", "42723", "42P06"):
            assert state in _ALREADY_EXISTS_SQLSTATES

    def test_skips_already_exists_and_raises_on_real_failures(
        self,
        monkeypatch,
        tmp_path,
    ):
        sql = "CREATE TABLE a(x int); ALTER TABLE a ADD CONSTRAINT pk PRIMARY KEY (x); BAD STMT;"
        schema_dir = tmp_path / "db/schema"
        schema_dir.mkdir(parents=True)
        (schema_dir / "001.sql").write_text(sql, encoding="utf-8")
        conn = _FakeConn(
            errors={
                # table already exists -> tolerated
                "CREATE TABLE a(x int)": _FakeError("42P07"),
                # primary key already exists -> tolerated
                "ALTER TABLE a ADD CONSTRAINT pk PRIMARY KEY (x)": _FakeError("42P16"),
                # genuine error -> reported in failed
                "BAD STMT": _FakeError("42601"),
            }
        )
        with pytest.raises(schema_state.SchemaReconcileError, match="42601"):
            reconcile_canonical_schema(
                connect=lambda: conn,
                root=tmp_path,
            )
        assert len(conn.executed) == 3
        assert conn.autocommit is False

    def test_applies_missing_objects(self, tmp_path):
        sql = "CREATE TABLE only_new(x int);"
        schema_dir = tmp_path / "db/schema"
        schema_dir.mkdir(parents=True)
        (schema_dir / "001.sql").write_text(sql, encoding="utf-8")
        conn = _FakeConn()
        summary = reconcile_canonical_schema(
            connect=lambda: conn,
            root=tmp_path,
        )
        assert summary["applied"] == 1
        assert summary["skipped"] == 0
        assert summary["failed"] == []
        assert conn.executed == ["CREATE TABLE only_new(x int)"]


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
