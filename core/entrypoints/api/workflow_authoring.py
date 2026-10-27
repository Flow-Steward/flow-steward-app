"""Thin project-bound HTTP adapters for workflow authoring."""

from __future__ import annotations

from collections.abc import Callable
from typing import Annotated, Any

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse

from core.application.management.errors import ServiceError
from core.application.workflows.authoring.catalog import (
    CapabilityDescriptionResult,
    CapabilitySearchResult,
    WorkflowExamplesResult,
)
from core.application.workflows.authoring.drafts import DraftSaveResult, DraftValidationResult
from core.application.workflows.authoring.schema import AuthoringSchemaResult
from core.application.workflows.authoring.service import WorkflowAuthoringService
from core.application.workflows.workflow_authoring_requests import WorkflowDraftInput


def create_workflow_authoring_router(
    *,
    workflow_authoring_service: Callable[[Request], WorkflowAuthoringService],
    request_scope: Callable[..., Any],
    service_error_response: Callable[..., JSONResponse],
    api_error_response: Callable[..., JSONResponse],
) -> APIRouter:
    """Parse requests, dispatch the shared facade and map current service errors."""
    router = APIRouter(tags=["workflow-authoring"])

    def dispatch(request: Request, operation: str, **arguments: Any) -> Any:
        try:
            service = workflow_authoring_service(request)
            return getattr(service, operation)(**arguments)
        except ServiceError as exc:
            return service_error_response(exc, request=request)
        except Exception as exc:
            return api_error_response(exc, request=request)

    @router.get(
        "/api/projects/{project_id}/workflow-authoring/capabilities",
        response_model=CapabilitySearchResult,
    )
    def capabilities(
        request: Request,
        project_id: str,
        query: str,
        kinds: Annotated[list[str] | None, Query()] = None,
        limit: Annotated[int, Query(ge=1, le=50)] = 12,
    ) -> Any:
        return dispatch(
            request,
            "capability_search",
            scope=request_scope(request, project_id=project_id),
            project_id=project_id,
            query=query,
            kinds=kinds,
            limit=limit,
        )

    @router.get(
        "/api/projects/{project_id}/workflow-authoring/capability",
        response_model=CapabilityDescriptionResult,
    )
    def capability(request: Request, project_id: str, ref: str, operation_id: str = "") -> Any:
        return dispatch(
            request,
            "capability_describe",
            scope=request_scope(request, project_id=project_id),
            project_id=project_id,
            ref=ref,
            operation_id=operation_id,
        )

    @router.get(
        "/api/projects/{project_id}/workflow-authoring/examples",
        response_model=WorkflowExamplesResult,
    )
    def examples(
        request: Request, project_id: str, query: str, limit: Annotated[int, Query(ge=1, le=50)] = 5
    ) -> Any:
        return dispatch(
            request,
            "workflow_examples_search",
            scope=request_scope(request, project_id=project_id),
            project_id=project_id,
            query=query,
            limit=limit,
        )

    @router.get("/api/workflow-authoring/schema", response_model=AuthoringSchemaResult)
    def schema(request: Request, step_kind: str | None = None) -> Any:
        return dispatch(request, "workflow_authoring_schema", step_kind=step_kind)

    @router.post(
        "/api/projects/{project_id}/workflow-authoring/validate",
        response_model=DraftValidationResult,
    )
    def validate(request: Request, project_id: str, payload: WorkflowDraftInput) -> Any:
        return dispatch(
            request,
            "workflow_draft_validate",
            scope=request_scope(request, project_id=project_id),
            project_id=project_id,
            bundle_yaml=payload.bundle_yaml,
            resolutions=payload.resolutions,
        )

    @router.post(
        "/api/projects/{project_id}/workflow-authoring/drafts", response_model=DraftSaveResult
    )
    def drafts(request: Request, project_id: str, payload: WorkflowDraftInput) -> Any:
        return dispatch(
            request,
            "workflow_draft_save",
            scope=request_scope(request, project_id=project_id),
            project_id=project_id,
            bundle_yaml=payload.bundle_yaml,
            resolutions=payload.resolutions,
        )

    return router
