"""Application-owned persistence for transport-staged task attachments."""

from __future__ import annotations

import contextlib
import os
import uuid
from collections.abc import Callable, Iterable
from typing import Any
from urllib.parse import unquote

from core.application.management.command_models import ProjectTaskFileInput
from core.infrastructure.extension_runtime.object_storage import _safe_artifact_filename

_STREAM_CHUNK_BYTES = 1024 * 1024


def staged_attachment_filename_is_safe(filename: str) -> bool:
    """Validate a client filename before any durable side effect."""
    normalized = str(filename or "").strip()
    if not normalized:
        return False
    for candidate in {normalized, unquote(normalized)}:
        if not candidate or "\x00" in candidate:
            return False
        if os.path.basename(candidate) != candidate or "/" in candidate or "\\" in candidate:
            return False
    return True


def persist_staged_attachments(
    *,
    staged_files: Iterable[ProjectTaskFileInput],
    artifact_plane: Any,
    owner_scope: dict[str, Any],
    run_id: str,
    step_id: str,
    source_ref_prefix: str,
    heartbeat: Callable[[], None] | None = None,
    on_artifact_reserved: Callable[[dict[str, str]], None] | None = None,
) -> list[dict[str, Any]]:
    """Persist staged files without materializing their bodies in application RAM."""
    if artifact_plane is None:
        raise RuntimeError("Artifact storage is not configured")

    artifact_refs: list[dict[str, Any]] = []
    reserved_ids: list[str] = []
    try:
        for index, staged in enumerate(staged_files):
            if heartbeat is not None:
                heartbeat()
            path = str(staged.content_path or "").strip()
            if not path:
                raise RuntimeError("staged_attachment_path_required")
            expected_size = int(staged.size_bytes)
            expected_sha = str(staged.sha256 or "").strip().lower()
            safe_filename = _safe_artifact_filename(staged.filename or "upload.bin")
            artifact_id = f"art_{uuid.uuid4().hex[:24]}"
            reserved_ids.append(artifact_id)
            if on_artifact_reserved is not None:
                on_artifact_reserved(
                    {
                        "artifact_id": artifact_id,
                        "object_key": f"artifacts/{artifact_id}/{safe_filename}",
                    }
                )

            def _chunks(file_path: str = path):
                with open(file_path, "rb") as staged_file:
                    while True:
                        if heartbeat is not None:
                            heartbeat()
                        chunk = staged_file.read(_STREAM_CHUNK_BYTES)
                        if not chunk:
                            break
                        yield chunk

            handle, meta = artifact_plane.create_artifact_from_stream(
                artifact_id=artifact_id,
                owner_scope=dict(owner_scope),
                run_id=str(run_id or "").strip(),
                step_id=str(step_id or "").strip(),
                filename=safe_filename,
                content_type=str(staged.mime_type or "application/octet-stream").strip(),
                retention_class="run_window",
                chunks=_chunks(),
                max_size_bytes=max(1, expected_size),
                allow_in_memory=False,
            )
            if str(handle.artifact_id) != artifact_id:
                raise RuntimeError("artifact_identity_error")
            stored_size = meta.get("size_bytes")
            actual_size = int(stored_size) if stored_size is not None else -1
            actual_sha = str(meta.get("sha256") or "").strip().lower()
            if actual_size != expected_size or actual_sha != expected_sha:
                raise RuntimeError("artifact_integrity_error")
            artifact_refs.append(
                {
                    "artifact_id": artifact_id,
                    "filename": str(meta.get("filename") or safe_filename),
                    "content_type": str(
                        meta.get("content_type") or staged.mime_type or "application/octet-stream"
                    ),
                    "size_bytes": actual_size,
                    "sha256": actual_sha,
                    "object_key": str(meta.get("object_key") or "").strip(),
                    "source_ref": f"{source_ref_prefix}:{index}",
                    "retention_class": str(meta.get("retention_class") or "run_window").strip(),
                }
            )
    except BaseException:
        delete_artifact = getattr(artifact_plane, "delete_artifact", None)
        if callable(delete_artifact):
            for artifact_id in reserved_ids:
                with contextlib.suppress(Exception):
                    delete_artifact(artifact_id)
        raise
    return artifact_refs
