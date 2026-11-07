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
    logger._column_exists = lambda table_name, column_name: False  # type: ignore[method-assign]
    return logger


def test_get_job_usage_summary_returns_row_payload() -> None:
    logger = _make_logger()
    logger.conn.fetchone_results = [
        {
            "tokens_prompt": 11,
            "tokens_completion": 7,
            "tokens_total": 18,
            "llm_calls": 2,
            "dominant_agent": "composer",
        }
    ]

    payload = logger.get_job_usage_summary(41)

    sql, params = logger.conn.calls[-1]
    assert "FROM platform_action_log" in sql
    assert "model_used IS NOT NULL" in sql
    assert "cost_usd" not in sql
    assert "agent_tokens DESC" in sql
    assert params == (41, 41)
    assert payload["tokens_total"] == 18
    assert payload["llm_calls"] == 2
    assert payload["dominant_agent"] == "composer"


def test_get_jobs_usage_summary_normalizes_ids_and_backfills_zero_rows() -> None:
    logger = _make_logger()
    logger.conn.fetchall_results = [
        [
            {
                "job_id": 11,
                "tokens_prompt": 10,
                "tokens_completion": 5,
                "tokens_total": 15,
                "llm_calls": 1,
                "dominant_agent": "proofreader",
            }
        ]
    ]

    payload = logger.get_jobs_usage_summary([11, "12", 11, 0, "bad"])

    sql, params = logger.conn.calls[-1]
    assert "WHERE job_id = ANY(%s)" in sql
    assert "unknown_cli_tokens" not in sql
    assert "model_used IS NOT NULL" in sql
    assert "cost_usd" not in sql
    assert "agent_tokens DESC" in sql
    assert params == (["11", "12", "bad"], ["11", "12", "bad"])
    assert payload["11"]["dominant_agent"] == "proofreader"
    assert payload["12"]["dominant_agent"] is None
