from __future__ import annotations

from core.application.auth.config import DEFAULT_ACCOUNT_ID
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


def test_log_command_execution_writes_optional_event_and_dispatch_columns() -> None:
    logger = _make_logger()
    logger._table_exists = lambda table_name: table_name == "command_audit_log"  # type: ignore[method-assign]
    logger._column_exists = lambda table_name, column_name: (
        column_name in {"event_id", "dispatch_id"}
    )  # type: ignore[method-assign]
    logger.conn.fetchone_results = [
        {
            "id": 9,
            "command_id": "cmd-1",
            "command_type": "rerun",
            "account_id": "acc_ops",
            "actor_type": "user",
            "actor_id": "alice",
            "target_jsonb": {"job_id": 41},
            "payload_jsonb": {"force": True},
            "status": "accepted",
            "result_jsonb": {"ok": True},
            "error_text": None,
            "event_id": "evt-1",
            "dispatch_id": "dispatch-1",
            "created_at": None,
            "updated_at": None,
        }
    ]

    payload = logger.log_command_execution(
        command_id="cmd-1",
        command_type="rerun",
        account_id="acc_ops",
        actor_type="user",
        actor_id="alice",
        target={"job_id": 41},
        payload={"force": True},
        result={"ok": True},
        event_id="evt-1",
        dispatch_id="dispatch-1",
    )

    sql, params = logger.conn.calls[-1]
    assert "INSERT INTO command_audit_log" in sql
    assert "event_id" in sql
    assert "dispatch_id" in sql
    assert params[0:5] == ("cmd-1", "rerun", "acc_ops", "user", "alice")
    assert params[-2:] == ("evt-1", "dispatch-1")
    assert payload["target"] == {"job_id": 41}
    assert payload["payload"] == {"force": True}
    assert payload["result"] == {"ok": True}


def test_log_command_execution_defaults_account_and_system_actor() -> None:
    logger = _make_logger()
    logger._table_exists = lambda table_name: table_name == "command_audit_log"  # type: ignore[method-assign]
    logger._column_exists = lambda table_name, column_name: False  # type: ignore[method-assign]
    logger.conn.fetchone_results = [
        {
            "id": 10,
            "command_id": "cmd-2",
            "command_type": "cancel",
            "account_id": DEFAULT_ACCOUNT_ID,
            "actor_type": "system_actor",
            "actor_id": "system",
            "target_jsonb": {},
            "payload_jsonb": {},
            "status": "accepted",
            "result_jsonb": {},
            "error_text": None,
            "created_at": None,
            "updated_at": None,
        }
    ]

    payload = logger.log_command_execution(
        command_id="cmd-2",
        command_type="cancel",
        account_id="",
        actor_type="",
        actor_id="",
    )

    _sql, params = logger.conn.calls[-1]
    assert params[2] == DEFAULT_ACCOUNT_ID
    assert params[3] == "system_actor"
    assert params[4] == "system"
    assert payload["account_id"] == DEFAULT_ACCOUNT_ID


def test_update_command_execution_finalizes_existing_row() -> None:
    logger = _make_logger()
    logger.conn.fetchone_results = [
        {
            "id": 11,
            "command_id": "cmd-3",
            "command_type": "start_workflow",
            "account_id": DEFAULT_ACCOUNT_ID,
            "actor_type": "system_actor",
            "actor_id": "system",
            "target_jsonb": {"workflow_id": "weekly_digest"},
            "payload_jsonb": {},
            "status": "executed",
            "result_jsonb": {"job_id": "job-3"},
            "error_text": None,
            "created_at": None,
            "updated_at": None,
            "event_id": None,
            "dispatch_id": None,
        }
    ]

    payload = logger.update_command_execution(
        command_id="cmd-3",
        status="executed",
        result={"job_id": "job-3"},
        error_text=None,
    )

    sql, params = logger.conn.calls[-1]
    assert "UPDATE command_audit_log" in sql
    assert "updated_at = now()" in sql
    assert params == ("executed", '{"job_id": "job-3"}', None, "cmd-3")
    assert payload["status"] == "executed"
    assert payload["result"] == {"job_id": "job-3"}
    assert "target_jsonb" not in payload
