"""Workflow-owned canvas notes; execution nodes and job state are untouched."""

from __future__ import annotations

import contextlib
import copy
from typing import Any

from pydantic import ValidationError

from core.application.management.staged_attachment_service import staged_attachment_filename_is_safe
from core.application.registry.db_store_workflows_mixin import WorkflowRevisionConflictError
from core.application.workflows.workflow_canvas_annotation_contract import (
    ANNOTATIONS_REVISION_SETTING,
    ANNOTATIONS_SETTING,
    CanvasAnnotation,
    WorkflowAnnotationsRevisionConflictError,
)
from core.infrastructure.config.workflow_file_limits import file_preview_max_bytes

ANNOTATION_IMAGE_PURPOSE = "workflow_canvas_annotation"


class WorkflowCanvasAnnotationError(ValueError):
    def __init__(self, message: str, *, status_code: int = 400) -> None:
        super().__init__(message)
        self.status_code = status_code


def _image_type(body: bytes) -> str:
    if body.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if body.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if body.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if body.startswith(b"RIFF") and body[8:12] == b"WEBP":
        return "image/webp"
    raise WorkflowCanvasAnnotationError("Choose a PNG, JPEG, GIF or WebP image.", status_code=415)


class WorkflowCanvasAnnotationService:
    def __init__(self, repository: Any, artifact_plane: Any) -> None:
        self.repository = repository
        self.artifact_plane = artifact_plane

    def _load(self, workflow_id: str, account_id: str) -> dict[str, Any]:
        workflow = self.repository.get_workflow(workflow_id)
        if not isinstance(workflow, dict):
            raise WorkflowCanvasAnnotationError("Workflow not found.", status_code=404)
        if not account_id or str(workflow.get("account_id") or "") not in ("", str(account_id)):
            raise WorkflowCanvasAnnotationError("Access denied.", status_code=403)
        return copy.deepcopy(workflow)

    @staticmethod
    def _notes(workflow: dict[str, Any]) -> list[dict[str, Any]]:
        return workflow.setdefault("settings", {}).setdefault(ANNOTATIONS_SETTING, [])

    @staticmethod
    def _guard(workflow: dict[str, Any], expected_revision: int | None) -> int:
        if expected_revision is None:
            raise WorkflowCanvasAnnotationError(
                "Reload the workflow before saving.", status_code=428
            )
        if type(expected_revision) is not int or expected_revision < 1:
            raise WorkflowCanvasAnnotationError("expected_revision must be a positive integer.")
        if workflow.get("revision") != expected_revision:
            raise WorkflowRevisionConflictError(workflow["workflow_id"], expected_revision)
        return expected_revision

    def _note(self, workflow: dict[str, Any], annotation_id: str) -> dict[str, Any]:
        for note in self._notes(workflow):
            if note["id"] == annotation_id:
                return note
        raise WorkflowCanvasAnnotationError("Note not found.", status_code=404)

    @staticmethod
    def _annotation_guard(workflow: dict, expected_revision: int | None) -> int:
        if expected_revision is None:
            raise WorkflowCanvasAnnotationError("Reload the notes before saving.", status_code=428)
        if type(expected_revision) is not int or expected_revision < 0:
            raise WorkflowCanvasAnnotationError(
                "expected_annotations_revision must be a non-negative integer."
            )
        current = int((workflow.get("settings") or {}).get(ANNOTATIONS_REVISION_SETTING, 0))
        if current != expected_revision:
            raise WorkflowAnnotationsRevisionConflictError(
                workflow["workflow_id"], expected_revision
            )
        return expected_revision

    def _save(
        self,
        workflow: dict,
        account_id: str,
        expected_revision: int,
        expected_annotations_revision: int,
    ) -> dict:
        annotations_revision = self.repository.save_workflow_canvas_annotations(
            workflow["workflow_id"],
            account_id=account_id,
            annotations=self._notes(workflow),
            expected_revision=expected_revision,
            expected_annotations_revision=expected_annotations_revision,
        )
        return {
            "revision": expected_revision,
            "annotations_revision": annotations_revision,
            "annotations": self._notes(workflow),
        }

    def list_notes(self, *, workflow_id: str, account_id: str) -> dict:
        workflow = self._load(workflow_id, account_id)
        return {
            "revision": workflow["revision"],
            "annotations_revision": int(
                (workflow.get("settings") or {}).get(ANNOTATIONS_REVISION_SETTING, 0)
            ),
            "annotations": self._notes(workflow),
        }

    def save_note(
        self,
        *,
        workflow_id: str,
        account_id: str,
        annotation: dict,
        expected_revision: int | None,
        expected_annotations_revision: int | None,
    ) -> dict:
        try:
            note = CanvasAnnotation.model_validate(annotation).model_dump()
        except ValidationError as exc:
            raise WorkflowCanvasAnnotationError(
                "Invalid note text, identifier, position or size."
            ) from exc
        workflow = self._load(workflow_id, account_id)
        expected_revision = self._guard(workflow, expected_revision)
        expected_annotations_revision = self._annotation_guard(
            workflow, expected_annotations_revision
        )
        notes = self._notes(workflow)
        existing = next((row for row in notes if row["id"] == note["id"]), None)
        if existing is None:
            if len(notes) >= 500:
                raise WorkflowCanvasAnnotationError("A workflow can contain up to 500 notes.")
            notes.append(note)
        else:
            existing.update(note)
        return self._save(workflow, account_id, expected_revision, expected_annotations_revision)

    def _owned_image(
        self, image: Any, workflow_id: str, account_id: str, annotation_id: str
    ) -> dict | None:
        if not isinstance(image, dict) or self.artifact_plane is None:
            return None
        meta = self.artifact_plane.get_artifact(image.get("artifact_id"))
        if not isinstance(meta, dict):
            return None
        scope = meta.get("owner_scope") or {}
        expected = {
            "workflow_id": workflow_id,
            "account_id": str(account_id),
            "purpose": ANNOTATION_IMAGE_PURPOSE,
            "annotation_id": annotation_id,
        }
        return meta if all(scope.get(key) == value for key, value in expected.items()) else None

    def _delete_image(
        self, image: Any, workflow_id: str, account_id: str, annotation_id: str
    ) -> None:
        # As with sample files, failed cleanup must not turn a committed save into an error.
        with contextlib.suppress(Exception):
            if self._owned_image(image, workflow_id, account_id, annotation_id):
                self.artifact_plane.delete_artifact(image["artifact_id"])

    def delete_note(
        self,
        *,
        workflow_id: str,
        account_id: str,
        annotation_id: str,
        expected_revision: int | None,
        expected_annotations_revision: int | None,
    ) -> dict:
        workflow = self._load(workflow_id, account_id)
        expected_revision = self._guard(workflow, expected_revision)
        expected_annotations_revision = self._annotation_guard(
            workflow, expected_annotations_revision
        )
        note = self._note(workflow, annotation_id)
        self._notes(workflow).remove(note)
        result = self._save(workflow, account_id, expected_revision, expected_annotations_revision)
        self._delete_image(note.get("image"), workflow_id, account_id, annotation_id)
        return result

    def set_image(
        self,
        *,
        workflow_id: str,
        account_id: str,
        annotation_id: str,
        expected_revision: int | None,
        expected_annotations_revision: int | None,
        filename: str,
        content_type: str,
        body: bytes,
    ) -> dict:
        workflow = self._load(workflow_id, account_id)
        expected_revision = self._guard(workflow, expected_revision)
        expected_annotations_revision = self._annotation_guard(
            workflow, expected_annotations_revision
        )
        note = self._note(workflow, annotation_id)
        if not staged_attachment_filename_is_safe(filename):
            raise WorkflowCanvasAnnotationError("The file name must be a plain name.")
        if len(body) > file_preview_max_bytes():
            raise WorkflowCanvasAnnotationError(
                "The image exceeds the preview size limit.", status_code=413
            )
        actual_type = _image_type(body)
        if content_type.split(";", 1)[0].strip().lower() != actual_type:
            raise WorkflowCanvasAnnotationError(
                "The image type does not match its contents.", status_code=415
            )
        plane = self.artifact_plane
        if plane is None:
            raise WorkflowCanvasAnnotationError("File storage is unavailable.", status_code=503)
        try:
            handle, meta = plane.create_artifact(
                owner_scope={
                    "workflow_id": workflow_id,
                    "account_id": str(account_id),
                    "purpose": ANNOTATION_IMAGE_PURPOSE,
                    "annotation_id": annotation_id,
                },
                run_id=f"workflow:{workflow_id}",
                step_id=f"annotation:{annotation_id}",
                filename=filename,
                content_type=actual_type,
                retention_class="persistent",
                body=body,
            )
        except Exception as exc:
            raise WorkflowCanvasAnnotationError(
                "The image could not be stored.", status_code=503
            ) from exc
        previous = note.get("image")
        note["image"] = {
            "artifact_id": handle.artifact_id,
            "filename": filename,
            "content_type": actual_type,
            "size_bytes": meta.get("size_bytes", len(body)),
        }
        try:
            result = self._save(
                workflow, account_id, expected_revision, expected_annotations_revision
            )
        except BaseException:
            with contextlib.suppress(Exception):
                plane.delete_artifact(handle.artifact_id)
            raise
        self._delete_image(previous, workflow_id, account_id, annotation_id)
        return result

    def clear_image(
        self,
        *,
        workflow_id: str,
        account_id: str,
        annotation_id: str,
        expected_revision: int | None,
        expected_annotations_revision: int | None,
    ) -> dict:
        workflow = self._load(workflow_id, account_id)
        expected_revision = self._guard(workflow, expected_revision)
        expected_annotations_revision = self._annotation_guard(
            workflow, expected_annotations_revision
        )
        note = self._note(workflow, annotation_id)
        previous = note.pop("image", None)
        result = self._save(workflow, account_id, expected_revision, expected_annotations_revision)
        self._delete_image(previous, workflow_id, account_id, annotation_id)
        return result

    def preview_image(
        self, *, workflow_id: str, account_id: str, annotation_id: str
    ) -> tuple[bytes, str]:
        workflow = self._load(workflow_id, account_id)
        note = self._note(workflow, annotation_id)
        meta = self._owned_image(note.get("image"), workflow_id, account_id, annotation_id)
        if meta is None:
            raise WorkflowCanvasAnnotationError("Image not found.", status_code=404)
        try:
            body = self.artifact_plane.read_artifact_bytes(
                meta["artifact_id"], max_size_bytes=file_preview_max_bytes()
            )
        except ValueError as exc:
            raise WorkflowCanvasAnnotationError(
                "The image exceeds the preview size limit.", status_code=413
            ) from exc
        if body is None:
            raise WorkflowCanvasAnnotationError("Image not found.", status_code=404)
        return body, _image_type(body)

    def purge_workflow_images(self, workflow: dict) -> None:
        for note in self._notes(workflow):
            self._delete_image(
                note.get("image"),
                workflow["workflow_id"],
                workflow.get("account_id", ""),
                note["id"],
            )

    @contextlib.contextmanager
    def clone_annotations(self, source: dict, *, target_workflow_id: str, account_id: str):
        """Copy owned images; unwind external storage if the enclosing clone fails."""
        notes = copy.deepcopy(self._notes(source))
        created: list[str] = []
        try:
            for note in notes:
                if not note.get("image"):
                    continue
                try:
                    body, content_type = self.preview_image(
                        workflow_id=source["workflow_id"],
                        account_id=str(source.get("account_id") or account_id),
                        annotation_id=note["id"],
                    )
                except WorkflowCanvasAnnotationError as exc:
                    if exc.status_code != 404:
                        raise
                    # A missing attachment must not prevent copying usable notes
                    # and the executable workflow; never copy a dead local reference.
                    note.pop("image", None)
                    continue
                handle, meta = self.artifact_plane.create_artifact(
                    owner_scope={
                        "workflow_id": target_workflow_id,
                        "account_id": str(account_id),
                        "purpose": ANNOTATION_IMAGE_PURPOSE,
                        "annotation_id": note["id"],
                    },
                    run_id=f"workflow:{target_workflow_id}",
                    step_id=f"annotation:{note['id']}",
                    filename=note["image"]["filename"],
                    content_type=content_type,
                    retention_class="persistent",
                    body=body,
                )
                created.append(handle.artifact_id)
                note["image"] = {
                    **note["image"],
                    "artifact_id": handle.artifact_id,
                    "content_type": content_type,
                    "size_bytes": meta.get("size_bytes", len(body)),
                }
            yield notes
        except BaseException:
            for artifact_id in created:
                with contextlib.suppress(Exception):
                    self.artifact_plane.delete_artifact(artifact_id)
            raise
