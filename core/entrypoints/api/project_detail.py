"""Project detail and task-submission API routes."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from core.application.management.errors import ServiceError
from core.application.management.requests import WorkflowPreviewRequest


def create_api_project_detail_router(
    *,
    project_query_service: Callable[[], Any],
    request_scope: Callable[..., Any],
    service_error_response: Callable[..., JSONResponse],
    api_error_response: Callable[..., JSONResponse],
    logger: Any,
) -> APIRouter:
    del logger  # Reserved for the stable router-factory dependency contract.
    router = APIRouter(tags=["api-project-detail"])
    safe_error_messages = {
        "project_load": "Project load failed.",
        "task_queue": "Task queueing failed.",
        "workflow_preview": "Workflow preview unavailable.",
    }

    def _safe_surface_message(
        action: str,
        *,
        status_code: int | None = None,
        service_message: str = "",
    ) -> str:
        normalized = str(service_message or "").strip()
        if status_code is not None and status_code < 500 and normalized:
            return normalized
        return safe_error_messages[action]

    def _project_api_error_response(
        request: Request,
        exc: Exception,
        *,
        action: str,
    ) -> JSONResponse:
        status_code = int(getattr(exc, "status_code", 500) or 500)
        if status_code < 500:
            return service_error_response(exc, request=request)
        return api_error_response(
            exc,
            request=request,
            status_code=status_code,
            fallback_message=_safe_surface_message(action),
        )

    @router.post("/api/projects/{project_id}/workflow-preview")
    async def api_project_workflow_preview(
        request: Request, project_id: str, req: WorkflowPreviewRequest
    ):
        """Preview selected workflow and planned phases for a task submission."""
        try:
            payload = project_query_service().get_workflow_preview(
                project_id,
                task_summary=req.task_summary,
                workflow_id=req.workflow_id or "",
                scope=request_scope(request, project_id=project_id),
            )
        except ServiceError as exc:
            return _project_api_error_response(request, exc, action="workflow_preview")
        except Exception as exc:
            return api_error_response(
                exc,
                request=request,
                fallback_message=_safe_surface_message("workflow_preview"),
            )
        return JSONResponse(payload.dump())

    return router
