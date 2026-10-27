# ruff: noqa: F401
"""Generic workflow-owned webhook entrypoint routes."""

from __future__ import annotations

import base64
import errno
import hashlib
import hmac
import inspect
import ipaddress
import json
import os
import secrets
import tempfile
import uuid
from contextlib import suppress
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import UploadFile

from core.application.auth.serve_auth_runtime import public_base_url
from core.application.management.errors import ServiceForbiddenError
from core.application.management.guards import require_project_access, require_workflow_access
from core.application.management.scope import normalize_scope
from core.application.registry.registry_manager import RegistryManager
from core.application.registry.workflow_contract_service import WorkflowInterfaceService
from core.application.workflows import (
    workflow_entrypoint_repository,
    workflow_provider_trigger_repository,
)
from core.application.workflows.workflow_entrypoint_idempotency import (
    accept_idempotency_reservation as _mark_idempotency_reservation_accepted,
)
from core.application.workflows.workflow_entrypoint_idempotency import (
    claim_idempotency_reservation as _claim_idempotency_reservation,
)
from core.application.workflows.workflow_entrypoint_idempotency import (
    fail_idempotency_reservation as _mark_idempotency_reservation_failed,
)
from core.application.workflows.workflow_entrypoint_idempotency import (
    find_existing_idempotent_job as _find_existing_idempotent_job,
)
from core.application.workflows.workflow_entrypoint_idempotency import (
    journal_idempotency_reservation as _journal_idempotency_reservation,
)
from core.application.workflows.workflow_entrypoint_idempotency import (
    renew_idempotency_reservation as _renew_idempotency_reservation,
)
from core.application.workflows.workflow_entrypoint_input_service import (
    WorkflowEntrypointInputService,
)
from core.application.workflows.workflow_start_runtime import enqueue_workflow_start
from core.application.workflows.workflow_trigger_catalog import (
    find_provider_trigger_definition,
    list_workflow_trigger_catalog,
    validate_provider_trigger_config,
)

APPLICATION_JSON_VALUE = "application/json"
DUPLICATE_KEY_VALUE = "duplicate key"
FORM_URLENCODED_VALUE = "application/x-www-form-urlencoded"
INVALID_BASIC_AUTH_CREDENTIALS_VALUE = "Invalid basic auth credentials"
MULTIPART_FORM_DATA_VALUE = "multipart/form-data"
PUBLIC_WORKFLOW_WEBHOOK_PATH_PREFIX = "/webhook/workflows/"
REQUEST_BODY_EXCEEDS_SIZE_LIMIT_VALUE = "Request body exceeds size limit"
SECRET_STORAGE_IS_UNAVAILABLE_MESSAGE = "Secret storage is unavailable"  # pragma: allowlist secret
SHA256_VALUE = "sha256="
UPLOAD_BIN_VALUE = "upload.bin"
WORKFLOW_ENTRYPOINT_NOT_FOUND_MESSAGE = "Workflow entrypoint not found"
WORKFLOW_IS_NOT_LINKED_TO_AN_ENABLED_PROJECT_VALUE = (
    "Workflow is not linked to an enabled project binding"
)
WORKFLOW_IS_NOT_LINKED_TO_THE_SELECTED_PROJECT_VALUE = (
    "Workflow is not linked to the selected project"
)
WORKFLOW_ENTRYPOINT_PROJECT_UNRESOLVED_VALUE = (
    "Resolved project for this workflow entrypoint is unavailable; "
    "select an explicit, accessible project"
)


def _remove_staged_upload(path: str) -> None:
    with suppress(OSError):
        os.unlink(path)


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _isoformat(value: Any) -> str | None:
    if isinstance(value, datetime):
        return value.isoformat()
    text = str(value or "").strip()
    return text or None


def _expected_input_names(workflow_id: str) -> set[str]:
    return WorkflowEntrypointInputService(WorkflowInterfaceService()).expected_input_names(
        workflow_id
    )


def _validate_workflow_inputs(
    workflow_id: str,
    workflow_inputs: dict[str, Any],
    *,
    channel: str,
) -> Any:
    """Validate mapped inputs through the canonical workflow contract service."""
    return WorkflowEntrypointInputService(WorkflowInterfaceService()).validate_inputs(
        workflow_id, workflow_inputs, channel=channel
    )


def _build_workflow_inputs(
    *,
    workflow_id: str,
    mapping: dict[str, str],
    sources: dict[str, Any],
    fallback_body: Any,
    file_refs: list[dict[str, Any]],
) -> dict[str, Any]:
    return WorkflowEntrypointInputService(WorkflowInterfaceService()).build_inputs(
        workflow_id=workflow_id,
        mapping=mapping,
        sources=sources,
        fallback_body=fallback_body,
        file_refs=file_refs,
    )


class RequestPolicyError(RuntimeError):
    def __init__(self, message: str, *, status_code: int) -> None:
        super().__init__(message)
        self.status_code = int(status_code)


def _load_workflow_entrypoint(bl, entry_key: str) -> dict[str, Any]:
    return workflow_entrypoint_repository.load_workflow_entrypoint(bl, entry_key)


def _resolve_project_run_config_for_workflow(
    bl, workflow_id: str
) -> tuple[str | None, dict[str, Any]]:
    return workflow_entrypoint_repository.resolve_project_run_config_for_workflow(bl, workflow_id)


def _raise_scope_forbidden(exc: ServiceForbiddenError) -> None:
    raise HTTPException(status_code=403, detail=str(exc)) from exc


def _require_workflow_scope(scope: Any, workflow_id: str) -> None:
    try:
        require_workflow_access(scope, str(workflow_id or "").strip())
    except ServiceForbiddenError as exc:
        _raise_scope_forbidden(exc)


def _entrypoint_project_is_resolvable(bl: Any, project_id: str | None) -> bool:
    """True when the resolved entrypoint project can actually be loaded.

    A webhook entrypoint must not bind to a project that ``get_project`` cannot
    resolve (e.g. an orphaned ``project_workflows`` row pointing at a deleted
    project). Otherwise the entrypoint is created / accepts public traffic, but every
    enqueued job dies at runtime with ``Project lookup unavailable`` (see
    JobQueryService._job_context). We mirror that same lookup here so the failure is
    surfaced at create/trigger time instead of per-job at runtime.
    """
    normalized_project_id = str(project_id or "").strip()
    if not normalized_project_id:
        return False
    if not hasattr(bl, "get_project"):
        raise RuntimeError("Project storage is unavailable")
    return isinstance(bl.get_project(normalized_project_id), dict)


def _require_project_scope(bl: Any, scope: Any, project_id: str | None) -> str | None:
    normalized_project_id = str(project_id or "").strip() or None
    if not normalized_project_id:
        return None
    project = bl.get_project(normalized_project_id) if hasattr(bl, "get_project") else None
    if isinstance(project, dict):
        try:
            require_project_access(scope, dict(project))
        except ServiceForbiddenError as exc:
            _raise_scope_forbidden(exc)
        return normalized_project_id
    allowed_project_ids = getattr(scope, "allowed_project_ids", None)
    if allowed_project_ids and normalized_project_id not in allowed_project_ids:
        raise HTTPException(status_code=403, detail="Access denied")
    return normalized_project_id


def _is_table_missing_error(exc: Exception) -> bool:
    return _relation_missing_error(exc, "request_idempotency")


def _relation_missing_error(exc: Exception, relation_name: str) -> bool:
    text = str(exc or "").strip().lower()
    relation = str(relation_name or "").strip().lower()
    return bool(relation) and relation in text and "does not exist" in text


def _append_form_field(target: dict[str, Any], key: str, value: str) -> None:
    existing = target.get(key)
    if existing is None:
        target[key] = value
        return
    if isinstance(existing, list):
        existing.append(value)
        target[key] = existing
        return
    target[key] = [existing, value]


_MULTIPART_READ_CHUNK_BYTES = 1024 * 1024


async def _invoke_upload_heartbeat(heartbeat) -> None:
    if not callable(heartbeat):
        return
    result = heartbeat()
    if inspect.isawaitable(result):
        await result


async def _spool_upload_with_cap(
    raw: Any,
    *,
    destination: Any,
    max_file_bytes: int,
    heartbeat=None,
) -> tuple[int, str]:
    """Spool an upload with bounded memory while computing its durable identity."""
    cap = max(1, int(max_file_bytes or 0))
    digest = hashlib.sha256()
    total = 0
    while True:
        await _invoke_upload_heartbeat(heartbeat)
        chunk = await raw.read(_MULTIPART_READ_CHUNK_BYTES)
        if not chunk:
            break
        total += len(chunk)
        if total > cap:
            raise RequestPolicyError("Uploaded file exceeds size limit", status_code=413)
        digest.update(chunk)
        try:
            await run_in_threadpool(destination.write, chunk)
        except OSError as exc:
            if exc.errno == errno.ENOSPC:
                raise RequestPolicyError(
                    "Temporary upload storage is full",
                    status_code=507,
                ) from exc
            raise
    await run_in_threadpool(destination.flush)
    await run_in_threadpool(destination.seek, 0)
    return total, digest.hexdigest()


async def _stage_multipart_files(
    *,
    form_data,
    max_file_bytes: int,
    max_files: int,
    heartbeat=None,
) -> tuple[dict[str, Any], list[Any]]:
    from core.application.management.command_models import ProjectTaskFileInput
    from core.application.management.staged_attachment_service import (
        staged_attachment_filename_is_safe,
    )

    fields: dict[str, Any] = {}
    files: list[ProjectTaskFileInput] = []
    try:
        for field_name, raw in form_data.multi_items():
            await _invoke_upload_heartbeat(heartbeat)
            if isinstance(raw, UploadFile):
                if len(files) >= max(0, int(max_files or 0)):
                    raise RequestPolicyError("Too many uploaded files", status_code=413)
                if not staged_attachment_filename_is_safe(str(raw.filename or "")):
                    raise RequestPolicyError("Uploaded filename is invalid", status_code=400)
                staged_path = ""
                try:
                    staged_manager = await run_in_threadpool(
                        tempfile.NamedTemporaryFile,
                        prefix="fs-workflow-upload-",
                        delete=False,
                    )
                    staged_file = await run_in_threadpool(staged_manager.__enter__)
                    staged_path = staged_file.name
                    try:
                        expected_size, expected_sha = await _spool_upload_with_cap(
                            raw,
                            destination=staged_file,
                            max_file_bytes=max_file_bytes,
                            heartbeat=heartbeat,
                        )
                    finally:
                        await run_in_threadpool(staged_manager.__exit__, None, None, None)
                    await _invoke_upload_heartbeat(heartbeat)
                    if _is_zip_upload(raw.filename, raw.content_type):
                        from core.infrastructure.security.zip_guard import validate_zip_safe

                        def _inspect_zip(
                            file_path: str = staged_path,
                            zip_validator=validate_zip_safe,
                        ):
                            with open(file_path, "rb") as staged_file:
                                return zip_validator(staged_file)

                        guard = await run_in_threadpool(_inspect_zip)
                        if not guard.safe:
                            raise ValueError(f"Rejected archive '{raw.filename}': {guard.reason}")
                    files.append(
                        ProjectTaskFileInput(
                            filename=str(raw.filename or UPLOAD_BIN_VALUE).strip()
                            or UPLOAD_BIN_VALUE,
                            mime_type=str(raw.content_type or "application/octet-stream").strip(),
                            content_path=staged_path,
                            size_bytes=expected_size,
                            sha256=expected_sha,
                        )
                    )
                except BaseException:
                    if staged_path:
                        await run_in_threadpool(_remove_staged_upload, staged_path)
                    raise
                continue
            _append_form_field(fields, str(field_name), str(raw))
    except BaseException:
        for staged in files:
            staged.cleanup()
        raise
    return fields, files


def _is_zip_upload(filename: str | None, content_type: str | None) -> bool:
    """Return True if the upload looks like a zip archive based on filename or MIME type."""
    fname = str(filename or "").strip().lower()
    ct = str(content_type or "").strip().lower().split(";")[0].strip()
    return fname.endswith(".zip") or ct in (
        "application/zip",
        "application/x-zip",
        "application/x-zip-compressed",
    )


def _normalize_response_mode(raw: Any) -> str:
    mode = str(raw or "").strip().lower()
    if mode in {"", "accepted", "async", "queued", "202"}:
        return "accepted"
    if mode in {"ok", "sync", "immediate", "200"}:
        return "ok"
    if mode in {"no_content", "empty", "204"}:
        return "no_content"
    return "accepted"


def _entrypoint_success_response(
    payload: dict[str, Any],
    *,
    response_mode: Any,
    deduplicated: bool = False,
) -> Response:
    normalized_mode = _normalize_response_mode(response_mode)
    headers = {"x-workflow-response-mode": normalized_mode}
    if normalized_mode == "no_content":
        return Response(status_code=204, headers=headers)
    status_code = 200 if deduplicated or normalized_mode == "ok" else 202
    return JSONResponse(payload, status_code=status_code, headers=headers)


__all__ = [name for name in globals() if not (name.startswith("__") and name.endswith("__"))]
