"""Application-owned audit persistence for public workflow entrypoints."""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class WorkflowEntrypointAuditRecord:
    project_id: str | None
    account_id: str | None
    task_id: str
    task_summary: str
    status: str
    payload: dict[str, Any]
    result: dict[str, Any]
    error: str = ""
    semantic_meta: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class WorkflowEntrypointAuditRejection:
    """Transport-neutral rejected ingress facts consumed by application audit policy."""

    project_id: str | None
    workflow_id: str
    task_id: str
    reason: str
    error: str
    account_id: str | None
    request_id: str = ""
    idempotency_key_digest: str | None = None


@dataclass(frozen=True)
class WorkflowEntrypointPreparationAuditFacts:
    """Typed preparation facts evaluated by application audit policy."""

    code: str
    error: str
    project_id: str | None
    workflow_id: str
    account_id: str | None


class WorkflowEntrypointAuditPolicy:
    """Build domain audit records without transport response decisions."""

    def __init__(self, *, entry_key: str, now_iso: Callable[[], str]) -> None:
        self._entry_key = str(entry_key or "").strip()
        self._now_iso = now_iso

    def rejection(
        self,
        *,
        project_id: str | None,
        workflow_id: str,
        task_id: str,
        reason: str,
        error: str,
        account_id: str | None,
        request_id: str = "",
        idempotency_key_digest: str | None = None,
    ) -> WorkflowEntrypointAuditRecord:
        payload: dict[str, Any] = {"entry_key": self._entry_key, "reason": reason}
        if workflow_id:
            payload["workflow_id"] = workflow_id
        if request_id:
            payload["request_id"] = request_id
        if idempotency_key_digest:
            payload["idempotency_key_digest"] = idempotency_key_digest
        return WorkflowEntrypointAuditRecord(
            project_id=project_id,
            account_id=account_id,
            task_id=task_id,
            task_summary=f"Webhook entrypoint {self._entry_key}",
            status="rejected",
            payload=payload,
            result={},
            error=error,
        )

    def submission(
        self,
        *,
        command: Any,
        result: Any,
        account_id: str | None,
        idempotency_key_digest: str | None,
    ) -> WorkflowEntrypointAuditRecord:
        outcome = result.outcome
        reason = "duplicate_idempotency_key" if result.duplicate else outcome.code or "accepted"
        return WorkflowEntrypointAuditRecord(
            project_id=command.project_id,
            account_id=account_id,
            task_id=command.task_id,
            task_summary=outcome.task_summary or f"Webhook entrypoint {command.entry_key}",
            status="accepted" if outcome.ok else "rejected",
            payload={
                "entry_key": command.entry_key,
                "workflow_id": command.workflow_id,
                "request_timestamp": self._now_iso(),
                "request_id": command.request_id,
                "idempotency_key_digest": idempotency_key_digest,
                "reason": reason,
            },
            result={"job_id": outcome.job_id} if outcome.job_id else {},
            error=outcome.error if not outcome.ok else "",
            semantic_meta={"validation_errors": list(outcome.errors)},
        )

    def rejection_outcome(
        self,
        rejection: WorkflowEntrypointAuditRejection,
    ) -> WorkflowEntrypointAuditRecord | None:
        """Build an audit record from transport-neutral rejection facts."""
        return self.rejection(
            project_id=rejection.project_id,
            workflow_id=rejection.workflow_id,
            task_id=rejection.task_id or f"webhook:{self._entry_key}:rejected",
            reason=rejection.reason or "transport_rejected",
            error=rejection.error,
            account_id=rejection.account_id,
            request_id=rejection.request_id,
            idempotency_key_digest=rejection.idempotency_key_digest,
        )

    def preparation_rejection(
        self, facts: WorkflowEntrypointPreparationAuditFacts
    ) -> WorkflowEntrypointAuditRecord | None:
        """Choose whether and how a preparation failure belongs in the domain audit."""
        code = facts.code.strip()
        if code in {"entrypoint_storage_unavailable", "entrypoint_load_failed"}:
            return None
        suffix = {
            "entrypoint_not_found": "missing",
            "entrypoint_disabled": "disabled",
            "workflow_project_binding_not_found": "project-binding",
            "workflow_project_unresolved": "project-unresolved",
        }.get(code, "rejected")
        return self.rejection(
            project_id=facts.project_id,
            workflow_id=facts.workflow_id,
            task_id=f"webhook:{self._entry_key}:{suffix}",
            reason=code or "entrypoint_preparation_failed",
            error=facts.error,
            account_id=facts.account_id,
        )


class WorkflowEntrypointAuditService:
    """Persist application-defined audit records with adapter delivery metadata."""

    def __init__(self, bl: Any, *, entry_key: str, now_iso: Callable[[], str]) -> None:
        self._bl = bl
        self._policy = WorkflowEntrypointAuditPolicy(entry_key=entry_key, now_iso=now_iso)

    def record_rejection(self, *, delivery_meta: dict[str, Any], **kwargs: Any) -> None:
        self._record(self._policy.rejection(**kwargs), delivery_meta=delivery_meta)

    def record_submission(self, *, delivery_meta: dict[str, Any], **kwargs: Any) -> None:
        self._record(self._policy.submission(**kwargs), delivery_meta=delivery_meta)

    def record_rejection_outcome(
        self,
        *,
        rejection: WorkflowEntrypointAuditRejection,
        delivery_meta: dict[str, Any],
    ) -> None:
        record = self._policy.rejection_outcome(rejection)
        if record is not None:
            self._record(record, delivery_meta=delivery_meta)

    def record_preparation_rejection(
        self,
        *,
        facts: WorkflowEntrypointPreparationAuditFacts,
        delivery_meta: dict[str, Any],
    ) -> None:
        record = self._policy.preparation_rejection(facts)
        if record is not None:
            self._record(record, delivery_meta=delivery_meta)

    def _record(
        self,
        record: WorkflowEntrypointAuditRecord,
        *,
        delivery_meta: dict[str, Any],
    ) -> None:
        record_workflow_entrypoint_execution(
            self._bl,
            project_id=record.project_id,
            account_id=record.account_id,
            task_id=record.task_id,
            task_summary=record.task_summary,
            status=record.status,
            payload=record.payload,
            result=record.result,
            error=record.error,
            meta={**delivery_meta, **record.semantic_meta},
        )


def record_workflow_entrypoint_execution(
    bl: Any,
    *,
    project_id: str | None,
    account_id: str | None = None,
    task_id: str,
    task_summary: str,
    status: str,
    payload: dict[str, Any],
    result: dict[str, Any],
    error: str = "",
    meta: dict[str, Any] | None = None,
) -> None:
    """Persist the operator audit row and ingress domain event when applicable."""
    if hasattr(bl, "log_action"):
        try:
            bl.log_action(
                project_id=str(project_id or "").strip() or None,
                task_id=task_id,
                task_summary=task_summary,
                agent="workflow_entrypoint",
                action_type="workflow_entrypoint.invoke",
                status=status,
                payload=payload,
                result=result,
                error=error or None,
                meta=dict(meta or {}) or None,
            )
        except Exception as exc:
            logger.warning(
                "Workflow entrypoint audit persistence failed task_id=%s status=%s: %s",
                task_id,
                status,
                exc,
            )
    if not hasattr(bl, "record_domain_event"):
        return
    reason = str(dict(payload or {}).get("reason") or "").strip()
    if status != "rejected" and reason != "duplicate_idempotency_key":
        return

    from core.application.workflows.domain_event_runtime import project_account_id

    normalized_project_id = str(project_id or "").strip() or None
    normalized_account_id = str(account_id or "").strip() or None
    normalized_payload = dict(payload or {})
    normalized_result = dict(result or {})
    normalized_meta = dict(meta or {})
    existing_job_id = (
        str(normalized_result.get("job_id") or "").strip()
        if reason == "duplicate_idempotency_key"
        else ""
    )
    request_id = str(normalized_payload.get("request_id") or "").strip() or uuid.uuid4().hex
    event_type = (
        "workflow.ingress.duplicate"
        if reason == "duplicate_idempotency_key"
        else "workflow.ingress.rejected"
    )
    summary = (
        f"Duplicate webhook request reused job {existing_job_id}"
        if reason == "duplicate_idempotency_key" and existing_job_id
        else error or "Workflow ingress attempt was rejected before job creation"
    )
    with suppress(Exception):
        bl.record_domain_event(
            event_id=f"workflow-ingress:{request_id}:{reason or status}",
            event_type=event_type,
            account_id=normalized_account_id
            or project_account_id(bl, normalized_project_id)
            or "acc_default",
            project_id=normalized_project_id,
            workflow_id=str(normalized_payload.get("workflow_id") or "").strip() or None,
            job_id=existing_job_id or None,
            severity="warning" if reason == "duplicate_idempotency_key" else "error",
            summary=summary,
            payload={
                "channel": "webhook",
                "trigger_family": "incoming_webhook",
                "request_id": request_id,
                "reason": reason or status,
                "no_job_created": not bool(existing_job_id),
                "existing_job_id": existing_job_id or None,
                "entry_key": str(normalized_payload.get("entry_key") or "").strip() or None,
                "idempotency_key_digest": str(
                    normalized_payload.get("idempotency_key_digest") or ""
                ).strip()
                or None,
                "error": error or None,
                "http_status": normalized_meta.get("http_status"),
                "content_type": normalized_meta.get("content_type"),
                "source_ip": normalized_meta.get("source_ip"),
                "auth_outcome": normalized_meta.get("auth_outcome"),
                "validation_errors": list(normalized_meta.get("validation_errors") or []),
                "request_context_summary": dict(normalized_meta.get("payload_summary") or {}),
            },
            actor={"type": "system", "id": "workflow_entrypoint"},
            urls={},
            envelope={},
            occurred_at=datetime.now(UTC).isoformat(),
            source="workflow_entrypoint",
            aggregate_type="workflow",
            aggregate_id=str(normalized_payload.get("workflow_id") or "").strip() or None,
            correlation_id=request_id,
            status="recorded",
        )


__all__ = [
    "WorkflowEntrypointAuditPolicy",
    "WorkflowEntrypointAuditRecord",
    "WorkflowEntrypointAuditRejection",
    "WorkflowEntrypointAuditService",
    "WorkflowEntrypointPreparationAuditFacts",
    "record_workflow_entrypoint_execution",
]
