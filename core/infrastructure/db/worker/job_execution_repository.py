"""Database adapter for normal worker delivery persistence and atomic claims."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from core.application.jobs.execution_contracts import TaskClaimResult


@dataclass(frozen=True)
class JobExecutionRepositoryDependencies:
    persist_meta: Callable[[Any, str, dict[str, Any] | None], None]
    mark_delivery_terminal: Callable[..., None]
    interrupted_step_snapshot: Callable[[Any, dict[str, Any]], dict[str, Any] | None]
    mark_retryable_interrupted_step: Callable[
        [Any, str, dict[str, Any], dict[str, Any] | None], bool
    ]
    job_status_is_cancelled: Callable[[object, object], bool]


class JobExecutionRepositoryAdapter:
    """Translate the application job repository port to PlatformLogger operations."""

    def __init__(self, bl: Any, dependencies: JobExecutionRepositoryDependencies) -> None:
        self._bl = bl
        self._dependencies = dependencies

    @property
    def available(self) -> bool:
        return self._bl is not None

    @property
    def project_lookup_available(self) -> bool:
        return callable(getattr(self._bl, "get_project", None))

    def get_job(self, job_id: str) -> Mapping[str, Any] | None:
        getter = getattr(self._bl, "get_job", None)
        if not callable(getter):
            return None
        row = getter(str(job_id))
        return row if isinstance(row, Mapping) else None

    def get_project(self, project_id: str) -> Mapping[str, Any] | None:
        getter = getattr(self._bl, "get_project", None)
        if not callable(getter):
            return None
        row = getter(str(project_id))
        return row if isinstance(row, Mapping) else None

    def persist_meta(self, job_id: str, meta: dict[str, Any]) -> None:
        self._dependencies.persist_meta(self._bl, job_id, meta)

    def mark_delivery_terminal(self, job_id: str, *, status: str, error: str) -> None:
        self._dependencies.mark_delivery_terminal(
            self._bl,
            job_id=job_id,
            status=status,
            error=error,
        )

    def interrupted_step_snapshot(self, job_row: dict[str, Any]) -> dict[str, Any] | None:
        return self._dependencies.interrupted_step_snapshot(self._bl, job_row)

    def mark_retryable_interrupted_step(
        self,
        job_id: str,
        job_row: dict[str, Any],
        snapshot: dict[str, Any] | None,
    ) -> bool:
        return self._dependencies.mark_retryable_interrupted_step(
            self._bl,
            job_id,
            job_row,
            snapshot,
        )

    def in_progress_owner_has_active_lease(
        self,
        job_row: Mapping[str, Any],
    ) -> bool | None:
        get_lease = getattr(self._bl, "get_project_execution_lease", None)
        if not callable(get_lease):
            return None
        project_id = str(job_row.get("project_id") or "").strip()
        owner_worker_id = str(job_row.get("worker_id") or "").strip()
        if not project_id or not owner_worker_id:
            return False
        try:
            lease = get_lease(
                project_id,
                active_only=True,
                worker_instance_id=owner_worker_id,
            )
        except Exception:
            return None
        if not isinstance(lease, Mapping):
            return False
        return str(lease.get("active_job_id") or "").strip() == str(job_row.get("id") or "").strip()

    def recover_in_progress(
        self,
        job_id: str,
        *,
        expected_worker_id: str,
        reason: str,
    ) -> bool:
        recover = getattr(self._bl, "recover_in_progress_job", None)
        if not callable(recover) or not expected_worker_id:
            return False
        return bool(
            recover(
                str(job_id),
                expected_worker_id=expected_worker_id,
                reason=reason,
            )
        )

    def claim(
        self,
        job_id: str,
        *,
        worker_id: str,
        allow_in_progress: bool,
    ) -> TaskClaimResult:
        allowed_statuses = {"pending", "stuck", "orphaned"}
        if allow_in_progress:
            allowed_statuses.add("in_progress")
        conn = getattr(self._bl, "conn", None)
        if conn is None:
            return self._claim_without_connection(job_id, worker_id, allowed_statuses)
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE jobs
                   SET status = 'in_progress',
                       worker_id = %s,
                       started_at = COALESCE(started_at, NOW()),
                       finished_at = NULL,
                       duration_ms = NULL,
                       error = NULL,
                       heartbeat_at = NOW(),
                       last_error = NULL
                 WHERE id = %s
                   AND status = ANY(%s)
                """,
                (worker_id, job_id, sorted(allowed_statuses)),
            )
            conn.commit()
            claimed = bool(cur.rowcount)
        current = None if claimed else self.get_job(job_id)
        return TaskClaimResult(claimed=claimed, current_job=current)

    def _claim_without_connection(
        self,
        job_id: str,
        worker_id: str,
        allowed_statuses: set[str],
    ) -> TaskClaimResult:
        current = self.get_job(job_id)
        status = str((current or {}).get("status") or "").strip().lower()
        if status not in allowed_statuses:
            return TaskClaimResult(claimed=False, current_job=current)
        self._bl.update_job(job_id, status="in_progress", worker_id=worker_id)
        update_runtime = getattr(self._bl, "update_job_runtime", None)
        if callable(update_runtime):
            update_runtime(job_id, heartbeat_now=True, clear_last_error=True)
        return TaskClaimResult(claimed=True)

    def is_cancelled(self, job_id: str) -> bool:
        return self._dependencies.job_status_is_cancelled(self._bl, job_id)

    def merge_meta(self, job_id: str, patch: dict[str, Any]) -> None:
        merger = getattr(self._bl, "merge_job_meta", None)
        if callable(merger):
            merger(job_id, patch)

    def finish(
        self,
        job_id: str,
        *,
        status: str,
        final_report: Any,
        error: str | None,
        root_run_id: str | None,
    ) -> None:
        self._bl.finish_job(
            job_id,
            status=status,
            final_report=final_report,
            error=error,
            root_run_id=root_run_id,
        )

    def release_lock(self, job_id: str, *, reason: str, worker_id: str) -> None:
        release = getattr(self._bl, "release_scheduled_job_execution_lock_by_job_id", None)
        if callable(release):
            release(job_id=str(job_id), release_reason=reason, worker_instance_id=worker_id)

    def log_execution_error(
        self,
        job_id: str,
        error: Exception,
        *,
        worker_id: str,
        task_id: str,
        thread_id: str,
        root_run_id: str | None,
    ) -> None:
        source_hint, category_hint = self._exception_hints(
            error,
            worker_id=worker_id,
            task_id=task_id,
            thread_id=thread_id,
        )
        normalized = self._bl.normalize_exception_error(
            error,
            source_hint=source_hint,
            category_hint=category_hint,
            context={
                "worker": worker_id,
                "task_id": task_id,
                "thread_id": thread_id,
                "trace_id": f"job:{job_id}",
                "root_run_id": root_run_id,
            },
        )
        self._bl.log_job_error(
            job_id,
            source=normalized.get("source", "system"),
            category=normalized.get("category", "unknown"),
            provider_name=normalized.get("provider_name"),
            http_status=normalized.get("http_status"),
            provider_error_code=normalized.get("provider_error_code"),
            provider_error_type=normalized.get("provider_error_type"),
            request_id=normalized.get("request_id"),
            retryable=normalized.get("retryable"),
            error_message=normalized.get("error_message", str(error)),
            response_excerpt=normalized.get("response_excerpt"),
            traceback_text=normalized.get("traceback_text"),
            context_jsonb=normalized.get("context_jsonb") or {},
        )

    def _exception_hints(
        self,
        error: Exception,
        *,
        worker_id: str,
        task_id: str,
        thread_id: str,
    ) -> tuple[str, str]:
        infer = getattr(self._bl, "infer_exception_hints", None)
        if not callable(infer):
            return "system", "unknown"
        try:
            return infer(
                error,
                context={
                    "worker": worker_id,
                    "task_id": task_id,
                    "thread_id": thread_id,
                },
            )
        except Exception:
            return "system", "unknown"
