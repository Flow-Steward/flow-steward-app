"""Authoritative safe projection of a durable job as WorkflowRunStateV1."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from core.application.jobs.job_artifact_access import artifact_metadata_belongs_to_job
from core.application.jobs.public_approval_serializer import (
    bounded_approval_actions,
    bounded_approval_content,
    bounded_approval_text,
    build_approval_notification_payload,
)
from core.application.jobs.public_secret_redaction import contains_public_secret
from core.application.jobs.workflow_identity import resolve_job_workflow_id
from core.application.jobs.workflow_run_models import (
    PublicApprovalGateV1,
    PublicWorkflowErrorV1,
    PublicWorkflowResultV1,
    PublicWorkflowStepV1,
    WorkflowRunStateV1,
)
from core.application.observability_vocabulary import job_status_meta, normalize_job_status

_ARTIFACT_KEYS = frozenset(
    {
        "artifact_id",
        "id",
        "filename",
        "content_type",
        "mime_type",
        "size_bytes",
        "sha256",
        "retention_class",
    }
)
_ERROR_DETAIL_KEYS = frozenset(
    {"http_status", "request_id", "provider_code", "provider_error_code", "message_id"}
)
_PUBLIC_ERROR_MESSAGES = {
    "cancelled": "Workflow execution was cancelled",
    "max_iterations": "Workflow execution exhausted its retry limit",
    "retryable_interrupted_step": "Workflow execution was interrupted and can be retried",
    "triage_needed": "Workflow execution requires operator review",
}
_DEFAULT_PUBLIC_ERROR_MESSAGE = "Workflow execution did not complete successfully"
_ARTIFACT_HANDLE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,254}$")
_PUBLIC_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,254}$")
_PUBLIC_PATH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\[\]-]{0,255}$")
_SENSITIVE_VALUE_RE = re.compile(
    r"(?i)(?:bearer\s+|(?:api[_-]?key|token|secret|password|credential)\s*[:=]|"
    r"(?:https?|s3)://|AKIA[0-9A-Z]{16})"
)


class MultipleApprovalGatesUnsupported(RuntimeError):
    code = "multiple_approval_gates_unsupported"

    def __init__(self) -> None:
        super().__init__("Multiple simultaneous approval gates are not supported")


class ArtifactResolverUnavailable(RuntimeError):
    """Artifact handles cannot be authenticated because storage is unavailable."""


def ensure_single_approval_interrupt(state: Any) -> Any | None:
    interrupts = []
    for task in getattr(state, "tasks", None) or []:
        interrupts.extend(list(getattr(task, "interrupts", None) or []))
    if len(interrupts) > 1:
        raise MultipleApprovalGatesUnsupported()
    return interrupts[0] if interrupts else None


def approval_notification_payload_from_state(state: Any) -> dict[str, Any]:
    """Project one checkpoint interrupt into a bounded notification payload."""
    from core.application.jobs.public_approval_serializer import (
        build_approval_notification_payload,
    )

    interrupt = ensure_single_approval_interrupt(state)
    if interrupt is None:
        from core.application.orchestration.approval_payload import (
            state_based_approval_payload,
        )

        payload = state_based_approval_payload(_object(getattr(state, "values", {})))
        if not payload:
            return {}
    else:
        payload = _object(getattr(interrupt, "value", {}))
    interrupt_id = str(
        getattr(interrupt, "id", None) or getattr(interrupt, "interrupt_id", None) or ""
    ).strip()
    if interrupt_id and not payload.get("approval_id"):
        payload["approval_id"] = interrupt_id
    if not payload.get("wait_id"):
        # An authored Approval's wait was recorded by the hub; a recovery or
        # autonomy pause has none and gets its recovery identity downstream.
        from core.application.jobs.workflow_wait_projection import durable_wait_from_state

        wait = durable_wait_from_state(state)
        if wait is not None and str(wait.get("kind") or "") == "approval" and wait.get("wait_id"):
            payload["wait_id"] = str(wait["wait_id"])
    return build_approval_notification_payload(payload)


def _object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError):
            return {}
        return dict(parsed) if isinstance(parsed, dict) else {}
    return {}


def _text(value: Any, *, limit: int = 1000) -> str:
    return str(value or "").strip()[:limit]


def _safe_public_identifier(value: Any, *, limit: int, fallback: str = "") -> str:
    text = str(value or "").strip()
    if (
        not text
        or len(text) > limit
        or contains_public_secret(text)
        or _SENSITIVE_VALUE_RE.search(text)
        or not _PUBLIC_IDENTIFIER_RE.fullmatch(text)
    ):
        return fallback
    return text


def _datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
    if isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)
    return None


def _artifact_belongs_to_job(artifact: dict[str, Any], job: dict[str, Any]) -> bool:
    project_id = _text(job.get("project_id"), limit=255)
    job_id = _text(job.get("id"), limit=255)
    if artifact_metadata_belongs_to_job(
        artifact,
        project_id=project_id,
        job_id=job_id,
    ):
        return True
    run_id = _text(artifact.get("run_id"), limit=255)
    return bool(job_id and run_id == f"job:{job_id}")


def _artifact_id_from_reference(value: Any) -> str:
    if isinstance(value, dict):
        raw_handle = value.get("artifact_id") or value.get("id")
    elif isinstance(value, str):
        raw_handle = value
    else:
        return ""
    handle = str(raw_handle or "").strip()
    if handle.startswith("artifact:"):
        handle = handle.removeprefix("artifact:").strip()
    if not handle or not _ARTIFACT_HANDLE_RE.fullmatch(handle):
        return ""
    return _safe_public_identifier(handle, limit=255)


def _safe_artifacts(
    value: Any,
    *,
    job: dict[str, Any],
    resolve_artifact: Callable[[str], dict[str, Any] | None] | None,
) -> dict[str, Any]:
    artifacts = _object(value)
    safe: dict[str, Any] = {}
    for name, artifact in artifacts.items():
        key = _text(name, limit=128)
        if not key:
            continue
        if isinstance(artifact, list):
            # A returned collection is public only as a whole: one file that
            # cannot be authenticated withholds the output.
            files = [
                _safe_artifact(item, job=job, resolve_artifact=resolve_artifact)
                for item in artifact
            ]
            if files and all(files):
                safe[key] = files
            continue
        canonical = _safe_artifact(artifact, job=job, resolve_artifact=resolve_artifact)
        if canonical:
            safe[key] = canonical
    return safe


def _safe_artifact(
    artifact: Any,
    *,
    job: dict[str, Any],
    resolve_artifact: Callable[[str], dict[str, Any] | None] | None,
) -> dict[str, Any] | None:
    handle = _artifact_id_from_reference(artifact)
    if not handle:
        return None
    if resolve_artifact is None:
        raise ArtifactResolverUnavailable("Artifact resolver is unavailable")
    try:
        stored = resolve_artifact(handle)
    except Exception as exc:
        raise ArtifactResolverUnavailable("Artifact resolver is unavailable") from exc
    if not isinstance(stored, dict) or not _artifact_belongs_to_job(stored, job):
        return None
    from core.application.extensions.artifact_presentation import artifact_presentation_view

    stored = artifact_presentation_view(stored, resolve_artifact=resolve_artifact)
    canonical = {
        field: field_value
        for field, field_value in stored.items()
        if field in _ARTIFACT_KEYS and isinstance(field_value, (str, int, float, bool))
    }
    canonical["artifact_id"] = handle
    canonical.pop("id", None)
    return canonical


def build_public_result(
    job: dict[str, Any],
    status: str,
    *,
    resolve_artifact: Callable[[str], dict[str, Any] | None] | None = None,
) -> PublicWorkflowResultV1 | None:
    if status != "completed":
        return None
    workflow_outputs = _object(_object(job.get("meta")).get("workflow_outputs"))
    return PublicWorkflowResultV1(
        outputs=_object(workflow_outputs.get("outputs")),
        artifacts=_safe_artifacts(
            workflow_outputs.get("artifacts"),
            job=job,
            resolve_artifact=resolve_artifact,
        ),
    )


def build_public_error(
    job: dict[str, Any],
    status: str,
    job_errors: list[dict[str, Any]],
) -> PublicWorkflowErrorV1 | None:
    del job
    if status == "completed" or not bool(job_status_meta(status)["terminal"]):
        return None
    source = dict(job_errors[0]) if job_errors else {}
    source_context = _object(source.get("context_jsonb"))
    fallback_codes = {
        "cancelled": "cancelled",
        "max_iterations": "retry_exhausted",
        "retryable_interrupted_step": "interrupted_step",
        "triage_needed": "triage_needed",
    }
    fallback_code = fallback_codes.get(status) or "job_failed"
    code = _safe_public_identifier(
        source.get("error_code")
        or source.get("code")
        or source_context.get("error_code")
        or fallback_code,
        limit=128,
        fallback=fallback_code,
    )
    diagnostic_kind = _safe_public_identifier(
        source.get("diagnostic_kind") or source_context.get("diagnostic_kind"),
        limit=64,
    )
    selector = _safe_public_identifier(
        source.get("selector") or source_context.get("selector"),
        limit=255,
    )
    if diagnostic_kind == "transform" and code == "input_item_not_object":
        message = (
            f"Transform input at {selector} must resolve to an object."
            if selector
            else "Transform input must resolve to an object."
        )
    elif diagnostic_kind == "transform":
        message = "Transform step failed. Check its input and configuration."
    elif diagnostic_kind == "tabular_feed" and code == "state_reference_missing":
        message = (
            f"Tabular input at {selector} references missing workflow state."
            if selector
            else "Tabular input references missing workflow state."
        )
    elif diagnostic_kind == "dataset_query":
        candidate_message = str(source.get("error_message") or "").strip()
        message = (
            candidate_message
            if candidate_message
            and len(candidate_message) <= 512
            and "\n" not in candidate_message
            and not contains_public_secret(candidate_message)
            and not _SENSITIVE_VALUE_RE.search(candidate_message)
            else "Dataset query failed"
        )
    else:
        message = _PUBLIC_ERROR_MESSAGES.get(status, _DEFAULT_PUBLIC_ERROR_MESSAGE)
    details: dict[str, str | int | float | bool] = {}
    for key, value in source.items():
        if key not in _ERROR_DETAIL_KEYS or not isinstance(value, (str, int, float, bool)):
            continue
        if isinstance(value, str):
            safe_value = _safe_public_identifier(value, limit=255)
            if safe_value:
                details[key] = safe_value
        else:
            details[key] = value
    occurrence = source.get("occurrence", source_context.get("occurrence"))
    if diagnostic_kind in {"transform", "tabular_feed"} and selector:
        details["selector"] = selector
    if diagnostic_kind == "dataset_query":
        diagnostic_path = str(source.get("path") or source_context.get("path") or "").strip()
        correlation_id = _safe_public_identifier(
            source.get("correlation_id") or source_context.get("correlation_id"),
            limit=128,
        )
        if (
            diagnostic_path
            and _PUBLIC_PATH_RE.fullmatch(diagnostic_path)
            and not any(
                token in diagnostic_path.lower() for token in ("password", "secret", "token")
            )
        ):
            details["path"] = diagnostic_path
        if correlation_id:
            details["correlation_id"] = correlation_id
    try:
        occurrence = int(str(occurrence)) if occurrence not in (None, "") else None
    except (TypeError, ValueError):
        occurrence = None
    return PublicWorkflowErrorV1(
        code=code,
        message=message,
        step_id=_safe_public_identifier(
            source.get("step_id") or source_context.get("step_id"), limit=255
        )
        or None,
        step_execution_id=_safe_public_identifier(
            source.get("step_execution_id") or source_context.get("step_execution_id"), limit=64
        )
        or None,
        group_id=_safe_public_identifier(
            source.get("group_id") or source_context.get("group_id"), limit=255
        )
        or None,
        occurrence=occurrence,
        # `unknown`, not `execution`: the fallback has to be a member of the
        # canonical vocabulary the platform normalises to, and `execution` is
        # not one. Emitting it meant the public contract could carry a category
        # absent from the platform's own set.
        category=_safe_public_identifier(
            source.get("category") or "unknown", limit=128, fallback="unknown"
        ),
        retryable=bool(source.get("retryable", status == "retryable_interrupted_step")),
        details=details,
    )


def build_terminal_stop_error(
    step: dict[str, Any], operation: PublicWorkflowStepV1
) -> PublicWorkflowErrorV1:
    """Expose only a safe literal reason from the job-pinned Stop definition."""
    spec = _object(_object(step.get("override")).get("stop_fail"))
    reason = ""
    for key in ("message", "reason"):
        value = spec.get(key)
        if value and str(value).strip():
            reason = value.strip() if isinstance(value, str) else ""
            break
    if (
        not reason
        or len(reason) > 512
        or any(ord(char) < 32 or ord(char) == 127 for char in reason)
        or any(token in reason for token in ("{{", "}}", "${", "{%", "%}"))
        or contains_public_secret(reason)
        or _SENSITIVE_VALUE_RE.search(reason)
    ):
        reason = "A Stop Fail step ended the workflow."
    return PublicWorkflowErrorV1(
        code="workflow_stop_fail",
        message=reason,
        step_id=operation.step_id,
        step_execution_id=operation.step_execution_id,
        group_id=operation.group_id,
        occurrence=operation.occurrence,
        category="unknown",
        retryable=False,
    )


def _safe_approval(
    *,
    job: dict[str, Any],
    status: str,
    gate: dict[str, Any],
    previous: PublicApprovalGateV1 | None,
    now: datetime,
) -> PublicApprovalGateV1 | None:
    if status != "awaiting_approval":
        return None
    approval_id = _text(
        gate.get("approval_id") or gate.get("interrupt_id") or gate.get("id"), limit=255
    )
    if not approval_id:
        identity_payload = {
            "job_id": _text(job.get("id"), limit=255),
            "thread_id": _text(job.get("thread_id"), limit=255),
            "step_id": _text(gate.get("step_id"), limit=255),
            "step_execution_id": _text(gate.get("step_execution_id"), limit=64),
            "group_id": _text(gate.get("group_id"), limit=255),
            "message": _text(gate.get("message"), limit=1000),
        }
        identity_digest = hashlib.sha256(
            json.dumps(identity_payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()[:32]
        approval_id = f"derived-{identity_digest}"
    approval_id = approval_id or (previous.approval_id if previous else "derived-unknown")
    created_at = (
        previous.created_at
        if previous and previous.approval_id == approval_id
        else _datetime(gate.get("created_at")) or now
    )
    safe_content = bounded_approval_content(_object(gate.get("content")))
    allowed_actions = bounded_approval_actions(gate.get("allowed_actions"))
    notification = build_approval_notification_payload(gate)
    same_occurrence = previous is not None and previous.approval_id == approval_id
    wait_id = notification.get("wait_id")
    reason = gate.get("reason") or notification.get("reason")
    if same_occurrence and previous is not None:
        if not gate.get("wait_id") and not gate.get("request_id"):
            wait_id = previous.wait_id or wait_id
        reason = reason or previous.reason
    return PublicApprovalGateV1(
        approval_id=approval_id,
        wait_id=bounded_approval_text(wait_id, limit=255) or None,
        reason=reason if reason in {"authored", "recovery", "autonomy"} else None,
        job_id=_text(job.get("id"), limit=255),
        thread_id=_text(job.get("thread_id"), limit=255),
        step_id=_text(gate.get("step_id"), limit=255) or None,
        step_execution_id=_text(gate.get("step_execution_id"), limit=64) or None,
        group_id=_text(gate.get("group_id"), limit=255) or None,
        for_each_id=_text(gate.get("for_each_id"), limit=255) or None,
        message=bounded_approval_text(gate.get("message"), limit=1000),
        content=safe_content,
        allowed_actions=allowed_actions,
        created_at=created_at,
        expires_at=_datetime(gate.get("expires_at")),
    )


class WorkflowRunProjectionService:
    def __init__(
        self,
        logger: Any,
        *,
        now: Callable[[], datetime] | None = None,
        resolve_artifact: Callable[[str], dict[str, Any] | None] | None = None,
    ) -> None:
        self.logger = logger
        self.now = now or (lambda: datetime.now(UTC))
        self.resolve_artifact = resolve_artifact

    def get(
        self,
        *,
        job: dict[str, Any],
        approval_gate: dict[str, Any] | None = None,
        job_errors: list[dict[str, Any]] | None = None,
        force: bool = False,
    ) -> WorkflowRunStateV1:
        job_id = _text(job.get("id"), limit=255)
        try:
            current = self.logger.get_public_progress_snapshot(job_id)
        except LookupError:
            current = None
        if current is None:
            raise LookupError(f"Public progress snapshot is missing for job {job_id}")
        previous = current.snapshot if current is not None else None
        status = normalize_job_status(job.get("status"))
        now = self.now().astimezone(UTC)
        approval = _safe_approval(
            job=job,
            status=status,
            gate=dict(approval_gate or {}),
            previous=previous.approval if previous else None,
            now=now,
        )
        result = build_public_result(job, status, resolve_artifact=self.resolve_artifact)
        error = build_public_error(job, status, list(job_errors or []))
        finished_at = _datetime(job.get("finished_at"))

        if previous is not None:
            unchanged = (
                previous.status == status
                and previous.approval == approval
                and previous.result == result
                and previous.error == error
                and previous.finished_at == finished_at
            )
            if unchanged and not force:
                return previous

        recorded = self.logger.record_public_progress_transition(
            job_id=job_id,
            project_id=_text(job.get("project_id"), limit=255),
            workflow_id=resolve_job_workflow_id(job) or "unknown",
            job_status=status,
            occurred_at=now,
            queued_at=_datetime(job.get("queued_at")),
            started_at=_datetime(job.get("started_at")),
            finished_at=finished_at,
            approval=approval,
            result=result,
            error=error,
            force=force,
        )
        return recorded.snapshot


__all__ = [
    "ArtifactResolverUnavailable",
    "MultipleApprovalGatesUnsupported",
    "WorkflowRunProjectionService",
    "approval_notification_payload_from_state",
    "build_public_error",
    "build_public_result",
    "ensure_single_approval_interrupt",
]
