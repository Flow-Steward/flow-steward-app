"""Authorized metadata resolution for job-owned artifact downloads."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from core.application.management.errors import (
    ServiceConflictError,
    ServiceNotFoundError,
    ServiceUnavailableError,
)

_MESSAGE_ARTIFACT_NOT_FOUND = "Artifact not found"


@dataclass(frozen=True, slots=True)
class JobArtifactDownload:
    artifact_id: str
    filename: str
    content_type: str
    size_bytes: int
    # Presentation MIME never widens inline-preview eligibility.
    preview_content_type: str | None = None


def _metadata_dict(value: Any) -> dict[str, Any]:
    if hasattr(value, "model_dump"):
        dumped = value.model_dump(mode="json")
        return dict(dumped) if isinstance(dumped, dict) else {}
    if hasattr(value, "dump"):
        dumped = value.dump()
        return dict(dumped) if isinstance(dumped, dict) else {}
    return dict(value) if isinstance(value, dict) else {}


def artifact_metadata_is_downloadable(metadata: dict[str, Any]) -> bool:
    """Return whether metadata proves that a complete object was persisted.

    Legacy artifact sidecars predate ``persistence_status``. Those writers only
    persisted metadata after the object body and checksum were stored, so an
    absent status remains compatible when both durable fields are present. Any
    explicit non-available status stays fail-closed.
    """

    persistence_status = str(metadata.get("persistence_status") or "").strip().lower()
    status_allows_download = persistence_status == "available" or not persistence_status
    return bool(
        status_allows_download
        and not bool(metadata.get("expired"))
        and str(metadata.get("object_key") or "").strip()
        and str(metadata.get("sha256") or "").strip()
    )


def artifact_metadata_belongs_to_job(
    metadata: dict[str, Any],
    *,
    project_id: str,
    job_id: str,
) -> bool:
    """Recognize the retention owner or recorded producer within one project."""

    owner_scope = metadata.get("owner_scope")
    owner_scope = dict(owner_scope) if isinstance(owner_scope, dict) else {}
    expected_project_id = str(project_id or "").strip()
    expected_job_id = str(job_id or "").strip()
    if (
        not expected_project_id
        or not expected_job_id
        or str(owner_scope.get("project_id") or "").strip() != expected_project_id
    ):
        return False
    return expected_job_id in {
        str(owner_scope.get("job_id") or "").strip(),
        str(metadata.get("producer_job_id") or "").strip(),
        str(metadata.get("run_id") or "").strip(),
    }


def resolve_job_artifact_download(
    *,
    job: dict[str, Any],
    artifact_id: str,
    resolve_artifact: Callable[[str], Any] | None,
) -> JobArtifactDownload:
    """Resolve one complete artifact after exact project and job ownership checks."""

    normalized_artifact_id = str(artifact_id or "").strip()
    if not normalized_artifact_id:
        raise ServiceNotFoundError(_MESSAGE_ARTIFACT_NOT_FOUND)
    if not callable(resolve_artifact):
        raise ServiceUnavailableError("Artifact storage is not available")

    metadata = _metadata_dict(resolve_artifact(normalized_artifact_id))
    if str(metadata.get("artifact_id") or "").strip() != normalized_artifact_id:
        raise ServiceNotFoundError(_MESSAGE_ARTIFACT_NOT_FOUND)

    expected_project_id = str(job.get("project_id") or "").strip()
    expected_job_id = str(job.get("id") or "").strip()
    if not artifact_metadata_belongs_to_job(
        metadata,
        project_id=expected_project_id,
        job_id=expected_job_id,
    ):
        raise ServiceNotFoundError(_MESSAGE_ARTIFACT_NOT_FOUND)

    if not artifact_metadata_is_downloadable(metadata):
        raise ServiceConflictError("Artifact is not available for download")

    try:
        size_bytes = max(0, int(metadata.get("size_bytes") or 0))
    except (TypeError, ValueError):
        raise ServiceConflictError("Artifact metadata is invalid") from None

    from core.application.extensions.artifact_presentation import artifact_presentation_view

    # Ownership and availability above are checked against the original row.
    # Empty when the row declares no type: the preview then falls back to the file's extension.
    preview_content_type = str(metadata.get("content_type") or "").strip()
    metadata = artifact_presentation_view(metadata, resolve_artifact=resolve_artifact)
    return JobArtifactDownload(
        artifact_id=normalized_artifact_id,
        filename=str(metadata.get("filename") or "artifact.bin").strip() or "artifact.bin",
        content_type=str(metadata.get("content_type") or "application/octet-stream").strip()
        or "application/octet-stream",
        size_bytes=size_bytes,
        preview_content_type=preview_content_type,
    )


__all__ = [
    "JobArtifactDownload",
    "artifact_metadata_belongs_to_job",
    "artifact_metadata_is_downloadable",
    "resolve_job_artifact_download",
]
