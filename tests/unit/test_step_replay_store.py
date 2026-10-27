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
    logger._table_exists = lambda table_name: table_name == "job_step_replays"  # type: ignore[method-assign]
    return logger


def test_create_job_step_replay_normalizes_payload_and_starts_running_rows() -> None:
    logger = _make_logger()
    logger.conn.fetchone_results = [
        {
            "id": 22,
            "job_id": 10,
            "message_id": 7,
            "correlation_id": "corr-1",
            "requested_by": "alice",
            "replay_mode": "prompt_reinvoke",
            "status": "running",
        }
    ]

    row = logger.create_job_step_replay(
        10,
        message_id=7,
        correlation_id="corr-1",
        requested_by="alice",
        replay_mode="prompt_reinvoke",
        status="running",
        policy_snapshot={"mode": "safe"},
        captured_input={"prompt": "hello"},
        original_outcome={"status": "failed"},
    )

    sql, params = logger.conn.calls[-1]
    assert "INSERT INTO job_step_replays" in sql
    assert "CASE WHEN %s = 'running' THEN NOW() ELSE NULL END" in sql
    assert params[4] == "prompt_reinvoke"
    assert params[5] == "running"
    assert row["status"] == "running"


def test_update_job_step_replay_marks_terminal_status_and_truncates_error() -> None:
    logger = _make_logger()
    logger.conn.fetchone_results = [
        {
            "id": 22,
            "job_id": 10,
            "status": "failed",
            "error_message": "boom",
        }
    ]

    row = logger.update_job_step_replay(
        22,
        status="failed",
        replay_outcome={"status": "failed"},
        original_outcome={"status": "failed"},
        diff_excerpt="x" * 10,
        error_message="boom",
    )

    sql, params = logger.conn.calls[-1]
    assert "UPDATE job_step_replays" in sql
    assert "finished_at = CASE" in sql
    assert params[0] == "failed"
    assert row["status"] == "failed"


def test_list_job_step_replays_filters_by_message_and_correlation() -> None:
    logger = _make_logger()
    logger.conn.fetchall_results = [
        [
            {
                "id": 23,
                "job_id": 10,
                "message_id": 7,
                "correlation_id": "corr-1",
                "status": "queued",
            }
        ]
    ]

    rows = logger.list_job_step_replays(
        10,
        message_id=7,
        correlation_id="corr-1",
        limit=5,
    )

    sql, params = logger.conn.calls[-1]
    assert "message_id = %s" in sql
    assert "correlation_id = %s" in sql
    assert params == (10, "7", "corr-1", 5)
    assert rows[0]["correlation_id"] == "corr-1"
