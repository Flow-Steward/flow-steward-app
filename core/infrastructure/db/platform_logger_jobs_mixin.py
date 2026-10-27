# mypy: ignore-errors
from __future__ import annotations

from typing import Any

from core.application.jobs.job_lifecycle_repository import JobLifecycleRepository
from core.application.jobs.query_scope import job_scope_conditions
from core.application.reports.activity_repository import list_activity_facts
from core.infrastructure.db.job_scope_contract import JobQueryScope
from core.infrastructure.db.platform_logger_common import (
    logger,
)

TERMINAL_MEMORY_CURATION_EVENT_TYPE = "job.terminal.memory_curation_requested"


class PlatformLoggerJobsMixin:
    @staticmethod
    def _job_scope_conditions(scope: JobQueryScope) -> tuple[list[str], list[Any]]:
        """Compose the application policy before handing predicates to SQL adapters."""
        return job_scope_conditions(scope)

    def list_report_activity_facts(self, **kwargs: Any) -> list[dict]:
        """Delegate scoped operational facts to the application-owned repository."""
        return list_activity_facts(self.conn, **kwargs)

    def _job_lifecycle_repository(self):
        return JobLifecycleRepository(self.conn, self._column_exists)

    def create_job(
        self,
        project_id,
        task_id,
        task_summary=None,
        task_source="manual",
        thread_id=None,
        mode=None,
        complexity=None,
        workflow_path=None,
        status="in_progress",
        queue_name=None,
        selected_workflow_key=None,
        meta=None,
    ):
        """
        Create a new job row. Returns the job id.

        status defaults to 'in_progress' for direct-create flows.
        Pass status='pending' when the task is being enqueued to RabbitMQ and
        will be picked up by a worker later.
        queue_name is the RabbitMQ queue name (e.g. 'platform.project.1').
        """
        return self._job_lifecycle_repository().create(
            project_id=project_id,
            task_id=task_id,
            task_summary=task_summary,
            task_source=task_source,
            thread_id=thread_id,
            mode=mode,
            complexity=complexity,
            workflow_path=workflow_path,
            status=status,
            queue_name=queue_name,
            selected_workflow_key=selected_workflow_key,
            meta=meta,
        )

    def update_job(
        self,
        job_id,
        status=None,
        mode=None,
        complexity=None,
        workflow_path=None,
        queue_name=None,
        queued_at=None,
        worker_id=None,
        meta=None,
    ):
        """
        Partially update a job row.

        New queue-related fields:
          queue_name  — RabbitMQ queue name
          queued_at   — pass the string "NOW()" to set to current timestamp
          worker_id   — hostname+pid of the worker that picked it up
        """
        return self._job_lifecycle_repository().update(
            job_id=job_id,
            status=status,
            mode=mode,
            complexity=complexity,
            workflow_path=workflow_path,
            queue_name=queue_name,
            queued_at=queued_at,
            worker_id=worker_id,
            meta=meta,
        )

    def merge_job_meta(self, job_id: str, patch: dict | None = None) -> None:
        """Merge keys into jobs.meta for a single job (best-effort)."""
        return self._job_lifecycle_repository().merge_meta(
            job_id=job_id,
            patch=patch,
        )

    def recover_in_progress_job(
        self,
        job_id: str,
        *,
        expected_worker_id: str,
        reason: str,
    ) -> bool:
        """Atomically fence a dead worker before reclaiming its job."""
        return self._job_lifecycle_repository().recover_in_progress(
            job_id=str(job_id),
            expected_worker_id=str(expected_worker_id),
            reason=str(reason),
        )

    def update_queue_delivery_meta(
        self,
        job_id: str,
        *,
        queue_delivery: dict | None,
        runtime_journal: list | None,
        status: str | None = None,
        expected_dispatch_states: set[str] | None = None,
    ) -> bool:
        """Persist a publisher-owned queue transition without racing a worker claim."""
        return self._job_lifecycle_repository().update_queue_delivery_meta(
            job_id=job_id,
            queue_delivery=queue_delivery,
            runtime_journal=runtime_journal,
            status=status,
            expected_dispatch_states=expected_dispatch_states,
        )

    def update_job_runtime(
        self,
        job_id: str,
        *,
        selected_workflow: str | None = None,
        current_step_id: str | None = None,
        current_group_id: str | None = None,
        current_agent: str | None = None,
        heartbeat_now: bool = False,
        last_error: str | None = None,
        last_response_excerpt: str | None = None,
        clear_last_error: bool = False,
    ) -> None:
        """Persist runtime observability fields for a job.

        This method is intentionally best-effort and supports partial updates.
        If the runtime columns are not available yet (older schema), callers
        should treat failures as non-fatal.
        """
        return self._job_lifecycle_repository().update_runtime(
            job_id=job_id,
            selected_workflow=selected_workflow,
            current_step_id=current_step_id,
            current_group_id=current_group_id,
            current_agent=current_agent,
            heartbeat_now=heartbeat_now,
            last_error=last_error,
            last_response_excerpt=last_response_excerpt,
            clear_last_error=clear_last_error,
        )

    def finish_job(
        self, job_id, status="completed", final_report=None, error=None, root_run_id=None
    ):
        """Mark a job as finished with final status, report, duration, and trace metadata."""
        self._job_lifecycle_repository().finish(
            logger=logger,
            merge_job_meta_func=self.merge_job_meta,
            update_job_runtime_func=self.update_job_runtime,
            job_id=job_id,
            status=status,
            final_report=final_report,
            error=error,
            root_run_id=root_run_id,
        )
        normalized_status = str(status or "").strip().lower()
        if normalized_status in {
            "completed",
            "failed",
            "cancelled",
            "canceled",
            "max_iterations",
            "max-iterations",
        }:
            event_id = f"job-terminal-memory:{job_id}"
            self.enqueue_domain_event(
                event_type=TERMINAL_MEMORY_CURATION_EVENT_TYPE,
                event_id=event_id,
                aggregate_type="job",
                aggregate_id=str(job_id),
                job_id=job_id,
                severity="error" if normalized_status == "failed" else "info",
                summary="Terminal job memory curation requested",
                payload={"terminal_status": normalized_status},
                correlation_id=event_id,
            )
