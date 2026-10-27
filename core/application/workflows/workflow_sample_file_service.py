"""Test-run files attached to a workflow's file inputs.

A workflow that parses files is hard to test when the file normally arrives by
email, FTP or a parent workflow. An operator attaches a file to the input once;
every test run that does not pass that input gets a fresh copy of it. Real runs
(webhook, API, parent workflow) always pass their own file.

The attached file is a persistent artifact owned by the workflow. A test run
never uses it directly: a run binds its input files to its job, and a file that
belongs to one job cannot be handed to the next one, so each run gets a copy.
"""

from __future__ import annotations

import contextlib
import copy
from collections.abc import Callable
from typing import Any, Protocol

from core.application.management.staged_attachment_service import (
    staged_attachment_filename_is_safe,
)
from core.application.registry.workflow_contract import normalize_sample_file
from core.infrastructure.config.workflow_file_limits import workflow_test_file_max_bytes

SAMPLE_FILE_PURPOSE = "workflow_sample_file"
SAMPLE_SOURCE_REF_PREFIX = "sample"
_OCTET_STREAM = "application/octet-stream"
_MESSAGE_ACCESS_DENIED = "Access denied."


class WorkflowSampleFileRepository(Protocol):
    def get_workflow(self, workflow_id: str) -> dict[str, Any] | None: ...

    def save_workflow(
        self,
        data: dict[str, Any],
        *,
        author: str,
        expected_revision: int | None,
    ) -> int: ...


class WorkflowSampleFileError(ValueError):
    def __init__(
        self,
        message: str,
        *,
        code: str,
        status_code: int,
        current_revision: int | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code
        self.current_revision = current_revision

    def detail(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"code": self.code, "message": str(self)}
        if self.current_revision is not None:
            payload["current_revision"] = self.current_revision
        return payload


def _text(value: Any) -> str:
    return str(value or "").strip()


def _file_input_row(workflow: dict[str, Any], field_name: str) -> dict[str, Any]:
    settings = workflow.get("settings")
    callable_block = settings.get("callable") if isinstance(settings, dict) else None
    rows = callable_block.get("expected_inputs") if isinstance(callable_block, dict) else None
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict) or _text(row.get("name")) != field_name:
            continue
        if _text(row.get("type")).lower() != "file":
            raise WorkflowSampleFileError(
                f"Workflow input '{field_name}' is not a file input.",
                code="workflow_input_not_file",
                status_code=400,
            )
        return row
    raise WorkflowSampleFileError(
        f"Workflow has no input named '{field_name}'. Save the input first.",
        code="workflow_input_not_found",
        status_code=404,
    )


def _guard_revision(expected_revision: int | None, workflow: dict[str, Any]) -> int | None:
    current_revision = workflow.get("revision")
    if current_revision is None:
        return None
    if expected_revision is None:
        raise WorkflowSampleFileError(
            "Reload the workflow and retry with expected_revision.",
            code="workflow_revision_required",
            status_code=428,
            current_revision=int(current_revision),
        )
    if int(expected_revision) < 1:
        raise WorkflowSampleFileError(
            "expected_revision must be a positive integer.",
            code="workflow_revision_invalid",
            status_code=400,
        )
    return int(expected_revision)


def _owned_sample(meta: Any, *, workflow_id: str, account_id: str) -> bool:
    if not isinstance(meta, dict):
        return False
    owner = meta.get("owner_scope")
    owner = owner if isinstance(owner, dict) else {}
    return (
        _text(owner.get("purpose")) == SAMPLE_FILE_PURPOSE
        and _text(owner.get("workflow_id")) == workflow_id
        and _text(owner.get("account_id")) == account_id
    )


class WorkflowSampleFileService:
    """Attach, replace or remove the test-run file of one workflow file input."""

    def __init__(self, repository: WorkflowSampleFileRepository, artifact_plane: Any) -> None:
        self.repository = repository
        self.artifact_plane = artifact_plane

    def _require_plane(self) -> Any:
        if self.artifact_plane is None:
            raise WorkflowSampleFileError(
                "File storage is unavailable.",
                code="workflow_sample_file_storage_unavailable",
                status_code=503,
            )
        return self.artifact_plane

    def _load(
        self,
        *,
        workflow_id: str,
        field_name: str,
        account_id: str,
        expected_revision: int | None,
    ) -> tuple[dict[str, Any], dict[str, Any], int | None]:
        existing = self.repository.get_workflow(workflow_id)
        if not isinstance(existing, dict) or _text(existing.get("workflow_id")) != workflow_id:
            raise WorkflowSampleFileError(
                "Workflow was not found.", code="workflow_not_found", status_code=404
            )
        stored_account_id = _text(existing.get("account_id"))
        if not account_id or (stored_account_id and stored_account_id != account_id):
            raise WorkflowSampleFileError(
                _MESSAGE_ACCESS_DENIED,
                code="workflow_sample_file_scope_forbidden",
                status_code=403,
            )
        mutable = copy.deepcopy(existing)
        row = _file_input_row(mutable, field_name)
        return mutable, row, _guard_revision(expected_revision, mutable)

    def _delete_owned(self, sample: Any, *, workflow_id: str, account_id: str) -> None:
        artifact_id = _text(sample.get("artifact_id")) if isinstance(sample, dict) else ""
        plane = self.artifact_plane
        if not artifact_id or plane is None:
            return
        with contextlib.suppress(Exception):
            if _owned_sample(
                plane.get_artifact(artifact_id), workflow_id=workflow_id, account_id=account_id
            ):
                plane.delete_artifact(artifact_id)

    def set_sample_file(
        self,
        *,
        workflow_id: str,
        field_name: str,
        account_id: str,
        filename: str,
        content_type: str,
        body: bytes,
        author: str,
        expected_revision: int | None,
    ) -> dict[str, Any]:
        normalized_workflow_id = _text(workflow_id)
        normalized_field_name = _text(field_name)
        normalized_account_id = _text(account_id)
        clean_filename = _text(filename)
        if not staged_attachment_filename_is_safe(clean_filename):
            raise WorkflowSampleFileError(
                "The file name must be a plain name.",
                code="workflow_sample_file_name_invalid",
                status_code=400,
            )
        if not body:
            raise WorkflowSampleFileError(
                "The file is empty.", code="workflow_sample_file_empty", status_code=400
            )
        limit = workflow_test_file_max_bytes()
        if len(body) > limit:
            raise WorkflowSampleFileError(
                f"The file exceeds the {limit / (1024 * 1024):g} MB test-file limit "
                "(FS_WORKFLOW_TEST_FILE_MAX_BYTES).",
                code="workflow_sample_file_too_large",
                status_code=413,
            )
        existing, row, guarded_revision = self._load(
            workflow_id=normalized_workflow_id,
            field_name=normalized_field_name,
            account_id=normalized_account_id,
            expected_revision=expected_revision,
        )
        plane = self._require_plane()
        try:
            handle, meta = plane.create_artifact(
                owner_scope={
                    "account_id": normalized_account_id,
                    "workflow_id": normalized_workflow_id,
                    "purpose": SAMPLE_FILE_PURPOSE,
                    "field_name": normalized_field_name,
                },
                run_id=f"workflow:{normalized_workflow_id}",
                step_id=f"{SAMPLE_SOURCE_REF_PREFIX}:{normalized_field_name}",
                filename=clean_filename,
                content_type=_text(content_type) or _OCTET_STREAM,
                retention_class="persistent",
                body=bytes(body),
            )
        except Exception as exc:
            raise WorkflowSampleFileError(
                "The file could not be stored.",
                code="workflow_sample_file_storage_unavailable",
                status_code=503,
            ) from exc
        sample = normalize_sample_file({**dict(meta or {}), "artifact_id": handle.artifact_id})
        previous = row.get("sample_file")
        row["sample_file"] = sample
        try:
            saved_revision = self.repository.save_workflow(
                existing, author=author, expected_revision=guarded_revision
            )
        except BaseException:
            with contextlib.suppress(Exception):
                plane.delete_artifact(handle.artifact_id)
            raise
        self._delete_owned(
            previous, workflow_id=normalized_workflow_id, account_id=normalized_account_id
        )
        return {
            "workflow_id": normalized_workflow_id,
            "name": normalized_field_name,
            "sample_file": sample,
            "revision": int(saved_revision),
        }

    def clear_sample_file(
        self,
        *,
        workflow_id: str,
        field_name: str,
        account_id: str,
        author: str,
        expected_revision: int | None,
    ) -> dict[str, Any]:
        normalized_workflow_id = _text(workflow_id)
        normalized_field_name = _text(field_name)
        normalized_account_id = _text(account_id)
        existing, row, guarded_revision = self._load(
            workflow_id=normalized_workflow_id,
            field_name=normalized_field_name,
            account_id=normalized_account_id,
            expected_revision=expected_revision,
        )
        previous = row.pop("sample_file", None)
        saved_revision = self.repository.save_workflow(
            existing, author=author, expected_revision=guarded_revision
        )
        self._delete_owned(
            previous, workflow_id=normalized_workflow_id, account_id=normalized_account_id
        )
        return {
            "workflow_id": normalized_workflow_id,
            "name": normalized_field_name,
            "sample_file": None,
            "revision": int(saved_revision),
        }

    def purge_workflow_samples(self, workflow: Any) -> None:
        """Delete every test-run file of a workflow that is being deleted."""
        if not isinstance(workflow, dict):
            return
        workflow_id = _text(workflow.get("workflow_id"))
        account_id = _text(workflow.get("account_id"))
        settings = workflow.get("settings")
        callable_block = settings.get("callable") if isinstance(settings, dict) else None
        rows = callable_block.get("expected_inputs") if isinstance(callable_block, dict) else None
        for row in rows if isinstance(rows, list) else []:
            if isinstance(row, dict) and isinstance(row.get("sample_file"), dict):
                self._delete_owned(
                    row["sample_file"], workflow_id=workflow_id, account_id=account_id
                )


class TestRunSampleFiles:
    """Copies a workflow's test-run files for one test run.

    ``copy`` is handed to input materialization; the copies it made are then
    bound to the job once it exists, or deleted when the run is not launched.
    """

    __test__ = False

    def __init__(
        self,
        *,
        artifact_plane: Any,
        workflow_id: str,
        workflow_account_id: str,
        account_id: str,
        project_id: str,
    ) -> None:
        self.artifact_plane = artifact_plane
        self.workflow_id = _text(workflow_id)
        self.workflow_account_id = _text(workflow_account_id)
        self.account_id = _text(account_id)
        self.project_id = _text(project_id)
        self.copies: list[dict[str, Any]] = []

    def copy(self, field_name: str, sample: dict[str, Any]) -> dict[str, Any]:
        plane = self.artifact_plane
        if plane is None:
            raise RuntimeError("File storage is unavailable for the test-run file")
        sample_id = _text(sample.get("artifact_id"))
        meta = plane.get_artifact(sample_id) if sample_id else None
        if not isinstance(meta, dict) or not _owned_sample(
            meta, workflow_id=self.workflow_id, account_id=self.workflow_account_id
        ):
            raise RuntimeError(f"The test-run file of input '{field_name}' is missing")
        # A file attached under an earlier, larger limit is still copied whole.
        bound = max(workflow_test_file_max_bytes(), int(meta.get("size_bytes") or 0))
        body = plane.read_artifact_bytes(sample_id, max_size_bytes=bound)
        if body is None:
            raise RuntimeError(f"The test-run file of input '{field_name}' is missing")
        filename = _text(meta.get("filename")) or "file"
        content_type = _text(meta.get("content_type")) or _OCTET_STREAM
        handle, copied = plane.create_artifact(
            owner_scope={
                "account_id": self.account_id,
                "project_id": self.project_id,
                "workflow_id": self.workflow_id,
            },
            run_id=f"workflow:{self.workflow_id}",
            step_id=f"{SAMPLE_SOURCE_REF_PREFIX}:{field_name}",
            filename=filename,
            content_type=content_type,
            retention_class="run_window",
            body=body,
        )
        ref = {
            "artifact_id": handle.artifact_id,
            "filename": _text(copied.get("filename")) or filename,
            "content_type": content_type,
            "size_bytes": int(copied.get("size_bytes") or len(body)),
            "sha256": _text(copied.get("sha256")),
            "source_ref": f"{SAMPLE_SOURCE_REF_PREFIX}:{field_name}",
            "field_name": field_name,
            "retention_class": "run_window",
        }
        self.copies.append(ref)
        return dict(ref)

    def bind_to_job(self, job_id: str) -> None:
        """Make the copies the job's own files: downloadable from it, kept with it."""
        bind = getattr(self.artifact_plane, "bind_replay_owner", None)
        if not callable(bind):
            return
        for ref in self.copies:
            with contextlib.suppress(Exception):
                bind(
                    artifact_id=ref["artifact_id"],
                    project_id=self.project_id,
                    job_id=_text(job_id),
                )

    def discard(self) -> None:
        delete = getattr(self.artifact_plane, "delete_artifact", None)
        for ref in self.copies if callable(delete) else []:
            with contextlib.suppress(Exception):
                delete(ref["artifact_id"])
        self.copies = []


def preview_sample_file(field_name: str, sample: dict[str, Any]) -> dict[str, Any]:
    """What a dry run shows for a test-run file, without copying it."""
    return {
        **{key: value for key, value in sample.items() if key != "artifact_id"},
        "source_ref": f"{SAMPLE_SOURCE_REF_PREFIX}:{field_name}",
        "field_name": field_name,
    }


SampleFileProvider = Callable[[str, dict[str, Any]], dict[str, Any]]

__all__ = [
    "SampleFileProvider",
    "TestRunSampleFiles",
    "WorkflowSampleFileError",
    "WorkflowSampleFileRepository",
    "WorkflowSampleFileService",
    "preview_sample_file",
]
