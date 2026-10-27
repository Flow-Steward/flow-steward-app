"""Typed persistence boundary for approval decisions and their durable resume intent."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Mapping
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from core.application.jobs.public_approval_serializer import serialize_approval_history_entry
from core.application.jobs.workflow_resume_state import (
    RESUME_REASON_APPROVAL,
    WORKFLOW_RESUME_PUBLISH_LEASE_SECONDS,
    WorkflowResumeQueueRow,
    workflow_resume_revision,
)

from .command_models_api_inputs import ApprovalDecisionInput
from .errors import (
    ServiceConflictError,
    ServiceNotFoundError,
    ServiceUnavailableError,
)
from .scope import ScopeContext

logger = logging.getLogger(__name__)


def decode_job_meta(value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        with suppress(Exception):
            value = json.loads(value)
    return dict(value) if isinstance(value, Mapping) else {}


def build_approval_resume_value(decision: ApprovalDecisionInput) -> dict[str, Any]:
    """Build the one canonical graph-resume value persisted for worker execution."""
    effective_approved = bool(decision.approved) or decision.action == "override"
    resume_value: dict[str, Any] = {
        "approved": effective_approved,
        "comment": decision.comment,
    }
    if decision.action is not None:
        resume_value["action"] = decision.action
    if decision.override_content:
        resume_value["override_content"] = decision.override_content
    return resume_value


class ApprovalRepository:
    """Own SQL locking and atomic approval history/resume-intent persistence."""

    def lock_job_row(self, bl: Any, thread_id: str) -> dict[str, Any] | None:
        conn = getattr(bl, "conn", None)
        if conn is None:
            return None
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT j.id, j.status, j.meta, j.project_id, j.thread_id,
                           j.selected_workflow, j.selected_workflow_key,
                           j.queued_at, j.started_at, j.task_id, j.account_id
                      FROM jobs j
                     WHERE j.thread_id = %s
                     FOR UPDATE
                    """,
                    (str(thread_id),),
                )
                row = cur.fetchone()
            if isinstance(row, Mapping):
                return dict(row)
            if isinstance(row, (tuple, list)) and len(row) >= 5:
                return {
                    "id": row[0],
                    "status": row[1],
                    "meta": row[2],
                    "project_id": row[3],
                    "thread_id": row[4],
                    "selected_workflow": row[5] if len(row) > 5 else None,
                    "selected_workflow_key": row[6] if len(row) > 6 else None,
                    "queued_at": row[7] if len(row) > 7 else None,
                    "started_at": row[8] if len(row) > 8 else None,
                    "task_id": row[9] if len(row) > 9 else None,
                    "account_id": row[10] if len(row) > 10 else None,
                }
        except Exception as exc:
            logger.exception("Could not lock approval job for thread_id=%s", thread_id)
            raise ServiceUnavailableError("Could not lock approval job") from exc
        return None

    @staticmethod
    def _history_entry(
        *,
        approval_id: str,
        payload: Mapping[str, Any],
        decision: ApprovalDecisionInput,
        scope: ScopeContext,
    ) -> dict[str, Any]:
        entry: dict[str, Any] = {
            "approval_id": approval_id,
            "stage": str(payload.get("stage") or ""),
            "question": str(payload.get("message") or ""),
            "content_preview": str(payload.get("content") or "")[:200],
            "approved": bool(decision.approved),
            "action": decision.action,
            "comment": decision.comment,
            "override_content_preview": str(decision.override_content or "")[:200],
            "actor": scope.actor_name,
            "actor_type": str(scope.principal_type or "") or None,
            "timestamp": datetime.now(UTC).isoformat(),
        }
        if not bool(decision.approved):
            try:
                entry["retry_ordinal"] = (
                    max(
                        0,
                        int(payload.get("rework_retry_attempt") or 0),
                    )
                    + 1
                )
            except (TypeError, ValueError):
                entry["retry_ordinal"] = 1
        if decision.external_actor is not None:
            entry["external_actor"] = decision.external_actor.model_dump(mode="json")
        return entry

    def persist_decision(
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
        record_in_progress: Callable[..., None] | None = None,
        state_values: Mapping[str, Any] | None = None,
    ) -> WorkflowResumeQueueRow | None:
        meta = decode_job_meta(row.get("meta"))
        approvals = meta.get("approvals")
        if not isinstance(approvals, list):
            approvals = []
        for existing in approvals:
            if (
                isinstance(existing, Mapping)
                and str(existing.get("approval_id") or "").strip() == approval_id
            ):
                raise ServiceConflictError(
                    "Approval gate was already resolved",
                    payload={
                        "error": "Approval gate was already resolved",
                        "code": "approval_already_resolved",
                        "approval_id": approval_id,
                        "decision": serialize_approval_history_entry(existing),
                    },
                )
        resolution_ref = None
        if (
            state_values is not None
            and state_values.get("external_effect_resolution_ref") is not None
        ):
            from core.application.orchestration.external_effect_recovery import (
                resolve_authored_approval_retry,
            )

            if transaction_conn is None:
                raise ServiceUnavailableError(
                    "External operation decisions require durable storage"
                )
            if scope.project_id and str(row.get("project_id") or "") != str(scope.project_id):
                raise ServiceNotFoundError("Approval job not found")
            try:
                resolution_ref = resolve_authored_approval_retry(
                    transaction_conn,
                    row=row,
                    state=state_values,
                    approval_id=approval_id,
                    approved=build_approval_resume_value(decision)["approved"],
                    comment=decision.comment,
                    actor_id=str(scope.actor_id or ""),
                    actor_type=str(scope.principal_type or "user"),
                    account_id=scope.account_id,
                    project_id=str(row.get("project_id") or "") or None,
                    current_gate_payload=payload,
                )
            except ValueError as exc:  # includes ExternalEffectResolutionError
                raise ServiceConflictError(str(exc), payload={"code": str(exc)}) from exc
        entry = self._history_entry(
            approval_id=approval_id, payload=payload, decision=decision, scope=scope
        )
        approvals.append(entry)
        meta["approvals"] = approvals
        revision = workflow_resume_revision(meta) + 1
        publish_token = uuid4().hex
        effective_approved = bool(decision.approved) or decision.action == "override"
        resume_value = {
            "reason": RESUME_REASON_APPROVAL,
            **build_approval_resume_value(decision),
        }
        if resolution_ref is not None:
            resume_value["external_effect_resolution_ref"] = resolution_ref
        meta["workflow_resume"] = {
            "reason": RESUME_REASON_APPROVAL,
            "approval_id": approval_id,
            "state": "pending",
            "revision": revision,
            "resume_value": resume_value,
            "approved": effective_approved,
            "action": decision.action,
            "comment": decision.comment,
            "interrupt_payload": dict(payload),
            "actor_username": scope.actor_name,
            "queued_at": datetime.now(UTC).isoformat(),
            "published_at": None,
            "lease_token": "",
            "lease_expires_at": None,
            "last_publish_error": "",
            "publish_token": publish_token,
            "publish_lease_expires_at": (
                datetime.now(UTC) + timedelta(seconds=WORKFLOW_RESUME_PUBLISH_LEASE_SECONDS)
            ).isoformat(),
        }
        job_status = "resume_pending"
        resume_row = WorkflowResumeQueueRow(
            job_id=str(row.get("id") or ""),
            project_id=str(row.get("project_id") or ""),
            task_id=str(row.get("task_id") or ""),
            resume_revision=revision,
            publish_token=publish_token,
        )
        conn = getattr(bl, "conn", None)
        if conn is None:
            return None
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE jobs
                   SET meta = %s,
                       status = %s,
                       finished_at = NULL,
                       duration_ms = NULL,
                       final_report = NULL,
                       error = NULL,
                       heartbeat_at = NOW(),
                       last_error = NULL
                 WHERE thread_id = %s
                   AND status = 'awaiting_approval'
                """,
                (
                    json.dumps(meta, sort_keys=True, separators=(",", ":")),
                    job_status,
                    str(thread_id),
                ),
            )
            rowcount = getattr(cur, "rowcount", None)
            if isinstance(rowcount, int) and rowcount != 1:
                raise ServiceConflictError(
                    "Workflow resume was not accepted because the job is no longer awaiting approval"
                )
        marker = record_in_progress or self._record_in_progress_marker
        marker(
            transaction_conn=transaction_conn,
            row=row,
            payload=payload,
            job_status=job_status,
        )
        return resume_row

    @staticmethod
    def _record_in_progress_marker(
        *,
        transaction_conn: Any | None,
        row: Mapping[str, Any],
        payload: Mapping[str, Any],
        job_status: str = "in_progress",
    ) -> None:
        job_id = str(row.get("id") or "").strip()
        project_id = str(row.get("project_id") or "").strip()
        if transaction_conn is None or not job_id or not project_id:
            return
        from core.application.jobs.public_progress_repository import PublicProgressRepository
        from core.application.jobs.public_progress_service import PublicProgressService

        service = PublicProgressService(PublicProgressRepository(transaction_conn))
        try:
            current = service.current_snapshot(job_id=job_id).snapshot
        except LookupError:
            current = None
        step_id = str(payload.get("step_id") or "").strip()
        step_execution_id = str(payload.get("step_execution_id") or "").strip()
        awaiting = (
            [step for step in current.active_steps if step.status == "awaiting_approval"]
            if current is not None
            else []
        )
        matching = [
            step
            for step in awaiting
            if (not step_id or step.step_id == step_id)
            and (not step_execution_id or step.step_execution_id == step_execution_id)
        ]
        resolved_step = matching[0] if len(matching) == 1 else None
        if resolved_step is None and len(awaiting) == 1:
            resolved_step = awaiting[0]
        occurred_at = datetime.now(UTC)
        transition: dict[str, Any] = {
            "job_id": job_id,
            "project_id": project_id,
            "workflow_id": str(
                row.get("selected_workflow") or row.get("selected_workflow_key") or "unknown"
            ),
            "job_status": job_status,
            "occurred_at": occurred_at,
            "queued_at": row.get("queued_at"),
            "started_at": row.get("started_at"),
        }
        # An approval is a durable checkpoint interrupt and is projected
        # through WorkflowRunStateV1.approval, not through active_steps. Older
        # producers may also have emitted an awaiting step; complete it when it
        # can be identified, but never make that optional projection authoritative
        # for accepting a durable decision.
        if resolved_step is not None:
            transition["step"] = resolved_step.model_copy(
                update={"status": "completed", "finished_at": occurred_at}
            )
        service.record_transition(**transition)


__all__ = ["ApprovalRepository", "build_approval_resume_value", "decode_job_meta"]
