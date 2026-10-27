from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from dataclasses import dataclass
from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException as StarletteHTTPException

from core.application.idempotency.service import RequestIdempotencyService
from core.application.jobs.submission_admission import (
    SubmissionAdmissionController,
    SubmissionAdmissionLease,
)
from core.application.workflows import workflow_entrypoint_repository
from core.application.workflows.workflow_entrypoint_audit_service import (
    WorkflowEntrypointAuditRejection,
    WorkflowEntrypointAuditService,
    WorkflowEntrypointPreparationAuditFacts,
)
from core.application.workflows.workflow_entrypoint_execution_service import (
    WorkflowEntrypointExecutionCoordinator,
    WorkflowEntrypointPreparation,
    durable_job_dispatch_confirmed,
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
from core.application.workflows.workflow_entrypoint_request_service import (
    WorkflowEntrypointRequestCommand,
    WorkflowEntrypointRequestResult,
    WorkflowEntrypointRequestService,
)
from core.application.workflows.workflow_start_runtime import enqueue_workflow_start
from core.infrastructure.config.task_upload import TASK_UPLOAD_ADMISSION_SETTINGS

from .workflow_entrypoints_common import (
    APPLICATION_JSON_VALUE,
    FORM_URLENCODED_VALUE,
    MULTIPART_FORM_DATA_VALUE,
    RequestPolicyError,
    _append_form_field,
    _entrypoint_success_response,
    _load_workflow_entrypoint,
    _now_iso,
    _relation_missing_error,
    _stage_multipart_files,
)
from .workflow_entrypoints_execution_auth import (
    _derive_secret_replay_key,
    _validate_workflow_entrypoint_auth,
    is_body_dependent_auth_mode,
    workflow_entrypoint_auth_http_status,
)
from .workflow_entrypoints_registry import (
    _content_type_base,
    _delivery_meta,
    _effective_entrypoint_auth_mode,
    _enforce_content_length_limit,
    _enforce_content_type_policy,
    _install_staged_request_replay,
    _mapping_selector_allowed_for_auth,
    _normalize_allowed_content_types,
    _normalize_limit,
    _normalize_mapping,
    _normalize_source_restrictions,
    _payload_summary,
    _read_body_with_cap,
    _request_ip_for_security,
    _source_ip_allowed,
    _stage_request_body_with_cap,
)

workflow_entrypoint_submission_admission = SubmissionAdmissionController(
    capacity=TASK_UPLOAD_ADMISSION_SETTINGS.total_capacity,
    per_account_capacity=TASK_UPLOAD_ADMISSION_SETTINGS.per_account_capacity,
    per_principal_capacity=TASK_UPLOAD_ADMISSION_SETTINGS.per_principal_capacity,
)
_ADMISSION_RETRY_AFTER_SECONDS = 2
_BODY_READ_TIMEOUT_SECONDS = TASK_UPLOAD_ADMISSION_SETTINGS.body_read_timeout_seconds


@dataclass(frozen=True)
class _TransportBody:
    entrypoint: dict[str, Any]
    raw_body: bytes
    raw_body_sha256: str
    staged_body: Any | None
    max_file_bytes: int
    max_files: int
    auth_mode: str
    response_mode: Any
    account_id: str | None
    admission_lease: SubmissionAdmissionLease | None


@dataclass(frozen=True)
class WorkflowEntrypointTransportPayload:
    request_id: str
    idempotency_key: str
    idempotency_key_digest: str | None
    replay_key: str
    mapping: dict[str, Any]
    sources: dict[str, Any]
    fallback_body: Any
    staged_files: list[Any]
    request_context: dict[str, Any]
    payload_summary: dict[str, Any]
    auth_mode: str
    response_mode: Any
    account_id: str | None
    audit_meta: dict[str, Any]
    admission_lease: SubmissionAdmissionLease | None = None


@dataclass(frozen=True)
class WorkflowEntrypointTransportFailure:
    status_code: int
    error: str
    code: str
    reason: str
    task_id: str
    auth_mode: str
    response_mode: Any
    account_id: str | None
    audit_meta: dict[str, Any]
    request_id: str = ""
    idempotency_key_digest: str | None = None


def _transport_audit_rejection(
    preparation: WorkflowEntrypointPreparation,
    failure: WorkflowEntrypointTransportFailure,
) -> WorkflowEntrypointAuditRejection:
    return WorkflowEntrypointAuditRejection(
        project_id=str(preparation.project_id or "").strip() or None,
        workflow_id=str(preparation.workflow_id or "").strip(),
        task_id=str(failure.task_id or "").strip(),
        reason=str(failure.reason or failure.code or "transport_rejected").strip(),
        error=str(failure.error or "").strip(),
        account_id=str(failure.account_id or "").strip() or None,
        request_id=str(failure.request_id or "").strip(),
        idempotency_key_digest=failure.idempotency_key_digest,
    )


def _preparation_audit_rejection(
    preparation: WorkflowEntrypointPreparation,
) -> WorkflowEntrypointPreparationAuditFacts:
    entrypoint = dict(preparation.entrypoint or {})
    return WorkflowEntrypointPreparationAuditFacts(
        code=str(preparation.code or "").strip(),
        error=str(preparation.error or "").strip(),
        project_id=str(preparation.project_id or "").strip() or None,
        workflow_id=str(preparation.workflow_id or "").strip(),
        account_id=str(entrypoint.get("account_id") or "").strip() or None,
    )


def _request_service(ctx: dict[str, Any], bl: Any) -> WorkflowEntrypointRequestService:
    coordinator = WorkflowEntrypointExecutionCoordinator(
        logger=bl,
        artifact_plane=ctx["artifact_plane"],
        redeliver_task=ctx.get("redeliver_task"),
        enqueue_workflow_start=lambda **kwargs: enqueue_workflow_start(
            enqueue_task_fn=ctx["enqueue_task"], **kwargs
        ),
        now_iso=_now_iso,
        load_entrypoint=_load_workflow_entrypoint,
        resolve_bound_project=(
            workflow_entrypoint_repository.resolve_project_run_config_for_workflow_project
        ),
        storage_unavailable_error=lambda exc: _relation_missing_error(exc, "workflow_entrypoints"),
        claim_reservation=_claim_idempotency_reservation,
        find_existing_job=_find_existing_idempotent_job,
        accept_reservation=_mark_idempotency_reservation_accepted,
        fail_reservation=_mark_idempotency_reservation_failed,
        renew_reservation=_renew_idempotency_reservation,
        journal_reservation=_journal_idempotency_reservation,
        dispatch_confirmed=durable_job_dispatch_confirmed,
    )
    return WorkflowEntrypointRequestService(coordinator)


def _failure(
    *,
    request: Request,
    preparation: WorkflowEntrypointPreparation,
    status_code: int,
    error: str,
    code: str,
    reason: str,
    task_id: str,
    auth_mode: str,
    response_mode: Any,
    account_id: str | None,
    auth_outcome: str = "skipped",
    request_id: str = "",
    idempotency_key_digest: str | None = None,
) -> WorkflowEntrypointTransportFailure:
    del preparation
    return WorkflowEntrypointTransportFailure(
        status_code=status_code,
        error=error,
        code=code,
        reason=reason,
        task_id=task_id,
        auth_mode=auth_mode,
        response_mode=response_mode,
        account_id=account_id,
        request_id=request_id,
        idempotency_key_digest=idempotency_key_digest,
        audit_meta=_delivery_meta(
            request=request,
            auth_mode=auth_mode,
            auth_outcome=auth_outcome,
            http_status=status_code,
            response_mode=response_mode,
        ),
    )


async def _admit_transport_request(
    preparation: WorkflowEntrypointPreparation,
    *,
    request: Request,
    bl: Any,
    entry_key: str,
) -> _TransportBody | WorkflowEntrypointTransportFailure:
    entrypoint = preparation.entrypoint
    project_id = preparation.project_id
    workflow_id = preparation.workflow_id
    response_mode = entrypoint.get("response_mode")
    auth_mode = _effective_entrypoint_auth_mode(entrypoint)
    account_id = str(entrypoint.get("account_id") or "").strip() or None

    if not _source_ip_allowed(
        _normalize_source_restrictions(entrypoint.get("source_restrictions_jsonb")), request
    ):
        return _failure(
            request=request,
            preparation=preparation,
            status_code=403,
            error="Request source is not allowed",
            code="",
            reason="source_ip_blocked",
            task_id=f"webhook:{entry_key}:source-ip",
            auth_mode=auth_mode,
            response_mode=response_mode,
            account_id=account_id,
        )

    max_body_bytes = _normalize_limit(
        entrypoint.get("max_body_bytes"),
        default=44 * 1024 * 1024,
        minimum=1,
        maximum=100 * 1024 * 1024,
    )
    max_file_bytes = _normalize_limit(entrypoint.get("max_file_bytes"), default=10485760, minimum=1)
    max_files = _normalize_limit(entrypoint.get("max_files"), default=4, minimum=0)
    try:
        _enforce_content_type_policy(
            _normalize_allowed_content_types(entrypoint.get("allowed_content_types_jsonb")),
            request,
        )
        _enforce_content_length_limit(request, max_body_bytes=max_body_bytes)
    except RequestPolicyError as exc:
        return _failure(
            request=request,
            preparation=preparation,
            status_code=exc.status_code,
            error=str(exc),
            code="",
            reason="request_policy_failed",
            task_id=f"webhook:{entry_key}:request-policy",
            auth_mode=auth_mode,
            response_mode=response_mode,
            account_id=account_id,
        )

    if not is_body_dependent_auth_mode(auth_mode):
        auth_outcome = await run_in_threadpool(
            _validate_workflow_entrypoint_auth,
            bl=bl,
            project_id=project_id,
            clean_entry_key=entry_key,
            selected_workflow_id=workflow_id,
            entrypoint=entrypoint,
            request=request,
            raw_body=b"",
            raw_body_file=None,
            auth_mode=auth_mode,
            response_mode=response_mode,
        )
        if not auth_outcome.ok:
            auth_reason = str(auth_outcome.reason or "auth_failed")
            return _failure(
                request=request,
                preparation=preparation,
                status_code=workflow_entrypoint_auth_http_status(auth_outcome),
                error=str(auth_outcome.error or "Workflow entrypoint authentication failed"),
                code="",
                reason=auth_reason,
                task_id=f"webhook:{entry_key}:{auth_reason}",
                auth_mode=auth_mode,
                response_mode=response_mode,
                account_id=account_id,
                auth_outcome="failed",
            )

    staged_body = None
    raw_body = b""
    raw_body_sha256 = ""
    admission_lease: SubmissionAdmissionLease | None = None
    admission_handed_off = False
    admission_lease = workflow_entrypoint_submission_admission.try_acquire(
        account_id=account_id or project_id,
        principal_id=f"{entry_key}:{auth_mode}",
    )
    if admission_lease is None:
        return _failure(
            request=request,
            preparation=preparation,
            status_code=429,
            error="Workflow submission capacity is exhausted; retry later",
            code="submission_admission_limited",
            reason="submission_admission_limited",
            task_id=f"webhook:{entry_key}:submission-capacity",
            auth_mode=auth_mode,
            response_mode=response_mode,
            account_id=account_id,
        )
    try:
        try:
            try:
                async with asyncio.timeout(max(0.001, float(_BODY_READ_TIMEOUT_SECONDS))):
                    if _content_type_base(request) == MULTIPART_FORM_DATA_VALUE:
                        staged_body, raw_body_sha256 = await _stage_request_body_with_cap(
                            request, max_body_bytes=max_body_bytes
                        )
                        await _install_staged_request_replay(request, staged_body)
                    else:
                        raw_body = await _read_body_with_cap(request, max_body_bytes=max_body_bytes)
                        raw_body_sha256 = hashlib.sha256(raw_body).hexdigest()
            except TimeoutError as exc:
                raise RequestPolicyError(
                    "Workflow request body was not received before the deadline",
                    status_code=408,
                ) from exc
        except RequestPolicyError as exc:
            if staged_body is not None:
                staged_body.close()
            timed_out = exc.status_code == 408
            return _failure(
                request=request,
                preparation=preparation,
                status_code=exc.status_code,
                error=str(exc),
                code="request_body_timeout" if timed_out else "",
                reason="request_body_timeout" if timed_out else "body_too_large",
                task_id=f"webhook:{entry_key}:body-too-large",
                auth_mode=auth_mode,
                response_mode=response_mode,
                account_id=account_id,
            )

        if is_body_dependent_auth_mode(auth_mode):
            auth_outcome = await run_in_threadpool(
                _validate_workflow_entrypoint_auth,
                bl=bl,
                project_id=project_id,
                clean_entry_key=entry_key,
                selected_workflow_id=workflow_id,
                entrypoint=entrypoint,
                request=request,
                raw_body=raw_body,
                raw_body_file=staged_body,
                auth_mode=auth_mode,
                response_mode=response_mode,
            )
            if not auth_outcome.ok:
                if staged_body is not None:
                    staged_body.close()
                auth_reason = str(auth_outcome.reason or "auth_failed")
                return _failure(
                    request=request,
                    preparation=preparation,
                    status_code=workflow_entrypoint_auth_http_status(auth_outcome),
                    error=str(auth_outcome.error or "Workflow entrypoint authentication failed"),
                    code="",
                    reason=auth_reason,
                    task_id=f"webhook:{entry_key}:{auth_reason}",
                    auth_mode=auth_mode,
                    response_mode=response_mode,
                    account_id=account_id,
                    auth_outcome="failed",
                )
        admission_handed_off = True
        return _TransportBody(
            entrypoint=entrypoint,
            raw_body=raw_body,
            raw_body_sha256=raw_body_sha256,
            staged_body=staged_body,
            max_file_bytes=max_file_bytes,
            max_files=max_files,
            auth_mode=auth_mode,
            response_mode=response_mode,
            account_id=account_id,
            admission_lease=admission_lease,
        )
    finally:
        if admission_lease is not None and not admission_handed_off:
            admission_lease.release()


async def _parse_transport_payload(
    admitted: _TransportBody,
    *,
    preparation: WorkflowEntrypointPreparation,
    request: Request,
    entry_key: str,
    logger: Any,
) -> WorkflowEntrypointTransportPayload | WorkflowEntrypointTransportFailure:
    entrypoint = admitted.entrypoint
    raw_body = admitted.raw_body
    raw_body_sha256 = admitted.raw_body_sha256
    staged_body = admitted.staged_body
    max_file_bytes = admitted.max_file_bytes
    max_files = admitted.max_files
    auth_mode = admitted.auth_mode
    response_mode = admitted.response_mode
    account_id = admitted.account_id
    workflow_id = preparation.workflow_id

    request_id = str(
        request.headers.get("x-request-id")
        or request.headers.get("x-correlation-id")
        or uuid.uuid4().hex
    ).strip()
    idempotency_key = str(request.headers.get("Idempotency-Key") or "").strip()
    idempotency_digest = (
        RequestIdempotencyService.digest_key(idempotency_key) if idempotency_key else None
    )
    mapping = _normalize_mapping(entrypoint.get("input_mapping_jsonb"))
    invalid_selectors = sorted(
        source
        for source in mapping.values()
        if not _mapping_selector_allowed_for_auth(
            source, dict(entrypoint.get("auth_config_jsonb") or {})
        )
    )
    if invalid_selectors:
        if staged_body is not None:
            staged_body.close()
        return _failure(
            request=request,
            preparation=preparation,
            status_code=400,
            error="Workflow entrypoint contains a sensitive header mapping",
            code="sensitive_header_mapping",
            reason="sensitive_header_mapping",
            task_id=f"webhook:{entry_key}:{request_id}",
            auth_mode=auth_mode,
            response_mode=response_mode,
            account_id=account_id,
            auth_outcome="passed",
            request_id=request_id,
            idempotency_key_digest=idempotency_digest,
        )

    form_fields: dict[str, Any] = {}
    body_payload: Any = {}
    staged_files: list[Any] = []
    content_type = _content_type_base(request)
    try:
        if MULTIPART_FORM_DATA_VALUE in content_type:
            multipart_limits = {
                "max_files": max_files,
                "max_fields": 32,
                "max_part_size": 262_144,
            }
            form_data = await request.form(**multipart_limits)
            try:
                form_fields, staged_files = await _stage_multipart_files(
                    form_data=form_data,
                    max_file_bytes=max_file_bytes,
                    max_files=max_files,
                )
            finally:
                await form_data.close()
            body_payload = dict(form_fields)
        elif content_type == APPLICATION_JSON_VALUE:
            body_payload = {} if not raw_body else json.loads(raw_body.decode("utf-8"))
            if not isinstance(body_payload, dict):
                body_payload = {"body": body_payload}
        elif content_type == FORM_URLENCODED_VALUE:
            form_data = await request.form()
            for field_name, raw in form_data.multi_items():
                _append_form_field(form_fields, str(field_name), str(raw))
            body_payload = dict(form_fields)
        else:
            text = raw_body.decode("utf-8", errors="replace").strip()
            body_payload = {"body": text} if text else {}
    except StarletteHTTPException as exc:
        too_many_files = "too many files" in str(exc.detail or "").lower()
        return _failure(
            request=request,
            preparation=preparation,
            status_code=413 if too_many_files else 400,
            error="Too many uploaded files" if too_many_files else "Invalid request payload",
            code="payload_policy_failed" if too_many_files else "payload_parse_failed",
            reason="payload_policy_failed" if too_many_files else "payload_parse_failed",
            task_id=f"webhook:{entry_key}:{request_id}",
            auth_mode=auth_mode,
            response_mode=response_mode,
            account_id=account_id,
            auth_outcome="passed",
            request_id=request_id,
            idempotency_key_digest=idempotency_digest,
        )
    except RequestPolicyError as exc:
        return _failure(
            request=request,
            preparation=preparation,
            status_code=exc.status_code,
            error=str(exc),
            code="payload_policy_failed",
            reason="payload_policy_failed",
            task_id=f"webhook:{entry_key}:{request_id}",
            auth_mode=auth_mode,
            response_mode=response_mode,
            account_id=account_id,
            auth_outcome="passed",
            request_id=request_id,
            idempotency_key_digest=idempotency_digest,
        )
    except Exception as exc:
        logger.warning(
            "Workflow entrypoint payload parsing failed entry_key=%s error_type=%s",
            entry_key,
            type(exc).__name__,
        )
        return _failure(
            request=request,
            preparation=preparation,
            status_code=400,
            error="Invalid request payload",
            code="payload_parse_failed",
            reason="payload_parse_failed",
            task_id=f"webhook:{entry_key}:{request_id}",
            auth_mode=auth_mode,
            response_mode=response_mode,
            account_id=account_id,
            auth_outcome="passed",
            request_id=request_id,
            idempotency_key_digest=idempotency_digest,
        )
    finally:
        if staged_body is not None:
            staged_body.close()

    sources = {
        "body": body_payload,
        "form": form_fields,
        "query": dict(request.query_params),
        "headers": {str(key): str(value) for key, value in request.headers.items()},
        "files": [],
    }
    payload_summary = _payload_summary(
        body_payload=body_payload,
        form_fields=form_fields,
        query_params=dict(request.query_params),
        file_refs=[{"filename": staged.filename} for staged in staged_files],
    )
    idempotency_mode = str(entrypoint.get("idempotency_mode") or "none").strip().lower()
    replay_key = ""
    if is_body_dependent_auth_mode(auth_mode) or (
        auth_mode in {"bearer_token", "shared_secret_header", "basic_auth"}
        and idempotency_mode != "header"
    ):
        replay_key = _derive_secret_replay_key(
            auth_mode=auth_mode,
            entrypoint=entrypoint,
            request=request,
            raw_body=raw_body,
            raw_body_sha256=raw_body_sha256,
            selected_workflow_id=workflow_id,
        )
    return WorkflowEntrypointTransportPayload(
        request_id=request_id,
        idempotency_key=idempotency_key,
        idempotency_key_digest=idempotency_digest,
        replay_key=replay_key,
        mapping=mapping,
        sources=sources,
        fallback_body=body_payload,
        staged_files=staged_files,
        request_context={
            "source_ip": _request_ip_for_security(request),
            "content_type": content_type,
            "payload_summary": payload_summary,
        },
        payload_summary=payload_summary,
        auth_mode=auth_mode,
        response_mode=response_mode,
        account_id=account_id,
        audit_meta=_delivery_meta(
            request=request,
            auth_mode=auth_mode,
            auth_outcome="passed",
            http_status=0,
            response_mode=response_mode,
            payload_summary=payload_summary,
        ),
        admission_lease=admitted.admission_lease,
    )


async def _load_transport_payload(
    preparation: WorkflowEntrypointPreparation,
    *,
    request: Request,
    bl: Any,
    entry_key: str,
    logger: Any,
) -> WorkflowEntrypointTransportPayload | WorkflowEntrypointTransportFailure:
    admitted = await _admit_transport_request(
        preparation,
        request=request,
        bl=bl,
        entry_key=entry_key,
    )
    if isinstance(admitted, WorkflowEntrypointTransportFailure):
        return admitted
    try:
        parsed = await _parse_transport_payload(
            admitted,
            preparation=preparation,
            request=request,
            entry_key=entry_key,
            logger=logger,
        )
    except BaseException:
        if admitted.admission_lease is not None:
            admitted.admission_lease.release()
        raise
    if (
        isinstance(parsed, WorkflowEntrypointTransportFailure)
        and admitted.admission_lease is not None
    ):
        admitted.admission_lease.release()
    return parsed


def _submission_http_response(
    result: WorkflowEntrypointRequestResult,
    *,
    workflow_id: str,
    response_mode: Any,
):
    outcome = result.outcome
    if not outcome.ok:
        payload: dict[str, Any] = {"error": outcome.error, "code": outcome.code}
        if outcome.errors:
            payload["errors"] = outcome.errors
        if outcome.recoverable_job_id:
            payload["job_id"] = outcome.recoverable_job_id
        elif outcome.replay_job_id:
            payload["job_id"] = outcome.replay_job_id
        return JSONResponse(
            payload, status_code=_application_failure_http_status(outcome.failure_kind)
        )

    payload = {
        "status": "accepted",
        "job_id": outcome.job_id,
        "deduplicated": result.duplicate,
        "selected_workflow_id": workflow_id,
        "workflow_selection_source": "entry_binding",
    }
    if not result.duplicate:
        payload["file_count"] = outcome.file_count
    return _entrypoint_success_response(
        payload,
        response_mode=response_mode,
        deduplicated=result.duplicate,
    )


def _failure_http_response(failure: WorkflowEntrypointTransportFailure) -> JSONResponse:
    payload = {"error": failure.error}
    if failure.code:
        payload["code"] = failure.code
    headers = None
    if failure.status_code == 429 and failure.code == "submission_admission_limited":
        headers = {"Retry-After": str(_ADMISSION_RETRY_AFTER_SECONDS)}
    return JSONResponse(payload, status_code=failure.status_code, headers=headers)


def _application_failure_http_status(failure_kind: str | None) -> int:
    return {
        "invalid_request": 400,
        "not_found": 404,
        "conflict": 409,
        "gone": 410,
        "unavailable": 503,
        "internal": 500,
    }.get(str(failure_kind or "internal"), 500)


class _WorkflowEntrypointHttpController:
    def __init__(self, *, ctx: dict[str, Any], request: Request, entry_key: str, bl: Any) -> None:
        self._ctx = ctx
        self._request = request
        self._entry_key = str(entry_key or "").strip()
        self._bl = bl
        self._service = _request_service(ctx, bl)
        self._audit = WorkflowEntrypointAuditService(
            bl,
            entry_key=self._entry_key,
            now_iso=_now_iso,
        )

    async def execute(self):
        if not self._entry_key:
            return JSONResponse({"error": "entry_key is required"}, status_code=400)
        preparation = await run_in_threadpool(self._service.prepare, self._entry_key)
        if not preparation.ok:
            return await self._preparation_failure(preparation)
        transport = await _load_transport_payload(
            preparation,
            request=self._request,
            bl=self._bl,
            entry_key=self._entry_key,
            logger=self._ctx["logger"],
        )
        if isinstance(transport, WorkflowEntrypointTransportFailure):
            response = _failure_http_response(transport)
            await run_in_threadpool(
                self._audit.record_rejection_outcome,
                rejection=_transport_audit_rejection(preparation, transport),
                delivery_meta={
                    **transport.audit_meta,
                    "http_status": response.status_code,
                },
            )
            return response

        try:
            return await self._submit(preparation, transport)
        finally:
            if transport.admission_lease is not None:
                transport.admission_lease.release()

    async def _submit(
        self,
        preparation: WorkflowEntrypointPreparation,
        transport: WorkflowEntrypointTransportPayload,
    ):

        command = WorkflowEntrypointRequestCommand(
            entry_key=self._entry_key,
            task_id=f"webhook:{self._entry_key}:{transport.request_id}",
            project_id=str(preparation.project_id),
            run_config=dict(preparation.run_config or {}),
            workflow_id=preparation.workflow_id,
            request_id=transport.request_id,
            idempotency_mode=str(preparation.entrypoint.get("idempotency_mode") or "none")
            .strip()
            .lower(),
            idempotency_key=transport.idempotency_key,
            replay_key=transport.replay_key,
            mapping=dict(transport.mapping),
            sources=dict(transport.sources),
            fallback_body=transport.fallback_body,
            staged_files=tuple(transport.staged_files),
            request_context=dict(transport.request_context),
        )
        result = await run_in_threadpool(self._service.execute, command)
        if result.outcome.operator_error:
            self._ctx["logger"].warning(
                "Workflow entrypoint submission failed entry_key=%s code=%s detail=%s",
                self._entry_key,
                result.outcome.code,
                result.outcome.operator_error,
            )
        response = _submission_http_response(
            result,
            workflow_id=preparation.workflow_id,
            response_mode=transport.response_mode,
        )
        await run_in_threadpool(
            self._audit.record_submission,
            command=command,
            result=result,
            account_id=transport.account_id,
            idempotency_key_digest=transport.idempotency_key_digest,
            delivery_meta={
                **transport.audit_meta,
                "http_status": response.status_code,
            },
        )
        return response

    async def _preparation_failure(self, preparation: WorkflowEntrypointPreparation):
        if preparation.operator_error:
            self._ctx["logger"].warning(
                "Failed to prepare workflow entrypoint entry_key=%s code=%s detail=%s",
                self._entry_key,
                preparation.code,
                preparation.operator_error,
            )
        response = JSONResponse(
            {"error": preparation.error, "code": preparation.code},
            status_code=_application_failure_http_status(preparation.failure_kind),
        )
        entrypoint = dict(preparation.entrypoint or {})
        await run_in_threadpool(
            self._audit.record_preparation_rejection,
            facts=_preparation_audit_rejection(preparation),
            delivery_meta=_delivery_meta(
                request=self._request,
                auth_mode=_effective_entrypoint_auth_mode(entrypoint) if entrypoint else "",
                auth_outcome="skipped",
                http_status=response.status_code,
                response_mode=entrypoint.get("response_mode"),
            ),
        )
        return response


def _register_workflow_entrypoint_execution_route(ctx: dict[str, Any]) -> None:
    router = ctx["router"]

    @router.post(
        "/webhook/workflows/{entry_key}",
        responses={
            403: {"description": "Forbidden"},
            404: {"description": "Not found"},
            429: {
                "description": "Multipart admission capacity exhausted",
                "headers": {
                    "Retry-After": {
                        "description": "Seconds before retrying the request",
                        "schema": {"type": "integer", "minimum": 1},
                    }
                },
            },
        },
    )
    async def trigger_workflow_entrypoint(entry_key: str, request: Request):
        """Delegate protocol orchestration to the explicit HTTP controller."""
        bl = ctx["get_logger_only"]()
        if not bl:
            return JSONResponse({"error": ctx["err_database_not_configured"]}, status_code=503)
        return await _WorkflowEntrypointHttpController(
            ctx=ctx,
            request=request,
            entry_key=entry_key,
            bl=bl,
        ).execute()


__all__ = [name for name in globals() if not (name.startswith("__") and name.endswith("__"))]
