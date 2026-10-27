from __future__ import annotations

import logging
import time
from collections.abc import Callable, Mapping
from contextlib import suppress
from typing import Any

from core.application.idempotency import (
    RequestIdempotencyService,
)
from core.application.jobs.workflow_resume_state import WorkflowResumeRepositoryFactory

from .approval_decision_service import ApprovalDecisionService
from .approval_repository import (
    ApprovalRepository,
    build_approval_resume_value,
    decode_job_meta,
)
from .command_models_api_inputs import ApprovalDecisionInput, ApprovalResumeAcceptedView
from .command_services_common import _service_scope
from .errors import (
    ServiceError,
    ServiceNotFoundError,
    ServiceUnavailableError,
    ServiceValidationError,
)
from .guards import require_project_access, require_workflow_access
from .scope import ScopeContext

logger = logging.getLogger(__name__)


class ApprovalCommandService:
    _REJECT_ACTIONS = frozenset(
        {
            "reject",
            "rejected",
            "deny",
            "denied",
            "decline",
            "declined",
            "rework",
            "request_changes",
            "changes_requested",
        }
    )

    def __init__(
        self,
        *,
        resume_shutdown_in_progress: Callable[[], bool],
        get_checkpointer: Callable[[], Any],
        get_logger: Callable[[], Any],
        enqueue_workflow_resume: Callable[..., Any],
        workflow_resume_repository_factory: WorkflowResumeRepositoryFactory,
        get_thread_state_snapshot: Callable[[str], Any] | None = None,
    ):
        self._resume_shutdown_in_progress = resume_shutdown_in_progress
        self._get_checkpointer = get_checkpointer
        self._get_logger = get_logger
        self._enqueue_workflow_resume = enqueue_workflow_resume
        self._workflow_resume_repository_factory = workflow_resume_repository_factory
        self._get_thread_state_snapshot = get_thread_state_snapshot
        self._approval_repository = ApprovalRepository()

    def _workflow_resume_repository(self, bl: Any):
        return self._workflow_resume_repository_factory(bl)

    def _publish_durable_resume(
        self,
        *,
        bl: Any,
        thread_id: str,
        resume_row: Any,
    ) -> Any:
        publisher = self._enqueue_workflow_resume
        repository = self._workflow_resume_repository(bl)
        try:
            publisher(
                project_id=resume_row.project_id,
                job_id=resume_row.job_id,
                task_id=resume_row.task_id,
                thread_id=thread_id,
                resume_revision=resume_row.resume_revision,
            )
        except Exception as exc:
            with suppress(Exception):
                repository.mark_publish_failed(
                    thread_id=thread_id,
                    resume_revision=resume_row.resume_revision,
                    publish_token=resume_row.publish_token,
                    error=str(exc),
                )
            logger.warning(
                "workflow resume publish failed for thread_id=%s job_id=%s revision=%s; outbox will retry: %s",
                thread_id,
                resume_row.job_id,
                resume_row.resume_revision,
                exc,
            )
        else:
            try:
                repository.mark_published(
                    thread_id=thread_id,
                    resume_revision=resume_row.resume_revision,
                    publish_token=resume_row.publish_token,
                )
            except Exception as exc:
                logger.warning(
                    "workflow resume publish marker failed for thread_id=%s job_id=%s revision=%s; fenced outbox recovery remains active: %s",
                    thread_id,
                    resume_row.job_id,
                    resume_row.resume_revision,
                    exc,
                )
        return resume_row

    def _current_interrupt_payload(
        self,
        thread_id: str,
        *,
        attempts: int = 10,
        delay_seconds: float = 0.2,
    ) -> dict[str, Any]:
        if self._get_thread_state_snapshot is None:
            return {}
        for attempt in range(max(1, attempts)):
            try:
                from core.application.jobs.workflow_run_projection import (
                    MultipleApprovalGatesUnsupported,
                    ensure_single_approval_interrupt,
                )

                state = self._get_thread_state_snapshot(thread_id)
                interrupt = ensure_single_approval_interrupt(state)
                payload_value = getattr(interrupt, "value", {}) if interrupt is not None else {}
                raw_interrupt_id = getattr(interrupt, "id", None) or getattr(
                    interrupt, "interrupt_id", None
                )
            except MultipleApprovalGatesUnsupported:
                raise
            except Exception as exc:
                logger.warning(
                    "Could not read current approval interrupt for %s: %s",
                    thread_id,
                    exc,
                )
                return {}
            if interrupt is None:
                from core.application.orchestration.approval_payload import (
                    state_based_approval_payload,
                )

                # A Test Run's state-based gate has the same pinned identity and
                # action allow-list as its notification and public query.
                payload = state_based_approval_payload(getattr(state, "values", {}) or {})
                if payload:
                    return payload
            if isinstance(payload_value, dict) and interrupt is not None:
                payload = dict(payload_value)
                if raw_interrupt_id and not payload.get("interrupt_id"):
                    payload["interrupt_id"] = str(raw_interrupt_id)
                return payload
            if attempt < attempts - 1:
                time.sleep(max(0.0, delay_seconds))
        return {}

    @staticmethod
    def _decode_job_meta(value: Any) -> dict[str, Any]:
        return decode_job_meta(value)

    def _lock_job_row(self, bl: Any, thread_id: str) -> dict[str, Any] | None:
        return self._approval_repository.lock_job_row(bl, thread_id)

    @staticmethod
    def _current_approval_id(payload: Mapping[str, Any]) -> str:
        return str(
            payload.get("approval_id") or payload.get("interrupt_id") or payload.get("id") or ""
        ).strip()

    def _persist_canonical_decision(
        self,
        *,
        bl: Any,
        transaction_conn: Any | None,
        row: Mapping[str, Any],
        thread_id: str,
        approval_id: str,
        payload: Mapping[str, Any],
        decision: ApprovalDecisionInput,
        scope: ScopeContext,
    ) -> Any:
        snapshot = (
            self._get_thread_state_snapshot(thread_id) if self._get_thread_state_snapshot else None
        )
        values = getattr(snapshot, "values", None)
        return self._approval_repository.persist_decision(
            bl=bl,
            transaction_conn=transaction_conn,
            row=row,
            thread_id=thread_id,
            approval_id=approval_id,
            payload=payload,
            decision=decision,
            scope=scope,
            record_in_progress=self._record_in_progress_marker,
            state_values=values if isinstance(values, Mapping) else None,
        )

    @staticmethod
    def _record_in_progress_marker(
        *,
        transaction_conn: Any | None,
        row: Mapping[str, Any],
        payload: Mapping[str, Any],
        job_status: str = "in_progress",
    ) -> None:
        ApprovalRepository._record_in_progress_marker(
            transaction_conn=transaction_conn,
            row=row,
            payload=payload,
            job_status=job_status,
        )

    def _validate_current_gate(
        self,
        *,
        row: Mapping[str, Any] | None,
        payload: Mapping[str, Any],
        decision: ApprovalDecisionInput,
        token_principal: bool,
    ) -> str:
        return ApprovalDecisionService(
            lock_job_row=self._lock_job_row,
            current_interrupt_payload=self._current_interrupt_payload,
            persist_decision=self._persist_canonical_decision,
        ).validate_current_gate(
            row=row,
            payload=payload,
            decision=decision,
            token_principal=token_principal,
        )

    def _claim_and_record_decision(
        self,
        *,
        bl: Any,
        thread_id: str,
        scope: ScopeContext,
        decision: ApprovalDecisionInput,
        token_principal: bool,
        idempotency_key: str,
        before_new_decision: Callable[[], None] | None = None,
    ) -> tuple[dict[str, Any], str, RequestIdempotencyService | None, Any, bool, bool, Any]:
        return ApprovalDecisionService(
            lock_job_row=self._lock_job_row,
            current_interrupt_payload=self._current_interrupt_payload,
            persist_decision=self._persist_canonical_decision,
        ).claim_and_record(
            bl=bl,
            thread_id=thread_id,
            scope=scope,
            decision=decision,
            token_principal=token_principal,
            idempotency_key=idempotency_key,
            before_new_decision=before_new_decision,
        )

    def _require_resume_runtime(self) -> None:
        if self._resume_shutdown_in_progress():
            raise ServiceUnavailableError(
                "Service shutdown in progress — workflow resume is temporarily unavailable"
            )
        if not self._get_checkpointer():
            raise ServiceUnavailableError("Checkpointer not configured — cannot resume")

    def resume_task(
        self,
        *,
        thread_id: str,
        scope: ScopeContext | None,
        decision: ApprovalDecisionInput,
        idempotency_key: str = "",
    ) -> ApprovalResumeAcceptedView:
        interrupt_payload: dict[str, Any] = {}
        decision = ApprovalDecisionInput.model_validate(decision)
        resolved_scope = _service_scope(scope)
        auth_source = str(getattr(resolved_scope, "auth_source", "") or "").strip().lower()
        principal_type = str(getattr(resolved_scope, "principal_type", "") or "").strip().lower()
        token_principal = (
            auth_source in {"machine", "pat", "token"} or principal_type == "service_account"
        )
        if decision.external_actor is not None and auth_source != "machine":
            raise ServiceValidationError(
                "external_actor is only allowed for machine-token decisions"
            )
        if decision.action in self._REJECT_ACTIONS and decision.approved:
            raise ServiceValidationError("approved conflicts with reject action")
        if (
            token_principal
            and decision.action
            in {
                "approve",
                "approved",
                "accept",
                "accepted",
                "resume",
                "continue",
                "proceed",
                "override",
            }
            and not decision.approved
        ):
            raise ServiceValidationError("approved conflicts with approve action")

        row = None
        bl = self._get_logger()
        if bl and hasattr(bl, "conn"):
            with bl.conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT p.id, p.account_id, p.workspace_id,
                           COALESCE(
                               NULLIF(j.selected_workflow, ''),
                               NULLIF(j.selected_workflow_key, ''),
                               NULLIF(j.meta->>'selected_workflow_id', '')
                           ),
                           j.id
                      FROM jobs j
                      JOIN projects p ON p.id = j.project_id
                     WHERE j.thread_id = %s
                     LIMIT 1
                    """,
                    (thread_id,),
                )
                row = cur.fetchone()
            if row:
                project = {
                    "id": row[0],
                    "account_id": row[1],
                    "workspace_id": row[2],
                }
                require_project_access(resolved_scope, project)
                workflow_id = str(row[3] or "").strip() if len(row) > 3 else ""
                require_workflow_access(resolved_scope, workflow_id)

        if row is None:
            raise ServiceNotFoundError("Approval job not found")

        # Token-specific decision requirements are checked only after the
        # owning job/thread has been authorized, so malformed token calls cannot
        # become a cross-scope existence oracle.
        if token_principal:
            if not str(idempotency_key or "").strip():
                raise ServiceValidationError("Idempotency-Key is required for token decisions")
            if not decision.approval_id:
                raise ServiceValidationError("approval_id is required for token decisions")
            if decision.override_content:
                raise ServiceValidationError(
                    "override_content is not supported for token decisions"
                )
            requires_rejection_comment = not decision.approved and decision.action != "abort"
            if requires_rejection_comment and not decision.comment.strip():
                raise ServiceValidationError("comment is required when rejecting an approval")

        decision_result = self._claim_and_record_decision(
            bl=bl,
            thread_id=thread_id,
            scope=resolved_scope,
            decision=decision,
            token_principal=token_principal,
            idempotency_key=idempotency_key,
            before_new_decision=self._require_resume_runtime,
        )
        if len(decision_result) != 7:
            raise ServiceError("Approval decision persistence returned an invalid result")
        (
            interrupt_payload,
            canonical_approval_id,
            _idempotency_service,
            idempotency_claim,
            _accepted_in_transaction,
            has_durable_job,
            durable_resume_row,
        ) = decision_result
        if not has_durable_job:
            raise ServiceNotFoundError("Approval job not found")
        if isinstance(idempotency_claim, ApprovalResumeAcceptedView):
            return idempotency_claim

        resume_value = build_approval_resume_value(decision)

        actor_username = resolved_scope.actor_name
        effective_approved = bool(resume_value.get("approved"))

        if durable_resume_row is None:
            raise ServiceError("Approval decision did not record a workflow resume")
        resume_row = self._publish_durable_resume(
            bl=bl,
            thread_id=thread_id,
            resume_row=durable_resume_row,
        )
        return ApprovalResumeAcceptedView(
            job_id=resume_row.job_id,
            thread_id=thread_id,
            approval_id=canonical_approval_id,
            deduplicated=False,
            external_actor=decision.external_actor,
            approved=effective_approved,
            action=decision.action,
            comment=decision.comment,
            override_content=decision.override_content,
            actor=actor_username,
            actor_type=str(resolved_scope.principal_type or "").strip() or None,
            status="resume_pending",
            approval_reason=_approval_reason_of(interrupt_payload),
        )


def _approval_reason_of(interrupt_payload: Mapping[str, Any]) -> str | None:
    """authored/recovery/autonomy of the decided gate, from the host's own checkpoint."""
    gate_kind = str(interrupt_payload.get("gate_kind") or "").strip()
    if not gate_kind:
        return None
    from core.application.workflows.workflow_reaction_events import approval_reason

    return approval_reason(gate_kind)


__all__ = [name for name in globals() if not name.startswith("__")]
