from __future__ import annotations

import re
import uuid
from unittest.mock import MagicMock

import pytest

from core.application.auth.config import DEFAULT_WORKSPACE_ID
from core.infrastructure.db.platform_stores import PlatformObservabilityStore
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
        return (123,)

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


def _make_logger(has_complexity_column: bool) -> PlatformLogger:
    logger = PlatformLogger.__new__(PlatformLogger)
    logger.conn = _RecordingConn()
    logger._column_exists_cache = {}
    logger._column_exists = lambda table_name, column_name: bool(  # type: ignore[method-assign]
        has_complexity_column and table_name == "jobs" and column_name == "complexity"
    )
    return logger


def _insert_param_for_column(sql: str, params: object, column_name: str):
    assert isinstance(params, tuple)
    insert_prefix = str(sql).split("VALUES", 1)[0]
    normalized_columns = re.findall(r"Identifier\('([^']+)'\)", insert_prefix)
    return params[normalized_columns.index(column_name)]


def _update_param_for_column(sql: str, params: object, column_name: str):
    assert isinstance(params, tuple)
    update_prefix = str(sql).split("WHERE", 1)[0]
    normalized_columns = re.findall(r"([a-z_]+)=%s", update_prefix)
    return params[normalized_columns.index(column_name)]


class _FakeAESGCM:
    def __init__(self, key: bytes) -> None:
        self.key = key

    def encrypt(self, nonce: bytes, payload: bytes, aad) -> bytes:
        return b"enc:" + bytes(payload)

    def decrypt(self, nonce: bytes, payload: bytes, aad) -> bytes:
        assert bytes(payload).startswith(b"enc:")
        return bytes(payload)[4:]


def test_is_account_active_returns_true_when_accounts_table_missing():
    logger = _make_logger(has_complexity_column=False)
    logger._table_exists = lambda table_name: False  # type: ignore[method-assign]

    assert logger.is_account_active("acc_default") is True


def test_is_account_active_reads_accounts_state_when_table_available():
    logger = _make_logger(has_complexity_column=False)
    logger._table_exists = lambda table_name: table_name == "accounts"  # type: ignore[method-assign]
    logger.conn.fetchone_results = [{"is_active": False}]

    assert logger.is_account_active("acc_disabled") is False
    sql, params = logger.conn.calls[-1]
    assert "FROM accounts" in sql
    assert params == ("acc_disabled",)


def test_create_job_uses_complexity_column_when_present():
    logger = _make_logger(has_complexity_column=True)
    job_id = logger.create_job(
        project_id=1,
        task_id="task-1",
        task_summary="summary",
        task_source="manual",
        thread_id="thread-1",
        mode="brownfield",
        complexity="bugfix",
        status="pending",
    )

    assert job_id == 123
    sql, params = logger.conn.calls[-1]
    assert "complexity" in sql.lower()
    assert "bugfix" in params


def test_create_job_skips_complexity_column_when_missing():
    logger = _make_logger(has_complexity_column=False)
    job_id = logger.create_job(
        project_id=1,
        task_id="task-2",
        task_summary="summary",
        task_source="manual",
        thread_id="thread-2",
        mode="brownfield",
        complexity="legacy_complexity_value",
        status="pending",
    )

    assert job_id == 123
    sql, params = logger.conn.calls[-1]
    assert "complexity" not in sql.lower()
    assert "legacy_complexity_value" not in params


def test_create_job_truncates_task_summary():
    logger = _make_logger(has_complexity_column=False)
    huge_summary = "A" * 5000

    job_id = logger.create_job(
        project_id=1,
        task_id="task-3",
        task_summary=huge_summary,
        task_source="manual",
        thread_id="thread-3",
        mode="brownfield",
        status="pending",
    )

    assert job_id == 123
    _sql, params = logger.conn.calls[-1]
    assert huge_summary not in params
    assert "A" * 4000 in params
    assert "A" * 4001 not in params


def test_update_job_ignores_complexity_when_column_missing():
    logger = _make_logger(has_complexity_column=False)
    logger.update_job(job_id=55, status="in_progress", complexity="legacy_complexity_value")

    sql, params = logger.conn.calls[-1]
    assert "complexity" not in sql.lower()
    assert "legacy_complexity_value" not in params


def test_update_job_writes_complexity_when_column_present():
    logger = _make_logger(has_complexity_column=True)
    logger.update_job(job_id=56, status="in_progress", complexity="bugfix")

    sql, params = logger.conn.calls[-1]
    assert "complexity" in sql.lower()
    assert "bugfix" in params


def test_delete_job_routes_cleanup_through_store_layer():
    logger = _make_logger(has_complexity_column=False)
    logger.get_job = lambda job_id: {"id": job_id, "status": "completed"}  # type: ignore[method-assign]
    logger._table_exists = lambda table_name: (
        table_name
        in {  # type: ignore[method-assign]
            "platform_events",
            "job_attachments",
        }
    )

    deleted = logger.delete_job(77)

    assert deleted is True
    executed_sql = "\n".join(sql for sql, _ in logger.conn.calls)
    assert "DELETE FROM platform_events WHERE job_id = %s" in executed_sql
    assert "DELETE FROM job_attachments WHERE job_id = %s" in executed_sql
    assert "DELETE FROM jobs WHERE id = %s" in executed_sql


def test_log_job_reaction_event_updates_existing_dedupe_key_without_on_conflict():
    logger = _make_logger(has_complexity_column=False)
    logger._table_exists = lambda table_name: table_name == "job_reaction_events"  # type: ignore[method-assign]
    logger.conn.fetchone_results = [
        {
            "id": 9,
            "job_id": 1,
            "workflow_id": "wf",
            "event_type": "external.comment.received",
            "source": "sample_tasks",
            "source_id": "c1",
            "event_key": "comment.created",
            "payload_jsonb": {},
            "dedupe_key": "dup-1",
            "processed": False,
            "created_at": None,
        },
        {
            "id": 9,
            "job_id": 2,
            "workflow_id": "wf",
            "event_type": "external.comment.received",
            "source": "sample_tasks",
            "source_id": "c1",
            "event_key": "comment.created",
            "payload_jsonb": {"text": "retry"},
            "dedupe_key": "dup-1",
            "processed": True,
            "created_at": None,
        },
    ]

    row = logger.log_job_reaction_event(
        job_id=2,
        workflow_id="wf",
        event_type="external.comment.received",
        source="sample_tasks",
        source_id="c1",
        event_key="comment.created",
        payload={"text": "retry"},
        dedupe_key="dup-1",
        processed=True,
    )

    executed_sql = "\n".join(sql for sql, _ in logger.conn.calls)
    assert "ON CONFLICT (dedupe_key)" not in executed_sql
    assert "UPDATE job_reaction_events" in executed_sql
    assert row["job_id"] == 2
    assert row["processed"] is True


def test_log_job_reaction_event_preserves_processed_true_on_dedupe_reingest():
    logger = _make_logger(has_complexity_column=False)
    logger._table_exists = lambda table_name: table_name == "job_reaction_events"  # type: ignore[method-assign]
    logger.conn.fetchone_results = [
        {
            "id": 12,
            "job_id": 3,
            "workflow_id": "wf",
            "event_type": "external.comment.received",
            "source": "sample_tasks",
            "source_id": "c9",
            "event_key": "comment.created",
            "payload_jsonb": {"text": "first"},
            "dedupe_key": "dup-9",
            "processed": True,
            "created_at": None,
        },
        {
            "id": 12,
            "job_id": 3,
            "workflow_id": "wf",
            "event_type": "external.comment.received",
            "source": "sample_tasks",
            "source_id": "c9",
            "event_key": "comment.created",
            "payload_jsonb": {"text": "repeat"},
            "dedupe_key": "dup-9",
            "processed": True,
            "created_at": None,
        },
    ]

    row = logger.log_job_reaction_event(
        job_id=3,
        workflow_id="wf",
        event_type="external.comment.received",
        source="sample_tasks",
        source_id="c9",
        event_key="comment.created",
        payload={"text": "repeat"},
        dedupe_key="dup-9",
        processed=False,
    )

    executed_sql = "\n".join(sql for sql, _ in logger.conn.calls)
    assert "processed = (job_reaction_events.processed OR %s)" in executed_sql
    assert row["processed"] is True


def test_update_event_dispatch_status_rejects_invalid_terminal_transition():
    logger = _make_logger(has_complexity_column=False)
    logger._table_exists = lambda table_name: table_name == "event_dispatches"  # type: ignore[method-assign]
    logger.conn.fetchone_results = [
        {"status": "succeeded", "event_id": "evt-1"},
    ]

    with pytest.raises(ValueError, match="Invalid event dispatch status transition"):
        logger.update_event_dispatch_status("dispatch-1", status="queued")

    executed_sql = "\n".join(sql for sql, _ in logger.conn.calls)
    assert "UPDATE event_dispatches" not in executed_sql


def test_update_event_dispatch_status_allows_queued_to_dispatching_transition():
    logger = _make_logger(has_complexity_column=False)
    logger._table_exists = lambda table_name: table_name == "event_dispatches"  # type: ignore[method-assign]
    logger._refresh_domain_event_status = lambda _event_id: None  # type: ignore[method-assign]
    logger.conn.fetchone_results = [
        {"status": "queued", "event_id": "evt-1"},
        {"status": "dispatching", "event_id": "evt-1", "dispatch_id": "dispatch-1"},
    ]

    row = logger.update_event_dispatch_status("dispatch-1", status="dispatching")

    assert row is not None
    assert str(row.get("status") or "") == "dispatching"
    executed_sql = "\n".join(sql for sql, _ in logger.conn.calls)
    assert "UPDATE event_dispatches" in executed_sql


def test_record_event_delivery_attempt_dead_letters_after_retry_exhaustion():
    logger = _make_logger(has_complexity_column=False)
    logger._table_exists = lambda table_name: (
        table_name in {"event_delivery_attempts", "event_dispatches"}
    )  # type: ignore[method-assign]
    logger.update_event_dispatch_status = MagicMock()  # type: ignore[method-assign]
    logger.conn.fetchone_results = [
        {"status": "failed", "attempt_count": 2, "max_attempts": 3},
        {"next_attempt_no": 3},
        {
            "id": 91,
            "event_id": "evt-1",
            "dispatch_id": "dispatch-1",
            "attempt_no": 3,
            "status": "failed",
            "worker_id": None,
            "handler": "notification_runtime",
            "started_at": None,
            "finished_at": None,
            "duration_ms": None,
            "request_jsonb": {},
            "response_jsonb": {},
            "error_text": "timeout",
            "retry_in_seconds": 30,
            "created_at": None,
        },
    ]

    row = logger.record_event_delivery_attempt(
        event_id="evt-1",
        dispatch_id="dispatch-1",
        status="failed",
        handler="notification_runtime",
        error_text="timeout",
        retry_in_seconds=30,
    )

    assert row is not None
    kwargs = logger.update_event_dispatch_status.call_args.kwargs
    assert kwargs["status"] == "dead_lettered"
    assert kwargs["reason_code"] == "max_retries_exhausted"
    assert "max_attempts=3" in str(kwargs["reason_detail"])
    assert kwargs["attempt_count_increment"] == 1


def test_save_workflow_reactions_upserts_before_pruning_existing_rows():
    logger = _make_logger(has_complexity_column=False)
    logger._table_exists = lambda table_name: table_name == "workflow_reactions"  # type: ignore[method-assign]

    logger.save_workflow_reactions(
        "wf",
        [
            {
                "reaction_key": "comment_resume",
                "event_type": "external.comment.received",
                "action": {"type": "resume_job"},
                "enabled": True,
            }
        ],
    )

    executed_sql = [sql for sql, _ in logger.conn.calls]
    insert_index = next(
        index for index, sql in enumerate(executed_sql) if "INSERT INTO workflow_reactions" in sql
    )
    assert "registry_workflows" in executed_sql[0] and "FOR UPDATE" in executed_sql[0]
    assert "ON CONFLICT (workflow_id, reaction_key)" in executed_sql[insert_index]
    assert insert_index < len(executed_sql) - 1
    assert "DELETE FROM workflow_reactions" in executed_sql[-1]
    assert "reaction_key <> ALL(%s)" in executed_sql[-1]


def test_save_notification_routes_upserts_before_pruning_existing_rows():
    logger = _make_logger(has_complexity_column=False)
    logger._table_exists = lambda table_name: (
        table_name in {"notification_routes", "providers", "provider_types"}
    )  # type: ignore[method-assign]
    logger.conn.fetchone_results = [
        (19, True),
    ]

    logger.save_notification_routes(
        7,
        [
            {
                "route_key": "errors",
                "display_name": "Errors to Fixture Notifier",
                "provider_key": "fixture_notifier",
                "event_type": "job.failed",
                "severity": "error",
                "route_config": {"chat_id": "123"},
                "enabled": True,
            }
        ],
    )

    executed_sql = [sql for sql, _ in logger.conn.calls]
    assert any("SELECT p.id" in sql and "notification_provider" in sql for sql in executed_sql)
    assert any("INSERT INTO notification_routes" in sql for sql in executed_sql)
    assert any("ON CONFLICT (project_id, route_key)" in sql for sql in executed_sql)
    assert "DELETE FROM notification_routes" in executed_sql[-1]
    assert "route_key <> ALL(%s)" in executed_sql[-1]


def test_list_notification_routes_tolerates_missing_account_column():
    logger = _make_logger(has_complexity_column=False)
    logger._table_exists = lambda table_name: table_name in {"notification_routes", "providers"}  # type: ignore[method-assign]
    logger._column_exists = lambda table_name, column_name: False  # type: ignore[method-assign]
    logger.conn.fetchall_results = [
        [
            {
                "route_id": 17,
                "project_id": 7,
                "account_id": None,
                "route_key": "errors",
                "display_name": "Errors to Fixture Notifier",
                "workflow_id": None,
                "provider_id": 19,
                "provider_key": "fixture_notifier",
                "provider_type_key": "notification_provider",
                "provider_display_name": "Fixture Notifier",
                "implementation_kind": "extension",
                "integration_key": "fixture_notifier",
                "provider_config": {"api_token": "secret"},
                "event_type": "job.failed",
                "severity": "error",
                "route_config_jsonb": {"chat_id": "123"},
                "enabled": True,
                "created_at": None,
                "updated_at": None,
            }
        ]
    ]

    rows = logger.list_notification_routes(7, enabled_only=False)

    sql, params = logger.conn.calls[-1]
    assert "NULL::text AS account_id" in sql
    assert "nr.account_id" not in sql
    assert params == (7,)
    assert rows[0]["account_id"] is None
    assert rows[0]["provider_key"] == "fixture_notifier"


def test_log_notification_delivery_inserts_delivery_row():
    logger = _make_logger(has_complexity_column=False)
    logger._table_exists = lambda table_name: (
        table_name in {"notification_delivery_logs", "providers"}
    )  # type: ignore[method-assign]
    logger.conn.fetchone_results = [
        {"id": 21},
        {
            "delivery_id": 77,
            "route_id": 4,
            "project_id": 7,
            "workflow_id": "weekly_report",
            "job_id": 55,
            "provider_id": 21,
            "event_type": "job.failed",
            "severity": "error",
            "status": "failed",
            "request_jsonb": {"foo": "bar"},
            "response_jsonb": {"ok": False},
            "provider_message_id": None,
            "error_text": "boom",
            "created_at": None,
        },
    ]

    row = logger.log_notification_delivery(
        route_id=4,
        project_id=7,
        workflow_id="weekly_report",
        job_id=55,
        provider_key="fixture_notifier",
        event_type="job.failed",
        severity="error",
        status="failed",
        request={"foo": "bar"},
        response={"ok": False},
        error_text="boom",
    )

    executed_sql = "\n".join(sql for sql, _ in logger.conn.calls)
    assert "INSERT INTO notification_delivery_logs" in executed_sql
    assert row["delivery_id"] == "77"
    assert row["provider_key"] == "fixture_notifier"
    assert row["status"] == "failed"


def test_log_notification_delivery_serializes_uuid_values_in_payloads():
    logger = _make_logger(has_complexity_column=False)
    logger._table_exists = lambda table_name: (
        table_name in {"notification_delivery_logs", "providers"}
    )  # type: ignore[method-assign]
    effect_id = uuid.uuid4()
    logger.conn.fetchone_results = [
        {"id": 21},
        {
            "delivery_id": 78,
            "route_id": 4,
            "project_id": 7,
            "workflow_id": "weekly_report",
            "job_id": None,
            "provider_id": 21,
            "event_type": "workflow.completed",
            "severity": "info",
            "status": "delivered",
            "request_jsonb": {"effect_id": str(effect_id)},
            "response_jsonb": {"external_effect": {"effect_id": str(effect_id)}},
            "provider_message_id": "message-1",
            "error_text": None,
            "created_at": None,
        },
    ]

    row = logger.log_notification_delivery(
        route_id=4,
        project_id=7,
        workflow_id="weekly_report",
        provider_key="fixture_notifier",
        event_type="workflow.completed",
        status="delivered",
        request={"effect_id": effect_id},
        response={"external_effect": {"effect_id": effect_id}},
        provider_message_id="message-1",
    )

    insert_call = next(
        (sql, params)
        for sql, params in logger.conn.calls
        if "INSERT INTO notification_delivery_logs" in sql
    )
    assert str(effect_id) in insert_call[1][8]
    assert str(effect_id) in insert_call[1][9]
    assert row["status"] == "delivered"


def test_list_notification_delivery_logs_returns_external_effect_id():
    logger = _make_logger(has_complexity_column=False)
    logger._table_exists = lambda table_name: (
        table_name
        in {  # type: ignore[method-assign]
            "notification_delivery_logs",
            "providers",
        }
    )
    logger._column_exists = lambda table_name, column_name: (  # type: ignore[method-assign]
        table_name == "notification_delivery_logs" and column_name == "external_effect_id"
    )
    logger.conn.fetchall_results = [
        [
            {
                "delivery_id": 77,
                "route_id": 4,
                "project_id": 7,
                "workflow_id": "weekly_report",
                "job_id": 55,
                "provider_id": 21,
                "provider_key": "example.host-fixture",
                "event_type": "job.failed",
                "severity": "error",
                "status": "failed",
                "request_jsonb": {"foo": "bar"},
                "response_jsonb": {"ok": False},
                "provider_message_id": None,
                "error_text": "boom",
                "external_effect_id": "eff_email_1",
                "created_at": None,
            }
        ]
    ]

    rows = logger.list_notification_delivery_logs(project_id=7, limit=10)

    assert rows[0]["external_effect_id"] == "eff_email_1"
    executed_sql = "\n".join(sql for sql, _ in logger.conn.calls)
    assert "ndl.external_effect_id" in executed_sql


def test_acquire_project_execution_lease_returns_none_when_slots_table_missing():
    logger = _make_logger(has_complexity_column=False)
    logger._table_exists = lambda _table_name: False  # type: ignore[method-assign]

    row = logger.acquire_project_execution_lease(
        project_id=7,
        worker_instance_id="worker-a",
        lease_token="token-a",
        ttl_seconds=45,
    )

    assert row is None
    assert logger.conn.calls == []


def test_release_project_execution_lease_returns_false_when_slots_table_missing():
    logger = _make_logger(has_complexity_column=False)
    logger._table_exists = lambda _table_name: False  # type: ignore[method-assign]

    released = logger.release_project_execution_lease(
        project_id=8,
        worker_instance_id="worker-b",
        lease_token="lease-1",
    )

    assert released is False
    assert logger.conn.calls == []


def test_acquire_project_execution_lease_uses_slots_table_when_available():
    logger = _make_logger(has_complexity_column=False)
    logger._table_exists = lambda table_name: (
        table_name
        in {  # type: ignore[method-assign]
            "project_execution_slots",
            "project_policies",
        }
    )
    logger.conn.fetchone_results = [
        None,
        {"concurrency_cap": 3},
        (1,),
        {
            "project_id": 9,
            "worker_instance_id": "worker-slot",
            "lease_token": "slot-token",
            "heartbeat_at": None,
            "expires_at": None,
            "acquired_at": None,
            "active_job_id": None,
            "drain_status": "acquired",
            "created_at": None,
            "updated_at": None,
        },
    ]

    row = logger.acquire_project_execution_lease(
        project_id=9,
        worker_instance_id="worker-slot",
        lease_token="slot-token",
        ttl_seconds=60,
    )

    executed_sql = "\n".join(sql for sql, _ in logger.conn.calls)
    assert "pg_advisory_lock" in executed_sql
    assert "DELETE FROM project_execution_slots" in executed_sql
    assert "SELECT concurrency_cap" in executed_sql
    assert "SELECT COUNT(*)" in executed_sql
    assert "INSERT INTO project_execution_slots" in executed_sql
    prune_calls = [
        (sql, params)
        for sql, params in logger.conn.calls
        if "DELETE FROM project_execution_slots" in sql
    ]
    assert len(prune_calls) == 1
    prune_sql, prune_params = prune_calls[0]
    assert "expires_at <= now()" in prune_sql
    assert "lease_job.status" not in prune_sql
    assert prune_params == ("9",)
    assert row["project_id"] == "9"
    assert row["worker_instance_id"] == "worker-slot"


def test_release_project_execution_lease_prefers_slots_table_when_available():
    logger = _make_logger(has_complexity_column=False)
    logger._table_exists = lambda table_name: table_name == "project_execution_slots"  # type: ignore[method-assign]

    released = logger.release_project_execution_lease(
        project_id=18,
        worker_instance_id="worker-slot",
        lease_token="slot-1",
    )

    executed_sql = logger.conn.calls[-1][0]
    assert "DELETE FROM project_execution_slots" in executed_sql
    assert released is True


def test_claim_external_agent_job_uses_skip_locked_and_marks_job_in_progress():
    logger = _make_logger(has_complexity_column=False)
    logger.conn.fetchone_results = [
        {
            "id": 41,
            "project_id": 7,
            "project_name": "Demo",
            "account_id": "acc_ops",
            "task_id": "TASK-41",
            "task_summary": "Review task",
            "task_source": "external_agent",
            "status": "pending",
            "queue_name": "",
            "selected_workflow": "wf_demo",
            "meta": {"external_agent": {"enabled": True}},
        }
    ]

    row = logger.claim_external_agent_job(
        project_id=7,
        worker_instance_id="worker-1",
        workflow_id="wf_demo",
        allowed_workflow_ids=["wf_demo"],
        claim_token="claim-1",
        agent_name="ext-worker",
    )

    executed_sql = "\n".join(sql for sql, _ in logger.conn.calls)
    assert "FOR UPDATE OF j SKIP LOCKED" in executed_sql
    assert "j.status = 'pending'" in executed_sql
    assert "j.meta->'external_agent'->>'enabled'" in executed_sql
    assert "UPDATE jobs" in executed_sql
    assert row["status"] == "in_progress"
    assert row["worker_id"] == "worker-1"
    assert row["meta"]["external_agent"]["claim_token"] == "claim-1"


def test_log_job_error_sanitizes_retryable_status_and_truncates_payloads():
    logger = _make_logger(has_complexity_column=False)
    logger._table_exists = lambda table_name: table_name == "job_errors"  # type: ignore[method-assign]
    logger.conn.fetchone_results = [
        {
            "id": 81,
            "job_id": 55,
            "source": "http",
            "category": "transient",
            "http_status": 503,
            "retryable": True,
            "error_message": "trimmed",
        }
    ]

    row = logger.log_job_error(
        55,
        source="HTTP",
        category="Transient",
        provider_name="provider-x",
        http_status="503",
        request_id="r" * 240,
        error_message="A" * 17000,
        response_excerpt="B" * 5000,
        traceback_text="C" * 33000,
        context_jsonb={"stage": "enqueue"},
        agent_id="agent-1" * 30,
        group_id="group-1" * 30,
        step_id="step-1" * 30,
        step_execution_id="not-a-uuid",
    )

    sql, params = logger.conn.calls[-1]
    assert "INSERT INTO job_errors" in sql
    assert params[6] == "http"
    assert params[7] == "transient"
    assert params[9] == 503
    assert params[13] is True
    assert len(params[12]) == 200
    assert len(params[14]) == 16000
    assert len(params[15]) == 4000
    assert len(params[16]) == 32000
    assert len(params[2]) == 120
    assert len(params[3]) == 120
    assert len(params[4]) == 120
    # A malformed execution id never reaches the uuid column.
    assert params[5] is None
    assert params[17] == '{"stage": "enqueue"}'
    assert row["job_id"] == 55


def test_log_job_error_redacts_unsafe_diagnostic_ids_for_persistence():
    logger = _make_logger(has_complexity_column=False)
    logger._table_exists = lambda table_name: table_name == "job_errors"  # type: ignore[method-assign]
    logger.conn.fetchone_results = [{"id": 82, "job_id": 55}]

    row = logger.log_job_error(
        55,
        source="provider",
        category="provider",
        provider_error_code="api_key=abcdefghijklmnopqrstuvwxyz",
        request_id="postgres://admin:secret@db/app",
        error_message="failure",
    )

    _, params = logger.conn.calls[-1]
    assert params[10] != "api_key=abcdefghijklmnopqrstuvwxyz"
    assert "postgres://" not in str(params[12])
    assert row["job_id"] == 55


def test_normalize_exception_error_infers_http_provider_and_request_metadata():
    class _ProviderError(RuntimeError):
        pass

    _ProviderError.__module__ = "openai.errors"

    class _Response:
        def __init__(self) -> None:
            self.status_code = 429
            self.headers = {"x-request-id": "req-123"}
            self.text = '{"error":"rate limit"}'

    err = _ProviderError("slow down")
    err.response = _Response()
    err.code = "rate_limit"
    err.type = "rate_limit_error"

    payload = PlatformLogger.normalize_exception_error(
        err,
        source_hint="unknown",
        category_hint="unknown",
        context={"stage": "enqueue_publish"},
    )

    assert payload["source"] == "http"
    assert payload["category"] == "transient"
    assert payload["provider_name"] == "openai"
    assert payload["provider_error_code"] == "rate_limit"
    assert payload["provider_error_type"] == "rate_limit_error"
    assert payload["http_status"] == 429
    assert payload["request_id"] == "req-123"
    assert payload["retryable"] is True
    assert payload["response_excerpt"] == '{"error":"rate limit"}'
    assert payload["context_jsonb"] == {"stage": "enqueue_publish"}
    assert "slow down" in str(payload["error_message"])


def test_get_provider_secret_returns_project_specific_value_first():
    logger = _make_logger(has_complexity_column=False)
    logger._table_exists = lambda table_name: table_name == "provider_secrets"  # type: ignore[method-assign]
    logger.conn.fetchone_results = [
        (17,),
        {
            "secret_value_ciphertext": "project-token",
            "secret_value_nonce": None,
            "key_version": "plain-v1",
        },
    ]

    value = logger.get_provider_secret(
        provider_key="sample_tasks",
        secret_key="api_token",
        project_id=7,
        include_global=True,
    )

    assert value == "project-token"
    executed_sql = "\n".join(sql for sql, _ in logger.conn.calls)
    assert "FROM provider_secrets" in executed_sql
    assert "project_id = %s" in executed_sql


def test_save_provider_secret_writes_provider_secret_row():
    import os

    import core.infrastructure.db.platform_logger as logger_module

    logger = _make_logger(has_complexity_column=False)
    logger._table_exists = lambda table_name: table_name == "provider_secrets"  # type: ignore[method-assign]
    logger.conn.fetchone_results = [(21,)]

    old_secret_key = os.environ.get("FS_SECRET_KEY")
    try:
        original_aesgcm = logger_module.AESGCM
        logger_module.AESGCM = _FakeAESGCM
        os.environ["FS_SECRET_KEY"] = "unit-test-secret-key"
        saved = logger.save_provider_secret(
            provider_key="fixture_notifier",
            secret_key="api_token",
            secret_value="secret-123",
            project_id=8,
            metadata={"scope": "project"},
        )
    finally:
        logger_module.AESGCM = original_aesgcm
        if old_secret_key is None:
            os.environ.pop("FS_SECRET_KEY", None)
        else:
            os.environ["FS_SECRET_KEY"] = old_secret_key

    assert saved is True
    executed_sql = "\n".join(sql for sql, _ in logger.conn.calls)
    assert "UPDATE provider_secrets" in executed_sql
    assert "INSERT INTO provider_secrets" in executed_sql


def test_save_provider_secret_requires_encryption_key_by_default(monkeypatch):
    logger = _make_logger(has_complexity_column=False)
    logger._table_exists = lambda table_name: table_name == "provider_secrets"  # type: ignore[method-assign]
    logger.conn.fetchone_results = [(21,)]
    monkeypatch.delenv("FS_SECRET_KEY", raising=False)
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)

    with pytest.raises(RuntimeError, match="FS_SECRET_KEY"):
        logger.save_provider_secret(
            provider_key="fixture_notifier",
            secret_key="api_token",
            secret_value="secret-123",
            project_id=8,
            metadata={"scope": "project"},
        )


def test_save_project_secret_writes_project_secret_row(monkeypatch):
    monkeypatch.setattr("core.infrastructure.db.platform_logger.AESGCM", _FakeAESGCM)
    logger = _make_logger(has_complexity_column=False)
    logger._table_exists = lambda table_name: table_name == "project_secrets"  # type: ignore[method-assign]
    monkeypatch.setenv("FS_SECRET_KEY", "unit-test-secret-key")

    saved = logger.save_project_secret(
        project_id=8,
        secret_key="api_token",
        secret_value="secret-123",
        metadata={"scope": "project"},
    )

    assert saved is True
    executed_sql = "\n".join(sql for sql, _ in logger.conn.calls)
    assert "UPDATE project_secrets" in executed_sql
    assert "INSERT INTO project_secrets" in executed_sql


def test_get_project_secret_returns_plain_value():
    logger = _make_logger(has_complexity_column=False)
    logger._table_exists = lambda table_name: table_name == "project_secrets"  # type: ignore[method-assign]
    logger.conn.fetchone_results = [
        {
            "secret_value_ciphertext": "project-token",
            "secret_value_nonce": None,
            "key_version": "plain-v1",
        },
    ]

    value = logger.get_project_secret(project_id=7, secret_key="api_token")

    assert value == "project-token"
    executed_sql = "\n".join(sql for sql, _ in logger.conn.calls)
    assert "FROM project_secrets" in executed_sql


def test_get_project_run_config_no_longer_falls_back_to_plaintext_project_row_db_dsn():
    logger = _make_logger(has_complexity_column=False)
    logger.get_project = lambda project_id: {  # type: ignore[method-assign]
        "id": int(project_id),
        "name": "Demo",
        "account_id": "acc_alpha",
        "tenant_id": "tenant_alpha",
        "workspace_id": "workspace_alpha",
        "project_root": "/host/demo",
        "container_root": "/target/demo",
        "git_root": "/target/demo",
        "default_branch": "main",
        "docker_compose_path": "",
        "db_schema": "public",
        "db_dsn": "postgresql://retired:plain@db:5432/app",
        "supported_extensions": [],
        "exclude_dirs": [],
        "exclude_files": [],
        "agents_config": {},
        "config": {},
        "watch_config": {},
        "project_rules": "",
    }
    logger.get_project_workflows = lambda *args, **kwargs: []  # type: ignore[method-assign]
    logger.get_project_policy = lambda *args, **kwargs: {}  # type: ignore[method-assign]
    logger.get_project_policy_checks = lambda *args, **kwargs: []  # type: ignore[method-assign]
    logger.get_project_provider_bindings = lambda *args, **kwargs: []  # type: ignore[method-assign]
    logger.get_project_secret = lambda **kwargs: ""  # type: ignore[method-assign]

    run_cfg = logger.get_project_run_config(7)

    assert "db_dsn" not in run_cfg
    assert "db_type" not in run_cfg
    assert run_cfg["account_id"] == "acc_alpha"
    assert run_cfg["tenant_id"] == "tenant_alpha"
    assert run_cfg["workspace_id"] == "workspace_alpha"


def test_get_project_run_config_does_not_embed_secret_db_dsn_in_runtime_payload():
    logger = _make_logger(has_complexity_column=False)
    logger.get_project = lambda project_id: {  # type: ignore[method-assign]
        "id": int(project_id),
        "name": "Demo",
        "account_id": "acc_alpha",
        "project_root": "/host/demo",
        "container_root": "/target/demo",
        "git_root": "/target/demo",
        "default_branch": "main",
        "docker_compose_path": "",
        "db_schema": "public",
        "db_dsn": "postgresql://retired:plain@db:5432/app",
        "supported_extensions": [],
        "exclude_dirs": [],
        "exclude_files": [],
        "agents_config": {},
        "config": {},
        "watch_config": {},
        "project_rules": "",
    }
    logger.get_project_workflows = lambda *args, **kwargs: []  # type: ignore[method-assign]
    logger.get_project_policy = lambda *args, **kwargs: {}  # type: ignore[method-assign]
    logger.get_project_policy_checks = lambda *args, **kwargs: []  # type: ignore[method-assign]
    logger.get_project_provider_bindings = lambda *args, **kwargs: []  # type: ignore[method-assign]
    logger.get_project_secret = MagicMock(return_value="postgresql://secure:secret@db:5432/app")  # type: ignore[method-assign]

    run_cfg = logger.get_project_run_config(7)

    assert "db_dsn" not in run_cfg
    assert "db_type" not in run_cfg
    logger.get_project_secret.assert_not_called()


def test_add_project_drops_retired_db_dsn_write():
    logger = _make_logger(has_complexity_column=False)
    logger._table_exists = lambda _table_name: False  # type: ignore[method-assign]

    with pytest.raises(TypeError):
        logger.add_project(
            name="demo",
            db_dsn="postgresql://retired-inline",
            workspace_id=DEFAULT_WORKSPACE_ID,
        )


def test_update_project_drops_retired_db_dsn_write():
    logger = _make_logger(has_complexity_column=False)
    logger._table_exists = lambda _table_name: False  # type: ignore[method-assign]

    with pytest.raises(TypeError):
        logger.update_project(
            7,
            name="demo",
            db_dsn="postgresql://retired-inline",
        )


def test_attachment_schema_validation_fails_when_crypto_columns_missing():
    logger = _make_logger(has_complexity_column=False)
    logger._table_exists = lambda table_name: table_name == "job_attachments"  # type: ignore[method-assign]
    logger._column_exists = lambda table_name, column_name: bool(  # type: ignore[method-assign]
        table_name == "job_attachments" and column_name == "content_ciphertext"
    )

    with pytest.raises(RuntimeError, match="Attachment encryption schema is incomplete"):
        logger._validate_attachment_encryption_schema()


def test_save_files_writes_encrypted_blob_columns(monkeypatch):
    monkeypatch.setattr("core.infrastructure.db.platform_logger.AESGCM", _FakeAESGCM)
    logger = _make_logger(has_complexity_column=False)
    monkeypatch.setenv("FS_SECRET_KEY", "unit-test-secret-key")
    logger._column_exists = lambda table_name, column_name: bool(  # type: ignore[method-assign]
        table_name == "job_attachments"
        and column_name in {"content_ciphertext", "content_nonce", "key_version"}
    )
    logger.conn.fetchone_results = [(0,), (123,)]

    rows = logger.save_files(
        job_id=55,
        files=[
            {
                "filename": "notes.txt",
                "mime_type": "text/plain",
                "size_bytes": 4,
                "content": b"data",
            }
        ],
    )

    assert rows[0]["filename"] == "notes.txt"
    assert rows[0]["content_type"] == "text/plain"
    assert rows[0]["source_ref"] == "job_attachment:123"
    lock_sql, lock_params = logger.conn.calls[0]
    order_sql, order_params = logger.conn.calls[1]
    sql, _params = logger.conn.calls[2]
    unlock_sql, unlock_params = logger.conn.calls[3]
    assert "pg_advisory_lock" in lock_sql
    assert lock_params == ("job_attachments_upload_order", "55")
    assert "SELECT COALESCE(MAX(upload_order) + 1, 0)" in order_sql
    assert order_params == (55,)
    assert "content_ciphertext" in sql
    assert "content_nonce" in sql
    assert "upload_order" in sql
    assert "VALUES (%s, %s, %s, %s, %s, NULL, %s, %s, %s)" in sql
    assert "pg_advisory_unlock" in unlock_sql
    assert unlock_params == lock_params


def test_save_files_continues_upload_order_across_batches(monkeypatch):
    monkeypatch.setattr("core.infrastructure.db.platform_logger.AESGCM", _FakeAESGCM)
    logger = _make_logger(has_complexity_column=False)
    monkeypatch.setenv("FS_SECRET_KEY", "unit-test-secret-key")
    logger._column_exists = lambda table_name, column_name: bool(  # type: ignore[method-assign]
        table_name == "job_attachments"
        and column_name in {"content_ciphertext", "content_nonce", "key_version"}
    )
    logger.conn.fetchone_results = [(0,), ("attachment-a",), (2,), ("attachment-b",)]

    logger.save_files(
        job_id="11111111-1111-1111-1111-111111111111",
        files=[
            {
                "filename": "first.txt",
                "mime_type": "text/plain",
                "size_bytes": 5,
                "content": b"first",
            }
        ],
    )
    logger.save_files(
        job_id="11111111-1111-1111-1111-111111111111",
        files=[
            {
                "filename": "second.txt",
                "mime_type": "text/plain",
                "size_bytes": 6,
                "content": b"second",
            }
        ],
    )

    insert_params = [
        params for sql, params in logger.conn.calls if "INSERT INTO job_attachments" in sql
    ]
    lock_calls = [(sql, params) for sql, params in logger.conn.calls if "pg_advisory_lock" in sql]
    unlock_calls = [
        (sql, params) for sql, params in logger.conn.calls if "pg_advisory_unlock" in sql
    ]
    assert len(insert_params) == 2
    assert len(lock_calls) == 2
    assert len(unlock_calls) == 2
    assert lock_calls[0][1] == (
        "job_attachments_upload_order",
        "11111111-1111-1111-1111-111111111111",
    )
    assert unlock_calls[-1][1] == lock_calls[0][1]
    assert insert_params[0][1] == 0
    assert insert_params[1][1] == 2


def test_get_attachments_decrypts_encrypted_blob_rows(monkeypatch):
    from core.infrastructure.db.platform_logger import _encrypt_attachment_value

    monkeypatch.setattr("core.infrastructure.db.platform_logger.AESGCM", _FakeAESGCM)
    logger = _make_logger(has_complexity_column=False)
    monkeypatch.setenv("FS_SECRET_KEY", "unit-test-secret-key")
    logger._column_exists = lambda table_name, column_name: bool(  # type: ignore[method-assign]
        table_name == "job_attachments"
        and column_name in {"content_ciphertext", "content_nonce", "key_version"}
    )
    ciphertext, nonce_value, key_version = _encrypt_attachment_value(b"payload-42")
    logger.conn.fetchall_results = [
        [
            {
                "id": 1,
                "filename": "notes.txt",
                "mime_type": "text/plain",
                "size_bytes": 10,
                "content": None,
                "content_ciphertext": ciphertext,
                "content_nonce": nonce_value,
                "key_version": key_version,
            }
        ]
    ]

    rows = logger.get_attachments(55)
    sql, _params = logger.conn.calls[-1]

    assert rows == [
        {
            "id": 1,
            "filename": "notes.txt",
            "mime_type": "text/plain",
            "size_bytes": 10,
            "content": b"payload-42",
        }
    ]
    assert "ORDER BY upload_order ASC, uploaded_at ASC, id ASC" in sql


def test_build_job_trace_normalizes_tool_events_into_tool_layer():
    logger = _make_logger(has_complexity_column=False)
    logger.get_job = lambda job_id: {  # type: ignore[method-assign]
        "id": int(job_id),
        "project_id": 7,
        "status": "in_progress",
        "task_summary": "Trace tool path",
        "meta": {},
    }
    logger.get_project = lambda project_id: {  # type: ignore[method-assign]
        "id": int(project_id),
        "name": "Demo",
        "account_id": "acc_alpha",
    }
    logger.get_logs = lambda *args, **kwargs: []  # type: ignore[method-assign]
    logger.get_events = lambda *args, **kwargs: [  # type: ignore[method-assign]
        {
            "id": 1,
            "timestamp": "2026-03-24T10:00:00+00:00",
            "phase": "delivery",
            "agent": "composer",
            "event_type": "tool_call",
            "severity": "info",
            "title": "Tool call: git_status",
            "detail": {
                "tool_name": "git_status",
                "tool_call_id": "call-1",
                "args_excerpt": "{}",
            },
        },
        {
            "id": 2,
            "timestamp": "2026-03-24T10:00:01+00:00",
            "phase": "delivery",
            "agent": "composer",
            "event_type": "tool_result",
            "severity": "info",
            "title": "Tool result: git_status",
            "detail": {
                "tool_name": "git_status",
                "tool_call_id": "call-1",
                "result_excerpt": "clean",
                "is_error": False,
            },
        },
    ]
    logger.get_agent_messages = lambda *args, **kwargs: []  # type: ignore[method-assign]
    logger.list_extension_invocations = lambda *args, **kwargs: []  # type: ignore[method-assign]
    logger.list_domain_events = lambda *args, **kwargs: []  # type: ignore[method-assign]
    logger.list_event_dispatches = lambda *args, **kwargs: []  # type: ignore[method-assign]
    logger.list_job_errors = lambda *args, **kwargs: []  # type: ignore[method-assign]

    payload = logger.build_job_trace(42, account_id="acc_alpha")

    assert payload is not None
    tool_spans = [row for row in payload["spans"] if row["layer"] == "tool"]
    assert len(tool_spans) == 2
    assert tool_spans[0]["span_id"] == "tool:call-1"
    assert tool_spans[0]["attributes"]["tool_call_id"] == "call-1"
    assert tool_spans[1]["parent_span_id"] == "tool:call-1"
    assert tool_spans[1]["name"] == "git_status"
    assert tool_spans[1]["status"] == "done"


def test_observability_store_builds_trace_from_authorized_job_projection():
    store = PlatformObservabilityStore.__new__(PlatformObservabilityStore)
    store.get_logs = lambda *args, **kwargs: []  # type: ignore[method-assign]
    store.get_events = lambda *args, **kwargs: []  # type: ignore[method-assign]
    store.get_agent_messages = lambda *args, **kwargs: []  # type: ignore[method-assign]
    store.list_extension_invocations = lambda *args, **kwargs: []  # type: ignore[method-assign]
    store.list_domain_events = lambda *args, **kwargs: []  # type: ignore[method-assign]
    store.list_event_dispatches = lambda *args, **kwargs: []  # type: ignore[method-assign]
    store.list_job_errors = lambda *args, **kwargs: []  # type: ignore[method-assign]

    payload = store.build_job_trace(
        "00000000-0000-0000-0000-000000000042",
        account_id="acc_alpha",
        job={
            "id": "00000000-0000-0000-0000-000000000042",
            "project_id": "00000000-0000-0000-0000-000000000007",
            "project_name": "Demo",
            "account_id": "acc_alpha",
            "status": "in_progress",
            "task_summary": "Trace an authorized job",
            "meta": {},
        },
    )

    assert payload is not None
    assert payload["account_id"] == "acc_alpha"
    assert payload["project_id"] == "00000000-0000-0000-0000-000000000007"
    assert payload["spans"][0]["name"] == "Trace an authorized job"


def test_build_job_trace_captures_end_to_end_observability_chain():
    logger = _make_logger(has_complexity_column=False)
    logger.get_job = lambda job_id: {  # type: ignore[method-assign]
        "id": int(job_id),
        "project_id": 7,
        "status": "succeeded",
        "task_summary": "End-to-end trace",
        "queued_at": "2026-03-27T09:00:00+00:00",
        "started_at": "2026-03-27T09:00:01+00:00",
        "finished_at": "2026-03-27T09:00:10+00:00",
        "meta": {"trace_id": "job:91", "root_run_id": "run-root"},
    }
    logger.get_project = lambda project_id: {  # type: ignore[method-assign]
        "id": int(project_id),
        "name": "Demo",
        "account_id": "acc_alpha",
    }
    logger.get_logs = lambda *args, **kwargs: [  # type: ignore[method-assign]
        {
            "id": 10,
            "timestamp": "2026-03-27T09:00:01+00:00",
            "action_type": "node_ingest",
            "status": "running",
            "agent": "coordinator",
            "meta": {
                "run_id": "run-ingest",
                "parent_run_id": "run-root",
                "correlation_id": "corr-1",
            },
            "payload": {"source": "provider_webhook"},
            "result": {},
        },
        {
            "id": 11,
            "timestamp": "2026-03-27T09:00:04+00:00",
            "action_type": "node_developer",
            "status": "succeeded",
            "agent": "composer",
            "meta": {
                "run_id": "run-workflow",
                "parent_run_id": "run-root",
                "correlation_id": "corr-1",
            },
            "payload": {"step": "draft_summary"},
            "result": {"status": "ok"},
        },
    ]
    logger.get_events = lambda *args, **kwargs: [  # type: ignore[method-assign]
        {
            "id": 21,
            "timestamp": "2026-03-27T09:00:05+00:00",
            "phase": "delivery",
            "agent": "composer",
            "event_type": "tool_call",
            "severity": "info",
            "title": "Tool call: run_tests",
            "detail": {
                "tool_name": "run_tests",
                "tool_call_id": "call-77",
                "correlation_id": "corr-1",
            },
        },
        {
            "id": 22,
            "timestamp": "2026-03-27T09:00:06+00:00",
            "phase": "delivery",
            "agent": "composer",
            "event_type": "tool_result",
            "severity": "info",
            "title": "Tool result: run_tests",
            "detail": {
                "tool_name": "run_tests",
                "tool_call_id": "call-77",
                "correlation_id": "corr-1",
                "is_error": False,
            },
        },
    ]
    logger.get_agent_messages = lambda *args, **kwargs: [  # type: ignore[method-assign]
        {
            "id": 31,
            "timestamp": "2026-03-27T09:00:03+00:00",
            "from_agent": "scout",
            "to_agent": "composer",
            "step_id": "delivery",
            "skill": "draft_summary",
            "status": "sent",
            "summary": "Implement and validate story",
            "correlation_id": "corr-1",
        }
    ]
    logger.list_extension_invocations = lambda *args, **kwargs: []  # type: ignore[method-assign]
    logger.list_domain_events = lambda *args, **kwargs: [  # type: ignore[method-assign]
        {
            "id": 41,
            "event_id": "evt-1",
            "event_type": "job.completed",
            "status": "succeeded",
            "severity": "info",
            "summary": "Workflow completed",
            "group_id": "delivery",
            "step_id": "publish",
            "payload_jsonb": {"outcome": "success"},
            "source": "orchestrator",
            "occurred_at": "2026-03-27T09:00:08+00:00",
            "created_at": "2026-03-27T09:00:08+00:00",
            "correlation_id": "corr-1",
            "envelope_jsonb": {"metadata": {"trace_id": "job:91"}},
            "account_id": "acc_alpha",
            "project_id": 7,
        }
    ]
    logger.list_event_dispatches = lambda *args, **kwargs: [  # type: ignore[method-assign]
        {
            "id": 51,
            "dispatch_id": "dispatch-1",
            "target_type": "webhook",
            "target_id": "ops",
            "target_label": "webhook:ops",
            "status": "succeeded",
            "reason_code": "",
            "reason_detail": "",
            "attempt_count": 1,
            "created_at": "2026-03-27T09:00:09+00:00",
        }
    ]
    logger.list_job_errors = lambda *args, **kwargs: []  # type: ignore[method-assign]

    payload = logger.build_job_trace(91, account_id="acc_alpha")

    assert payload is not None
    layers = {span["layer"] for span in payload["spans"]}
    assert {"job", "node", "prompt", "tool", "event", "dispatch"}.issubset(layers)

    root = next(span for span in payload["spans"] if span["span_id"] == "job:91:root")
    assert root["status"] == "succeeded"

    event_span = next(
        span
        for span in payload["spans"]
        if span["layer"] == "event" and span["name"] == "job.completed"
    )
    dispatch_span = next(
        span
        for span in payload["spans"]
        if span["layer"] == "dispatch" and span["span_id"] == "dispatch:dispatch-1"
    )
    assert event_span["status"] == "succeeded"
    assert dispatch_span["parent_span_id"] == event_span["span_id"]
    assert payload["status"] == "succeeded"
    assert "corr-1" in payload["linkage"]["correlation_ids"]
    assert "run-workflow" in payload["linkage"]["run_ids"]
