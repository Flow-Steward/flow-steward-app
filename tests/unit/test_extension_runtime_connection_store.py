from __future__ import annotations

from datetime import UTC, datetime

from core.infrastructure.db import extension_runtime_connection_store


class _RecordingCursor:
    def __init__(self, calls: list[tuple[str, object]]) -> None:
        self._calls = calls
        self._fetchall_results: list[list[object]] = []
        self.description = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def execute(self, sql, params=None):
        self._calls.append((str(sql), params))

    def fetchall(self):
        if self._fetchall_results:
            return self._fetchall_results.pop(0)
        return []


class _RecordingConn:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []
        self.fetchall_results: list[list[object]] = []
        self.description = []

    def cursor(self, *args, **kwargs):
        cursor = _RecordingCursor(self.calls)
        cursor._fetchall_results = self.fetchall_results
        cursor.description = self.description
        return cursor


def test_list_extension_runtime_connections_builds_scoped_query() -> None:
    conn = _RecordingConn()
    created_at = datetime(2026, 3, 31, 7, 0, tzinfo=UTC)
    conn.description = [("connection_id",), ("extension_install_id",), ("created_at",)]
    conn.fetchall_results = [[("conn_1", "pi_1", created_at)]]

    rows = extension_runtime_connection_store.list_extension_runtime_connections(
        conn,
        account_id="acc_default",
        install_id="pi_1",
        connection_id="conn_1",
        project_id=7,
    )

    sql, params = conn.calls[-1]
    assert "FROM project_connections" in sql
    assert "connection_id = %s" in sql
    assert "project_id = %s" in sql
    assert "enabled = true" in sql
    assert params == ("acc_default", "pi_1", "conn_1", "7")
    assert rows == [
        {
            "connection_id": "conn_1",
            "extension_install_id": "pi_1",
            "created_at": created_at.timestamp(),
        }
    ]


def test_list_extension_runtime_connection_counts_aggregates_rows() -> None:
    conn = _RecordingConn()
    conn.description = [("extension_install_id",), ("connection_count",)]
    conn.fetchall_results = [[("pi_1", 2), ("pi_2", 1)]]

    counts = extension_runtime_connection_store.list_extension_runtime_connection_counts(
        conn,
        account_id="acc_default",
        install_ids=["pi_1", "pi_2"],
    )

    sql, params = conn.calls[-1]
    assert "COUNT(*) AS connection_count" in sql
    assert "extension_install_id IN (%s, %s)" in sql
    assert params == ("acc_default", "pi_1", "pi_2")
    assert counts == {"pi_1": 2, "pi_2": 1}
