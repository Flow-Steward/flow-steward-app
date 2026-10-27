"""Job-control API routes: workflow launches, approvals, and job actions."""

from __future__ import annotations

from collections.abc import Callable
from typing import Annotated, Any

from fastapi import APIRouter, Path, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from core.application.management import ServiceError
from core.application.management.command_models import (
    ApprovalResumeAcceptedView,
    JobAcknowledgeInput,
    WorkflowRunAcceptedView,
    WorkflowRunInput,
)
from core.application.management.query_models import JobCancelAcceptedView
from core.entrypoints.api.approval_decision_transport import approval_decision_from_request
from core.entrypoints.api.idempotency_headers import validate_idempotency_key


def create_jobs_control_router(
    *,
    project_command_service: Callable[[], Any],
    approval_command_service: Callable[[], Any],
    job_command_service: Callable[[], Any],
    request_scope: Callable[..., Any],
    service_error_response: Callable[..., JSONResponse],
    api_error_response: Callable[..., JSONResponse],
    logger: Any,
) -> APIRouter:
    router = APIRouter(tags=["jobs-control"])
    from core.entrypoints.api.workflows_builder_run import create_builder_workflow_run_router

    router.include_router(
        create_builder_workflow_run_router(
            project_command_service=project_command_service,
            request_scope=request_scope,
            service_error_response=service_error_response,
            api_error_response=api_error_response,
        )
    )

    @router.post(
        "/api/workflow-runs",
        operation_id="startWorkflowRun",
        response_model=WorkflowRunAcceptedView,
        response_description="Accepted asynchronous workflow launch with its durable job identity.",
    )
    async def start_workflow_run(request: Request, command: WorkflowRunInput):
        """Start a concrete workflow using its MCP-channel input contract.

        Workflow/project/account access, readiness and declared inputs are checked before enqueueing.
        An optional Idempotency-Key header returns the same job for an identical retry.
        """
        try:
            key = validate_idempotency_key(request.headers.get("Idempotency-Key"))
            result = await run_in_threadpool(
                project_command_service().start_mcp_workflow_run,
                command,
                idempotency_key=key,
                scope=request_scope(
                    request,
                    project_id=command.project_id,
                    workflow_id=command.workflow_id,
                    default_actor="system",
                ),
            )
            return result.dump()
        except ServiceError as exc:
            response = service_error_response(exc)
            if str((exc.payload or {}).get("code") or "") == "idempotency_in_progress":
                response.headers["Retry-After"] = str((exc.payload or {}).get("retry_after") or 2)
            return response
        except Exception as exc:
            logger.exception("Failed to start workflow run")
            return api_error_response(exc)

    @router.post(
        "/api/approvals/{thread_id}",
        response_model=ApprovalResumeAcceptedView,
        status_code=202,
        operation_id="resumeApproval",
        summary="Submit an approval decision.",
        description=(
            "Resume a paused workflow thread after a human approval or rejection decision. "
            "Use this endpoint when a job is waiting on an approval gate. A 202 response means "
            "the decision and recoverable resume intent are durable; workflow execution is asynchronous."
        ),
        response_description="Accepted approval decision with the resumed thread status.",
        responses={
            202: {
                "description": "Accepted approval decision with the resumed thread status.",
                "content": {
                    "application/json": {
                        "example": {
                            "job_id": "42",
                            "thread_id": "thread-7",
                            "approval_id": "interrupt-7",
                            "deduplicated": False,
                            "approved": True,
                            "action": "resume",
                            "comment": "Approved after confirming the rollout window.",
                            "override_content": "",
                            "actor": "api-client",
                            "status": "in_progress",
                        }
                    }
                },
            },
            400: {"description": "Decision body or token contract validation failed."},
            409: {
                "description": ("Approval gate is stale/resolved or the idempotency key conflicts.")
            },
            410: {"description": "The idempotency replay result has expired."},
        },
        openapi_extra={
            "parameters": [
                {
                    "name": "Idempotency-Key",
                    "in": "header",
                    "required": False,
                    "schema": {"type": "string", "maxLength": 255},
                    "description": (
                        "Required for PAT, service-account, and machine-token decisions; "
                        "scope it to this approval gate."
                    ),
                }
            ],
            "requestBody": {
                "required": True,
                "content": {
                    "application/json": {
                        "schema": {
                            "type": "object",
                            "additionalProperties": False,
                            "required": ["approval_id"],
                            "anyOf": [
                                {"required": ["approved"]},
                                {"required": ["action"]},
                            ],
                            "properties": {
                                "approved": {"type": "boolean"},
                                "action": {"type": "string", "maxLength": 255},
                                "comment": {"type": "string", "maxLength": 10000},
                                "approval_id": {"type": "string", "maxLength": 255},
                                "override_content": {"type": "string"},
                                "external_actor": {
                                    "type": "object",
                                    "required": ["id"],
                                    "additionalProperties": False,
                                    "properties": {
                                        "id": {"type": "string", "maxLength": 255},
                                        "display_name": {"type": "string", "maxLength": 200},
                                    },
                                },
                            },
                        },
                        "description": (
                            "Machine/PAT callers must send an explicit approved or action value, "
                            "the current approval_id in JSON, and Idempotency-Key in the request "
                            "header."
                        ),
                    }
                },
            },
        },
    )
    async def approve_task(
        request: Request,
        thread_id: Annotated[
            str, Path(description="Workflow thread waiting on an approval decision.")
        ],
    ):
        """Resume a paused graph after human approval or rejection."""
        try:
            decision = await approval_decision_from_request(request)
            payload = await run_in_threadpool(
                approval_command_service().resume_task,
                thread_id=thread_id,
                scope=request_scope(request),
                decision=decision,
                idempotency_key=validate_idempotency_key(request.headers.get("Idempotency-Key")),
            )
        except ServiceError as e:
            return service_error_response(e)
        except Exception as e:
            return api_error_response(e)
        return JSONResponse(payload.dump(), status_code=202)

    @router.post("/api/jobs/{job_id}/acknowledge")
    def job_acknowledge(job_id: str, request: Request):
        """Mark a failed job as acknowledged (triaged) without changing its status."""
        username = "system"
        if hasattr(request.state, "user") and request.state.user:
            username = getattr(request.state.user, "username", None) or "system"
        try:
            payload = job_command_service().acknowledge_job(
                job_id,
                JobAcknowledgeInput(actor=username),
                scope=request_scope(request),
            )
        except ServiceError as e:
            return service_error_response(e)
        except Exception as e:
            return api_error_response(e)
        return {"status": payload.status, "job_id": payload.job_id}

    @router.post("/api/jobs/{job_id}/unacknowledge")
    def job_unacknowledge(job_id: str, request: Request):
        """Remove acknowledgment from a job, returning it to needs-attention."""
        try:
            payload = job_command_service().unacknowledge_job(
                job_id,
                scope=request_scope(request),
            )
        except ServiceError as e:
            return service_error_response(e)
        except Exception as e:
            return api_error_response(e)
        return {"status": payload.status, "job_id": payload.job_id}

    @router.post(
        "/api/jobs/{job_id}/cancel",
        response_model=JobCancelAcceptedView,
        operation_id="cancelJob",
        summary="Cancel a job.",
        description=(
            "Request cancellation for a pending or in-progress job. "
            "The API path always returns JSON so integrations can handle cancellation consistently."
        ),
        response_description="Cancellation request accepted for the specified job.",
        responses={
            200: {
                "description": "Cancellation request accepted for the specified job.",
                "content": {
                    "application/json": {"example": {"job_id": "job-987", "cancelled": True}}
                },
            }
        },
    )
    def job_cancel(
        request: Request,
        job_id: Annotated[str, Path(description="Job to cancel.")],
    ):
        """Cancel a pending, resume-pending, in-progress, or awaiting_approval job."""
        try:
            username = "operator"
            if hasattr(request.state, "user") and request.state.user:
                username = getattr(request.state.user, "username", None) or "operator"
            job_command_service().cancel_job(
                job_id,
                actor=username,
                comment="Cancelled by user",
                scope=request_scope(request),
            )
            return JSONResponse({"cancelled": True, "job_id": job_id})
        except ServiceError as e:
            return service_error_response(e)
        except Exception as e:
            return api_error_response(e)

    @router.post("/api/jobs/{job_id}/restart")
    def job_restart(job_id: str, request: Request):
        """Re-enqueue a completed/failed/cancelled job with the same task parameters."""
        try:
            payload = job_command_service().restart_job(
                job_id,
                scope=request_scope(request),
            )
            logger.info("Job %s restarted as new job %s", job_id, payload.new_job_id)
            return JSONResponse(
                {"job_id": job_id, "new_job_id": payload.new_job_id}, status_code=200
            )
        except ServiceError as e:
            status_code = int(getattr(e, "status_code", 500) or 500)
            if status_code in {400, 404, 503}:
                return JSONResponse({"error": str(e)}, status_code=status_code)
            return service_error_response(e)
        except Exception as e:
            return api_error_response(e)

    return router
