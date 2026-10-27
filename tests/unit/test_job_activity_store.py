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
    return logger


def test_get_active_jobs_queries_runtime_fields_from_joined_project_rows() -> None:
    logger = _make_logger()
    logger.conn.fetchall_results = [[{"id": 41, "project_name": "Demo", "status": "pending"}]]

    rows = logger.get_active_jobs()

    sql, params = logger.conn.calls[-1]
    assert "FROM jobs j" in sql
    assert "JOIN projects p ON p.id = j.project_id" in sql
    assert "awaiting_approval" in sql
    assert "awaiting_timer" in sql
    assert "awaiting_child" in sql
    assert "resume_pending" in sql
    assert params is None
    assert rows == [{"id": 41, "project_name": "Demo", "status": "pending"}]


def test_pending_job_recovery_includes_resume_pending() -> None:
    logger = _make_logger()
    logger.conn.fetchall_results = [[{"id": 42, "status": "resume_pending"}]]

    rows = logger.get_pending_jobs("project-42")

    sql, params = logger.conn.calls[-1]
    assert "resume_pending" in sql
    assert params == ("project-42",)
    assert rows == [{"id": 42, "status": "resume_pending"}]
