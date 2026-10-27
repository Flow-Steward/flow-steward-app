"""Application orchestration policy for durable approval-resume execution."""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from core.application.jobs.execution_contracts import (
    ApprovalPublisher,
    TokenFactory,
    WorkflowResumeGraphOutcome,
    WorkflowResumeGraphPort,
    WorkflowResumeLeasePort,
    WorkflowResumeRequest,
    WorkflowResumeTerminalContext,
    WorkflowResumeTerminalEffectsPort,
)
from core.application.jobs.workflow_resume_state import (
    WORKFLOW_RESUME_FINALIZATION_STEPS,
    WORKFLOW_RESUME_MAX_ATTEMPTS,
    WorkflowResumeWorkerRepository,
    coerce_resume_meta,
    workflow_resume_intent,
)
from core.application.jobs.workflow_wait_projection import DURABLE_WAIT_STATUSES

logger = logging.getLogger(__name__)

WORKFLOW_RESUME_LEASE_SECONDS = 300
WORKFLOW_RESUME_OUTBOX_BATCH_SIZE = 100
WORKFLOW_RESUME_TERMINAL_STATUSES = frozenset(
    {
        "completed",
        "failed",
        "stuck",
        "max_iterations",
        "cancelled",
    }
)
# A resumed run either ends or parks again on a durable pause.
WORKFLOW_RESUME_FINALIZABLE_STATUSES = WORKFLOW_RESUME_TERMINAL_STATUSES | DURABLE_WAIT_STATUSES


@dataclass(frozen=True)
class _JobLookup:
    row: Mapping[str, Any] | None = None
    failure: dict[str, object] | None = None


class WorkflowResumeExecutionService:
    """Own approval revision, fence, graph-outcome, and finalizer sequencing."""

    def __init__(
        self,
        *,
        repository: WorkflowResumeWorkerRepository | None,
        graph: WorkflowResumeGraphPort,
        lease: WorkflowResumeLeasePort,
        terminal: WorkflowResumeTerminalEffectsPort,
        worker_id: Callable[[], str],
        token_factory: TokenFactory,
    ) -> None:
        self._repository = repository
        self._graph = graph
        self._lease = lease
        self._terminal = terminal
        self._worker_id = worker_id
        self._token_factory = token_factory

    def execute(self, raw_message: Mapping[str, Any] | None) -> dict[str, object]:
        message = dict(raw_message or {}) if isinstance(raw_message, Mapping) else {}
        job_lookup = self._load_job(_clean(message.get("job_id")))
        if job_lookup.failure is not None:
            return job_lookup.failure
        identifiers = _message_identifiers(message)
        if identifiers is None:
            return _delivery("dead_letter", reason="invalid_workflow_resume_payload")
        job_id, thread_id, resume_revision = identifiers
        job_row = dict(job_lookup.row) if isinstance(job_lookup.row, Mapping) else None
        if job_row is None:
            return _delivery("retry", reason="workflow_resume_job_lookup_failed")
        intent = workflow_resume_intent(job_row)
        validation = _validate_intent(job_row, intent, resume_revision)
        if validation is not None:
            return validation
        previous_status = _clean(job_row.get("status")).lower()
        if previous_status in WORKFLOW_RESUME_FINALIZABLE_STATUSES:
            return self._recover_terminal(
                job_id=job_id,
                resume_revision=resume_revision,
                job_row=job_row,
                intent=intent,
            )
        return self._execute_claimed_resume(
            message=message,
            job_id=job_id,
            thread_id=thread_id,
            resume_revision=resume_revision,
            job_row=job_row,
            intent=intent,
        )

    def dispatch_pending(
        self,
        *,
        publisher: ApprovalPublisher,
        batch_size: int = WORKFLOW_RESUME_OUTBOX_BATCH_SIZE,
    ) -> int:
        repository = self._repository
        if repository is None or not repository.available:
            return 0
        recovered = 0
        for row in repository.pending_rows(batch_size=max(1, int(batch_size or 100))):
            normalized = _pending_row(row)
            if normalized is None:
                continue
            (
                job_id,
                project_id,
                task_id,
                thread_id,
                resume_revision,
                publish_state,
                publish_token,
            ) = normalized
            try:
                publisher(
                    project_id=project_id,
                    job_id=job_id,
                    task_id=task_id,
                    thread_id=thread_id,
                    resume_revision=resume_revision,
                )
            except Exception as exc:
                repository.mark_publish_state(
                    job_id=job_id,
                    resume_revision=resume_revision,
                    publish_token=publish_token,
                    expected_state=publish_state,
                    state=_publish_target_state(publish_state, succeeded=False),
                    error=str(exc),
                )
                logger.warning(
                    "workflow resume outbox publish failed job_id=%s revision=%s",
                    job_id,
                    resume_revision,
                )
                continue
            updated = repository.mark_publish_state(
                job_id=job_id,
                resume_revision=resume_revision,
                publish_token=publish_token,
                expected_state=publish_state,
                state=_publish_target_state(publish_state, succeeded=True),
            )
            if updated:
                recovered += 1
        return recovered

    def _load_job(self, job_id: str) -> _JobLookup:
        repository = self._repository
        if not job_id or repository is None or not repository.available:
            return _JobLookup()
        try:
            job_row = repository.get_job(job_id)
        except Exception as exc:
            logger.warning("job_id=%s could not be loaded before claim: %s", job_id, exc)
            return _JobLookup(failure=_delivery("retry", reason="job_lookup_failed"))
        if job_row is None:
            return _JobLookup(failure=_delivery("discard", reason="missing_job_row"))
        return _JobLookup(row=dict(job_row))

    def _execute_claimed_resume(
        self,
        *,
        message: Mapping[str, Any],
        job_id: str,
        thread_id: str,
        resume_revision: int,
        job_row: Mapping[str, Any],
        intent: Mapping[str, Any],
    ) -> dict[str, object]:
        repository = self._require_repository()
        claim = repository.claim_execution(
            job_id=job_id,
            resume_revision=resume_revision,
            worker_id=self._worker_id(),
            lease_token=self._token_factory(),
            lease_seconds=WORKFLOW_RESUME_LEASE_SECONDS,
        )
        if not claim.claimed:
            return _delivery("discard", reason="workflow_resume_claim_lost")
        if claim.exhausted:
            return self._retry_or_exhaust(
                job_id=job_id,
                resume_revision=resume_revision,
                lease_token=claim.lease_token,
                attempts=claim.attempts,
                error="Workflow resume attempts exhausted",
            )
        heartbeat = self._lease.start_execution(
            job_id=job_id,
            resume_revision=resume_revision,
            lease_token=claim.lease_token,
        )
        try:
            try:
                request = _resume_request(message, job_id, thread_id, resume_revision, intent)
                outcome = self._graph.resume(request, cancelled=heartbeat.cancelled)
            except Exception as exc:
                if heartbeat.cancelled():
                    return _delivery("discard", reason="workflow_resume_lease_lost")
                logger.exception("workflow resume job_id=%s failed before graph outcome", job_id)
                return self._retry_or_exhaust(
                    job_id=job_id,
                    resume_revision=resume_revision,
                    lease_token=claim.lease_token,
                    attempts=claim.attempts,
                    error=str(exc),
                )
        finally:
            heartbeat.stop()
        if heartbeat.cancelled():
            return _delivery("discard", reason="workflow_resume_lease_lost")
        retry_result = self._handle_retryable_outcome(
            job_id=job_id,
            resume_revision=resume_revision,
            lease_token=claim.lease_token,
            outcome=outcome,
            attempts=claim.attempts,
        )
        if retry_result is not None:
            return retry_result
        if _is_finalizable_outcome(outcome):
            return self._finalize_new_terminal(
                job_id=job_id,
                resume_revision=resume_revision,
                lease_token=claim.lease_token,
                job_row=job_row,
                outcome=outcome,
            )
        completed = repository.complete(
            job_id=job_id,
            resume_revision=resume_revision,
            lease_token=claim.lease_token,
        )
        if not completed:
            return _delivery("discard", reason="workflow_resume_lease_lost")
        return _delivery("ack", reason="workflow_resume_accepted")

    def _handle_retryable_outcome(
        self,
        *,
        job_id: str,
        resume_revision: int,
        lease_token: str,
        outcome: WorkflowResumeGraphOutcome,
        attempts: int,
    ) -> dict[str, object] | None:
        if not outcome.started or outcome.retryable:
            return self._retry_or_exhaust(
                job_id=job_id,
                resume_revision=resume_revision,
                lease_token=lease_token,
                attempts=attempts,
                error=_clean(outcome.reason or outcome.error),
            )
        return None

    def _retry_or_exhaust(
        self,
        *,
        job_id: str,
        resume_revision: int,
        lease_token: str,
        attempts: int,
        error: str,
    ) -> dict[str, object]:
        repository = self._require_repository()
        if attempts >= WORKFLOW_RESUME_MAX_ATTEMPTS:
            try:
                completed = repository.complete_failed(
                    job_id=job_id,
                    resume_revision=resume_revision,
                    lease_token=lease_token,
                    worker_id=self._worker_id(),
                    error=error,
                )
            except Exception:
                # A failed transaction leaves the running intent durable. Defer
                # terminal persistence; subsequent claims never re-enter graph code.
                logger.exception("workflow resume job_id=%s exhausted persistence failed", job_id)
            else:
                if not completed:
                    return _delivery("discard", reason="workflow_resume_lease_lost")
                finalized = self._run_finalizer(
                    job_id=job_id,
                    resume_revision=resume_revision,
                    job_row={},
                    final_status="failed",
                    result={"status": "failed", "error_code": "resume_failed", "error": error},
                    completed_steps={},
                )
                return finalized or _delivery("ack", reason="workflow_resume_exhausted")
        deferred = repository.mark_retryable(
            job_id=job_id,
            resume_revision=resume_revision,
            lease_token=lease_token,
            error=error,
        )
        return _delivery("ack" if deferred else "discard", reason="workflow_resume_deferred")

    def _finalize_new_terminal(
        self,
        *,
        job_id: str,
        resume_revision: int,
        lease_token: str,
        job_row: Mapping[str, Any],
        outcome: WorkflowResumeGraphOutcome,
    ) -> dict[str, object]:
        repository = self._require_repository()
        final_status = _clean(outcome.status).lower()
        result = dict(outcome.result)
        if not repository.mark_finalizing(
            job_id=job_id,
            resume_revision=resume_revision,
            lease_token=lease_token,
            final_status=final_status,
            result=result,
            error=_clean(outcome.error) or None,
        ):
            return _delivery("discard", reason="workflow_resume_lease_lost")
        finalized = self._run_finalizer(
            job_id=job_id,
            resume_revision=resume_revision,
            job_row=job_row,
            final_status=final_status,
            result=result,
            completed_steps={},
            incomplete_result=_delivery(
                "discard",
                reason="workflow_resume_lease_lost",
            ),
        )
        if finalized is not None:
            return finalized
        return _delivery("ack", reason="workflow_resume_accepted")

    def _recover_terminal(
        self,
        *,
        job_id: str,
        resume_revision: int,
        job_row: Mapping[str, Any],
        intent: Mapping[str, Any],
    ) -> dict[str, object]:
        final_status = _clean(intent.get("final_status") or job_row.get("status")).lower()
        result = coerce_resume_meta(intent.get("result"))
        completed_steps = intent.get("finalization_steps")
        finalized = self._run_finalizer(
            job_id=job_id,
            resume_revision=resume_revision,
            job_row=job_row,
            final_status=final_status,
            result=result,
            completed_steps=(completed_steps if isinstance(completed_steps, Mapping) else {}),
        )
        if finalized is not None:
            return finalized
        return _delivery("ack", reason="workflow_resume_finalized")

    def _run_finalizer(
        self,
        *,
        job_id: str,
        resume_revision: int,
        job_row: Mapping[str, Any],
        final_status: str,
        result: Mapping[str, Any],
        completed_steps: Mapping[str, Any],
        incomplete_result: dict[str, object] | None = None,
    ) -> dict[str, object] | None:
        repository = self._require_repository()
        finalizer_token = self._token_factory()
        if not repository.claim_finalizer(
            job_id=job_id,
            resume_revision=resume_revision,
            finalizer_token=finalizer_token,
            lease_seconds=WORKFLOW_RESUME_LEASE_SECONDS,
        ):
            return _delivery("discard", reason="workflow_resume_finalizer_claim_lost")
        heartbeat = self._lease.start_finalizer(
            job_id=job_id,
            resume_revision=resume_revision,
            finalizer_token=finalizer_token,
        )
        try:
            fresh = repository.get_job(job_id)
            if isinstance(fresh, Mapping):
                job_row = fresh
                fresh_steps = workflow_resume_intent(fresh).get("finalization_steps")
                if isinstance(fresh_steps, Mapping):
                    completed_steps = fresh_steps
            context = _terminal_context(
                job_id=job_id,
                job_row=job_row,
                final_status=final_status,
                result=result,
                resume_revision=resume_revision,
                finalizer_token=finalizer_token,
            )
            if (
                int(workflow_resume_intent(job_row).get("finalization_attempts") or 0)
                > WORKFLOW_RESUME_MAX_ATTEMPTS
            ):
                raise RuntimeError("Workflow resume finalization attempts exhausted")
            self._run_finalization_steps(context, completed_steps, heartbeat.cancelled)
            if heartbeat.cancelled():
                raise RuntimeError("Lost approval-resume finalizer fence before completion")
            completed = repository.complete(
                job_id=job_id,
                resume_revision=resume_revision,
                finalizer_token=finalizer_token,
            )
        except Exception as exc:
            if heartbeat.cancelled():
                return _delivery("discard", reason="workflow_resume_finalizer_lease_lost")
            deferred = repository.release_finalizer(
                job_id=job_id,
                resume_revision=resume_revision,
                finalizer_token=finalizer_token,
                error=str(exc),
            )
            logger.exception("workflow resume job_id=%s terminal finalizer failed", job_id)
            return _delivery(
                "ack" if deferred else "discard", reason="workflow_resume_finalization_deferred"
            )
        finally:
            heartbeat.stop()
        if not completed:
            return incomplete_result or _delivery(
                "discard",
                reason="workflow_resume_finalization_incomplete",
            )
        return None

    def _run_finalization_steps(
        self,
        context: WorkflowResumeTerminalContext,
        completed_steps: Mapping[str, Any],
        cancelled: Callable[[], bool],
    ) -> None:
        if context.final_status not in WORKFLOW_RESUME_FINALIZABLE_STATUSES:
            return
        first_failure: Exception | None = None
        for step in WORKFLOW_RESUME_FINALIZATION_STEPS:
            if _clean(completed_steps.get(step)).lower() == "completed":
                continue
            if cancelled():
                raise RuntimeError("Lost approval-resume finalizer fence before side effect")
            # A parked run keeps its lock and provider state; its notification
            # step reports the wait (an approval to routes and reactions, a
            # timer to reactions, a child wait catches up a pending approval).
            parked = context.final_status in DURABLE_WAIT_STATUSES
            effect_failure: Exception | None = None
            if not parked or step == "notification":
                try:
                    self._run_terminal_effect(step, context)
                except Exception as exc:
                    effect_failure = exc
            if cancelled():
                raise RuntimeError("Lost approval-resume finalizer fence after side effect")
            if effect_failure is not None:
                # Independent effects still run under the same live fence.
                # Only successful effects enter the journal and avoid retry.
                if first_failure is None:
                    first_failure = effect_failure
                continue
            if not self._require_repository().mark_finalization_step_completed(
                job_id=context.job_id,
                resume_revision=context.resume_revision,
                finalizer_token=context.finalizer_token,
                step=step,
            ):
                raise RuntimeError(f"Lost approval-resume finalizer fence while recording {step}")
        if first_failure is not None:
            raise first_failure

    def _run_terminal_effect(
        self,
        step: str,
        context: WorkflowResumeTerminalContext,
    ) -> None:
        if step == "lock_release":
            self._terminal.release_lock(context)
        elif step == "notification":
            if not _resume_test_mode(context.job_row):
                self._terminal.notify(context)
        elif step == "provider_sync":
            self._terminal.sync_provider(context)

    def _require_repository(self) -> WorkflowResumeWorkerRepository:
        repository = self._repository
        if repository is None or not repository.available:
            raise RuntimeError("Workflow resume repository is unavailable")
        return repository


def _validate_intent(
    job_row: Mapping[str, Any],
    intent: Mapping[str, Any],
    resume_revision: int,
) -> dict[str, object] | None:
    try:
        intent_revision = int(intent.get("revision") or 0)
    except (TypeError, ValueError):
        intent_revision = 0
    if resume_revision <= 0 or intent_revision != resume_revision:
        return _delivery("discard", reason="workflow_resume_revision_mismatch")
    status = _clean(job_row.get("status")).lower()
    state = _clean(intent.get("state")).lower()
    allowed = {"resume_pending", "in_progress"} | set(WORKFLOW_RESUME_FINALIZABLE_STATUSES)
    if status not in allowed:
        return _delivery("discard", reason="workflow_resume_status_changed")
    if status in WORKFLOW_RESUME_FINALIZABLE_STATUSES and state != "finalizing":
        return _delivery("discard", reason="workflow_resume_status_changed")
    return None


def _resume_request(
    message: Mapping[str, Any],
    job_id: str,
    thread_id: str,
    resume_revision: int,
    intent: Mapping[str, Any],
) -> WorkflowResumeRequest:
    action_value = intent.get("action")
    return WorkflowResumeRequest(
        message=dict(message),
        job_id=job_id,
        thread_id=thread_id,
        resume_revision=resume_revision,
        approval_id=_clean(intent.get("approval_id")),
        resume_value=coerce_resume_meta(intent.get("resume_value")),
        approved=bool(intent.get("approved")),
        action=_clean(action_value) if action_value is not None else None,
        comment=_clean(intent.get("comment")),
        interrupt_payload=coerce_resume_meta(intent.get("interrupt_payload")),
        actor_username=_clean(intent.get("actor_username")) or "operator",
    )


def _message_identifiers(message: Mapping[str, Any]) -> tuple[str, str, int] | None:
    job_id = _clean(message.get("job_id"))
    thread_id = _clean(message.get("thread_id"))
    try:
        revision = int(message.get("resume_revision") or 0)
    except (TypeError, ValueError):
        revision = 0
    if not job_id or not thread_id or revision <= 0:
        return None
    return job_id, thread_id, revision


def _is_finalizable_outcome(outcome: WorkflowResumeGraphOutcome) -> bool:
    return (
        bool(outcome.terminal)
        or _clean(outcome.status).lower() in WORKFLOW_RESUME_FINALIZABLE_STATUSES
    )


def _resume_test_mode(job_row: Mapping[str, Any]) -> bool:
    meta = coerce_resume_meta(job_row.get("meta"))
    value = meta.get("test_mode")
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off", ""}:
            return False
    return bool(value)


def _terminal_context(
    *,
    job_id: str,
    job_row: Mapping[str, Any],
    final_status: str,
    result: Mapping[str, Any],
    resume_revision: int,
    finalizer_token: str,
) -> WorkflowResumeTerminalContext:
    meta = coerce_resume_meta(job_row.get("meta"))
    event_type = ""
    severity = "info"
    if final_status == "completed":
        event_type = "job.completed"
    elif final_status == "awaiting_approval":
        event_type, severity = "job.awaiting_approval", "warning"
    elif final_status == "awaiting_timer":
        event_type = "job.awaiting_timer"
    elif final_status in {"failed", "max_iterations", "stuck"}:
        event_type, severity = "job.failed", "error"
    elif final_status == "cancelled":
        event_type, severity = "job.failed", "warning"
    workflow_id = _clean(
        job_row.get("workflow_id")
        or job_row.get("selected_workflow")
        or job_row.get("selected_workflow_id")
        or job_row.get("selected_workflow_key")
        or meta.get("selected_workflow_id")
        or meta.get("requested_workflow_id")
        or meta.get("workflow_id")
    )
    return WorkflowResumeTerminalContext(
        job_id=job_id,
        job_row=job_row,
        final_status=final_status,
        result=dict(result),
        resume_revision=resume_revision,
        finalizer_token=finalizer_token,
        workflow_id=workflow_id,
        notification_event_type=event_type,
        notification_severity=severity,
    )


def _pending_row(row: Mapping[str, Any]) -> tuple[str, str, str, str, int, str, str] | None:
    job_id = _clean(row.get("id"))
    project_id = _clean(row.get("project_id"))
    task_id = _clean(row.get("task_id"))
    thread_id = _clean(row.get("thread_id"))
    publish_state = _clean(row.get("publish_state")).lower()
    publish_token = _clean(row.get("publish_token"))
    try:
        revision = int(row.get("resume_revision") or 0)
    except (TypeError, ValueError):
        revision = 0
    if (
        not job_id
        or not project_id
        or not task_id
        or not thread_id
        or revision <= 0
        or not publish_state
        or not publish_token
    ):
        return None
    return (
        job_id,
        project_id,
        task_id,
        thread_id,
        revision,
        publish_state,
        publish_token,
    )


def _publish_target_state(expected_state: str, *, succeeded: bool) -> str:
    if expected_state in {"pending", "publish_failed"}:
        return "published" if succeeded else "publish_failed"
    return expected_state


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _delivery(action: str, **detail: object) -> dict[str, object]:
    result: dict[str, object] = {"action": _clean(action).lower() or "ack"}
    result.update(detail)
    return result
