from __future__ import annotations

import logging
from types import SimpleNamespace

from core.infrastructure.db import event_store
from core.infrastructure.db.psycopg_runtime import errorcodes
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


def test_log_event_inserts_semantic_event_row() -> None:
    logger = _make_logger()

    ok = logger.log_event(
        job_id=9,
        phase="delivery",
        agent="writer",
        event_type="step.completed",
        title="done",
        detail={"ok": True},
    )

    assert ok is True
    sql, params = logger.conn.calls[-1]
    assert "INSERT INTO platform_events" in sql
    assert params[0] == 9
    assert params[3] == "step.completed"


def test_replay_queued_events_marks_successful_rows_sent() -> None:
    logger = _make_logger()
    logger.conn.fetchall_results = [
        [
            {
                "id": 14,
                "payload": {
                    "job_id": 12,
                    "phase": "checker",
                    "agent": "tester",
                    "event_type": "review.failed",
                    "title": "check failed",
                    "detail": {"check": "lint"},
                },
            }
        ]
    ]

    replayed = logger.replay_queued_events(max_events=3)

    assert replayed == 1
    executed_sql = "\n".join(sql for sql, _ in logger.conn.calls)
    assert "SELECT id, payload" in executed_sql
    assert "INSERT INTO platform_events" in executed_sql
    assert "UPDATE platform_event_retry_queue" in executed_sql


def test_list_events_since_filters_allowed_project_scope() -> None:
    logger = _make_logger()
    logger.conn.fetchall_results = [
        [
            {
                "id": 21,
                "job_id": 9,
                "event_type": "job.updated",
                "project_id": 77,
            }
        ]
    ]

    rows = logger.list_events_since(
        100,
        "acc-1",
        allowed_project_ids=["77", "78"],
        event_type="job.updated",
        limit=5,
    )

    sql, params = logger.conn.calls[-1]
    assert "(e.timestamp, e.id::text) >" in sql
    assert "p.id = ANY(%s)" in sql
    assert "e.event_type = %s" in sql
    assert params == ("acc-1", "100", "100", ["77", "78"], "job.updated", 5)
    assert rows[0]["project_id"] == 77


def test_public_state_marker_queries_use_monotonic_revision_cursor() -> None:
    logger = _make_logger()

    latest = event_store.get_latest_public_state_marker(
        "job-1",
        "acc-1",
        conn=logger.conn,
        logger=logging.getLogger(__name__),
        sql_project_account_id_eq="p.account_id = %s",
        sql_and=" AND ",
    )
    markers = event_store.list_public_state_markers_since(
        "marker-1",
        "job-1",
        "acc-1",
        conn=logger.conn,
        logger=logging.getLogger(__name__),
        sql_project_account_id_eq="p.account_id = %s",
        sql_and=" AND ",
    )

    latest_sql = logger.conn.calls[-2][0]
    replay_sql = logger.conn.calls[-1][0]
    revision_sql = "COALESCE((e.detail ->> 'revision')::bigint, 0)"
    assert latest is None
    assert markers == []
    assert f"ORDER BY {revision_sql} DESC" in latest_sql
    assert f"{revision_sql} > (" in replay_sql
    assert "SELECT COALESCE((marker.detail ->> 'revision')::bigint, 0)" in replay_sql
    assert f"ORDER BY {revision_sql} ASC" in replay_sql
    assert "(e.timestamp, e.id::text) >" not in replay_sql


def test_log_event_drops_missing_job_foreign_key_errors() -> None:
    class _FakeFKError(Exception):
        def __init__(self) -> None:
            super().__init__('Key (job_id)=(91) is not present in table "jobs".')
            self.pgcode = errorcodes.FOREIGN_KEY_VIOLATION
            self.diag = SimpleNamespace(constraint_name="platform_events_job_id_fkey")

    ok = event_store.log_event(
        insert_event_row_func=lambda **_: (_ for _ in ()).throw(_FakeFKError()),
        logger=logging.getLogger(__name__),
        job_id=91,
        phase="checker",
        agent="tester",
        event_type="phase_complete",
        title="done",
    )

    assert ok is True


def test_log_event_drops_psycopg3_missing_job_foreign_key_errors() -> None:
    class _FakePsycopg3FKError(Exception):
        def __init__(self) -> None:
            super().__init__(
                'insert or update on table "platform_events" violates foreign key '
                'constraint "platform_events_job_id_fkey"\n'
                'DETAIL:  Key (job_id)=(91) is not present in table "jobs".'
            )
            self.sqlstate = errorcodes.FOREIGN_KEY_VIOLATION
            self.diag = SimpleNamespace(constraint_name="platform_events_job_id_fkey")

    ok = event_store.log_event(
        insert_event_row_func=lambda **_: (_ for _ in ()).throw(_FakePsycopg3FKError()),
        logger=logging.getLogger(__name__),
        job_id=91,
        phase="checker",
        agent="tester",
        event_type="phase_complete",
        title="done",
    )

    assert ok is True


def test_log_event_drops_malformed_job_id_uuid_errors() -> None:
    class _FakeInvalidUuidError(Exception):
        def __init__(self) -> None:
            super().__init__(
                'invalid input syntax for type uuid: "dry-run-delivery"\n'
                "CONTEXT:  unnamed portal parameter $1 = '...'"
            )

    ok = event_store.log_event(
        insert_event_row_func=lambda **_: (_ for _ in ()).throw(_FakeInvalidUuidError()),
        logger=logging.getLogger(__name__),
        job_id="dry-run-delivery",
        phase="delivery",
        agent="coordinator",
        event_type="agent_completed",
        title="done",
    )

    assert ok is True


def test_replay_queued_events_marks_missing_job_rows_sent_without_retrying() -> None:
    class _FakeFKError(Exception):
        def __init__(self) -> None:
            super().__init__('Key (job_id)=(12) is not present in table "jobs".')
            self.pgcode = errorcodes.FOREIGN_KEY_VIOLATION
            self.diag = SimpleNamespace(constraint_name="platform_events_job_id_fkey")

    logger = _make_logger()
    logger.conn.fetchall_results = [
        [
            {
                "id": 14,
                "payload": {
                    "job_id": 12,
                    "phase": "checker",
                    "agent": "tester",
                    "event_type": "review.failed",
                    "title": "check failed",
                    "detail": {"check": "lint"},
                },
            }
        ]
    ]

    replayed = event_store.replay_queued_events(
        conn=logger.conn,
        insert_event_row_func=lambda **_: (_ for _ in ()).throw(_FakeFKError()),
        logger=logging.getLogger(__name__),
        max_events=3,
    )

    assert replayed == 0
    executed_sql = "\n".join(sql for sql, _ in logger.conn.calls)
    assert "SELECT id, payload" in executed_sql
    assert "SET sent_at = NOW()" in executed_sql
    assert "retry_count = retry_count + 1" not in executed_sql
