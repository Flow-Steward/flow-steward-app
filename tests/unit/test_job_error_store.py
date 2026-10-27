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
    logger._table_exists = lambda table_name: table_name == "job_errors"  # type: ignore[method-assign]
    return logger


def test_list_job_errors_applies_limit_and_returns_rows() -> None:
    logger = _make_logger()
    logger.conn.fetchall_results = [[{"id": 5, "job_id": 11}]]

    rows = logger.list_job_errors(11, limit=3)

    sql, params = logger.conn.calls[-1]
    assert "FROM job_errors" in sql
    assert params == (11, 3)
    assert rows == [{"id": 5, "job_id": 11}]


def test_infer_exception_hints_detects_provider_from_context_json() -> None:
    source, category = PlatformLogger.infer_exception_hints(
        RuntimeError("request failed"),
        context={"provider": "openai", "request_id": "req-1"},
    )

    assert source == "provider"
    assert category == "provider"
