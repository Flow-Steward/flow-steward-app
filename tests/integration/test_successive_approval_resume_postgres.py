"""Actual graph outcomes cross revision/lease fences into durable second approvals."""

from __future__ import annotations

import json
import uuid
from typing import Any
from unittest.mock import MagicMock

import psycopg
import pytest
from psycopg.rows import dict_row

from core.application.jobs.public_workflow_snapshot_query import PublicWorkflowSnapshotQuery
from core.application.jobs.workflow_resume_execution_service import WorkflowResumeExecutionService
from core.application.management.command_models import ApprovalDecisionInput
from core.application.management.command_services_approval import ApprovalCommandService
from core.application.management.errors import ServiceConflictError
from core.application.management.query_services_approval import ApprovalQueryService
from core.application.management.scope import ScopeContext
from core.application.orchestration.run_definition import clear_run_definition_cache
from core.application.reports.job_detail_sections import build_execution_steps
from core.application.workflows.test_run_query_service import WorkflowTestRunQueryService
from core.infrastructure.config.platform_identity import DEFAULT_ACCOUNT_ID, DEFAULT_TENANT_ID
from core.infrastructure.db import job_thread_store
from core.infrastructure.db.platform_logger import PlatformLoggerCallback
from core.infrastructure.db.platform_stores import PlatformWorkerStore
from core.infrastructure.db.worker import WorkflowResumeRepositoryAdapter
from core.infrastructure.db.workflow_test_run_query_repository import (
    PostgresWorkflowTestRunJobRepository,
)
from core.infrastructure.runtime.worker.worker_execution_adapters import (
    _persist_awaiting_approval_state,
)
from core.infrastructure.runtime.worker.worker_resume_adapters import (
    WorkerWorkflowResumeGraphAdapter,
    WorkerWorkflowResumeLeaseAdapter,
)
from tests._successive_approval_graph import ApprovalGraphRun

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("loop", [False, True], ids=["root", "loop"])
def test_actual_resume_publishes_next_gate_and_fences_old_decisions(
    disposable_db, monkeypatch, loop: bool
) -> None:
    from core.application.orchestration.agents import common

    # Only external event/message delivery is isolated; real callback progress,
    # cancellation, graph, commands, leases, finalization and readers stay live.
    monkeypatch.setattr(common, "emit_event", lambda *args, **kwargs: None)
    monkeypatch.setattr(common, "publish_agent_message", lambda *args, **kwargs: True)
    clear_run_definition_cache()
    project_id, job_id = str(uuid.uuid4()), str(uuid.uuid4())
    run = ApprovalGraphRun(loop=loop)
    task_id = f"TASK-{uuid.uuid4()}"
    with psycopg.connect(disposable_db.dsn) as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO projects (id, name, account_id) VALUES (%s, %s, %s)",
            (project_id, f"successive approval {project_id}", DEFAULT_ACCOUNT_ID),
        )
        cur.execute(
            """INSERT INTO jobs (id, account_id, tenant_id, project_id, task_id, task_summary,
                    thread_id, status, worker_id, started_at, heartbeat_at, meta)
                    VALUES (%s, %s, %s, %s, %s, 'Review synthetic items', %s, 'in_progress',
                    'approval-regression-worker', NOW() - INTERVAL '1 minute', NOW() - INTERVAL '1 minute', %s::jsonb)""",
            (
                job_id,
                DEFAULT_ACCOUNT_ID,
                DEFAULT_TENANT_ID,
                project_id,
                task_id,
                run.thread_id,
                json.dumps(
                    {
                        "workflow_test_run": True,
                        "test_mode": True,
                        "selected_workflow_id": "approval_regression",
                    }
                ),
            ),
        )
    store = PlatformWorkerStore(disposable_db.dsn)
    repository = WorkflowResumeRepositoryAdapter(store)
    run.store = store
    run.job_thread_store = job_thread_store
    run.initial.update(
        job_id=job_id,
        project_id=project_id,
        account_id=DEFAULT_ACCOUNT_ID,
        tenant_id=DEFAULT_TENANT_ID,
    )
    run.config["callbacks"] = [PlatformLoggerCallback(store, project_id, job_id)]
    messages: list[dict[str, Any]] = []
    commands = ApprovalCommandService(
        resume_shutdown_in_progress=lambda: False,
        get_checkpointer=lambda: run.checkpointer,
        get_logger=lambda: store,
        enqueue_workflow_resume=lambda **message: messages.append(message),
        workflow_resume_repository_factory=WorkflowResumeRepositoryAdapter,
        get_thread_state_snapshot=lambda _: run.snapshot(),
    )
    queries = ApprovalQueryService(
        get_logger=lambda: store,
        get_thread_state_snapshot=lambda _: run.snapshot(),
        approval_fallback_payload=lambda _: {},
    )
    adapter = WorkerWorkflowResumeGraphAdapter(bl=store)
    service = WorkflowResumeExecutionService(
        repository=repository,
        graph=adapter,
        lease=WorkerWorkflowResumeLeaseAdapter(
            repository=repository, repository_factory=WorkflowResumeRepositoryAdapter, bl=store
        ),
        terminal=MagicMock(),
        worker_id=lambda: "approval-regression-worker",
        token_factory=lambda: uuid.uuid4().hex,
    )
    scope = ScopeContext(account_id=DEFAULT_ACCOUNT_ID, actor_id="chat:reviewer")

    def fresh() -> dict[str, Any]:
        with psycopg.connect(disposable_db.dsn, row_factory=dict_row) as conn, conn.cursor() as cur:
            cur.execute("SELECT * FROM jobs WHERE id = %s", (job_id,))
            return dict(cur.fetchone())

    def fenced_runner(**kwargs):
        running = fresh()
        intent = running["meta"]["workflow_resume"]
        assert running["status"] == "in_progress" and intent["state"] == "running"
        # Exercise each SQL predicate independently while the other predicates
        # (including running status and a live lease) really match.
        for revision, token in (
            (intent["revision"] - 1, intent["lease_token"]),
            (intent["revision"], "stale-owner"),
        ):
            assert not repository.mark_finalizing(
                job_id=job_id,
                resume_revision=revision,
                lease_token=token,
                final_status="completed",
                result={"status": "completed"},
            )
        assert fresh()["meta"]["workflow_resume"] == intent
        return run.runner(**kwargs)

    adapter._runner = fenced_runner

    def decide(approval_id: str, *, approved: bool, key: str):
        return commands.resume_task(
            thread_id=run.thread_id,
            scope=scope,
            idempotency_key=key,
            decision=ApprovalDecisionInput(
                approved=approved,
                action="approve" if approved else "reject",
                comment="reviewed",
                approval_id=approval_id,
            ),
        )

    try:
        run.start()
        first = run.approval()
        _persist_awaiting_approval_state(
            bl=store,
            expected_worker_id="approval-regression-worker",
            job_id=job_id,
            project_id=project_id,
            selected_workflow_id="approval_regression",
            pending_interrupt_state=run.snapshot(),
            result=run.snapshot().values,
        )
        assert queries.get_current_gate(job_id, scope=scope).approval_id == first["approval_id"]
        before = fresh()
        accepted = decide(first["approval_id"], approved=True, key="decision-one")
        assert accepted.status == "resume_pending"
        assert len(messages) == 1
        assert service.execute(messages[0]) == {
            "action": "ack",
            "reason": "workflow_resume_accepted",
        }
        second = run.approval()
        assert second["approval_id"] != first["approval_id"]
        paused = fresh()
        # RED on the original runtime: checkpoint is correct, durable row is running.
        assert paused["status"] == "awaiting_approval"
        assert paused["finished_at"] is None and paused["duration_ms"] is None
        assert paused["heartbeat_at"] > before["heartbeat_at"]
        meta = paused["meta"]
        assert meta["state_approval_id"] == second["approval_id"]
        assert meta["approval_context"] == second["message"]
        assert meta["workflow_resume"]["state"] == "completed"
        assert meta["workflow_resume"]["revision"] == 1
        wait = meta["workflow_wait"]
        for key in ("approval_id", "step_execution_id", "step_id", "wait_id"):
            assert wait[key] == second[key]
        current = queries.get_current_gate(job_id, scope=scope)
        assert current.approval_id == second["approval_id"]
        assert current.allowed_actions == ["approve", "reject"]
        polling = WorkflowTestRunQueryService(
            PostgresWorkflowTestRunJobRepository(lambda: store)
        ).get_public_state(run.thread_id)
        assert polling["status"] == "awaiting_approval" and polling["needs_user_approval"]
        assert polling["approval_id"] == second["approval_id"]
        assert polling["approval_context"] == second["message"]
        public = PublicWorkflowSnapshotQuery(
            store, get_thread_state_snapshot=lambda _: run.snapshot(), to_json_obj=lambda v: v
        ).get(paused)
        steps, _ = build_execution_steps(
            job=paused,
            thread_state=run.snapshot().values,
            workflow_run=public.model_dump(mode="json"),
        )
        by_execution = {step["step_execution_id"]: step for step in steps}
        assert by_execution[first["step_execution_id"]]["status"] == "completed"
        assert by_execution[second["step_execution_id"]]["status"] == "awaiting_review"
        checkpoint = run.snapshot().values
        with pytest.raises(ServiceConflictError) as stale:
            decide(first["approval_id"], approved=True, key="fresh-old-decision")
        assert stale.value.payload["code"] == "stale_approval_gate"
        replay = decide(first["approval_id"], approved=True, key="decision-one")
        assert replay.deduplicated
        assert len(messages) == 1
        assert fresh()["meta"]["workflow_resume"]["revision"] == 1
        assert fresh()["meta"]["state_approval_id"] == second["approval_id"]
        assert run.snapshot().values == checkpoint
        # Both revision and lease are real SQL CAS predicates, not test finalizers.
        for revision, token in ((0, "old-owner"), (1, "old-owner")):
            assert not repository.mark_finalizing(
                job_id=job_id,
                resume_revision=revision,
                lease_token=token,
                final_status="completed",
                result={"status": "completed"},
            )
        assert fresh()["status"] == "awaiting_approval"
        assert fresh()["meta"]["state_approval_id"] == second["approval_id"]
        decide(second["approval_id"], approved=False, key="decision-two")
        assert len(messages) == 2
        assert service.execute(messages[1]) == {
            "action": "ack",
            "reason": "workflow_resume_accepted",
        }
        final = fresh()
        assert final["status"] == "failed"
        assert final["finished_at"] is not None and final["duration_ms"] is not None
        assert final["meta"]["workflow_resume"]["state"] == "completed"
        assert run.snapshot().values["workflow_terminal"]["step_id"] == "rejected"
        history = queries.list_history(job_id, scope=scope).dump()["approvals"]
        assert [(item["approval_id"], item["approved"]) for item in history] == [
            (first["approval_id"], True),
            (second["approval_id"], False),
        ]
        decisions = [
            entry
            for entry in run.snapshot().values["orchestrator_journal"]
            if entry["event"] == "approval_resume_decision"
            and entry["detail"].get("gate_kind") == "approval"
        ]
        assert len(decisions) == 2
    finally:
        direct = getattr(store, "_direct_conn", None)
        if direct is not None:
            direct.close()
        clear_run_definition_cache()
