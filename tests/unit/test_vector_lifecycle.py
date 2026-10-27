from __future__ import annotations

from typing import Any

import pytest

from core.application.tooling.db import vector_lifecycle


class _Cursor:
    def __init__(self, batches: list[list[tuple[Any, ...]]], statements: list[str]) -> None:
        self._batches = batches
        self._current: list[tuple[Any, ...]] = []
        self._statements = statements

    def __enter__(self) -> _Cursor:
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        return False

    def execute(self, sql, params=None) -> None:
        del params
        self._statements.append(str(sql))
        self._current = self._batches.pop(0)

    def fetchone(self):
        return self._current.pop(0) if self._current else None

    def fetchall(self):
        rows = list(self._current)
        self._current.clear()
        return rows


class _Conn:
    def __init__(self, *batches: list[tuple[Any, ...]]) -> None:
        self.batches = list(batches)
        self.statements: list[str] = []

    def cursor(self) -> _Cursor:
        return _Cursor(self.batches, self.statements)


def _snapshot(
    *,
    vector_rows: int,
    allow_vector_backfill: bool,
) -> dict[str, dict[str, Any]]:
    return {
        "memory_records": {
            "present": True,
            "vector_column_expected": True,
            "vector_column_present": True,
            "rows": 10,
            "vector_rows": vector_rows,
            "stable_key_checksum": 123,
            "authoritative_payload_checksum": 456,
            "allow_vector_backfill": allow_vector_backfill,
        }
    }


def test_require_pgvector_image_rejects_old_image_files() -> None:
    conn = _Conn([("0.8.3", "0.8.2")])

    with pytest.raises(RuntimeError, match="rebuild or pull/recreate pg-fs"):
        vector_lifecycle.require_pgvector_image(conn)


def test_require_pgvector_image_accepts_target_files_with_old_metadata() -> None:
    conn = _Conn([("0.8.5", "0.8.2")])

    assert vector_lifecycle.require_pgvector_image(conn) == {
        "default_version": "0.8.5",
        "installed_version": "0.8.2",
    }


def test_assert_protected_data_unchanged_accepts_rebuilt_indexes() -> None:
    snapshot = _snapshot(vector_rows=10, allow_vector_backfill=False)

    vector_lifecycle.assert_protected_data_unchanged(snapshot, snapshot)


def test_assert_protected_data_unchanged_rejects_missing_embeddings() -> None:
    before = _snapshot(vector_rows=10, allow_vector_backfill=False)
    after = {
        "memory_records": {
            **before["memory_records"],
            "vector_rows": 9,
        }
    }

    with pytest.raises(RuntimeError, match="protected data changed"):
        vector_lifecycle.assert_protected_data_unchanged(before, after)


def test_assert_protected_data_unchanged_allows_json_vector_backfill() -> None:
    before = {
        "platform_workflow_knowledge_documents": {
            **_snapshot(
                vector_rows=8,
                allow_vector_backfill=True,
            )["memory_records"],
        }
    }
    after = {
        "platform_workflow_knowledge_documents": {
            **before["platform_workflow_knowledge_documents"],
            "vector_rows": 10,
        }
    }

    vector_lifecycle.assert_protected_data_unchanged(before, after)


def test_capture_protected_data_snapshot_includes_non_vector_tables() -> None:
    conn = _Conn(
        [(1,)],
        [(True,)],
        [(10, 10, 101, 201)],
        [(1,)],
        [(True,)],
        [(30, 29, 103, 203)],
        [(1,)],
        [(40, 0, 104, 204)],
        [(1,)],
        [(50, 0, 105, 205)],
    )

    snapshot = vector_lifecycle.capture_protected_data_snapshot(conn)

    assert snapshot["memory_records"]["vector_rows"] == 10
    assert snapshot["platform_workflow_knowledge_nodes"] == {
        "present": True,
        "vector_column_expected": False,
        "vector_column_present": False,
        "rows": 40,
        "vector_rows": 0,
        "stable_key_checksum": 104,
        "authoritative_payload_checksum": 204,
        "allow_vector_backfill": False,
    }
    assert any("platform_workflow_knowledge_edges" in sql for sql in conn.statements)


def test_shallow_vector_health_never_scans_protected_tables() -> None:
    conn = _Conn(
        [("0.8.5", "0.8.5")],
        [
            ("memory_records", "embedding"),
            ("platform_workflow_knowledge_documents", "embedding"),
        ],
        [
            ("idx_memory_records_embedding_cosine", True, True),
            ("idx_platform_workflow_knowledge_docs_embedding_cosine", True, True),
        ],
        [(1.0,)],
    )

    health = vector_lifecycle.inspect_vector_health(conn)

    assert health["status"] == "healthy"
    assert health["coverage_checked"] is False
    sql = "\n".join(conn.statements)
    assert "count(*)" not in sql.lower()
    assert "jsonb_array_elements" not in sql.lower()
