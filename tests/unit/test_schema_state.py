from __future__ import annotations

from pathlib import Path

import pytest

from core.infrastructure.db import schema_state


def _write_state_layout(root: Path) -> None:
    schema_dir = root / "db" / "schema"
    seeds_dir = root / "db" / "seeds"
    schema_dir.mkdir(parents=True, exist_ok=True)
    seeds_dir.mkdir(parents=True, exist_ok=True)
    (schema_dir / "001_schema_part_01.sql").write_text("-- schema a", encoding="utf-8")
    (seeds_dir / "lookup_seed.json").write_text('{"lookup":1}', encoding="utf-8")
    (seeds_dir / "registry_seed.json").write_text('{"registry":1}', encoding="utf-8")


def test_monitoring_part_contributes_to_canonical_fingerprint(tmp_path: Path) -> None:
    _write_state_layout(tmp_path)
    before = schema_state.canonical_schema_fingerprint(tmp_path)
    source = Path(__file__).resolve().parents[2] / "db/schema/001_schema_part_12.sql"
    (tmp_path / "db/schema/001_schema_part_12.sql").write_text(source.read_text(), encoding="utf-8")
    assert schema_state.canonical_schema_fingerprint(tmp_path) != before
    assert len(schema_state.canonical_schema_files(tmp_path)) == 2


def test_schema_fingerprint_ignores_seed_changes(tmp_path: Path) -> None:
    _write_state_layout(tmp_path)

    schema_before = schema_state.canonical_schema_fingerprint(tmp_path)
    seed_before = schema_state.canonical_seed_fingerprint(tmp_path)
    (tmp_path / "db" / "seeds" / "registry_seed.json").write_text(
        '{"registry":2}',
        encoding="utf-8",
    )

    assert schema_state.canonical_schema_fingerprint(tmp_path) == schema_before
    assert schema_state.canonical_seed_fingerprint(tmp_path) != seed_before


def test_reconcile_uses_a_fresh_connection_per_schema_file(tmp_path: Path) -> None:
    schema_dir = tmp_path / "db" / "schema"
    schema_dir.mkdir(parents=True)
    (schema_dir / "001.sql").write_text(
        "SET search_path = ''; SELECT 1;",
        encoding="utf-8",
    )
    (schema_dir / "002.sql").write_text(
        "SELECT 'public-visible';",
        encoding="utf-8",
    )
    connections = []

    class _Cursor:
        def __init__(self, executed) -> None:
            self.executed = executed

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def execute(self, sql) -> None:
            self.executed.append(str(sql))

    class _Connection:
        def __init__(self) -> None:
            self.autocommit = False
            self.executed: list[str] = []
            self.closed = False

        def cursor(self):
            return _Cursor(self.executed)

        def close(self) -> None:
            self.closed = True

    def connect():
        conn = _Connection()
        connections.append(conn)
        return conn

    summary = schema_state.reconcile_canonical_schema(
        connect=connect,
        root=tmp_path,
    )

    assert summary["files"] == 2
    assert len(connections) == 2
    assert all(conn.closed for conn in connections)


def test_reconcile_raises_on_unexpected_error(tmp_path: Path) -> None:
    schema_dir = tmp_path / "db" / "schema"
    schema_dir.mkdir(parents=True)
    (schema_dir / "001.sql").write_text("SELECT 1;", encoding="utf-8")
    (schema_dir / "002.sql").write_text("BAD STMT;", encoding="utf-8")

    class _Error(RuntimeError):
        sqlstate = "42601"

    class _Cursor:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def execute(self, sql) -> None:
            if "BAD STMT" in str(sql):
                raise _Error("syntax error")

    class _Connection:
        autocommit = False

        def cursor(self):
            return _Cursor()

        def close(self) -> None:
            return None

    with pytest.raises(schema_state.SchemaReconcileError, match=r"002\.sql"):
        schema_state.reconcile_canonical_schema(
            connect=_Connection,
            root=tmp_path,
        )


def test_inspect_canonical_schema_state_requires_upgrade_when_state_missing(tmp_path: Path) -> None:
    _write_state_layout(tmp_path)

    class _Conn:
        def cursor(self):
            return self

        def execute(self, *_args, **_kwargs) -> None:
            return None

        def fetchone(self):
            return None

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    status = schema_state.inspect_canonical_schema_state(_Conn(), root=tmp_path)

    assert status["needs_upgrade"] is True
    assert status["current"] is None
