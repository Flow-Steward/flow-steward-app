"""Versioned API contract layer for React migration consumers.

The existing `/api/*` routes remain available for page scripts that still use
them. This router exposes normalized `/api/v2/*` envelopes for
list/detail/mutation flows so React pages can adopt one predictable contract
shape incrementally.
"""

from __future__ import annotations

import inspect
import json
from collections.abc import Callable
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse

from core.application.management import ServiceError
from core.application.management.command_models import (
    ApiV2DashboardSummaryEnvelopeView,
    ApiV2JobDetailEnvelopeView,
    ApiV2JobsEnvelopeView,
    ApiV2ProjectJobsBulkDeleteEnvelopeView,
    ApiV2ProjectJobsEnvelopeView,
    ApiV2ProjectsEnvelopeView,
    ProjectJobsBulkDeleteInput,
)
from core.application.management.requests import ProjectJobsBulkDeleteRequest
from core.application.reports.job_detail_contract import (
    build_job_detail_contract,
    build_job_execution_steps_page,
)
from core.entrypoints.api import dashboard_metrics as dashboard_metrics_api
from core.entrypoints.api import dependencies as api_dependencies

REQUEST_FAILED_VALUE = "Request failed"


_STATUS_ERROR_CODES: dict[int, str] = {
    401: "unauthorized",
    403: "forbidden",
    404: "not_found",
    409: "conflict",
    422: "validation_error",
    500: "server_error",
}


def _error_code_for_status(status_code: int) -> str:
    if status_code >= 500:
        return _STATUS_ERROR_CODES[500]
    return _STATUS_ERROR_CODES.get(status_code, "request_failed")


def _extract_pagination(payload: Any) -> dict[str, int] | None:
    if not isinstance(payload, dict):
        return None
    keys = ("page", "per_page", "total", "total_pages")
    if not all(key in payload for key in keys):
        return None
    try:
        return {
            "page": int(payload.get("page") or 1),
            "per_page": int(payload.get("per_page") or 25),
            "total": int(payload.get("total") or 0),
            "total_pages": int(payload.get("total_pages") or 1),
        }
    except (TypeError, ValueError):
        return None


def _success_envelope(
    *,
    kind: str,
    resource: str,
    data: Any,
    meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload_meta: dict[str, Any] = {"resource": resource}
    if meta:
        payload_meta.update(meta)
    pagination = _extract_pagination(data)
    if pagination is not None:
        payload_meta["pagination"] = pagination
    return {
        "ok": True,
        "kind": kind,
        "data": data,
        "error": None,
        "meta": payload_meta,
    }


def _error_envelope(
    *,
    status_code: int,
    kind: str,
    resource: str,
    message: str,
    details: Any | None = None,
    code: str | None = None,
) -> JSONResponse:
    return JSONResponse(
        {
            "ok": False,
            "kind": kind,
            "data": None,
            "error": {
                "code": code or _error_code_for_status(status_code),
                "message": str(message or REQUEST_FAILED_VALUE),
                "status": int(status_code),
                "details": details,
            },
            "meta": {"resource": resource},
        },
        status_code=int(status_code),
    )


def _service_error(
    exc: ServiceError,
    *,
    kind: str,
    resource: str,
) -> JSONResponse:
    status_code = int(getattr(exc, "status_code", 500) or 500)
    details = getattr(exc, "payload", None)
    message = ""
    if isinstance(details, dict):
        candidate = details.get("error") or details.get("message")
        if isinstance(candidate, str):
            message = candidate
    if not message:
        message = str(getattr(exc, "message", exc) or REQUEST_FAILED_VALUE)
    return _error_envelope(
        status_code=status_code,
        kind=kind,
        resource=resource,
        message=message,
        details=details,
    )


def _unexpected_error(exc: Exception, *, kind: str, resource: str) -> JSONResponse:
    return _error_envelope(
        status_code=500,
        kind=kind,
        resource=resource,
        message="Internal server error",
        details={"exception": str(exc)},
    )


def _payload_or_dump(payload: Any) -> Any:
    if hasattr(payload, "dump") and callable(payload.dump):
        return payload.dump()
    return payload


def _json_body(response: JSONResponse) -> Any:
    try:
        body = response.body
        if isinstance(body, bytes):
            return json.loads(body.decode("utf-8") or "{}")
        if isinstance(body, str):
            return json.loads(body or "{}")
    except Exception:
        return {}
    return {}


def create_api_contracts_v2_router(
    *,
    request_scope: Callable[..., Any],
    resolve_user_role: Callable[[Request], str],
    job_query_service: Callable[[], Any],
    job_command_service: Callable[[], Any],
    approval_query_service: Callable[[], Any] | None = None,
    project_query_service: Callable[[], Any],
    require_authenticated_user: Callable[..., Any],
    local_auth_enabled: Callable[[], bool],
    external_auth_enabled: bool,
    auth_admin_roles: set[str],
    err_admin_role_required: str,
) -> APIRouter:
    router = APIRouter(tags=["api-contracts-v2"])

    @router.get(
        "/api/v2/jobs",
        response_model=ApiV2JobsEnvelopeView,
        response_description="Versioned jobs list envelope for API v2 consumers.",
    )
    async def api_v2_jobs(
        request: Request,
        group_by: Annotated[str, Query()] = "",
        project_id: Annotated[str, Query()] = "",
        workspace_id: Annotated[str, Query()] = "",
        limit: Annotated[int, Query()] = 25,
        page: Annotated[int, Query()] = 1,
        per_page: Annotated[int, Query()] = 0,
        status: Annotated[list[str] | None, Query()] = None,
        workflow_id: Annotated[str, Query()] = "",
        task_source: Annotated[Literal["workflow_test_run"] | None, Query()] = None,
        search: Annotated[str, Query()] = "",
        hide_acknowledged: Annotated[bool, Query()] = False,
    ):
        try:
            payload = job_query_service().list_jobs(
                group_by=group_by,
                project_id=project_id,
                workspace_id=workspace_id,
                limit=limit,
                page=page,
                per_page=per_page,
                status=status or [],
                workflow_id=workflow_id,
                task_source=task_source,
                search=search,
                hide_acknowledged=hide_acknowledged,
                scope=request_scope(
                    request,
                    project_id=project_id or None,
                    workspace_id=workspace_id or None,
                ),
            )
            data = _payload_or_dump(payload)
        except ServiceError as exc:
            return _service_error(exc, kind="list", resource="jobs")
        except Exception as exc:
            return _unexpected_error(exc, kind="list", resource="jobs")
        return JSONResponse(_success_envelope(kind="list", resource="jobs", data=data))

    @router.get(
        "/api/v2/jobs/{job_id}",
        response_model=ApiV2JobDetailEnvelopeView,
        response_description="Versioned job-detail envelope for API v2 consumers.",
    )
    async def api_v2_job_detail(
        request: Request,
        job_id: str,
        compact: Annotated[bool, Query()] = False,
        step_limit: Annotated[int, Query(ge=0, le=100)] = 0,
    ):
        try:
            payload = await build_job_detail_contract(
                job_id=job_id,
                scope=request_scope(request),
                job_query_service=job_query_service(),
                approval_query_service=approval_query_service()
                if approval_query_service is not None
                else None,
                user_role=resolve_user_role(request),
                resolve_artifact=api_dependencies.resolve_artifact,
                execution_step_limit=step_limit if compact else None,
                include_workflow_run=not compact,
            )
            data = _payload_or_dump(payload)
        except ServiceError as exc:
            return _service_error(exc, kind="detail", resource="jobs")
        except Exception as exc:
            return _unexpected_error(exc, kind="detail", resource="jobs")
        return JSONResponse(_success_envelope(kind="detail", resource="jobs", data=data))

    @router.get(
        "/api/v2/jobs/{job_id}/execution-steps",
        response_description="One cursor page of durable execution-step history.",
    )
    async def api_v2_job_execution_steps(
        request: Request,
        job_id: str,
        cursor: Annotated[str, Query(max_length=256)] = "",
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
    ):
        try:
            data = await build_job_execution_steps_page(
                job_id=job_id,
                scope=request_scope(request),
                job_query_service=job_query_service(),
                limit=limit,
                cursor=cursor,
                resolve_artifact=api_dependencies.resolve_artifact,
            )
        except ServiceError as exc:
            return _service_error(exc, kind="list", resource="job-execution-steps")
        except Exception as exc:
            return _unexpected_error(exc, kind="list", resource="job-execution-steps")
        return JSONResponse(
            _success_envelope(kind="list", resource="job-execution-steps", data=data)
        )

    @router.get(
        "/api/v2/projects",
        response_model=ApiV2ProjectsEnvelopeView,
        response_description="Versioned projects list envelope for API v2 consumers.",
    )
    async def api_v2_projects(
        request: Request,
        page: Annotated[int, Query()] = 1,
        per_page: Annotated[int, Query()] = 25,
        search: Annotated[str, Query()] = "",
        workspace_id: Annotated[str, Query()] = "",
        has_jobs: Annotated[str, Query()] = "",
        sort: Annotated[str, Query()] = "",
        direction: Annotated[str, Query()] = "",
    ):
        try:
            payload = project_query_service().list_projects(
                page=page,
                per_page=per_page,
                search=search,
                has_jobs=has_jobs,
                sort=sort,
                direction=direction,
                scope=request_scope(request, workspace_id=workspace_id or None),
            )
            data = _payload_or_dump(payload)
        except ServiceError as exc:
            return _service_error(exc, kind="list", resource="projects")
        except Exception as exc:
            return _unexpected_error(exc, kind="list", resource="projects")
        return JSONResponse(_success_envelope(kind="list", resource="projects", data=data))

    @router.get(
        "/api/v2/projects/{project_id}/jobs",
        response_model=ApiV2ProjectJobsEnvelopeView,
        response_description="Versioned project-jobs list envelope for API v2 consumers.",
    )
    async def api_v2_project_jobs(
        request: Request,
        project_id: str,
        page: Annotated[int, Query()] = 1,
        per_page: Annotated[int, Query()] = 25,
        status: Annotated[str, Query()] = "",
    ):
        try:
            payload = job_query_service().list_project_jobs(
                project_id,
                page=page,
                per_page=per_page,
                status=status,
                scope=request_scope(request, project_id=project_id),
            )
            data = _payload_or_dump(payload)
        except ServiceError as exc:
            return _service_error(exc, kind="list", resource="project-jobs")
        except Exception as exc:
            return _unexpected_error(exc, kind="list", resource="project-jobs")
        return JSONResponse(_success_envelope(kind="list", resource="project-jobs", data=data))

    @router.post(
        "/api/v2/projects/{project_id}/jobs/bulk-delete",
        response_model=ApiV2ProjectJobsBulkDeleteEnvelopeView,
        response_description="Versioned bulk-delete result envelope for API v2 consumers.",
    )
    async def api_v2_project_jobs_bulk_delete(
        request: Request, project_id: str, req: ProjectJobsBulkDeleteRequest
    ):
        if local_auth_enabled() or external_auth_enabled:
            try:
                require_authenticated_user(request, required_roles=auth_admin_roles)
            except PermissionError:
                return _error_envelope(
                    status_code=403,
                    kind="mutation",
                    resource="project-jobs",
                    message=err_admin_role_required,
                    code=_error_code_for_status(403),
                )
        try:
            payload = job_command_service().bulk_delete_project_jobs(
                ProjectJobsBulkDeleteInput(
                    project_id=project_id,
                    ids=req.ids or [],
                    confirm=req.confirm or "",
                ),
                scope=request_scope(request, project_id=project_id),
            )
            data = {
                "status": payload.status,
                "requested": payload.requested,
                "deleted": payload.deleted,
                "deleted_count": payload.deleted_count,
                "skipped": payload.skipped,
            }
        except ServiceError as exc:
            return _service_error(exc, kind="mutation", resource="project-jobs")
        except Exception as exc:
            return _unexpected_error(exc, kind="mutation", resource="project-jobs")
        return JSONResponse(_success_envelope(kind="mutation", resource="project-jobs", data=data))

    @router.get(
        "/api/v2/dashboard/summary",
        response_model=ApiV2DashboardSummaryEnvelopeView,
        response_description="Versioned dashboard summary envelope for API v2 consumers.",
    )
    async def api_v2_dashboard_summary(
        request: Request,
        range: Annotated[str, Query()] = "7d",
    ):
        try:
            # api_dashboard_summary is usually synchronous, but tests and future
            # adapters may provide an async-compatible implementation.
            payload = dashboard_metrics_api.api_dashboard_summary(request=request, range=range)
            if inspect.isawaitable(payload):
                payload = await payload
        except ServiceError as exc:
            return _service_error(exc, kind="detail", resource="dashboard-summary")
        except Exception as exc:
            return _unexpected_error(exc, kind="detail", resource="dashboard-summary")

        if isinstance(payload, JSONResponse):
            status_code = int(payload.status_code or 500)
            body = _json_body(payload)
            if status_code >= 400:
                if isinstance(body, dict):
                    message = str(body.get("error") or body.get("message") or REQUEST_FAILED_VALUE)
                else:
                    message = REQUEST_FAILED_VALUE
                return _error_envelope(
                    status_code=status_code,
                    kind="detail",
                    resource="dashboard-summary",
                    message=message,
                    details=body,
                )
            payload = body

        return JSONResponse(
            _success_envelope(
                kind="detail",
                resource="dashboard-summary",
                data=payload,
            )
        )

    return router
