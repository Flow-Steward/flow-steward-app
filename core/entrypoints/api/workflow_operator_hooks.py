"""Thin HTTP adapters of the hook and where-used operator commands.

Every route validates with the shared command DTOs and calls the one
``WorkflowOperatorService`` method the CLI and MCP use.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Annotated, Any

from fastapi import APIRouter, Path, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from core.application.management import ServiceError
from core.application.management.command_specs.workflows import (
    WorkflowHookBindingCommandInput,
    WorkflowHookBindingResult,
    WorkflowHookCreateCommandInput,
    WorkflowHookPreviewCommandInput,
    WorkflowHookPreviewResult,
    WorkflowHookResult,
    WorkflowHooksResult,
    WorkflowHookUnbindCommandInput,
    WorkflowWhereUsedResult,
)

_ProjectId = Annotated[str, Path(description="Provisioned project identifier.")]
_WorkflowId = Annotated[str, Path(description="Identifier of the workflow linked to the project.")]
_HookId = Annotated[str, Path(description="Stable id of a hook the workflow declares.")]

_HOOK_EXAMPLE = {
    "project_id": "df0d2afe-7064-4206-941b-61d2d4db425c",
    "workflow_id": "invoice-flow",
    "hook_id": "before_invoice",
    "step_id": "before",
    "display_name": "Before creating an invoice",
    "contract_version": 1,
    "binding_revision": 1,
    "binding": {"target_workflow_id": "enrich_invoice", "input_mapping": {}, "output_mapping": {}},
}
_BINDING_EXAMPLE = {
    "project_id": "df0d2afe-7064-4206-941b-61d2d4db425c",
    "workflow_id": "invoice-flow",
    "hook_id": "before_invoice",
    "binding_revision": 2,
    "revision_id": "0d5a3c1e-7b2f-4e8a-9c6d-1f2e3a4b5c6d",
    "target_workflow_id": "enrich_invoice",
}


class HookBindingBody(BaseModel):
    """The binding a request checks or saves; project, workflow and hook are in the path."""

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "example": {
                "expected_revision": 0,
                "target_workflow_id": "enrich_invoice",
                "input_mapping": {"invoice_id": "invoice_id"},
                "output_mapping": {"company_name": "company"},
            }
        },
    )

    expected_revision: int = Field(ge=0)
    target_workflow_id: str
    input_mapping: dict[str, str] = Field(default_factory=dict)
    output_mapping: dict[str, str] = Field(default_factory=dict)
    hook_contract_version: int | None = None
    hook_semantic_digest: str = ""
    release_id: str = ""


class HookPreviewBody(HookBindingBody):
    """A binding to check and, optionally, hook inputs with a child result to run through it."""

    sample: dict[str, Any] | None = Field(
        default=None,
        description=(
            '{"inputs": {...}, "child_outputs": {...}}: also return the hook output '
            "a run would publish for them."
        ),
    )


class HookUnbindBody(BaseModel):
    model_config = ConfigDict(
        extra="forbid", json_schema_extra={"example": {"expected_revision": 2}}
    )

    expected_revision: int = Field(gt=0)


class HookCreateBody(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "example": {
                "expected_revision": 0,
                "child_workflow_id": "invoice_actions",
                "display_name": "Invoice actions",
            }
        },
    )

    expected_revision: int = Field(ge=0)
    child_workflow_id: str
    display_name: str = ""


def _json(example: dict[str, Any], description: str) -> dict[int | str, dict[str, Any]]:
    return {
        200: {"description": description, "content": {"application/json": {"example": example}}}
    }


def register_workflow_hook_routes(
    router: APIRouter,
    *,
    workflow_operator_service: Callable[[], Any],
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
                "Invalid hook request",
                status_code=422,
                payload={"message": "Invalid hook request", "errors": errors},
            )
        )

    def _call(request: Request, project_id: str, method: str, **arguments: Any):
        try:
            scope = request_scope(request, project_id=project_id)
            return getattr(workflow_operator_service(), method)(
                project_id=project_id, scope=scope, **arguments
            )
        except ServiceError as exc:
            return service_error_response(exc)
        except ValidationError as exc:
            return _invalid(exc)
        except Exception as exc:
            return api_error_response(exc)

    @router.get(
        "/api/projects/{project_id}/workflows/{workflow_id}/hooks",
        response_model=WorkflowHooksResult,
        operation_id="listProjectWorkflowHooks",
        summary="List the hooks of a workflow.",
        description="Every hook the workflow declares, with its binding in the project.",
        responses=_json(
            {
                "project_id": _HOOK_EXAMPLE["project_id"],
                "workflow_id": "invoice-flow",
                "hooks": [_HOOK_EXAMPLE],
            },
            "Hooks with their project bindings.",
        ),
    )
    async def api_workflow_hooks(
        request: Request, project_id: _ProjectId, workflow_id: _WorkflowId
    ):
        return _call(request, project_id, "hooks_list", workflow_id=workflow_id)

    @router.get(
        "/api/projects/{project_id}/workflows/{workflow_id}/hooks/{hook_id}",
        response_model=WorkflowHookResult,
        operation_id="getProjectWorkflowHook",
        summary="Get one hook and its binding.",
        description="The hook's contract and its binding in the project.",
        responses=_json(_HOOK_EXAMPLE, "The hook and its project binding."),
    )
    async def api_workflow_hook(
        request: Request, project_id: _ProjectId, workflow_id: _WorkflowId, hook_id: _HookId
    ):
        return _call(request, project_id, "hook_get", workflow_id=workflow_id, hook_id=hook_id)

    def _binding(project_id: str, workflow_id: str, hook_id: str, body: BaseModel, model: Any):
        return model.model_validate(
            {
                **body.model_dump(),
                "project_id": project_id,
                "workflow_id": workflow_id,
                "hook_id": hook_id,
            }
        ).model_dump(exclude={"project_id"})

    @router.post(
        "/api/projects/{project_id}/workflows/{workflow_id}/hooks/{hook_id}/preview",
        response_model=WorkflowHookPreviewResult,
        operation_id="previewProjectWorkflowHookBinding",
        summary="Check a hook binding without saving it.",
        description="Run every binding check and report the issues; nothing is written.",
        responses=_json({"valid": True, "issues": [], "binding_revision": 0}, "Check result."),
    )
    async def api_workflow_hook_preview(
        request: Request,
        project_id: _ProjectId,
        workflow_id: _WorkflowId,
        hook_id: _HookId,
        body: HookPreviewBody,
    ):
        try:
            arguments = _binding(
                project_id, workflow_id, hook_id, body, WorkflowHookPreviewCommandInput
            )
        except ValidationError as exc:
            return _invalid(exc)
        return _call(request, project_id, "hook_preview", **arguments)

    @router.post(
        "/api/projects/{project_id}/workflows/{workflow_id}/hooks/{hook_id}/bind",
        response_model=WorkflowHookBindingResult,
        operation_id="bindProjectWorkflowHook",
        summary="Connect a workflow to a hook.",
        description="Save a checked binding as the next revision; new runs of the project use it.",
        responses=_json(_BINDING_EXAMPLE, "The saved binding revision."),
    )
    async def api_workflow_hook_bind(
        request: Request,
        project_id: _ProjectId,
        workflow_id: _WorkflowId,
        hook_id: _HookId,
        body: HookBindingBody,
    ):
        try:
            arguments = _binding(
                project_id, workflow_id, hook_id, body, WorkflowHookBindingCommandInput
            )
        except ValidationError as exc:
            return _invalid(exc)
        return _call(request, project_id, "hook_bind", **arguments)

    @router.post(
        "/api/projects/{project_id}/workflows/{workflow_id}/hooks/{hook_id}/unbind",
        response_model=WorkflowHookBindingResult,
        operation_id="unbindProjectWorkflowHook",
        summary="Disconnect the workflow of a hook.",
        description="Clear the binding; new runs of the project pass the hook inputs through.",
        responses=_json({**_BINDING_EXAMPLE, "target_workflow_id": None}, "The cleared binding."),
    )
    async def api_workflow_hook_unbind(
        request: Request,
        project_id: _ProjectId,
        workflow_id: _WorkflowId,
        hook_id: _HookId,
        body: HookUnbindBody,
    ):
        try:
            arguments = _binding(
                project_id, workflow_id, hook_id, body, WorkflowHookUnbindCommandInput
            )
        except ValidationError as exc:
            return _invalid(exc)
        return _call(request, project_id, "hook_unbind", **arguments)

    @router.post(
        "/api/projects/{project_id}/workflows/{workflow_id}/hooks/{hook_id}/create-and-bind",
        response_model=WorkflowHookBindingResult,
        operation_id="createAndBindProjectWorkflowHook",
        summary="Create a workflow for a hook and connect it.",
        description="Create the empty workflow that holds a hook's steps, then bind it.",
        responses=_json(
            {**_BINDING_EXAMPLE, "target_workflow_id": "invoice_actions"},
            "The new workflow's binding revision.",
        ),
    )
    async def api_workflow_hook_create_and_bind(
        request: Request,
        project_id: _ProjectId,
        workflow_id: _WorkflowId,
        hook_id: _HookId,
        body: HookCreateBody,
    ):
        try:
            arguments = _binding(
                project_id, workflow_id, hook_id, body, WorkflowHookCreateCommandInput
            )
        except ValidationError as exc:
            return _invalid(exc)
        return _call(request, project_id, "hook_create_and_bind", **arguments)

    @router.get(
        "/api/projects/{project_id}/workflows/{workflow_id}/where-used",
        response_model=WorkflowWhereUsedResult,
        operation_id="getProjectWorkflowWhereUsed",
        summary="Show where a workflow is used.",
        description="The account's Invoke steps and hook bindings, with names where the caller can read them.",
        responses=_json(
            {
                "project_id": _HOOK_EXAMPLE["project_id"],
                "workflow_id": "enrich_invoice",
                "workflow_display_name": "Enrich invoice",
                "consumers": [
                    {
                        "kind": "hook",
                        "caller_workflow_id": "invoice-flow",
                        "caller_display_name": "Prepare the invoice",
                        "hook_id": "before_invoice",
                        "project_id": _HOOK_EXAMPLE["project_id"],
                    }
                ],
            },
            "Consumers of the workflow.",
        ),
    )
    async def api_workflow_where_used(
        request: Request,
        project_id: _ProjectId,
        workflow_id: _WorkflowId,
        locale: Annotated[str, Query(max_length=32)] = "",
    ):
        return _call(request, project_id, "where_used", workflow_id=workflow_id, locale=locale)


__all__ = ["register_workflow_hook_routes"]
