from __future__ import annotations

import json
import uuid

from core.application.orchestration.agent_messages import build_agent_message
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


def _sample_envelope() -> dict:
    return build_agent_message(
        from_agent="planner",
        to_agent="orchestrator",
        step_id="design",
        skill="draft_plan",
        status="pass",
        correlation_id="corr-1",
        artifacts={"doc": "ok"},
        summary="done",
        published_at="2026-03-10T00:00:00Z",
    )


def test_log_agent_message_persists_validated_envelope() -> None:
    logger = _make_logger()

    ok = logger.log_agent_message(44, _sample_envelope())

    sql, params = logger.conn.calls[-1]
    assert ok is True
    assert "INSERT INTO platform_agent_messages" in sql
    assert params[0] == 44
    assert params[3] == "planner"


def test_log_agent_message_serializes_uuid_artifacts() -> None:
    logger = _make_logger()
    trace_id = uuid.UUID("58eeaeb3-a9c1-4c51-b292-14eb86899d86")
    envelope = _sample_envelope()
    envelope["artifacts"]["trace_id"] = trace_id

    ok = logger.log_agent_message(44, envelope)

    _sql, params = logger.conn.calls[-1]
    assert ok is True
    assert json.loads(params[13])["trace_id"] == str(trace_id)
    assert json.loads(params[15])["artifacts"]["trace_id"] == str(trace_id)


def test_get_agent_messages_applies_filters_and_reverses_desc_page() -> None:
    logger = _make_logger()
    logger.conn.fetchall_results = [[{"id": 12}, {"id": 11}]]

    rows = logger.get_agent_messages(
        44,
        before_id=20,
        newest=True,
        step_id="design",
        from_agent="planner",
        skill="draft_plan",
        status="pass",
        limit=2,
    )

    sql, params = logger.conn.calls[-1]
    assert "(timestamp, id::text) <" in sql
    assert "step_id = %s" in sql
    assert "from_agent = %s" in sql
    assert "skill = %s" in sql
    assert "status = %s" in sql
    assert params == (44, "20", "20", "design", "planner", "draft_plan", "pass", 2)
    assert [row["id"] for row in rows] == [11, 12]


def test_get_latest_agent_message_uses_optional_filters() -> None:
    logger = _make_logger()
    logger.conn.fetchone_results = [{"id": 13, "from_agent": "planner"}]

    row = logger.get_latest_agent_message(44, from_agent="planner", step_id="design")

    sql, params = logger.conn.calls[-1]
    assert "from_agent = %s" in sql
    assert "step_id = %s" in sql
    assert params == (44, "planner", "design")
    assert row["id"] == 13
