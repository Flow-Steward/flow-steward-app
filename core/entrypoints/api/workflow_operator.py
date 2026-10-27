"""Operator workflow catalog and contract routes."""

from __future__ import annotations

from collections.abc import Callable
from typing import Annotated, Any

from fastapi import APIRouter, Path, Query, Request
from fastapi.responses import JSONResponse

from core.application.management import ServiceError
from core.application.management.guards import require_workflow_access
from core.application.management.query_models import (
    ProjectWorkflowEntrypointsView,
    PublicProjectWorkflowCatalogView,
    WorkflowContractResultView,
)
from core.entrypoints.api.workflow_operator_hooks import register_workflow_hook_routes
from core.entrypoints.api.workflow_operator_portability import (
    register_workflow_portability_routes,
)


def create_workflow_operator_router(
    *,
    workflow_operator_service: Callable[..., Any],
    request_scope: Callable[..., Any],
    service_error_response: Callable[..., JSONResponse],
    api_error_response: Callable[..., JSONResponse],
) -> APIRouter:
    router = APIRouter(tags=["workflow-operator"])

    @router.get(
        "/api/projects/{project_id}/workflow-catalog",
        response_model=PublicProjectWorkflowCatalogView,
        operation_id="listProjectWorkflowCatalog",
        summary="List project workflows.",
        description=(
            "Return the workflows that an external integration can launch for a project. "
            "Use this endpoint to discover available workflow options before submitting work."
        ),
        response_description="Integration-facing workflow catalog for the requested project.",
        responses={
            200: {
                "description": "Integration-facing workflow catalog for the requested project.",
                "content": {
                    "application/json": {
                        "example": {
                            "project_id": "proj_42",
                            "workflows": [
                                {
                                    "workflow_id": "release-report",
                                    "display_name": "Release Report",
                                    "description": "Produces a release-readiness report for a requested version.",
                                    "enabled": True,
                                    "availability_status": "available",
                                    "new_runs_blocked": False,
                                }
                            ],
                        }
                    }
                },
            }
        },
    )
    async def api_project_workflow_catalog(
        request: Request,
        project_id: Annotated[str, Path(description="Provisioned project identifier.")],
        enabled_only: Annotated[
            bool,
            Query(
                description="When true, return only workflows that are currently enabled for new runs."
            ),
        ] = True,
        include_validation: Annotated[bool, Query()] = False,
    ):
        try:
            payload = workflow_operator_service().list_project_workflows(
                project_id,
                enabled_only=enabled_only,
                include_validation=include_validation,
                scope=request_scope(request, project_id=project_id),
            )
        except ServiceError as exc:
            return service_error_response(exc)
        except Exception as exc:
            return api_error_response(exc)
        return payload.dump()

    @router.get(
        "/api/projects/{project_id}/workflow-entrypoints",
        response_model=ProjectWorkflowEntrypointsView,
        operation_id="listProjectWorkflowEntrypoints",
        summary="List workflow entrypoints.",
        description=(
            "Return callable entrypoints for workflows in a project, including the methods and "
            "input keys that an external integration can use."
        ),
        response_description="Callable workflow entrypoints available for the requested project.",
        responses={
            200: {
                "description": "Callable workflow entrypoints available for the requested project.",
                "content": {
                    "application/json": {
                        "example": {
                            "project_id": "proj_42",
                            "entrypoints": [
                                {
                                    "entry_key": "release-report-webhook",
                                    "workflow_id": "release-report",
                                    "enabled": True,
                                    "allowed_methods": ["POST"],
                                    "idempotency_mode": "client_key",
                                    "response_mode": "accepted",
                                    "requires_secret": True,
                                    "input_mapping_keys": ["release_version", "requested_by"],
                                }
                            ],
                        }
                    }
                },
            }
        },
    )
    async def api_project_workflow_entrypoints(
        request: Request,
        project_id: Annotated[str, Path(description="Provisioned project identifier.")],
        enabled_only: Annotated[
            bool,
            Query(description="When true, return only entrypoints that are currently enabled."),
        ] = True,
    ):
        try:
            payload = workflow_operator_service().list_project_entrypoints(
                project_id,
                enabled_only=enabled_only,
                scope=request_scope(request, project_id=project_id),
            )
        except ServiceError as exc:
            return service_error_response(exc)
        except Exception as exc:
            return api_error_response(exc)
        return payload.dump()

    @router.get("/api/projects/{project_id}/workflows/{workflow_id}/validate")
    async def api_project_workflow_validate(request: Request, project_id: str, workflow_id: str):
        try:
            payload = workflow_operator_service().project_workflow_validate(
                project_id,
                workflow_id,
                scope=request_scope(request, project_id=project_id),
            )
        except ServiceError as exc:
            return service_error_response(exc)
        except Exception as exc:
            return api_error_response(exc)
        return payload.dump()

    @router.get(
        "/api/projects/{project_id}/workflows/{workflow_id}/contract",
        response_model=WorkflowContractResultView,
        operation_id="getProjectWorkflowContract",
        summary="Get a workflow contract.",
        description=(
            "Return the machine-readable input and output contract for one workflow. "
            "Use this endpoint to inspect required input fields before calling the workflow."
        ),
        response_description="Machine-readable workflow contract for the requested workflow.",
        responses={
            200: {
                "description": "Machine-readable workflow contract for the requested workflow.",
                "content": {
                    "application/json": {
                        "example": {
                            "workflow_id": "release-report",
                            "display_name": "Release Report",
                            "enabled": True,
                            "interface": {
                                "input": {
                                    "required": ["release_version"],
                                    "properties": {
                                        "release_version": {
                                            "type": "string",
                                            "description": "Version to evaluate.",
                                        }
                                    },
                                },
                                "output": {
                                    "properties": {
                                        "report_url": {"type": "string", "format": "uri"},
                                        "status": {"type": "string"},
                                    }
                                },
                            },
                        }
                    }
                },
            }
        },
    )
    async def api_project_workflow_contract(
        request: Request,
        project_id: Annotated[str, Path(description="Provisioned project identifier.")],
        workflow_id: Annotated[
            str, Path(description="Workflow identifier returned by the catalog endpoint.")
        ],
    ):
        try:
            payload = workflow_operator_service().project_workflow_contract(
                project_id,
                workflow_id,
                scope=request_scope(request, project_id=project_id),
            )
        except ServiceError as exc:
            return service_error_response(exc)
        except Exception as exc:
            return api_error_response(exc)
        return payload.dump()

    @router.get("/api/projects/{project_id}/workflows/{workflow_id}/suggestions")
    async def api_project_workflow_suggestions(request: Request, project_id: str, workflow_id: str):
        try:
            payload = workflow_operator_service().project_workflow_suggestions(
                project_id,
                workflow_id,
                scope=request_scope(request, project_id=project_id),
            )
        except ServiceError as exc:
            return service_error_response(exc)
        except Exception as exc:
            return api_error_response(exc)
        return payload.dump()

    @router.get("/api/workflows/{workflow_id}/validate")
    async def api_workflow_validate(request: Request, workflow_id: str):
        try:
            scope = request_scope(request, workflow_id=workflow_id)
            require_workflow_access(scope, workflow_id)
            return workflow_operator_service().workflow_validate(workflow_id).dump()
        except ServiceError as exc:
            return service_error_response(exc)
        except Exception as exc:
            return api_error_response(exc)

    # GET /api/workflows/{workflow_id}/contract is owned by the admin authoring
    # route. A second registration here was never reachable — Starlette
    # dispatches the first match, and the admin router is included first — so it
    # was a handler that existed, was tested, and could not be called. Run-facing
    # contract lookup is the project-scoped route above, which stays gated.

    @router.get("/api/workflows/{workflow_id}/suggestions")
    async def api_workflow_suggestions(request: Request, workflow_id: str):
        try:
            scope = request_scope(request, workflow_id=workflow_id)
            require_workflow_access(scope, workflow_id)
            return workflow_operator_service().workflow_suggestions(workflow_id).dump()
        except ServiceError as exc:
            return service_error_response(exc)
        except Exception as exc:
            return api_error_response(exc)

    for register in (register_workflow_hook_routes, register_workflow_portability_routes):
        register(
            router,
            workflow_operator_service=workflow_operator_service,
            request_scope=request_scope,
            service_error_response=service_error_response,
            api_error_response=api_error_response,
        )
    return router
