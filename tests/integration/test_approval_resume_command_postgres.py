"""Real-PostgreSQL evidence for atomic approval decision and resume publication."""

from __future__ import annotations

import json
import uuid
from types import SimpleNamespace
from unittest.mock import MagicMock

import psycopg
import pytest

from core.application.jobs.workflow_run_projection import build_public_result
from core.application.management.command_models import ApprovalDecisionInput
from core.application.management.command_services_approval import ApprovalCommandService
from core.application.management.errors import ServiceConflictError
from core.application.management.query_services_approval import ApprovalQueryService
from core.application.management.scope import ScopeContext
from core.infrastructure.config.platform_identity import DEFAULT_ACCOUNT_ID, DEFAULT_TENANT_ID
from core.infrastructure.db.platform_stores import PlatformWorkerStore
from core.infrastructure.db.worker import WorkflowResumeRepositoryAdapter

pytestmark = pytest.mark.integration


@pytest.mark.parametrize(
    ("final_status", "expects_finished"),
    [("awaiting_approval", False), ("completed", True)],
)
def test_postgres_mark_finalizing_preserves_nonterminal_timestamps(
    disposable_db,
    final_status: str,
    expects_finished: bool,
) -> None:
    project_id = str(uuid.uuid4())
    job_id = str(uuid.uuid4())
    resume_intent = {
        "state": "running",
        "revision": 7,
        "lease_token": "lease-7",
        "lease_expires_at": "2999-01-01T00:00:00+00:00",
    }
    expected_outputs = {
        "outputs": {"normalized": {"message": "Approved synthetic value"}},
        "artifacts": {},
    }
    with psycopg.connect(disposable_db.dsn) as seed, seed.cursor() as cur:
        cur.execute(
            "INSERT INTO projects (id, name, account_id) VALUES (%s, %s, %s)",
            (project_id, f"approval finalizing {project_id}", DEFAULT_ACCOUNT_ID),
        )
        cur.execute(
            """
            INSERT INTO jobs (
                id, account_id, tenant_id, project_id, task_id, task_summary,
                thread_id, status, started_at, meta
            ) VALUES (
                %s, %s, %s, %s, %s, %s, %s, 'in_progress',
                NOW() - INTERVAL '2 seconds', %s::jsonb
            )
            """,
            (
                job_id,
                DEFAULT_ACCOUNT_ID,
                DEFAULT_TENANT_ID,
                project_id,
                f"TASK-{uuid.uuid4()}",
                "Finalize workflow resume",
                f"approval-{uuid.uuid4()}",
                json.dumps({"workflow_resume": resume_intent, "unrelated": {"keep": True}}),
            ),
        )

    store = PlatformWorkerStore(disposable_db.dsn)
    repository = WorkflowResumeRepositoryAdapter(store)
    try:
        assert repository.mark_finalizing(
            job_id=job_id,
            resume_revision=7,
            lease_token="lease-7",
            final_status=final_status,
            result={"status": final_status, "workflow_outputs": expected_outputs},
        )
        with psycopg.connect(disposable_db.dsn) as observer, observer.cursor() as cur:
            cur.execute(
                "SELECT status, finished_at, duration_ms, meta FROM jobs WHERE id = %s",
                (job_id,),
            )
            status, finished_at, duration_ms, meta = cur.fetchone()
        assert status == final_status
        assert (finished_at is not None) is expects_finished
        assert (duration_ms is not None) is expects_finished
        assert meta["workflow_outputs"] == expected_outputs
        assert meta["unrelated"] == {"keep": True}
        if final_status == "completed":
            assert (
                build_public_result({"id": job_id, "meta": meta}, final_status).model_dump()
                == expected_outputs
            )
        assert not repository.mark_finalizing(
            job_id=job_id,
            resume_revision=6,
            lease_token="lease-7",
            final_status="completed",
            result={
                "workflow_outputs": {
                    "outputs": {"normalized": {"message": "Stale overwrite"}},
                    "artifacts": {},
                }
            },
        )
        with psycopg.connect(disposable_db.dsn) as observer, observer.cursor() as cur:
            cur.execute("SELECT meta FROM jobs WHERE id = %s", (job_id,))
            assert cur.fetchone()[0]["workflow_outputs"] == expected_outputs
    finally:
        direct = getattr(store, "_direct_conn", None)
        if direct is not None:
            direct.close()


def test_postgres_approval_decision_commits_singular_resume_before_publish(disposable_db) -> None:
    project_id = str(uuid.uuid4())
    job_id = str(uuid.uuid4())
    thread_id = f"approval-{uuid.uuid4()}"
    task_id = f"TASK-{uuid.uuid4()}"
    with psycopg.connect(disposable_db.dsn) as seed, seed.cursor() as cur:
        cur.execute(
            "INSERT INTO projects (id, name, account_id) VALUES (%s, %s, %s)",
            (project_id, "workflow resume integration", DEFAULT_ACCOUNT_ID),
        )
        cur.execute(
            """
            INSERT INTO jobs (
                id, account_id, tenant_id, project_id, task_id, task_summary,
                thread_id, status, meta
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, 'awaiting_approval', %s::jsonb)
            """,
            (
                job_id,
                DEFAULT_ACCOUNT_ID,
                DEFAULT_TENANT_ID,
                project_id,
                task_id,
                "Approve the release",
                thread_id,
                json.dumps({}),
            ),
        )

    store = PlatformWorkerStore(disposable_db.dsn)
    published: list[dict[str, object]] = []
    visible_at_publish: list[tuple[str, dict[str, object]]] = []

    def publish(**message: object) -> None:
        published.append(dict(message))
        with psycopg.connect(disposable_db.dsn) as observer, observer.cursor() as cur:
            cur.execute("SELECT status, meta FROM jobs WHERE id = %s", (job_id,))
            status, meta = cur.fetchone()
        visible_at_publish.append((str(status), dict(meta)))

    interrupt = SimpleNamespace(
        id="gate-current",
        value={
            "message": "Approve the release",
            "allowed_actions": ["approve", "reject"],
        },
    )
    service = ApprovalCommandService(
        resume_shutdown_in_progress=lambda: False,
        get_checkpointer=lambda: object(),
        get_logger=lambda: store,
        enqueue_workflow_resume=publish,
        workflow_resume_repository_factory=WorkflowResumeRepositoryAdapter,
        get_thread_state_snapshot=lambda _thread_id: SimpleNamespace(
            tasks=[SimpleNamespace(interrupts=[interrupt])]
        ),
    )
    service._record_in_progress_marker = MagicMock()
    scope = ScopeContext(account_id=DEFAULT_ACCOUNT_ID, actor_id="chat:alice")
    decision = ApprovalDecisionInput(
        approved=True,
        action="approve",
        comment="ship it",
        approval_id="gate-current",
    )

    try:
        first = service.resume_task(
            thread_id=thread_id,
            scope=scope,
            decision=decision,
            idempotency_key="chat-update-current",
        )

        assert first.status == "resume_pending"
        assert first.job_id == job_id
        assert first.approval_id == "gate-current"
        assert len(published) == 1
        assert published[0] == {
            "project_id": project_id,
            "job_id": job_id,
            "task_id": task_id,
            "thread_id": thread_id,
            "resume_revision": 1,
        }
        status_at_publish, meta_at_publish = visible_at_publish[0]
        assert status_at_publish == "resume_pending"
        assert meta_at_publish["approvals"][0]["approval_id"] == "gate-current"
        assert meta_at_publish["approvals"][0]["approved"] is True
        assert meta_at_publish["approvals"][0]["comment"] == "ship it"
        history = (
            ApprovalQueryService(
                get_logger=lambda: store,
                get_thread_state_snapshot=lambda _: None,
                approval_fallback_payload=lambda _: {},
            )
            .list_history(job_id, scope=scope)
            .dump()
        )
        assert history["job_id"] == job_id
        assert len(history["approvals"]) == 1
        assert history["approvals"][0]["approval_id"] == "gate-current"
        assert history["approvals"][0]["approved"] is True
        assert history["approvals"][0]["comment"] == "ship it"
        assert meta_at_publish["workflow_resume"]["reason"] == "approval"
        assert meta_at_publish["workflow_resume"]["approval_id"] == "gate-current"
        assert meta_at_publish["workflow_resume"]["revision"] == 1
        assert meta_at_publish["workflow_resume"]["resume_value"] == {
            "reason": "approval",
            "approved": True,
            "action": "approve",
            "comment": "ship it",
        }

        replay = service.resume_task(
            thread_id=thread_id,
            scope=scope,
            decision=decision,
            idempotency_key="chat-update-current",
        )
        assert replay.status == "resume_pending"
        assert replay.deduplicated is True
        assert len(published) == 1

        with psycopg.connect(disposable_db.dsn) as observer, observer.cursor() as cur:
            cur.execute("SELECT status, meta FROM jobs WHERE id = %s", (job_id,))
            before_stale = cur.fetchone()
        _raises_input_211_1 = ApprovalDecisionInput(
            approved=True, action="approve", comment="stale", approval_id="old-gate"
        )
        with pytest.raises(ServiceConflictError):
            service.resume_task(
                thread_id=thread_id,
                scope=scope,
                decision=_raises_input_211_1,
                idempotency_key="chat-update-stale",
            )
        with psycopg.connect(disposable_db.dsn) as observer, observer.cursor() as cur:
            cur.execute("SELECT status, meta FROM jobs WHERE id = %s", (job_id,))
            after_stale = cur.fetchone()
        assert after_stale == before_stale
        assert len(published) == 1
    finally:
        direct = getattr(store, "_direct_conn", None)
        if direct is not None:
            direct.close()
