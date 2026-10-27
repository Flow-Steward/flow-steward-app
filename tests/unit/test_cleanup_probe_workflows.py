from __future__ import annotations

from core.application.tooling.ops.cleanup_probe_workflows import (
    PROBE_WORKFLOW_PREFIXES,
    _matched_workflow_ids,
    _select_sql,
    cleanup_probe_workflows,
)


class _FakeCursor:
    def __init__(self, rows: list[dict]) -> None:
        self._rows = rows
        self.executed: list[tuple[str, object]] = []
        self.rowcount = 0

    def __enter__(self) -> _FakeCursor:
        return self

    def __exit__(self, *_a: object) -> bool:
        return False

    def execute(self, sql: str, params: object = None) -> None:
        self.executed.append((sql, params))
        if sql.strip().upper().startswith("DELETE"):
            self.rowcount = 2

    def fetchall(self) -> list[dict]:
        return self._rows


class _FakeDB:
    def __init__(self, rows: list[dict]) -> None:
        self._rows = rows
        self.deleted: list[tuple[str, str]] = []
        self._tx_cursor = _FakeCursor([])

    def _cursor(self) -> _FakeCursor:
        return _FakeCursor(self._rows)

    def _transaction(self) -> _FakeCursor:
        return self._tx_cursor

    def delete_workflow(self, workflow_id: str, author: str = "admin") -> None:
        self.deleted.append((workflow_id, author))


def test_select_sql_builds_escaped_or_clauses():
    sql, params = _select_sql(("audit_", "e2e_wf_"))
    assert sql.count("LIKE %s ESCAPE '\\'") == 2
    assert " OR " in sql
    assert "deleted_at IS NULL" in sql
    assert params == (r"audit\_%", r"e2e\_wf\_%")


def test_matched_ids_filter_out_non_allowlisted_ids():
    # Defense in depth: even if the query returned a real workflow, the Python-side
    # allowlist filter drops anything not starting with a probe prefix.
    db = _FakeDB(
        [
            {"workflow_id": "audit_x"},
            {"workflow_id": "sample_weather_brief"},  # real — must be excluded
            {"workflow_id": "weekly_report"},  # real — must be excluded
            {"workflow_id": "e2e_wf_1"},
        ]
    )
    assert _matched_workflow_ids(db, PROBE_WORKFLOW_PREFIXES) == ["audit_x", "e2e_wf_1"]


def test_dry_run_deletes_nothing():
    db = _FakeDB([{"workflow_id": "audit_x"}, {"workflow_id": "ui_live_incoming_1"}])
    out = cleanup_probe_workflows(db, apply=False, author="probe_cleanup")
    assert out["dry_run"] is True
    assert out["matched_count"] == 2
    assert out["deleted_count"] == 0
    assert out["entrypoints_removed"] == 0
    assert db.deleted == []


def test_apply_soft_deletes_and_removes_entrypoints():
    db = _FakeDB([{"workflow_id": "audit_x"}, {"workflow_id": "ui_live_incoming_1"}])
    out = cleanup_probe_workflows(db, apply=True, author="probe_cleanup")
    assert out["dry_run"] is False
    assert out["deleted_count"] == 2
    assert db.deleted == [
        ("audit_x", "probe_cleanup"),
        ("ui_live_incoming_1", "probe_cleanup"),
    ]
    # Entrypoints for the cleaned workflows are removed (no dangling public triggers).
    assert out["entrypoints_removed"] == 2
    assert any("workflow_entrypoints" in sql for sql, _ in db._tx_cursor.executed)
