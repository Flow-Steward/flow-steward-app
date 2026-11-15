"""The files a run step read or wrote, as the run view lists them.

A step records a file as an ``artifact:<id>`` handle (or an ``artifact_id``
field) in its inputs and outputs. These helpers find those references and
turn each one owned by the job into a download descriptor.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from core.application.jobs.job_artifact_access import (
    artifact_metadata_belongs_to_job,
    artifact_metadata_is_downloadable,
)
from core.infrastructure.config.workflow_file_limits import file_preview_max_bytes

_ARTIFACT_HANDLE_PREFIX = "artifact:"


def _artifact_id_from_value(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    normalized = value.strip()
    if not normalized.startswith(_ARTIFACT_HANDLE_PREFIX):
        return ""
    return normalized.removeprefix(_ARTIFACT_HANDLE_PREFIX).strip()


def _artifact_ids(value: Any) -> list[str]:
    resolved: list[str] = []

    def _visit(candidate: Any) -> None:
        artifact_id = _artifact_id_from_value(candidate)
        if artifact_id:
            if artifact_id not in resolved:
                resolved.append(artifact_id)
            return
        if isinstance(candidate, dict):
            direct_artifact_id = str(candidate.get("artifact_id") or "").strip()
            if direct_artifact_id and direct_artifact_id not in resolved:
                resolved.append(direct_artifact_id)
            for nested in candidate.values():
                _visit(nested)
        elif isinstance(candidate, (list, tuple)):
            for nested in candidate:
                _visit(nested)

    _visit(value)
    return resolved


def _artifact_metadata(value: Any) -> dict[str, Any]:
    if hasattr(value, "model_dump"):
        dumped = value.model_dump(mode="json")
        return dict(dumped) if isinstance(dumped, dict) else {}
    if hasattr(value, "dump"):
        dumped = value.dump()
        return dict(dumped) if isinstance(dumped, dict) else {}
    return dict(value) if isinstance(value, dict) else {}


def _artifact_descriptors(
    value: Any,
    *,
    role: str,
    project_id: str,
    job_id: str,
    resolve_artifact: Callable[[str], Any] | None,
) -> list[dict[str, Any]]:
    if not callable(resolve_artifact) or not project_id or not job_id:
        return []
    descriptors: list[dict[str, Any]] = []
    for artifact_id in _artifact_ids(value):
        try:
            metadata = _artifact_metadata(resolve_artifact(artifact_id))
        except Exception:
            metadata = {}
        if not artifact_metadata_belongs_to_job(
            metadata,
            project_id=project_id,
            job_id=job_id,
        ):
            continue
        available = artifact_metadata_is_downloadable(metadata)
        from core.application.extensions.artifact_presentation import artifact_presentation_view

        metadata = artifact_presentation_view(metadata, resolve_artifact=resolve_artifact)
        size_bytes = max(0, int(metadata.get("size_bytes") or 0))
        descriptors.append(
            {
                "artifact_id": artifact_id,
                "role": role,
                "filename": str(metadata.get("filename") or "Artifact").strip() or "Artifact",
                "content_type": str(
                    metadata.get("content_type") or "application/octet-stream"
                ).strip(),
                "size_bytes": size_bytes,
                # Known before anyone asks: the run view says so instead of offering a preview.
                "preview_too_large": size_bytes > file_preview_max_bytes(),
                "available": available,
                "download_url": (
                    f"/api/jobs/{job_id}/artifacts/{artifact_id}" if available else None
                ),
            }
        )
    return descriptors
