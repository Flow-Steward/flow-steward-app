"""Thin HTTP adapters of the workflow export, import and stats operator commands.

Every route validates with the shared request models and calls the one
``WorkflowOperatorService`` method the CLI and MCP use, with the arguments the
CLI binding passes. The bundle travels as JSON text (``bundle_yaml``), so an
API client never has to carry a YAML response.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Annotated, Any

from fastapi import APIRouter, Path, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from core.application.management import ServiceError
from core.application.management.command_specs.workflow_authoring import (
    WorkflowExportCommandInput,
    WorkflowExportResult,
    WorkflowImportResult,
    WorkflowStatsResult,
)
from core.application.management.command_specs.workflow_shared import WorkflowTargetCommandInput
from core.application.workflows.workflow_authoring_requests import WorkflowImportRequest

_ProjectId = Annotated[str, Path(description="Provisioned project identifier.")]
_WorkflowId = Annotated[str, Path(description="Identifier of the workflow linked to the project.")]
_PROJECT_EXAMPLE = "df0d2afe-7064-4206-941b-61d2d4db425c"
_BUNDLE_EXAMPLE = "schema_version: 1\nworkflows:\n  - workflow_id: invoice-flow\n"


class WorkflowImportBody(BaseModel):
    """A YAML bundle, as ``workflow_export`` returns it, and the import options."""

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "example": {
                "bundle_yaml": _BUNDLE_EXAMPLE,
                "target_workflow_id": "invoice-flow-copy",
                "resolutions": [{"dependency_key": "agent:writer", "action": "map_existing"}],
            }
        },
    )

    bundle_yaml: str = Field(min_length=1)
    target_workflow_id: str = ""
    resolutions: list[dict[str, Any]] = Field(default_factory=list)


def _json(example: dict[str, Any], description: str) -> dict[int | str, dict[str, Any]]:
    return {
        200: {"description": description, "content": {"application/json": {"example": example}}}
    }


def register_workflow_portability_routes(
    router: APIRouter,
    *,
    workflow_operator_service: Callable[..., Any],
    request_scope: Callable[..., Any],
    service_error_response: Callable[..., JSONResponse],
    api_error_response: Callable[..., JSONResponse],
) -> None:
    def _invalid(exc: ValidationError) -> JSONResponse:
        errors = [
            {"loc": list(error.get("loc", ())), "msg": str(error.get("msg", ""))}
            for error in exc.errors()
        ]
        return service_error_response(
            ServiceError(
                "Invalid workflow request",
                status_code=422,
                payload={"message": "Invalid workflow request", "errors": errors},
            )
        )

    def _call(request: Request, project_id: str, method: str, **arguments: Any):
        try:
            scope = request_scope(request, project_id=project_id)
            # The request goes with the service: authoring checks its own caller.
            return getattr(workflow_operator_service(request), method)(
                project_id=project_id, scope=scope, **arguments
            )
        except ServiceError as exc:
            return service_error_response(exc)
        except ValidationError as exc:
            return _invalid(exc)
        except Exception as exc:
            return api_error_response(exc)

    @router.get(
        "/api/projects/{project_id}/workflows/{workflow_id}/export",
        response_model=WorkflowExportResult,
        operation_id="exportProjectWorkflow",
        summary="Export a workflow as a YAML bundle.",
        description=(
            "The portable YAML bundle of a workflow linked to the project, as JSON text; "
            "include_children adds the workflows it invokes."
        ),
        responses=_json(
            {
                "workflow_id": "invoice-flow",
                "filename": "invoice-flow.workflow.yaml",
                "include_children": False,
                "bundle_yaml": _BUNDLE_EXAMPLE,
            },
            "The workflow's YAML bundle.",
        ),
    )
    async def api_workflow_export(
        request: Request,
        project_id: _ProjectId,
        workflow_id: _WorkflowId,
        include_children: Annotated[
            bool, Query(description="Also export the child workflows it invokes.")
        ] = False,
        reference_only: Annotated[
            bool,
            Query(
                description=(
                    "Name the agents, skills, data contracts and guardrails without "
                    "their definitions."
                )
            ),
        ] = False,
    ):
        try:
            command = WorkflowExportCommandInput(
                project_id=project_id,
                workflow_id=workflow_id,
                include_children=include_children,
                reference_only=reference_only,
            )
        except ValidationError as exc:
            return _invalid(exc)
        return _call(
            request,
            command.project_id,
            "export_workflow",
            workflow_id=command.workflow_id,
            include_children=command.include_children,
            reference_only=command.reference_only,
        )

    @router.get(
        "/api/projects/{project_id}/workflows/{workflow_id}/stats",
        response_model=WorkflowStatsResult,
        operation_id="getProjectWorkflowStats",
        summary="Get the run statistics of a workflow.",
        description="Run counts and timings of a workflow linked to the project.",
        responses=_json(
            {"workflow_id": "invoice-flow", "total_runs": 12, "completed": 11, "failed": 1},
            "The workflow's run statistics.",
        ),
    )
    async def api_workflow_stats(
        request: Request, project_id: _ProjectId, workflow_id: _WorkflowId
    ):
        try:
            command = WorkflowTargetCommandInput(project_id=project_id, workflow_id=workflow_id)
        except ValidationError as exc:
            return _invalid(exc)
        return _call(request, command.project_id, "workflow_stats", workflow_id=command.workflow_id)

    @router.post(
        "/api/projects/{project_id}/workflow-imports",
        response_model=WorkflowImportResult,
        operation_id="importProjectWorkflow",
        summary="Import a workflow bundle into a project.",
        description=(
            "Apply a YAML bundle as disabled drafts linked to the project, with the chosen "
            "dependency resolutions; target_workflow_id imports it under another id."
        ),
        responses=_json(
            {"imported": ["invoice-flow-copy"], "workflow_id": "invoice-flow-copy"},
            "The imported workflows.",
        ),
    )
    async def api_workflow_import(
        request: Request, project_id: _ProjectId, body: WorkflowImportBody
    ):
        try:
            command = WorkflowImportRequest(
                bundle_yaml=body.bundle_yaml,
                target_workflow_id=body.target_workflow_id or None,
                project_id=project_id,
                resolutions=body.resolutions,
            )
        except ValidationError as exc:
            return _invalid(exc)
        return _call(
            request,
            project_id,
            "import_workflow",
            bundle_yaml=command.bundle_yaml or "",
            target_workflow_id=command.target_workflow_id,
            resolutions=command.resolutions,
        )


__all__ = ["WorkflowImportBody", "register_workflow_portability_routes"]
