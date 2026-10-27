"""Browser-only ordinary Builder launch through shared durable admission."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from starlette.concurrency import run_in_threadpool

from core.application.jobs.workflow_definition_snapshot_repository import (
    WorkflowDefinitionPinError,
    WorkflowRevisionChangedError,
)
from core.application.management import ServiceError
from core.application.management.command_models import WorkflowRunAcceptedView
from core.application.management.command_models_api_inputs import BuilderWorkflowRunInput
from core.entrypoints.api.idempotency_headers import validate_idempotency_key


def create_builder_workflow_run_router(
    *,
    project_command_service: Callable[[], Any],
    request_scope: Callable[..., Any],
    service_error_response: Callable[..., Any],
    api_error_response: Callable[..., Any],
) -> APIRouter:
    router = APIRouter(tags=["workflows-definition"])

    @router.post(
        "/api/workflows/{workflow_id}/run",
        response_model=WorkflowRunAcceptedView,
        operation_id="startBuilderWorkflowRun",
    )
    async def run_workflow(request: Request, workflow_id: str, command: BuilderWorkflowRunInput):
        # Middleware retains editor admin authorization and session CSRF. Tokens
        # must use the MCP route, never acquire UI-channel authority here.
        user = getattr(request.state, "auth_user", None)
        source = str(getattr(request.state, "auth_source", "") or "")
        if (
            not isinstance(user, dict)
            or source not in {"local", "external"}
            or user.get("principal_type") != "admin"
            or "admin" not in user.get("roles", [])
            or request.headers.get("authorization")
        ):
            raise HTTPException(
                status_code=403, detail="An administrator browser session is required"
            )
        try:
            result = await run_in_threadpool(
                project_command_service().start_builder_workflow_run,
                workflow_id,
                command,
                idempotency_key=validate_idempotency_key(request.headers.get("Idempotency-Key")),
                scope=request_scope(
                    request,
                    project_id=command.project_id,
                    workflow_id=workflow_id,
                    default_actor="system",
                ),
            )
            return result.dump()
        except WorkflowRevisionChangedError as exc:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "workflow_revision_conflict",
                    "message": str(exc),
                },
            ) from exc
        except WorkflowDefinitionPinError as exc:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "workflow_not_runnable",
                    "message": str(exc),
                    "issues": list(exc.issues),
                },
            ) from exc
        except ServiceError as exc:
            response = service_error_response(exc)
            if str((exc.payload or {}).get("code") or "") == "idempotency_in_progress":
                response.headers["Retry-After"] = str((exc.payload or {}).get("retry_after") or 2)
            return response
        except Exception as exc:
            return api_error_response(exc)

    return router
