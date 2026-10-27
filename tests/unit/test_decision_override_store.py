from __future__ import annotations

from tests.support.platform_test_store import PlatformTestStore as PlatformLogger


class _RecordingCursor:
    def __init__(self, calls: list[tuple[str, object]]) -> None:
        self._calls = calls
        self._fetchone_results: list[object] = []
        self._fetchall_results: list[list[object]] = []
        self.rowcount = 1

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def execute(self, sql, params=None):
        self._calls.append((str(sql), params))

    def fetchone(self):
        if self._fetchone_results:
            return self._fetchone_results.pop(0)
        return None

    def fetchall(self):
        if self._fetchall_results:
            return self._fetchall_results.pop(0)
        return []


class _RecordingConn:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []
        self.fetchone_results: list[object] = []
        self.fetchall_results: list[list[object]] = []

    def cursor(self, *args, **kwargs):
        cursor = _RecordingCursor(self.calls)
        cursor._fetchone_results = self.fetchone_results
        cursor._fetchall_results = self.fetchall_results
        return cursor


def _make_logger() -> PlatformLogger:
    logger = PlatformLogger.__new__(PlatformLogger)
    logger.conn = _RecordingConn()
    logger._column_exists_cache = {}
    logger._table_exists = lambda table_name: table_name == "job_decision_overrides"  # type: ignore[method-assign]
    return logger


def test_create_job_decision_override_deactivates_matching_scope_before_insert() -> None:
    logger = _make_logger()
    logger.conn.fetchone_results = [
        {
            "id": 7,
            "job_id": 15,
            "domain": "gate_outcome",
            "message_id": 99,
            "workflow_id": "wf-1",
            "step_id": "step-1",
            "step_execution_id": "6f1c2a1e-3b9d-4c55-9a0e-2f7f3e8d1b42",
            "active": True,
        }
    ]

    row = logger.create_job_decision_override(
        15,
        domain="gate_outcome",
        override_value={"action": "reroute"},
        original_value={"action": "retry"},
        reason="operator override",
        message_id=99,
        workflow_id="wf-1",
        step_id="step-1",
        step_execution_id="6f1c2a1e-3b9d-4c55-9a0e-2f7f3e8d1b42",
    )

    executed_sql = "\n".join(sql for sql, _ in logger.conn.calls)
    assert "step_execution_id IS NOT DISTINCT FROM %s" in executed_sql
    assert "phase_id" not in executed_sql
    update_params = next(
        params for sql, params in logger.conn.calls if "UPDATE job_decision_overrides" in sql
    )
    assert update_params[-2:] == ("step-1", "6f1c2a1e-3b9d-4c55-9a0e-2f7f3e8d1b42")
    assert "UPDATE job_decision_overrides" in executed_sql
    assert "INSERT INTO job_decision_overrides" in executed_sql
    assert row["domain"] == "gate_outcome"
    assert row["active"] is True


def test_list_job_decision_overrides_filters_active_scope_and_domain() -> None:
    logger = _make_logger()
    logger.conn.fetchall_results = [
        [
            {
                "id": 8,
                "job_id": 15,
                "domain": "gate_outcome",
                "active": True,
            }
        ]
    ]

    rows = logger.list_job_decision_overrides(
        15,
        active_only=True,
        domain="gate_outcome",
        limit=5,
    )

    sql, params = logger.conn.calls[-1]
    assert "active = true" in sql
    assert "domain = %s" in sql
    assert params == (15, "gate_outcome", 5)
    assert rows[0]["domain"] == "gate_outcome"
