"""Durable resume retry limits and cancellation fences against PostgreSQL."""

from __future__ import annotations

import json
import uuid
from types import SimpleNamespace

import psycopg
import pytest

from core.application.jobs.execution_contracts import TaskGraphOutcome
from core.application.jobs.workflow_resume_repository import WorkflowResumeRepositoryAdapter
from core.infrastructure.db.platform_stores import PlatformControlScheduleStore, PlatformWorkerStore
from core.infrastructure.runtime.control import scans
from core.infrastructure.runtime.worker.worker_execution_adapters import (
    WorkerTerminalEffectsAdapter,
)

pytestmark = pytest.mark.integration


@pytest.fixture
def runtime_db(disposable_db):
    conn = psycopg.connect(disposable_db.dsn)
    store = PlatformWorkerStore(disposable_db.dsn)
    seeded_projects = []
    try:
        yield SimpleNamespace(conn=conn, store=store, seeded_projects=seeded_projects)
    finally:
        conn.rollback()
        for project_id in seeded_projects:
            conn.execute(
                "DELETE FROM platform_action_log WHERE job_id IN "
                "(SELECT id FROM jobs WHERE project_id = %s)",
                (project_id,),
            )
            conn.execute(
                "DELETE FROM platform_events WHERE job_id IN "
                "(SELECT id FROM jobs WHERE project_id = %s)",
                (project_id,),
            )
            conn.execute("DELETE FROM jobs WHERE project_id = %s", (project_id,))
            conn.execute("DELETE FROM projects WHERE id = %s", (project_id,))
        conn.commit()
        conn.close()
        direct = getattr(store, "_direct_conn", None)
        if direct is not None:
            direct.close()


def _seed(db, *, status="resume_pending"):
    project_id, job_id = str(uuid.uuid4()), str(uuid.uuid4())
    with db.conn.cursor() as cur:
        cur.execute("INSERT INTO projects (id, name) VALUES (%s, %s)", (project_id, job_id))
        cur.execute(
            """INSERT INTO jobs (id, project_id, task_id, thread_id, status, worker_id, meta)
               VALUES (%s, %s, %s, %s, %s, 'worker-original', %s::jsonb)""",
            (
                job_id,
                project_id,
                f"task-{job_id}",
                f"thread-{job_id}",
                status,
                json.dumps({"workflow_resume": {"state": "pending", "revision": 1}}),
            ),
        )
    db.conn.commit()
    db.seeded_projects.append(project_id)
    return job_id, project_id


def _row(db, job_id):
    with db.conn.cursor() as cur:
        cur.execute("SELECT status, worker_id, meta FROM jobs WHERE id = %s", (job_id,))
        result = cur.fetchone()
    db.conn.commit()
    return result


def _claim(repository, job_id, token):
    return repository.claim_execution(
        job_id=job_id,
        resume_revision=1,
        worker_id="worker-original",
        lease_token=token,
        lease_seconds=300,
    )


def test_execution_attempt_is_persisted_at_claim_and_not_incremented_by_duplicate(runtime_db):
    job_id, _ = _seed(runtime_db)
    repository = WorkflowResumeRepositoryAdapter(runtime_db.store)
    assert _claim(repository, job_id, "first").claimed
    assert not _claim(repository, job_id, "duplicate").claimed
    assert _row(runtime_db, job_id)[2]["workflow_resume"].get("execution_attempts") == 1


def test_retry_waits_outside_ready_outbox_and_rejects_early_duplicate(runtime_db):
    job_id, _ = _seed(runtime_db)
    repository = WorkflowResumeRepositoryAdapter(runtime_db.store)
    assert _claim(repository, job_id, "first").claimed
    assert repository.mark_retryable(
        job_id=job_id, resume_revision=1, lease_token="first", error="checkpoint unavailable"
    )
    assert not _claim(repository, job_id, "too-early").claimed
    assert all(str(row["job_id"]) != job_id for row in repository.pending_rows(batch_size=100))


def _pause(kind):
    payload = {
        "gate_kind": "wait",
        "wait_id": "wait-original",
        "kind": kind,
        "step_id": "pause",
        "step_execution_id": str(uuid.uuid4()),
        "attempt": 1,
    }
    if kind == "timer":
        payload["due_epoch_ms"] = 1
    elif kind == "child":
        payload["child_job_id"] = str(uuid.uuid4())
    else:
        payload = {"gate_kind": "approval", "approval_id": "approval-original"}
    return SimpleNamespace(
        values={}, tasks=[SimpleNamespace(interrupts=[SimpleNamespace(value=payload)])]
    )


@pytest.mark.parametrize("pause_kind", ["timer", "child", "approval"])
@pytest.mark.parametrize("race", ["cancelled", "replacement_owner"])
def test_worker_pause_cannot_overwrite_cancel_or_new_owner(
    runtime_db, monkeypatch, pause_kind, race
):
    job_id, project_id = _seed(runtime_db, status="in_progress")
    original_get = runtime_db.store.get_job
    changed = False

    def get_then_compete(key):
        nonlocal changed
        result = original_get(key)
        if not changed:
            changed = True
            with runtime_db.conn.cursor() as cur:
                if race == "cancelled":
                    cur.execute("UPDATE jobs SET status = 'cancelled' WHERE id = %s", (job_id,))
                else:
                    cur.execute("UPDATE jobs SET worker_id = 'worker-new' WHERE id = %s", (job_id,))
            runtime_db.conn.commit()
        return result

    monkeypatch.setattr(runtime_db.store, "get_job", get_then_compete)
    status = f"awaiting_{pause_kind}"
    adapter = WorkerTerminalEffectsAdapter(bl=runtime_db.store, worker_id=lambda: "worker-original")
    adapter.persist(
        SimpleNamespace(job_id=job_id, workflow_id="workflow", test_mode=True),
        job_row=None,
        project_id=project_id,
        final_status=status,
        result={},
        graph_outcome=TaskGraphOutcome.from_result(
            {}, pending_interrupt=True, interrupt_state=_pause(pause_kind)
        ),
    )
    saved_status, owner, meta = _row(runtime_db, job_id)
    assert saved_status == ("cancelled" if race == "cancelled" else "in_progress")
    assert owner == ("worker-original" if race == "cancelled" else "worker-new")
    assert "workflow_wait" not in meta


@pytest.mark.parametrize("race", ["cancelled", "replacement_owner"])
def test_scan_pause_cannot_overwrite_cancel_or_new_owner(runtime_db, monkeypatch, race):
    job_id, _ = _seed(runtime_db, status="in_progress")
    observed = runtime_db.store.get_job(job_id)

    def read_checkpoint(_thread):
        with runtime_db.conn.cursor() as cur:
            if race == "cancelled":
                cur.execute("UPDATE jobs SET status = 'cancelled' WHERE id = %s", (job_id,))
            else:
                cur.execute("UPDATE jobs SET worker_id = 'worker-new' WHERE id = %s", (job_id,))
        runtime_db.conn.commit()
        return _pause("timer")

    monkeypatch.setattr(scans, "get_committed_pause", read_checkpoint)
    assert scans._restore_pause_projection(runtime_db.store, job_id, observed) is None
    saved_status, owner, meta = _row(runtime_db, job_id)
    assert saved_status == ("cancelled" if race == "cancelled" else "in_progress")
    assert owner == ("worker-original" if race == "cancelled" else "worker-new")
    assert "workflow_wait" not in meta


def _resume_service(db, graph, monkeypatch):
    from core.application.jobs.workflow_resume_execution_service import (
        WorkflowResumeExecutionService,
    )
    from core.infrastructure.runtime.worker.worker_resume_adapters import (
        WorkerWorkflowResumeTerminalAdapter,
    )

    terminal = WorkerWorkflowResumeTerminalAdapter(bl=db.store, worker_id=lambda: "worker-original")
    for effect in ("release_lock", "notify", "sync_provider"):
        monkeypatch.setattr(terminal, effect, lambda _context: None)
    heartbeat = SimpleNamespace(cancelled=lambda: False, stop=lambda: None)
    lease = SimpleNamespace(
        start_execution=lambda **_kw: heartbeat, start_finalizer=lambda **_kw: heartbeat
    )
    return WorkflowResumeExecutionService(
        repository=WorkflowResumeRepositoryAdapter(db.store),
        graph=graph,
        lease=lease,
        terminal=terminal,
        worker_id=lambda: "worker-original",
        token_factory=lambda: str(uuid.uuid4()),
    )


def _resume_message(job_id):
    return {
        "message_type": "workflow_resume",
        "job_id": job_id,
        "thread_id": f"thread-{job_id}",
        "resume_revision": 1,
    }


def _make_retry_due(db, job_id):
    with db.conn.cursor() as cur:
        cur.execute(
            """UPDATE jobs SET meta = jsonb_set(meta,
                    '{workflow_resume,retry_not_before}', '"2000-01-01T00:00:00Z"'::jsonb)
                    WHERE id = %s""",
            (job_id,),
        )
    db.conn.commit()


def test_poisoned_resume_exhausts_without_queue_requeue_and_requests_child_cancel(
    runtime_db, monkeypatch
):
    job_id, _ = _seed(runtime_db)
    child_id = str(uuid.uuid4())
    with runtime_db.conn.cursor() as cur:
        cur.execute(
            "UPDATE jobs SET meta = meta || %s::jsonb WHERE id = %s",
            (
                json.dumps(
                    {
                        "invoke_children": {
                            child_id: {
                                "child_job_id": child_id,
                                "invocation_key": "child-call",
                                "step_id": "invoke",
                            }
                        }
                    }
                ),
                job_id,
            ),
        )
    runtime_db.conn.commit()
    calls = []

    def resume(_request, *, cancelled):
        calls.append("attempt")
        raise RuntimeError("checkpoint cannot resume")

    service = _resume_service(runtime_db, SimpleNamespace(resume=resume), monkeypatch)
    for attempt in range(3):
        result = service.execute(_resume_message(job_id))
        assert result["action"] == "ack"
        if attempt < 2:
            assert _row(runtime_db, job_id)[0] == "resume_pending"
            _make_retry_due(runtime_db, job_id)
    status, _, meta = _row(runtime_db, job_id)
    assert status == "failed"
    assert meta["public_progress_v1"]["snapshot"]["error"]["code"] == "resume_failed"
    assert meta["invoke_cancel_pending"] is True
    assert meta["invoke_children"][child_id]["cancel_requested_at"]
    service.execute(_resume_message(job_id))
    assert len(calls) == 3


def test_transient_resume_succeeds_on_second_claim_and_redelivery_does_not_execute(
    runtime_db, monkeypatch
):
    from core.application.jobs.execution_contracts import WorkflowResumeGraphOutcome

    job_id, _ = _seed(runtime_db)
    calls = []

    def resume(_request, *, cancelled):
        calls.append("attempt")
        if len(calls) == 1:
            raise RuntimeError("temporary checkpoint outage")
        return WorkflowResumeGraphOutcome.accepted(
            status="completed", result={"status": "completed"}, terminal=True
        )

    service = _resume_service(runtime_db, SimpleNamespace(resume=resume), monkeypatch)
    assert service.execute(_resume_message(job_id))["action"] == "ack"
    _make_retry_due(runtime_db, job_id)
    assert service.execute(_resume_message(job_id))["action"] == "ack"
    assert _row(runtime_db, job_id)[0] == "completed"
    service.execute(_resume_message(job_id))
    assert len(calls) == 2


def test_poisoned_finalizer_has_a_bounded_delayed_budget_without_rewriting_completed_run(
    runtime_db, monkeypatch
):
    from core.application.jobs.execution_contracts import WorkflowResumeGraphOutcome

    job_id, _ = _seed(runtime_db, status="completed")
    with runtime_db.conn.cursor() as cur:
        cur.execute(
            "UPDATE jobs SET meta = jsonb_set(meta, '{workflow_resume,state}', '\"finalizing\"'::jsonb) WHERE id = %s",
            (job_id,),
        )
    runtime_db.conn.commit()
    graph = SimpleNamespace(
        resume=lambda *_args, **_kwargs: WorkflowResumeGraphOutcome.accepted(
            status="completed", result={}
        )
    )
    service = _resume_service(runtime_db, graph, monkeypatch)
    attempts = []

    def fail_notification(_context):
        attempts.append("notification")
        raise RuntimeError("notification permanently unavailable")

    monkeypatch.setattr(service._terminal, "notify", fail_notification)
    for _ in range(3):
        assert service.execute(_resume_message(job_id))["action"] == "ack"
        _make_retry_due(runtime_db, job_id)
    status, _, meta = _row(runtime_db, job_id)
    assert status == "completed"
    assert meta["workflow_resume"]["state"] != "finalizing"
    service.execute(_resume_message(job_id))
    assert len(attempts) == 3


def test_exhausted_resume_retries_failed_terminal_transaction_without_reentering_graph(
    runtime_db, monkeypatch
):
    job_id, _ = _seed(runtime_db)
    calls = []

    def resume(_request, *, cancelled):
        calls.append("graph")
        raise RuntimeError("poison checkpoint")

    service = _resume_service(runtime_db, SimpleNamespace(resume=resume), monkeypatch)
    original = runtime_db.store.finish_job
    attempts = []

    def fail_first_terminal(*args, **kwargs):
        attempts.append("persist")
        if len(attempts) == 1:
            raise RuntimeError("transient database failure after progress writes")
        return original(*args, **kwargs)

    monkeypatch.setattr(runtime_db.store, "finish_job", fail_first_terminal)
    for _ in range(3):
        assert service.execute(_resume_message(job_id))["action"] == "ack"
        _make_retry_due(runtime_db, job_id)
    status, _, meta = _row(runtime_db, job_id)
    assert status == "resume_pending"
    assert meta["workflow_resume"]["state"] != "completed"
    assert "public_progress_v1" not in meta
    assert service.execute(_resume_message(job_id))["action"] == "ack"
    assert _row(runtime_db, job_id)[0] == "failed"
    assert len(calls) == 3
    assert len(attempts) == 2


@pytest.mark.parametrize("pause_kind", ["timer", "child", "approval"])
def test_current_worker_can_atomically_project_a_pause(runtime_db, pause_kind):
    job_id, project_id = _seed(runtime_db, status="in_progress")
    status = f"awaiting_{pause_kind}"
    adapter = WorkerTerminalEffectsAdapter(bl=runtime_db.store, worker_id=lambda: "worker-original")
    adapter.persist(
        SimpleNamespace(job_id=job_id, workflow_id="workflow", test_mode=True),
        job_row=None,
        project_id=project_id,
        final_status=status,
        result={},
        graph_outcome=TaskGraphOutcome.from_result(
            {}, pending_interrupt=True, interrupt_state=_pause(pause_kind)
        ),
    )
    saved_status, owner, meta = _row(runtime_db, job_id)
    assert saved_status == status
    assert owner == "worker-original"
    assert meta["public_progress_v1"]["snapshot"]["status"] == status
    if pause_kind != "approval":
        assert meta["workflow_wait"]["kind"] == pause_kind


def test_exhaustion_finalizer_retries_without_reentering_graph(runtime_db, monkeypatch):
    job_id, _ = _seed(runtime_db)
    graph_calls = []
    effects = []

    def resume(_request, *, cancelled):
        graph_calls.append("graph")
        raise RuntimeError("poison checkpoint")

    service = _resume_service(runtime_db, SimpleNamespace(resume=resume), monkeypatch)
    notifications = []

    def notify(_context):
        notifications.append("notification")
        if len(notifications) == 1:
            raise RuntimeError("temporary notification outage")

    monkeypatch.setattr(service._terminal, "release_lock", lambda _: effects.append("release_lock"))
    monkeypatch.setattr(
        service._terminal, "sync_provider", lambda _: effects.append("provider_sync")
    )
    monkeypatch.setattr(service._terminal, "notify", notify)
    for attempt in range(3):
        assert service.execute(_resume_message(job_id))["action"] == "ack"
        if attempt < 2:
            _make_retry_due(runtime_db, job_id)
    status, _, meta = _row(runtime_db, job_id)
    assert status == "failed"
    assert meta["workflow_resume"]["state"] == "finalizing"
    assert service.execute(_resume_message(job_id))["action"] == "discard"
    assert len(notifications) == 1
    _make_retry_due(runtime_db, job_id)
    assert service.execute(_resume_message(job_id))["action"] == "ack"
    assert _row(runtime_db, job_id)[2]["workflow_resume"]["state"] == "completed"
    assert len(graph_calls) == 3
    assert len(notifications) == 2
    assert effects == ["release_lock", "provider_sync"]


def test_budget_abort_resume_uses_canonical_cancel_and_requests_child_cancellation(
    runtime_db, monkeypatch
):
    from core.application.jobs.execution_contracts import WorkflowResumeGraphOutcome

    job_id, _ = _seed(runtime_db)
    child_id = str(uuid.uuid4())
    with runtime_db.conn.cursor() as cur:
        cur.execute(
            "UPDATE jobs SET meta = meta || %s::jsonb WHERE id = %s",
            (
                json.dumps(
                    {
                        "invoke_children": {
                            child_id: {
                                "child_job_id": child_id,
                                "invocation_key": "budget-child",
                                "step_id": "invoke",
                            }
                        }
                    }
                ),
                job_id,
            ),
        )
    runtime_db.conn.commit()
    outcome = WorkflowResumeGraphOutcome.accepted(
        status="cancelled",
        result={"status": "cancelled"},
        terminal=True,
    )
    service = _resume_service(
        runtime_db,
        SimpleNamespace(resume=lambda *_args, **_kwargs: outcome),
        monkeypatch,
    )
    assert service.execute(_resume_message(job_id))["action"] == "ack"
    status, _, meta = _row(runtime_db, job_id)
    assert status == "cancelled"
    assert meta.get("invoke_cancel_pending") is True
    assert meta["invoke_children"][child_id]["cancel_requested_at"]
    assert meta["public_progress_v1"]["snapshot"]["error"]["code"] == "cancelled"
    assert meta["workflow_resume"]["state"] == "completed"


def _finalizing_resume_service(db, monkeypatch, *, status="completed"):
    job_id, project_id = _seed(db, status=status)
    with db.conn.cursor() as cur:
        cur.execute(
            "UPDATE jobs SET meta = jsonb_set(meta, '{workflow_resume,state}', "
            "'\"finalizing\"'::jsonb) WHERE id = %s",
            (job_id,),
        )
    db.conn.commit()

    def unexpected_graph(*_args, **_kwargs):
        pytest.fail("finalizer recovery must not execute the graph again")

    return (
        _resume_service(db, SimpleNamespace(resume=unexpected_graph), monkeypatch),
        job_id,
        project_id,
    )


@pytest.mark.parametrize("failed_effect", ["release_lock", "notify"])
def test_terminal_finalizer_failure_does_not_skip_independent_effects(
    runtime_db, monkeypatch, failed_effect
):
    service, job_id, _ = _finalizing_resume_service(runtime_db, monkeypatch)
    calls = []

    def effect(name):
        def run(_context):
            calls.append(name)
            if name == failed_effect:
                raise RuntimeError(f"permanent {name} outage")

        return run

    for name in ("release_lock", "notify", "sync_provider"):
        monkeypatch.setattr(service._terminal, name, effect(name))
    for _ in range(3):
        assert service.execute(_resume_message(job_id))["action"] == "ack"
        _make_retry_due(runtime_db, job_id)

    status, _, meta = _row(runtime_db, job_id)
    assert status == "completed"
    assert meta["workflow_resume"]["finalization_exhausted"] is True
    assert calls.count(failed_effect) == 3
    for name in {"release_lock", "notify", "sync_provider"} - {failed_effect}:
        assert calls.count(name) == 1
    assert service.execute(_resume_message(job_id))["action"] == "discard"


@pytest.mark.parametrize("status", ["completed", "awaiting_child"])
def test_scheduled_lock_reconciler_recovers_exhausted_finalizer_but_preserves_parked_run(
    runtime_db, disposable_db, monkeypatch, status
):
    service, job_id, project_id = _finalizing_resume_service(runtime_db, monkeypatch, status=status)
    workflow_id = f"resume-cleanup-{uuid.uuid4()}"
    schedule_id, lock_id = str(uuid.uuid4()), str(uuid.uuid4())
    with runtime_db.conn.cursor() as cur:
        cur.execute(
            "INSERT INTO registry_workflows (workflow_key, display_name) VALUES (%s, %s)",
            (workflow_id, "Resume finalizer cleanup test"),
        )
        cur.execute(
            "INSERT INTO scheduled_jobs (id, project_id, account_id, workflow_id, cron_expr) "
            "SELECT %s, id, account_id, %s, '0 * * * *' FROM projects WHERE id = %s",
            (schedule_id, workflow_id, project_id),
        )
        cur.execute(
            "INSERT INTO scheduled_job_execution_locks "
            "(id, scheduled_job_id, project_id, account_id, workflow_id, lock_key, job_id, status) "
            "SELECT %s, id, project_id, account_id, workflow_id, %s, %s, 'active' "
            "FROM scheduled_jobs WHERE id = %s",
            (lock_id, workflow_id, job_id, schedule_id),
        )
    runtime_db.conn.commit()
    try:

        def unavailable(_context):
            raise RuntimeError("terminal side effect unavailable")

        failed_effect = "release_lock" if status == "completed" else "notify"
        monkeypatch.setattr(service._terminal, failed_effect, unavailable)
        for _ in range(3):
            assert service.execute(_resume_message(job_id))["action"] == "ack"
            _make_retry_due(runtime_db, job_id)
        assert _row(runtime_db, job_id)[2]["workflow_resume"]["finalization_exhausted"] is True

        # Use the existing scheduler's real DB reconciliation owner, not a new
        # finalizer queue or a mocked lock-release operation.
        scheduler_store = PlatformControlScheduleStore(disposable_db.dsn)
        try:
            scheduler_store.reconcile_scheduled_job_execution_locks()
            scheduler_store.conn.commit()
        finally:
            direct = getattr(scheduler_store, "_direct_conn", None)
            if direct is not None:
                direct.close()
        with runtime_db.conn.cursor() as cur:
            cur.execute(
                "SELECT released_at, release_reason FROM scheduled_job_execution_locks WHERE id = %s",
                (lock_id,),
            )
            released_at, release_reason = cur.fetchone()
        runtime_db.conn.commit()
        assert (released_at is not None) is (status == "completed")
        assert release_reason == ("job_completed" if status == "completed" else "")
        assert _row(runtime_db, job_id)[0] == status
    finally:
        runtime_db.conn.rollback()
        runtime_db.conn.execute("DELETE FROM scheduled_jobs WHERE id = %s", (schedule_id,))
        runtime_db.conn.execute(
            "DELETE FROM registry_workflows WHERE workflow_key = %s", (workflow_id,)
        )
        runtime_db.conn.commit()
