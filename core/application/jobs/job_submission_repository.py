"""Application boundary for atomically creating a job and its dispatch intent."""

from __future__ import annotations

from typing import Any

from core.application.jobs.dispatch_outbox_repository import DispatchOutboxRepository
from core.application.jobs.job_lifecycle_repository import JobLifecycleRepository
from core.application.jobs.workflow_definition_snapshot_repository import (
    WorkflowDefinitionSnapshotRepository,
)


class JobSubmissionRepository:
    def __init__(self, bl: Any, transaction_conn: Any) -> None:
        self.bl = bl
        self.transaction_conn = transaction_conn

    def create(self, *, encrypted_envelope: str, outbox_status: str, **job_fields: Any) -> Any:
        owner_token = job_fields.pop("preparation_owner_token", None)
        lease_seconds = int(job_fields.pop("preparation_lease_seconds", 300))
        job_id = JobLifecycleRepository(
            self.transaction_conn,
            self.bl._column_exists,
        ).create(**job_fields)
        workflow_id = str(job_fields.get("selected_workflow_key") or "").strip()
        if workflow_id:
            # The definition the job runs is pinned before its dispatch intent
            # exists: a worker never executes a workflow job without one.
            WorkflowDefinitionSnapshotRepository(self.transaction_conn).pin_for_job(
                job_id=job_id,
                workflow_id=workflow_id,
                validate=str(job_fields.get("task_source") or "") != "step_test_run",
            )
        DispatchOutboxRepository(self.transaction_conn).create(
            job_id=str(job_id),
            encrypted_envelope=encrypted_envelope,
            status=outbox_status,
            preparation_owner_token=owner_token,
            preparation_lease_seconds=lease_seconds,
        )
        return job_id
