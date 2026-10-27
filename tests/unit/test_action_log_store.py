from __future__ import annotations

from types import SimpleNamespace

from core.infrastructure.db import action_log_store
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
        self.rollback_calls = 0
        self.transaction_status = 0

    def cursor(self, *args, **kwargs):
        cursor = _RecordingCursor(self.calls)
        cursor._fetchone_results = self.fetchone_results
        cursor._fetchall_results = self.fetchall_results
        return cursor

    def rollback(self):
        self.rollback_calls += 1

    def get_transaction_status(self):
        return self.transaction_status


def _make_logger() -> PlatformLogger:
    logger = PlatformLogger.__new__(PlatformLogger)
    logger.conn = _RecordingConn()
    logger._column_exists_cache = {}
    return logger


def test_log_action_clamps_negative_duration_and_persists_payloads() -> None:
    logger = _make_logger()
    logger.conn.fetchone_results = [{"id": 4, "duration": 0}]

    row = logger.log_action(
        7,
        "task-1",
        "summary",
        "writer",
        "llm_call",
        duration=-5,
        payload={"prompt": "hi"},
        result={"answer": "ok"},
    )

    sql, params = logger.conn.calls[-1]
    assert "INSERT INTO platform_action_log" in sql
    assert params[11] == 0
    assert row["duration"] == 0


def test_get_logs_reverses_desc_page_for_before_cursor() -> None:
    logger = _make_logger()
    logger.conn.fetchall_results = [[{"id": 9}, {"id": 8}]]

    rows = logger.get_logs(3, before_id=10, limit=2)

    sql, params = logger.conn.calls[-1]
    assert "(timestamp, id::text) <" in sql
    assert "ORDER BY timestamp DESC, id DESC" in sql
    assert params == (3, "10", "10", 2)
    assert [row["id"] for row in rows] == [8, 9]


def test_get_latest_log_entry_filters_action_types_and_error() -> None:
    logger = _make_logger()
    logger.conn.fetchone_results = [{"id": 11, "action_type": "llm_call"}]

    row = logger.get_latest_log_entry(3, action_types=["llm_call", "tool_call"], require_error=True)

    sql, params = logger.conn.calls[-1]
    assert "action_type IN (%s,%s)" in sql
    assert "error IS NOT NULL AND error <> ''" in sql
    assert params == (3, "llm_call", "tool_call")
    assert row["id"] == 11


def test_log_action_drops_missing_job_foreign_key_errors() -> None:
    class _FakeFKError(Exception):
        def __init__(self) -> None:
            super().__init__('Key (job_id)=(77) is not present in table "jobs".')
            self.pgcode = errorcodes.FOREIGN_KEY_VIOLATION
            self.diag = SimpleNamespace(constraint_name="platform_action_log_job_id_fkey")

    class _ExplodingCursor:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def execute(self, sql, params=None):
            raise _FakeFKError()

        def fetchone(self):
            return None

    class _ExplodingConn:
        def cursor(self, *args, **kwargs):
            return _ExplodingCursor()

    row = action_log_store.log_action(
        conn=_ExplodingConn(),
        project_id=7,
        job_id=77,
        task_id="task-1",
        task_summary="summary",
        lifecycle_id=None,
        agent="writer",
        action_type="node_end",
        status="done",
    )

    assert row is None


def test_missing_job_reference_detection_uses_psycopg3_sqlstate() -> None:
    class _Psycopg3FKError(Exception):
        sqlstate = errorcodes.FOREIGN_KEY_VIOLATION
        diag = SimpleNamespace(constraint_name="platform_action_log_job_id_fkey")

    assert action_log_store._is_missing_job_reference_error(_Psycopg3FKError()) is True


def test_log_action_uses_savepoint_to_recover_missing_job_foreign_key_errors() -> None:
    class _FakeFKError(Exception):
        def __init__(self) -> None:
            super().__init__('Key (job_id)=(77) is not present in table "jobs".')
            self.pgcode = errorcodes.FOREIGN_KEY_VIOLATION
            self.diag = SimpleNamespace(constraint_name="platform_action_log_job_id_fkey")

    class _Cursor:
        def __init__(self, calls):
            self.calls = calls
            self._insert_attempted = False

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def execute(self, sql, params=None):
            statement = str(sql)
            self.calls.append((statement, params))
            if "INSERT INTO platform_action_log" in statement and not self._insert_attempted:
                self._insert_attempted = True
                raise _FakeFKError()

        def fetchone(self):
            return None

    class _Conn:
        def __init__(self):
            self.calls = []
            self.rollback_calls = 0

        def cursor(self, *args, **kwargs):
            return _Cursor(self.calls)

        def rollback(self):
            self.rollback_calls += 1

        def get_transaction_status(self):
            return 2

    conn = _Conn()

    row = action_log_store.log_action(
        conn=conn,
        project_id=7,
        job_id=77,
        task_id="task-1",
        task_summary="summary",
        lifecycle_id=None,
        agent="writer",
        action_type="node_end",
        status="done",
    )

    assert row is None
    executed_sql = [sql for sql, _ in conn.calls]
    assert executed_sql[0] == "SAVEPOINT platform_action_log_guard"
    assert any(sql == "ROLLBACK TO SAVEPOINT platform_action_log_guard" for sql in executed_sql)
    assert any(sql == "RELEASE SAVEPOINT platform_action_log_guard" for sql in executed_sql)
    assert conn.rollback_calls == 0


def test_log_action_does_not_issue_savepoint_on_idle_connection() -> None:
    logger = _make_logger()
    logger.conn.fetchone_results = [{"id": 4, "job_id": 77}]

    row = logger.log_action(
        7,
        "task-1",
        "summary",
        "writer",
        "node_end",
        job_id=77,
        status="done",
    )

    executed_sql = [sql for sql, _ in logger.conn.calls]
    assert all("SAVEPOINT" not in sql for sql in executed_sql)
    assert "INSERT INTO platform_action_log" in executed_sql[0]
    assert row == {"id": 4, "job_id": 77}
