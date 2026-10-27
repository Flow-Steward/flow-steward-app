"""Application orchestration policy for normal background job deliveries."""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from typing import Any

from core.application.jobs.execution_contracts import (
    RuntimeModePort,
    TaskExecutionRepository,
    TaskExecutionRequest,
    TaskGraphOutcome,
    TaskGraphPort,
    TaskNotification,
    TaskTerminalEffectsPort,
)
from core.application.jobs.queue_delivery_runtime import (
    record_queue_delivery_claimed,
    record_queue_delivery_discard,
    record_queue_delivery_failure,
    record_queue_delivery_terminal_failure,
)
from core.application.jobs.task_execution_request import (
    _build_request,
    _clean,
    _ProjectLookup,
    _queue_context,
    _with_project_scope,
)
from core.application.jobs.workflow_wait_projection import (
    DURABLE_WAIT_STATUSES,
    durable_wait_from_state,
    wait_status,
)
from core.application.management.mcp_connector_client import mcp_execution_scope

logger = logging.getLogger(__name__)

_RETRYABLE_INTERRUPTED_STEP_STATUS = "retryable_interrupted_step"
_NORMAL_TERMINAL_STATUSES = frozenset(
    {
        "completed",
        "failed",
        "stuck",
        "awaiting_approval",
        "awaiting_timer",
        "awaiting_child",
        "max_iterations",
        "cancelled",
    }
)


class TaskExecutionService:
    """Sequence delivery decisions while delegating technical effects to ports."""

    def __init__(
        self,
        *,
        repository: TaskExecutionRepository | None,
        graph: TaskGraphPort,
        terminal: TaskTerminalEffectsPort,
        runtime_mode: RuntimeModePort,
        worker_id: Callable[[], str],
    ) -> None:
        self._repository = repository
        self._graph = graph
        self._terminal = terminal
        self._runtime_mode = runtime_mode
        self._worker_id = worker_id

    def execute(self, raw_message: Mapping[str, Any] | None) -> dict[str, object]:
        message = dict(raw_message or {}) if isinstance(raw_message, Mapping) else {}
        job_id = _clean(message.get("job_id"))
        job_row_or_result = self._load_job(job_id)
        if isinstance(job_row_or_result, dict) and "action" in job_row_or_result:
            return job_row_or_result
        job_row = job_row_or_result if isinstance(job_row_or_result, Mapping) else None
        repository = self._repository
        parent_job_lookup = (
            repository.get_job if repository is not None and repository.available else None
        )
        request = _build_request(message, job_row, parent_job_lookup=parent_job_lookup)
        project_lookup = self._load_project(job_row)
        request = _with_project_scope(request, job_row, project_lookup)
        queue_context = _queue_context(message, job_row)
        validation_result = self._validate_delivery(
            request,
            job_row,
            project_lookup,
            queue_context,
        )
        if validation_result is not None:
            return validation_result
        recovery_result, allow_in_progress = self._recover_redelivery(
            request,
            job_row,
            queue_context,
        )
        if recovery_result is not None:
            return recovery_result
        claim_result = self._claim(request, job_row, queue_context, allow_in_progress)
        if isinstance(claim_result, dict):
            return claim_result
        return self._run_claimed(
            request,
            job_row,
            project_lookup.project_id,
            claimed=claim_result,
        )

    def _load_job(self, job_id: str) -> Mapping[str, Any] | dict[str, object] | None:
        repository = self._repository
        if repository is None or not repository.available or not job_id:
            return None
        try:
            job_row = repository.get_job(job_id)
        except Exception as exc:
            logger.warning("job_id=%s could not be loaded before claim: %s", job_id, exc)
            return _delivery("retry", reason="job_lookup_failed")
        if job_row is None:
            return _delivery("discard", reason="missing_job_row")
        return dict(job_row)

    def _load_project(
        self,
        job_row: Mapping[str, Any] | None,
    ) -> _ProjectLookup:
        repository = self._repository
        if repository is None or not repository.available or not job_row:
            return _ProjectLookup()
        project_id = _clean(job_row.get("project_id")) or None
        if project_id is None:
            return _ProjectLookup()
        lookup_available = bool(getattr(repository, "project_lookup_available", True))
        if not lookup_available:
            return _ProjectLookup(project_id=project_id)
        try:
            project = repository.get_project(project_id)
        except Exception as exc:
            return _ProjectLookup(project_id=project_id, attempted=True, error=exc)
        return _ProjectLookup(
            project_id=project_id,
            attempted=True,
            row=project if isinstance(project, Mapping) else None,
        )

    def _validate_delivery(
        self,
        request: TaskExecutionRequest,
        job_row: Mapping[str, Any] | None,
        project_lookup: _ProjectLookup,
        queue_context: tuple[dict[str, Any], str, str, Mapping[str, Any] | None],
    ) -> dict[str, object] | None:
        project_result = self._validate_project_reference(
            request,
            job_row,
            project_lookup,
            queue_context,
        )
        if project_result is not None:
            return project_result
        missing = _missing_required_fields(request)
        if not missing:
            return None
        message = "Queue payload missing required field(s): " + ", ".join(missing)
        self._record_terminal_delivery(
            request.job_id,
            queue_context,
            failure_class="invalid_payload",
            failure_code="missing_required_fields",
            failure_message=message,
        )
        return _delivery("dead_letter", reason="invalid_payload")

    def _validate_project_reference(
        self,
        request: TaskExecutionRequest,
        job_row: Mapping[str, Any] | None,
        project_lookup: _ProjectLookup,
        queue_context: tuple[dict[str, Any], str, str, Mapping[str, Any] | None],
    ) -> dict[str, object] | None:
        repository = self._repository
        if repository is None or not repository.available or not job_row:
            return None
        if project_lookup.error is not None:
            return self._record_retryable_delivery_failure(
                request.job_id,
                queue_context,
                failure_class="project_lookup_failed",
                failure_code="project_lookup_failed",
                failure_message=str(project_lookup.error),
                terminal_error=(
                    "RabbitMQ delivery retries were exhausted before the worker "
                    "could validate the project reference."
                ),
                reason="project_lookup_failed",
            )
        normalized_project_id = _clean(job_row.get("project_id")) or ""
        if normalized_project_id and (
            not project_lookup.attempted or isinstance(project_lookup.row, Mapping)
        ):
            return None
        failure_message = (
            "Queue payload references a project that no longer exists."
            if normalized_project_id
            else "Queued job is missing a valid project reference."
        )
        self._record_terminal_delivery(
            request.job_id,
            queue_context,
            failure_class="missing_reference",
            failure_code=(
                "project_not_found" if normalized_project_id else "missing_project_reference"
            ),
            failure_message=failure_message,
        )
        return _delivery("dead_letter", reason="missing_project_reference")

    def _recover_redelivery(
        self,
        request: TaskExecutionRequest,
        job_row: Mapping[str, Any] | None,
        queue_context: tuple[dict[str, Any], str, str, Mapping[str, Any] | None],
    ) -> tuple[dict[str, object] | None, bool]:
        repository = self._repository
        if repository is None or not repository.available or not job_row:
            return None, False
        previous_status = _clean(job_row.get("status")).lower()
        if previous_status == "cancelled":
            self._record_discard(
                request.job_id,
                queue_context,
                failure_code="cancelled_after_enqueue",
                failure_message="Job was cancelled before the worker could begin execution.",
            )
            return _delivery("discard", reason="cancelled_before_claim"), False
        if previous_status == _RETRYABLE_INTERRUPTED_STEP_STATUS:
            return _delivery("discard", reason="retryable_interrupted_step"), False
        if previous_status != "in_progress":
            return None, False
        inspect_owner = getattr(repository, "in_progress_owner_has_active_lease", None)
        owner_lease_active = inspect_owner(job_row) if callable(inspect_owner) else None
        if owner_lease_active is not False:
            if request.message.get("_queue_redelivered") is True:
                return _delivery("retry", reason="active_owner_redelivery"), False
            return _delivery("discard", reason="duplicate_in_progress"), False
        previous_worker_id = _clean(job_row.get("worker_id"))
        recover = getattr(repository, "recover_in_progress", None)
        try:
            recovered = bool(
                previous_worker_id
                and callable(recover)
                and recover(
                    request.job_id,
                    expected_worker_id=previous_worker_id,
                    reason=(
                        "Execution owner lease expired; atomically fenced before "
                        "queue redelivery claim."
                    ),
                )
            )
        except Exception as exc:
            logger.warning("job_id=%s dead-owner recovery failed: %s", request.job_id, exc)
            recovered = False
        if recovered:
            return None, False
        try:
            refreshed_job = repository.get_job(request.job_id)
        except Exception:
            refreshed_job = None
        refreshed_status = _clean((refreshed_job or {}).get("status")).lower()
        if refreshed_status not in {"pending", "stuck", "orphaned"}:
            return _delivery("discard", reason="in_progress_recovery_raced"), False
        return None, False

    def _claim(
        self,
        request: TaskExecutionRequest,
        job_row: Mapping[str, Any] | None,
        queue_context: tuple[dict[str, Any], str, str, Mapping[str, Any] | None],
        allow_in_progress: bool,
    ) -> bool | dict[str, object]:
        del job_row
        repository = self._repository
        if repository is None or not repository.available or not request.job_id:
            return False
        try:
            claim = repository.claim(
                request.job_id,
                worker_id=self._worker_id(),
                allow_in_progress=allow_in_progress,
            )
        except Exception as exc:
            return self._record_retryable_delivery_failure(
                request.job_id,
                queue_context,
                failure_class="delivery_claim_failed",
                failure_code="job_claim_failed",
                failure_message=str(exc),
                terminal_error=(
                    "RabbitMQ delivery retries were exhausted before the worker "
                    "could safely claim the job."
                ),
                reason="job_claim_failed",
            )
        if not claim.claimed:
            self._record_claim_race(request.job_id, claim.current_job, queue_context)
            return _delivery("discard", reason="job_status_changed")
        meta, queue_name, dead_letter_queue_name, policy = queue_context
        try:
            updated_meta, _state = record_queue_delivery_claimed(
                meta,
                queue_name=queue_name,
                dead_letter_queue_name=dead_letter_queue_name,
                project_policy=policy,
            )
            repository.persist_meta(request.job_id, updated_meta)
        except Exception as exc:
            logger.debug("job_id=%s delivery claim metadata failed: %s", request.job_id, exc)
        return True

    def _run_claimed(
        self,
        request: TaskExecutionRequest,
        job_row: Mapping[str, Any] | None,
        project_id: str | None,
        *,
        claimed: bool,
    ) -> dict[str, object]:
        result: dict[str, Any] = {}
        final_status = "failed"
        root_run_id: str | None = None
        graph_outcome: TaskGraphOutcome | None = None
        with (
            self._runtime_mode.activate(request.test_mode),
            mcp_execution_scope(
                account_id=_clean((job_row or {}).get("account_id")) or "acc_default",
                job_id=request.job_id,
            ),
        ):
            try:
                graph_outcome = self._graph.run(
                    request,
                    job_row=job_row,
                    project_id=project_id,
                )
                root_run_id = graph_outcome.root_run_id
                final_status, result = self._interpret_graph_outcome(request, graph_outcome)
            except GeneratorExit:
                final_status, result = self._generator_exit_result(request)
            except Exception as exc:
                final_status, result = "failed", {"status": "failed", "error": str(exc)}
                self._log_graph_error(request, exc, root_run_id)
            finally:
                final_status = self._finalize_normal(
                    request,
                    job_row,
                    project_id,
                    final_status,
                    result,
                    root_run_id,
                    graph_outcome,
                )
        return _delivery("ack", claimed=claimed, final_status=final_status)

    def _interpret_graph_outcome(
        self,
        request: TaskExecutionRequest,
        outcome: TaskGraphOutcome,
    ) -> tuple[str, dict[str, Any]]:
        if outcome.kind == "cancelled":
            return "cancelled", {**outcome.result, "status": "cancelled"}
        if outcome.kind == "generator_exit":
            raise GeneratorExit
        if outcome.kind == "error":
            repository = self._repository
            if repository is not None and repository.available and request.job_id:
                try:
                    if repository.is_cancelled(request.job_id):
                        return "cancelled", {"status": "cancelled"}
                except Exception as exc:
                    logger.debug(
                        "job_id=%s cancellation reconciliation failed: %s",
                        request.job_id,
                        exc,
                    )
            error = outcome.error or RuntimeError("Graph execution failed")
            self._log_graph_error(request, error, outcome.root_run_id)
            failed: dict[str, Any] = {"status": "failed", "error": str(error)}
            # A typed refusal (e.g. checkpoint_unavailable) keeps its code.
            error_code = str(getattr(error, "error_code", "") or "").strip()
            if error_code:
                failed["error_code"] = error_code
            return "failed", failed
        result = dict(outcome.result)
        graph_status = _clean(result.get("status")) or "in_progress"
        if graph_status not in _NORMAL_TERMINAL_STATUSES and outcome.pending_interrupt:
            # The pause the checkpoint names: a Wait timer, an authored or
            # recovery approval, or a child invocation.
            graph_status = wait_status(durable_wait_from_state(outcome.interrupt_state))
        if graph_status in _NORMAL_TERMINAL_STATUSES:
            if graph_status in {"failed", "stuck", "max_iterations"}:
                self._log_graph_failure_outcome(request, graph_status, outcome.root_run_id)
            return graph_status, result
        error = result.get("error") or (
            "Graph returned non-terminal status "
            f"'{graph_status}' without interrupt; refusing to mark job completed."
        )
        self._log_graph_failure_outcome(request, "non_terminal", outcome.root_run_id)
        return "failed", {
            **result,
            "status": "failed",
            "error": str(error),
            "unexpected_graph_status": graph_status,
        }

    def _generator_exit_result(
        self,
        request: TaskExecutionRequest,
    ) -> tuple[str, dict[str, Any]]:
        repository = self._repository
        if (
            repository is not None
            and repository.available
            and repository.is_cancelled(request.job_id)
        ):
            return "cancelled", {"status": "cancelled"}
        raise GeneratorExit

    def _finalize_normal(
        self,
        request: TaskExecutionRequest,
        job_row: Mapping[str, Any] | None,
        project_id: str | None,
        final_status: str,
        result: dict[str, Any],
        root_run_id: str | None,
        graph_outcome: TaskGraphOutcome | None,
    ) -> str:
        repository = self._repository
        if repository is None or not repository.available or not request.job_id:
            return final_status
        final_status = self._persist_normal_terminal(
            request,
            job_row,
            project_id,
            final_status,
            result,
            root_run_id,
            graph_outcome,
        )
        if final_status not in DURABLE_WAIT_STATUSES:
            try:
                repository.release_lock(
                    request.job_id,
                    reason=f"job_{final_status}",
                    worker_id=self._worker_id(),
                )
            except Exception as exc:
                logger.debug("job_id=%s scheduled lock release failed: %s", request.job_id, exc)
        if request.test_mode:
            return final_status
        try:
            self._terminal.sync_provider(
                request.job_id,
                final_status=final_status,
                repository=repository,
            )
        except Exception as exc:
            logger.warning("Provider status sync failed for job %s: %s", request.job_id, exc)
        notification = _normal_notification(
            request,
            project_id,
            final_status,
            result,
            graph_outcome=graph_outcome,
        )
        if notification is not None:
            try:
                self._terminal.notify(notification, repository=repository)
            except Exception as exc:
                logger.debug("Notification dispatch skipped for job %s: %s", request.job_id, exc)
        return final_status

    def _persist_normal_terminal(
        self,
        request: TaskExecutionRequest,
        job_row: Mapping[str, Any] | None,
        project_id: str | None,
        final_status: str,
        result: dict[str, Any],
        root_run_id: str | None,
        graph_outcome: TaskGraphOutcome | None,
    ) -> str:
        repository = self._repository
        if repository is None:
            return final_status
        persist = getattr(self._terminal, "persist", None)
        if callable(persist):
            persisted_status = persist(
                request,
                job_row=job_row,
                project_id=project_id,
                final_status=final_status,
                result=result,
                graph_outcome=graph_outcome,
            )
            if persisted_status:
                return str(persisted_status)
        outputs = result.get("workflow_outputs")
        if isinstance(outputs, dict):
            try:
                repository.merge_meta(request.job_id, {"workflow_outputs": outputs})
            except Exception as exc:
                logger.debug(
                    "job_id=%s workflow output persistence failed: %s", request.job_id, exc
                )
        error = _terminal_error(result)
        try:
            repository.finish(
                request.job_id,
                status=final_status,
                final_report=result.get("final_report"),
                error=error,
                root_run_id=root_run_id,
            )
        except Exception as exc:
            logger.warning("Could not finish job in DB: %s", exc)
        return final_status

    def _log_graph_error(
        self,
        request: TaskExecutionRequest,
        error: Exception,
        root_run_id: str | None,
    ) -> None:
        worker_id = self._worker_id()
        redacted_error = RuntimeError("graph exception detail redacted")
        logger.error(
            "job_id=%s task_id=%s thread_id=%s worker_id=%s root_run_id=%s graph execution failed",
            request.job_id,
            request.task_id,
            request.thread_id,
            worker_id,
            root_run_id,
            exc_info=(RuntimeError, redacted_error, error.__traceback__),
        )
        repository = self._repository
        if repository is None or not repository.available or not request.job_id:
            return
        try:
            repository.log_execution_error(
                request.job_id,
                error,
                worker_id=worker_id,
                task_id=request.task_id,
                thread_id=request.thread_id,
                root_run_id=root_run_id,
            )
        except Exception:
            logger.debug(
                "job_id=%s structured error persistence failed",
                request.job_id,
            )

    def _log_graph_failure_outcome(
        self,
        request: TaskExecutionRequest,
        status: str,
        root_run_id: str | None,
    ) -> None:
        logger.warning(
            "job_id=%s task_id=%s thread_id=%s worker_id=%s root_run_id=%s "
            "graph returned failure status=%s",
            request.job_id,
            request.task_id,
            request.thread_id,
            self._worker_id(),
            root_run_id,
            status,
        )

    def _record_terminal_delivery(
        self,
        job_id: str,
        queue_context: tuple[dict[str, Any], str, str, Mapping[str, Any] | None],
        *,
        failure_class: str,
        failure_code: str,
        failure_message: str,
    ) -> None:
        repository = self._repository
        if repository is None or not repository.available or not job_id:
            return
        meta, queue_name, dead_letter_queue_name, policy = queue_context
        try:
            updated_meta, _state = record_queue_delivery_terminal_failure(
                meta,
                queue_name=queue_name,
                dead_letter_queue_name=dead_letter_queue_name,
                project_policy=policy,
                failure_class=failure_class,
                failure_code=failure_code,
                failure_message=failure_message,
            )
            repository.persist_meta(job_id, updated_meta)
            repository.mark_delivery_terminal(
                job_id,
                status="triage_needed",
                error=failure_message,
            )
        except Exception as exc:
            logger.warning("job_id=%s terminal delivery persistence failed: %s", job_id, exc)

    def _record_retryable_delivery_failure(
        self,
        job_id: str,
        queue_context: tuple[dict[str, Any], str, str, Mapping[str, Any] | None],
        *,
        failure_class: str,
        failure_code: str,
        failure_message: str,
        terminal_error: str,
        reason: str,
    ) -> dict[str, object]:
        repository = self._repository
        if repository is None or not repository.available or not job_id:
            return _delivery("retry", reason=reason)
        meta, queue_name, dead_letter_queue_name, policy = queue_context
        try:
            updated_meta, _state, disposition = record_queue_delivery_failure(
                meta,
                queue_name=queue_name,
                dead_letter_queue_name=dead_letter_queue_name,
                project_policy=policy,
                failure_class=failure_class,
                failure_code=failure_code,
                failure_message=failure_message,
            )
            repository.persist_meta(job_id, updated_meta)
            if disposition == "dead_letter":
                repository.mark_delivery_terminal(
                    job_id,
                    status="triage_needed",
                    error=terminal_error,
                )
                return _delivery("dead_letter", reason=reason)
        except Exception as exc:
            logger.warning("job_id=%s delivery failure persistence failed: %s", job_id, exc)
        return _delivery("retry", reason=reason)

    def _record_discard(
        self,
        job_id: str,
        queue_context: tuple[dict[str, Any], str, str, Mapping[str, Any] | None],
        *,
        failure_code: str,
        failure_message: str,
    ) -> None:
        repository = self._repository
        if repository is None or not repository.available or not job_id:
            return
        meta, queue_name, dead_letter_queue_name, policy = queue_context
        try:
            updated_meta, _state = record_queue_delivery_discard(
                meta,
                queue_name=queue_name,
                dead_letter_queue_name=dead_letter_queue_name,
                project_policy=policy,
                failure_code=failure_code,
                failure_message=failure_message,
            )
            repository.persist_meta(job_id, updated_meta)
        except Exception as exc:
            logger.debug("job_id=%s discard persistence failed: %s", job_id, exc)

    def _record_claim_race(
        self,
        job_id: str,
        current_job: Mapping[str, Any] | None,
        fallback_context: tuple[dict[str, Any], str, str, Mapping[str, Any] | None],
    ) -> None:
        current_status = _clean((current_job or {}).get("status")).lower()
        context = _queue_context({}, current_job) if current_job else fallback_context
        cancelled = current_status == "cancelled"
        self._record_discard(
            job_id,
            context,
            failure_code="cancelled_after_dequeue" if cancelled else "job_status_changed",
            failure_message=(
                "Job was cancelled before the worker could claim it."
                if cancelled
                else "Queued message was discarded because the job status "
                f"changed to '{current_status or 'unknown'}' before claim."
            ),
        )


def _missing_required_fields(request: TaskExecutionRequest) -> list[str]:
    values = {
        "job_id": request.job_id,
        "task_id": request.task_id,
        "task_summary": request.task_summary,
        "task_source": request.task_source,
        "thread_id": request.thread_id,
    }
    return sorted(key for key, value in values.items() if not value)


def _terminal_error(result: Mapping[str, Any]) -> str | None:
    return _clean(result.get("error")) or None


def _normal_notification(
    request: TaskExecutionRequest,
    project_id: str | None,
    final_status: str,
    result: Mapping[str, Any],
    *,
    graph_outcome: TaskGraphOutcome | None = None,
) -> TaskNotification | None:
    event_type = ""
    severity = "info"
    if final_status == "awaiting_approval":
        event_type, severity = "job.awaiting_approval", "warning"
    elif final_status == "awaiting_timer":
        event_type = "job.awaiting_timer"
    elif final_status == "completed":
        event_type = "job.completed"
    elif final_status in {"failed", "max_iterations"}:
        event_type, severity = "job.failed", "error"
    elif final_status == "cancelled":
        event_type, severity = "job.failed", "warning"
    if not event_type or not project_id:
        return None
    payload = {
        "status": final_status,
        "task_id": request.task_id,
        "task_summary": request.task_summary,
        "task_source": request.task_source,
        "error": _clean(result.get("error")),
    }
    if final_status in {"awaiting_approval", "awaiting_timer"} and graph_outcome is not None:
        from core.application.jobs.workflow_wait_events import wait_notification_payload_from_state

        payload.update(
            wait_notification_payload_from_state(final_status, graph_outcome.interrupt_state)
        )
    return TaskNotification(
        project_id=project_id,
        event_type=event_type,
        severity=severity,
        workflow_id=request.workflow_id or None,
        job_id=request.job_id,
        payload=payload,
        dispatch_routes=final_status != "awaiting_timer",
    )


def _delivery(action: str, **detail: object) -> dict[str, object]:
    result: dict[str, object] = {"action": _clean(action).lower() or "ack"}
    result.update(detail)
    return result
