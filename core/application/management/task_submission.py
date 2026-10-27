"""Transport-neutral task submission normalization and fingerprinting."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from core.application.canonical_json import canonical_json_hash


class TaskSubmissionValidationError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class TaskAttachmentDescriptor:
    filename: str
    media_type: str
    size: int
    sha256: str

    def canonical(self) -> dict[str, Any]:
        return {
            "filename": str(self.filename or "").strip(),
            "media_type": str(self.media_type or "application/octet-stream").strip().lower(),
            "size": int(self.size),
            "sha256": str(self.sha256 or "").strip().lower(),
        }


@dataclass(frozen=True)
class NormalizedTaskSubmission:
    inputs: dict[str, Any] = field(default_factory=dict)
    extra_state: dict[str, Any] = field(default_factory=dict)
    job_meta: dict[str, Any] = field(default_factory=dict)


def normalize_workflow_inputs(
    *,
    inputs: dict[str, Any] | None,
    extra_state: dict[str, Any] | None = None,
    job_meta: dict[str, Any] | None = None,
) -> NormalizedTaskSubmission:
    normalized_state = dict(extra_state or {})
    if "workflow_input_payload" in normalized_state:
        raise TaskSubmissionValidationError(
            "workflow_input_location_unsupported",
            "Use inputs; extra_state.workflow_input_payload is not supported.",
        )
    return NormalizedTaskSubmission(
        inputs=dict(inputs or {}),
        extra_state=normalized_state,
        job_meta=dict(job_meta or {}),
    )


def canonical_task_fingerprint(
    submission: NormalizedTaskSubmission,
    *,
    project_id: str,
    workflow_selector: str,
    resolved_workflow_id: str,
    task_summary: str,
    mode: str | None,
    test_mode: bool,
    task_source: str,
    attachments: list[TaskAttachmentDescriptor],
) -> str:
    def _without_envelope_correlation_ids(container: dict[str, Any]) -> dict[str, Any]:
        normalized = dict(container)
        task_envelope = normalized.get("task_envelope")
        if isinstance(task_envelope, dict):
            normalized["task_envelope"] = {
                key: value
                for key, value in task_envelope.items()
                if key not in {"envelope_id", "source_event_id", "source_object_id"}
            }
        return normalized

    fingerprint_extra_state = _without_envelope_correlation_ids(submission.extra_state)
    fingerprint_job_meta = _without_envelope_correlation_ids(submission.job_meta)
    canonical_attachments = [descriptor.canonical() for descriptor in attachments]
    canonical_attachments.sort(
        key=lambda row: (
            row["filename"],
            row["media_type"],
            row["size"],
            row["sha256"],
        )
    )
    digest = canonical_json_hash(
        {
            "version": 1,
            "project_id": str(project_id or "").strip(),
            "workflow_selector": str(workflow_selector or "default").strip() or "default",
            "resolved_workflow_id": str(resolved_workflow_id or "").strip(),
            "task_summary": str(task_summary or "").strip(),
            "mode": str(mode or "").strip(),
            "test_mode": bool(test_mode),
            "task_source": str(task_source or "manual").strip() or "manual",
            "inputs": submission.inputs,
            "extra_state": fingerprint_extra_state,
            "job_meta": fingerprint_job_meta,
            "attachments": canonical_attachments,
        }
    )
    return f"sha256:{digest}"
