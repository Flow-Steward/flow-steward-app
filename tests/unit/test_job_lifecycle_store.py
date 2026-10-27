from __future__ import annotations

import uuid

from core.application.auth.config import DEFAULT_ACCOUNT_ID, DEFAULT_TENANT_ID, DEFAULT_WORKSPACE_ID
from core.application.jobs.dispatch_outbox_repository import DispatchOutboxRepository
from core.application.jobs.job_submission_repository import JobSubmissionRepository
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


def _make_logger(has_complexity_column: bool = True) -> PlatformLogger:
    logger = PlatformLogger.__new__(PlatformLogger)
    logger.conn = _RecordingConn()
    logger._column_exists_cache = {}
    scope_columns = {"tenant_id", "account_id", "workspace_id"}
    logger._column_exists = lambda table_name, column_name: bool(  # type: ignore[method-assign]
        table_name == "jobs"
        and (
            column_name in scope_columns or (has_complexity_column and column_name == "complexity")
        )
    )
    return logger


def test_create_job_copies_scope_from_project_row() -> None:
    logger = _make_logger()
    workspace_id = "00000000-0000-0000-0000-000000000099"
    logger.conn.fetchone_results = [
        {
            "tenant_id": DEFAULT_TENANT_ID,
            "account_id": DEFAULT_ACCOUNT_ID,
            "workspace_id": workspace_id,
        },
        ("00000000-0000-0000-0000-000000000123",),
    ]

    job_id = logger.create_job(
        project_id="00000000-0000-0000-0000-000000000077",
        task_id="task-1",
        task_summary="Run task",
    )

    assert job_id == "00000000-0000-0000-0000-000000000123"
    insert_sql, insert_params = logger.conn.calls[-1]
    assert "tenant_id, account_id, workspace_id, project_id" in insert_sql
    assert insert_params[:4] == (
        DEFAULT_TENANT_ID,
        DEFAULT_ACCOUNT_ID,
        workspace_id,
        "00000000-0000-0000-0000-000000000077",
    )


def test_create_job_uses_default_scope_only_when_project_scope_missing() -> None:
    logger = _make_logger()
    logger.conn.fetchone_results = [
        {},
        ("00000000-0000-0000-0000-000000000124",),
    ]

    logger.create_job(project_id="00000000-0000-0000-0000-000000000078", task_id="task-2")

    _sql, insert_params = logger.conn.calls[-1]
    assert insert_params[:3] == (DEFAULT_TENANT_ID, DEFAULT_ACCOUNT_ID, DEFAULT_WORKSPACE_ID)


def test_create_job_serializes_uuid_values_in_meta_snapshot() -> None:
    logger = _make_logger()
    project_id = uuid.UUID("58eeaeb3-a9c1-4c51-b292-14eb86899d86")
    logger.conn.fetchone_results = [
        {
            "tenant_id": DEFAULT_TENANT_ID,
            "account_id": DEFAULT_ACCOUNT_ID,
            "workspace_id": DEFAULT_WORKSPACE_ID,
        },
        ("00000000-0000-0000-0000-000000000125",),
    ]

    logger.create_job(
        project_id=project_id,
        task_id="task-uuid",
        meta={"project_workflows": [{"project_id": project_id}]},
    )

    _sql, insert_params = logger.conn.calls[-1]
    assert str(project_id) in insert_params[-1]


def test_create_job_persists_text_workflow_identity_in_canonical_key_column() -> None:
    logger = _make_logger()
    logger._column_exists = lambda table_name, column_name: bool(  # type: ignore[method-assign]
        table_name == "jobs"
        and column_name
        in {
            "tenant_id",
            "account_id",
            "workspace_id",
            "complexity",
            "selected_workflow_key",
        }
    )
    logger.conn.fetchone_results = [
        {
            "tenant_id": DEFAULT_TENANT_ID,
            "account_id": DEFAULT_ACCOUNT_ID,
            "workspace_id": DEFAULT_WORKSPACE_ID,
        },
        ("00000000-0000-0000-0000-000000000132",),
    ]

    logger.create_job(
        project_id="00000000-0000-0000-0000-000000000078",
        task_id="task-workflow-key",
        selected_workflow_key="wf.catalog-sync",
    )

    insert_sql, insert_params = logger.conn.calls[-1]
    assert "selected_workflow_key" in insert_sql
    assert "selected_workflow_id" not in insert_sql
    assert "wf.catalog-sync" in insert_params


def test_submission_repository_persists_encrypted_dispatch_outbox_with_job() -> None:
    logger = _make_logger()
    job_id = "00000000-0000-0000-0000-000000000126"
    logger.conn.fetchone_results = [
        {
            "tenant_id": DEFAULT_TENANT_ID,
            "account_id": DEFAULT_ACCOUNT_ID,
            "workspace_id": DEFAULT_WORKSPACE_ID,
        },
        (job_id,),
    ]

    assert (
        JobSubmissionRepository(logger, logger.conn).create(
            encrypted_envelope="encrypted-envelope",
            outbox_status="pending",
            project_id="00000000-0000-0000-0000-000000000078",
            task_id="task-outbox",
            meta={"safe": True},
        )
        == job_id
    )

    executed_sql = "\n".join(sql for sql, _params in logger.conn.calls)
    assert "INSERT INTO jobs" in executed_sql
    assert "INSERT INTO job_dispatch_outbox" in executed_sql
    assert logger.conn.calls[-1][1] == (
        job_id,
        "encrypted-envelope",
        "pending",
        None,
        None,
        300,
    )


def test_submission_repository_can_hold_attachment_dispatch_in_preparing_state() -> None:
    logger = _make_logger()
    job_id = "00000000-0000-0000-0000-000000000131"
    logger.conn.fetchone_results = [
        {
            "tenant_id": DEFAULT_TENANT_ID,
            "account_id": DEFAULT_ACCOUNT_ID,
            "workspace_id": DEFAULT_WORKSPACE_ID,
        },
        (job_id,),
    ]

    assert (
        JobSubmissionRepository(logger, logger.conn).create(
            encrypted_envelope="initial-envelope",
            outbox_status="preparing",
            preparation_owner_token="owner-1",
            preparation_lease_seconds=300,
            project_id="00000000-0000-0000-0000-000000000078",
            task_id="task-preparing",
            meta={"task_envelope": {"payload_complete": False}},
        )
        == job_id
    )

    sql, params = logger.conn.calls[-1]
    assert "status" in sql
    assert params == (job_id, "initial-envelope", "preparing", "owner-1", "owner-1", 300)


def test_renew_preparing_dispatch_lease_is_owner_fenced() -> None:
    logger = _make_logger()
    logger.conn.fetchone_results = [("job-1",)]

    assert DispatchOutboxRepository(logger.conn).renew_preparation_lease(
        job_id="job-1",
        owner_token="owner-1",
        lease_seconds=300,
    )

    sql, params = logger.conn.calls[-1]
    assert "preparation_owner_token = %s" in sql
    assert "preparation_lease_expires_at > now()" in sql
    assert params == (300, "job-1", "owner-1")


def test_finalize_job_dispatch_payload_updates_job_and_outbox_together() -> None:
    logger = _make_logger()
    logger.conn.fetchone_results = [("job-1",), ("job-1",), ("job-1",)]

    assert DispatchOutboxRepository(logger.conn).finalize_prepared_payload(
        job_id="job-1",
        meta_patch={
            "files": [{"artifact_id": "art-1"}],
            "task_envelope": {"payload_complete": True},
        },
        encrypted_envelope="final-envelope",
        preparation_owner_token="owner-1",
    )

    sql = "\n".join(statement for statement, _params in logger.conn.calls)
    assert "attachment_persistence_artifacts" in sql
    assert "status = 'pending'" in sql
    assert "status = 'preparing'" in sql
    assert logger.conn.calls[-1][1] == ("final-envelope", "job-1", "owner-1")


def test_claim_expired_preparing_dispatches_leases_cleanup_rows() -> None:
    logger = _make_logger()
    logger.conn.fetchall_results = [
        [
            (
                "job-1",
                {"attachment_persistence_artifacts": [{"artifact_id": "art-1"}]},
                "cleanup-owner-1",
            )
        ]
    ]

    rows = DispatchOutboxRepository(logger.conn).claim_expired_preparing(
        limit=25,
        lease_seconds=60,
    )

    assert rows == [
        {
            "job_id": "job-1",
            "meta": {"attachment_persistence_artifacts": [{"artifact_id": "art-1"}]},
            "owner_token": "cleanup-owner-1",
        }
    ]
    sql, params = logger.conn.calls[-1]
    assert "status = 'cleanup_pending'" in sql
    assert "status = 'preparing'" in sql
    assert "preparation_lease_expires_at <= now()" in sql
    assert "created_at <=" not in sql
    assert params == (25, 60, 60)


def test_claim_dispatch_outbox_is_leased_and_fenced_to_unstarted_jobs() -> None:
    logger = _make_logger()
    logger.conn.fetchall_results = [[("job-1",), ("job-2",)]]

    claimed = DispatchOutboxRepository(logger.conn).claim_pending(limit=2, lease_seconds=45)

    assert claimed == ["job-1", "job-2"]
    sql, params = logger.conn.calls[-1]
    assert "FOR UPDATE OF outbox SKIP LOCKED" in sql
    assert "jobs.status = 'pending'" in sql
    assert "NULLIF(jobs.worker_id, '') IS NULL" in sql
    assert "('not_started', 'attempted')" in sql
    assert "payload_complete" in sql
    assert params == (2, 45)


def test_settle_dispatch_outbox_after_execution_advanced_is_atomic() -> None:
    logger = _make_logger()
    logger.conn.fetchone_results = [("job-1",)]

    assert DispatchOutboxRepository(logger.conn).settle_after_execution_advanced(job_id="job-1")

    sql, params = logger.conn.calls[-1]
    assert "UPDATE job_dispatch_outbox AS outbox" in sql
    assert "JOIN jobs" not in sql
    assert "jobs.worker_id" in sql
    assert "delivery_succeeded" in sql
    assert "outbox.status = 'pending'" in sql
    assert params == ("job-1",)


def test_replace_dispatch_outbox_envelope_only_while_pending() -> None:
    logger = _make_logger()
    logger.conn.rowcount_results = [1]

    assert DispatchOutboxRepository(logger.conn).replace_envelope(
        job_id="job-1",
        encrypted_envelope="final-encrypted-envelope",
    )

    sql, params = logger.conn.calls[-1]
    assert "UPDATE job_dispatch_outbox" in sql
    assert "status = 'pending'" in sql
    assert params == ("final-encrypted-envelope", "job-1")


def test_merge_job_meta_merges_nested_dict_patch() -> None:
    logger = _make_logger()
    logger.conn.fetchone_results = [{"meta": {"trace": {"root_run_id": "old"}, "simple": 1}}]

    logger.merge_job_meta(9, {"trace": {"span_id": "new"}, "extra": True})

    sql, params = logger.conn.calls[-1]
    assert "UPDATE jobs SET meta = %s" in sql
    assert '"span_id": "new"' in params[0]
    assert '"root_run_id": "old"' in params[0]
    assert '"extra": true' in params[0]


def test_queue_delivery_marker_is_conditional_on_no_worker_claim() -> None:
    logger = _make_logger()
    logger.conn.fetchone_results = [(9,)]

    persisted = logger.update_queue_delivery_meta(
        9,
        queue_delivery={"state": "queued", "dispatch_state": "publisher_confirmed"},
        runtime_journal=[{"event": "queue_publisher_confirmed"}],
        expected_dispatch_states={"attempted"},
    )

    sql, params = logger.conn.calls[-1]
    assert persisted is True
    assert "jsonb_set" in sql
    assert "delivery_succeeded" in sql
    assert "status = 'pending'" in sql
    assert "worker_id" in sql
    assert params[3] == 9

    logger.conn.fetchone_results = [None]
    assert (
        logger.update_queue_delivery_meta(
            9,
            queue_delivery={"state": "queued"},
            runtime_journal=[],
            expected_dispatch_states={"attempted"},
        )
        is False
    )


def test_queue_delivery_failure_allows_attempted_to_attempted_cas() -> None:
    logger = _make_logger()
    logger.conn.fetchone_results = [(9,)]

    assert logger.update_queue_delivery_meta(
        9,
        queue_delivery={"state": "retrying_delivery", "dispatch_state": "attempted"},
        runtime_journal=[{"event": "queue_delivery_failed"}],
        expected_dispatch_states={"attempted"},
    )

    sql, params = logger.conn.calls[-1]
    assert "dispatch_state" in sql
    assert params[-1] == ["attempted"]


def test_dispatch_outbox_sweep_settles_advanced_pending_rows() -> None:
    logger = _make_logger()
    logger.conn.fetchone_results = [(3,)]

    assert DispatchOutboxRepository(logger.conn).sweep_advanced_pending(limit=50) == 3

    sql, params = logger.conn.calls[-1]
    assert "status = 'superseded'" in sql
    assert "jobs.status <> 'pending'" in sql
    assert params == (50,)


def test_update_job_runtime_sets_runtime_fields_and_clears_error() -> None:
    logger = _make_logger()

    logger.update_job_runtime(
        9,
        selected_workflow="wf-1",
        current_step_id="step-1",
        current_group_id="group-1",
        current_agent="composer",
        heartbeat_now=True,
        clear_last_error=True,
        last_response_excerpt="ok",
    )

    sql, params = logger.conn.calls[-1]
    assert "selected_workflow = %s" in sql
    assert "current_step_id = %s" in sql
    assert "current_group_id = %s" in sql
    assert "current_agent = %s" in sql
    assert "heartbeat_at = NOW()" in sql
    assert "last_error = NULL" in sql
    assert "last_response_excerpt = %s" in sql
    assert params == ("wf-1", "step-1", "group-1", "composer", "ok", 9)


def test_update_job_runtime_writes_the_step_scope_as_one_pair() -> None:
    logger = _make_logger()

    logger.update_job_runtime(9, current_step_id="root-step")
    sql, params = logger.conn.calls[-1]
    # A root step clears a group left over from a previous grouped step.
    assert "current_step_id = %s" in sql
    assert "current_group_id = %s" in sql
    assert params == ("root-step", None, 9)

    calls_before = len(logger.conn.calls)
    logger.update_job_runtime(9, current_group_id="orphan-group")
    # A group is never written without its step.
    assert len(logger.conn.calls) == calls_before


def test_job_lifecycle_never_persists_undocumented_error_status() -> None:
    logger = _make_logger()

    logger.update_job(9, status="error")
    logger.finish_job(9, status="error", error="approval resume failed")

    status_writes = [
        params
        for sql, params in logger.conn.calls
        if "UPDATE jobs SET" in sql and isinstance(params, tuple) and params
    ]
    assert status_writes[0] == ("failed", 9)
    assert status_writes[1][0] == "failed"


def test_recover_in_progress_job_fences_expected_worker() -> None:
    logger = _make_logger()

    assert logger.recover_in_progress_job(
        "job-9",
        expected_worker_id="crashed-worker",
        reason="owner lease expired",
    )

    sql, params = logger.conn.calls[-1]
    assert "status = 'orphaned'" in sql
    assert "status = 'in_progress'" in sql
    assert "worker_id = %s" in sql
    assert params == ("owner lease expired", "job-9", "crashed-worker")


def test_finish_job_updates_terminal_state_and_runtime_callbacks() -> None:
    logger = _make_logger()
    logger.conn.fetchone_results = [{"meta": {}}]
    logger._table_exists = lambda table_name: table_name == "domain_event_outbox"  # type: ignore[method-assign]
    logger._column_exists = lambda table_name, column_name: bool(  # type: ignore[method-assign]
        table_name == "domain_event_outbox"
        and column_name
        in {
            "account_id",
            "job_id",
            "severity",
            "summary",
            "event_envelope_jsonb",
            "event_id",
        }
    )

    logger.finish_job(9, status="failed", final_report="report", error="boom", root_run_id="run-1")

    executed_sql = "\n".join(sql for sql, _ in logger.conn.calls)
    assert "UPDATE jobs SET" in executed_sql
    assert "SELECT meta FROM jobs WHERE id = %s FOR UPDATE" in executed_sql
    assert "INSERT INTO domain_event_outbox" in executed_sql
    outbox_params = next(
        params for sql, params in logger.conn.calls if "INSERT INTO domain_event_outbox" in sql
    )
    assert "job.terminal.memory_curation_requested" in outbox_params
    assert "job-terminal-memory:9" in outbox_params
    assert any(
        params == (None, "boom", 9)
        for _sql, params in logger.conn.calls
        if isinstance(params, tuple)
    )
