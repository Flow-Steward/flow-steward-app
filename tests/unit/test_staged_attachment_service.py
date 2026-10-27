from __future__ import annotations

import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest

from core.application.management.command_models import ProjectTaskFileInput
from core.application.management.staged_attachment_service import (
    persist_staged_attachments,
)


class _ArtifactPlane:
    def __init__(self, *, stored_sha: str | None = None) -> None:
        self.stored_sha = stored_sha
        self.deleted: list[str] = []
        self.received = b""

    def create_artifact_from_stream(self, **kwargs):
        self.received = b"".join(kwargs["chunks"])
        digest = self.stored_sha or hashlib.sha256(self.received).hexdigest()
        artifact_id = kwargs["artifact_id"]
        return SimpleNamespace(artifact_id=artifact_id), {
            "filename": kwargs["filename"],
            "content_type": kwargs["content_type"],
            "size_bytes": len(self.received),
            "sha256": digest,
            "object_key": f"artifacts/{artifact_id}/{kwargs['filename']}",
            "retention_class": "run_window",
        }

    def delete_artifact(self, artifact_id: str) -> bool:
        self.deleted.append(artifact_id)
        return True


def _staged_file(tmp_path: Path, body: bytes) -> ProjectTaskFileInput:
    path = tmp_path / "invoice.bin"
    path.write_bytes(body)
    return ProjectTaskFileInput(
        filename="invoice.bin",
        mime_type="application/octet-stream",
        content_path=str(path),
        size_bytes=len(body),
        sha256=hashlib.sha256(body).hexdigest(),
    )


def test_persist_staged_attachments_streams_and_verifies_storage(tmp_path: Path) -> None:
    staged = _staged_file(tmp_path, b"invoice-body")
    plane = _ArtifactPlane()
    reserved: list[dict[str, str]] = []

    refs = persist_staged_attachments(
        staged_files=[staged],
        artifact_plane=plane,
        owner_scope={"entry_path": "webhook"},
        run_id="request-1",
        step_id="workflow_entrypoint",
        source_ref_prefix="multipart",
        on_artifact_reserved=reserved.append,
    )

    assert plane.received == b"invoice-body"
    assert refs[0]["size_bytes"] == len(b"invoice-body")
    assert refs[0]["sha256"] == hashlib.sha256(b"invoice-body").hexdigest()
    assert refs[0]["source_ref"] == "multipart:0"
    assert reserved == [
        {
            "artifact_id": refs[0]["artifact_id"],
            "object_key": refs[0]["object_key"],
        }
    ]


def test_persist_staged_attachments_rolls_back_integrity_mismatch(tmp_path: Path) -> None:
    staged = _staged_file(tmp_path, b"invoice-body")
    plane = _ArtifactPlane(stored_sha="0" * 64)

    with pytest.raises(RuntimeError, match="artifact_integrity_error"):
        persist_staged_attachments(
            staged_files=[staged],
            artifact_plane=plane,
            owner_scope={"entry_path": "webhook"},
            run_id="request-1",
            step_id="workflow_entrypoint",
            source_ref_prefix="multipart",
        )

    assert len(plane.deleted) == 1
