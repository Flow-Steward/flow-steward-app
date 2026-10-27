"""Jobs and runtime API routes extracted from serve.py."""

from __future__ import annotations

from collections.abc import Callable
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Path, Query, Request
from fastapi.responses import JSONResponse

from core.application.auth.enforce import require_roles
from core.application.jobs.job_polling import build_job_polling_payload
from core.application.jobs.public_approval_serializer import TOKEN_APPROVAL_KEYS
from core.application.jobs.terminal_persistence import cancel_job_terminally
from core.application.jobs.workflow_wait_projection import DURABLE_WAIT_STATUSES
from core.application.management import ServiceError, ServiceUnavailableError
from core.application.management.command_models import (
    JobDecisionOverrideInput,
    JobRetryInterruptedStepInput,
    JobStepReplayExecutionInput,
)
from core.application.management.query_models import (
    ApprovalGateView,
    JobDetailContractView,
    JobsAttentionGroupsView,
    JobsListView,
)
from core.application.management.requests import (
    JobDecisionOverrideRequest,
    JobRetryInterruptedStepRequest,
    JobStepReplayRequest,
    RecoveryActionRequest,
)
from core.application.reports.job_detail_contract import build_job_detail_contract
from core.entrypoints.api import dependencies as api_dependencies
from core.entrypoints.api.job_detail_routes import (
    JobDetailRouteDependencies,
    register_job_detail_routes,
)

_MESSAGE_ADMIN_ROLE_REQUIRED = "Admin role required"


#: Statuses of a run that is still active: running, queued, or paused on a
#: durable wait (approval, timer, child). Retrying one needs an explicit force.
_RETRY_REQUIRES_FORCE_STATUSES = frozenset(
    {"pending", "resume_pending", "in_progress", *DURABLE_WAIT_STATUSES}
)


def _is_token_scope(scope: Any) -> bool:
    return (
        str(getattr(scope, "auth_source", "") or "").strip().lower() in {"machine", "pat", "token"}
        or str(getattr(scope, "principal_type", "") or "").strip().lower() == "service_account"
    )


def _token_approval_gate_response(gate: dict[str, Any]) -> dict[str, Any]:
    """Flatten the typed token gate and omit the browser-only payload wrapper."""
    serialized = dict(gate)
    payload = serialized.pop("payload", {})
    if not isinstance(payload, dict):
        return serialized
    for key in TOKEN_APPROVAL_KEYS:
        if key in {"approval_id", "allowed_actions", "expires_at"} and key in serialized:
            continue
        if key in payload:
            serialized[key] = payload[key]
    return serialized


def create_api_jobs_router(
    *,
    job_query_service: Callable[[], Any],
    job_command_service: Callable[[], Any],
    approval_query_service: Callable[[], Any],
    request_scope: Callable[..., Any],
    resolve_user_role: Callable[[Request], str],
    resolve_actor_username: Callable[..., str],
    optional_actor_username: Callable[..., str],
    service_error_response: Callable[..., JSONResponse],
    api_error_response: Callable[..., JSONResponse],
    get_logger_only: Callable[[], Any],
    watchdog_scan: Callable[..., dict],
    parse_worker_pid: Callable[[str | None], int | None],
    terminate_worker_pid: Callable[..., dict],
    append_runtime_journal: Callable[..., Any],
    build_rerun_workflow_selection: Callable[..., tuple[dict, dict]],
    load_replay_snapshot: Callable[[dict], dict[str, Any]],
    enqueue_task: Callable[..., int],
    retryable_interrupted_step_status: str,
    err_database_not_configured: str,
) -> APIRouter:
    router = APIRouter(
        tags=["api-jobs"],
        responses={
            403: {"description": "Job operation is not permitted"},
            405: {"description": "Job operation is not supported"},
        },
    )

    async def _filter_watchdog_payload_for_scope(
        payload: dict[str, Any],
        *,
        scope: Any,
    ) -> dict[str, Any]:
        visible: dict[str, Any] = dict(payload)
        query = job_query_service()
        for key, count_key in (
            ("detections", "detection_count"),
            ("applied", "applied_count"),
            ("recoveries", "recovery_count"),
            ("recoveries_applied", "recovery_applied_count"),
        ):
            rows = payload.get(key)
            if not isinstance(rows, list):
                visible[key] = []
                visible[count_key] = 0
                continue
            if not rows:
                visible[key] = []
                visible[count_key] = int(payload.get(count_key) or 0)
                continue
            scoped_rows: list[dict[str, Any]] = []
            for row in rows:
                if not isinstance(row, dict):
                    continue
                candidate_job_id = str(row.get("job_id") or "").strip()
                if not candidate_job_id:
                    continue
                try:
                    await query.get_job_status(candidate_job_id, scope=scope)
                except ServiceUnavailableError:
                    scoped_rows.append(dict(row))
                    continue
                except Exception:
                    continue
                scoped_rows.append(dict(row))
            visible[key] = scoped_rows
            visible[count_key] = len(scoped_rows)
        return visible

    @router.get(
        "/api/jobs",
        response_model=JobsListView | JobsAttentionGroupsView,
        operation_id="listJobs",
        summary="List jobs.",
        description=(
            "Return a paginated list of jobs for a project or integration. "
            "Use this endpoint to monitor execution progress after submitting work."
        ),
        response_description="Paginated list of jobs that match the requested filters.",
        responses={
            200: {
                "description": "Paginated list of jobs that match the requested filters.",
                "content": {
                    "application/json": {
                        "example": {
                            "jobs": [
                                {
                                    "job_id": "job-987",
                                    "project_id": "proj_42",
                                    "project_name": "Payments API",
                                    "task_id": "task-123",
                                    "task_summary": "Generate a release-readiness report for version 2.4.",
                                    "display_summary": "Release-readiness report for version 2.4",
                                    "status": "in_progress",
                                    "selected_workflow": "release-report",
                                    "current_step_id": "draft_report",
                                    "current_group_id": None,
                                    "current_agent": "planner",
                                    "queued_at": "2026-05-27T09:12:00Z",
                                    "started_at": "2026-05-27T09:12:08Z",
                                    "finished_at": None,
                                }
                            ],
                            "total": 1,
                            "page": 1,
                            "per_page": 25,
                            "total_pages": 1,
                            "project_id": "proj_42",
                            "workspace_id": None,
                            "status": "in_progress",
                        }
                    }
                },
            }
        },
    )
    async def api_jobs(
        request: Request,
        group_by: Annotated[
            str, Query(description="Internal grouping mode for operator surfaces.")
        ] = "",
        project_id: Annotated[
            str,
            Query(description="Project identifier used to scope the job list."),
        ] = "",
        workspace_id: Annotated[
            str,
            Query(description="Optional internal workspace filter."),
        ] = "",
        limit: Annotated[
            int,
            Query(description="Maximum number of jobs to return."),
        ] = 25,
        page: Annotated[
            int,
            Query(description="Page number to return."),
        ] = 1,
        per_page: Annotated[
            int,
            Query(description="Internal page-size override for operator surfaces."),
        ] = 0,
        status: Annotated[
            list[str] | None,
            Query(
                description=(
                    "Status filter. Repeat it (`status=failed&status=stuck`) or pass a comma "
                    "list (`failed,stuck`) to list jobs in any of those statuses; "
                    "`acknowledged` selects acknowledged failures."
                )
            ),
        ] = None,
        workflow_id: Annotated[
            str,
            Query(description="Optional exact workflow identifier filter."),
        ] = "",
        search: Annotated[
            str,
            Query(description="Optional text search across job, workflow, and project fields."),
        ] = "",
        hide_acknowledged: Annotated[
            bool,
            Query(description="Leave out runs an operator has acknowledged."),
        ] = False,
    ):
        """List jobs, or group them for the attention-board surfaces."""
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
                search=search,
                hide_acknowledged=hide_acknowledged,
                scope=request_scope(
                    request,
                    project_id=project_id or None,
                    workspace_id=workspace_id or None,
                ),
            )
        except ServiceError as e:
            return service_error_response(e)
        except Exception as e:
            return api_error_response(e)
        return payload.dump()

    @router.get("/api/jobs/awaiting-approval")
    def api_jobs_awaiting_approval(request: Request):
        """Return all jobs currently awaiting user approval across all projects.

        Declared `def`, not `async def`: `list_awaiting_approval_jobs` is
        synchronous, and a blocking call in an async handler runs on the event
        loop and stalls the whole application (RD-002).
        """
        try:
            payload = job_query_service().list_awaiting_approval_jobs(
                scope=request_scope(request),
            )
        except Exception as exc:
            # Never answer an empty list on failure. The approvals drawer renders
            # an empty result as "No approvals are waiting", so swallowing the
            # error told operators there was nothing to decide while a run sat
            # blocked on a gate (AUDIT-038 symptom d). An error the UI can
            # distinguish is the whole point.
            return api_error_response(
                exc,
                request=request,
                fallback_message="Could not load jobs awaiting approval.",
            )
        return JSONResponse([row.dump() for row in payload])

    @router.get(
        "/api/watchdog/jobs/scan",
        responses={405: {"description": "Use POST to apply watchdog actions"}},
    )
    async def api_watchdog_scan(request: Request, apply: bool = False):
        """Scan active jobs for stuck/orphaned conditions; optionally apply marks."""
        if apply:
            raise HTTPException(status_code=405, detail="Use POST to apply watchdog actions")
        bl = get_logger_only()
        if not bl:
            return JSONResponse({"error": err_database_not_configured}, status_code=503)
        try:
            payload = watchdog_scan(bl, apply=bool(apply))
            scope = request_scope(request)
            return JSONResponse(await _filter_watchdog_payload_for_scope(payload, scope=scope))
        except Exception as e:
            return api_error_response(e)

    @router.post(
        "/api/watchdog/jobs/scan",
        responses={403: {"description": "Administrator access required"}},
    )
    async def api_watchdog_scan_apply(request: Request):
        """Apply watchdog marks for stuck/orphaned jobs."""
        auth_user = getattr(request.state, "auth_user", None)
        if not isinstance(auth_user, dict):
            raise HTTPException(status_code=403, detail=_MESSAGE_ADMIN_ROLE_REQUIRED)
        try:
            require_roles(auth_user, {"admin"})
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=_MESSAGE_ADMIN_ROLE_REQUIRED) from exc
        bl = get_logger_only()
        if not bl:
            return JSONResponse({"error": err_database_not_configured}, status_code=503)
        try:
            payload = watchdog_scan(bl, apply=True)
            scope = request_scope(request)
            return JSONResponse(await _filter_watchdog_payload_for_scope(payload, scope=scope))
        except Exception as e:
            return api_error_response(e)

    @router.post(
        "/api/watchdog/jobs/{job_id}/recover",
        responses={403: {"description": "Administrator access required"}},
    )
    async def api_watchdog_recover(job_id: str, req: RecoveryActionRequest, request: Request):
        """Execute deterministic recovery action for stuck/orphaned jobs."""
        # Destructive watchdog recovery (kill/retry) is admin-only, mirroring the
        # adjacent POST /api/watchdog/jobs/scan apply route. Operator/write roles
        # alone must not be able to terminate workers or re-enqueue jobs.
        auth_user = getattr(request.state, "auth_user", None)
        if not isinstance(auth_user, dict):
            raise HTTPException(status_code=403, detail=_MESSAGE_ADMIN_ROLE_REQUIRED)
        try:
            require_roles(auth_user, {"admin"})
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=_MESSAGE_ADMIN_ROLE_REQUIRED) from exc
        scope = request_scope(request)
        bl = get_logger_only()
        try:
            await job_query_service().get_job_status(job_id, scope=scope)
        except ServiceUnavailableError:
            pass
        except ServiceError as e:
            return service_error_response(e)
        except Exception as e:
            return api_error_response(e)
        if not bl:
            return JSONResponse({"error": err_database_not_configured}, status_code=503)
        job = bl.get_job(job_id)
        if not job:
            return JSONResponse({"error": "Job not found"}, status_code=404)

        action = str(req.action or "").strip().lower()
        reason = str(req.reason or "").strip() or "watchdog recovery action"
        actor_username = optional_actor_username(request)
        status = str(job.get("status") or "").strip().lower()
        terminal = {
            "completed",
            "failed",
            "cancelled",
            "max_iterations",
            retryable_interrupted_step_status,
        }

        if action == "kill":
            kill_result = {"attempted": False}
            worker_pid = parse_worker_pid(job.get("worker_id"))
            try:
                if worker_pid:
                    kill_result = terminate_worker_pid(worker_pid)
            except Exception as kill_err:
                kill_result = {"attempted": True, "error": str(kill_err), "pid": worker_pid}

            if status not in terminal:
                cancel_job_terminally(
                    bl,
                    job_id,
                    reason=f"Killed by watchdog action: {reason}"[:2000],
                    error_code="watchdog_killed",
                )

            append_runtime_journal(
                bl,
                job_id,
                event="watchdog_kill",
                reason=reason,
                detail={
                    "previous_status": status,
                    "worker_id": job.get("worker_id"),
                    "worker_pid": worker_pid,
                    "kill_result": kill_result,
                    "requested_by": actor_username,
                },
            )
            return JSONResponse(
                {
                    "status": "ok",
                    "action": "kill",
                    "job_id": job_id,
                    "kill_result": kill_result,
                }
            )

        if action != "retry":
            return JSONResponse({"error": f"Unsupported action '{action}'"}, status_code=400)

        if status in _RETRY_REQUIRES_FORCE_STATUSES and not req.force:
            return JSONResponse(
                {
                    "error": (
                        "Active job must be killed/cancelled before retry. Set force=true to override."
                    )
                },
                status_code=409,
            )

        project_id = job.get("project_id")
        if not project_id:
            return JSONResponse({"error": "Job has no project_id"}, status_code=400)
        run_cfg = job_command_service().load_project_run_config(str(project_id))
        project_name = run_cfg.get("project_name", "")
        try:
            replay_snapshot = load_replay_snapshot(job)
            rerun_extra_state, rerun_meta = build_rerun_workflow_selection(
                job,
                replay_snapshot=replay_snapshot,
            )
        except Exception as exc:
            from core.application.jobs.job_invocation_artifacts import JobReplaySnapshotError
            from core.application.workflows.serve_workflow_helpers import (
                WorkflowRerunSecretInputRequired,
            )

            if isinstance(exc, JobReplaySnapshotError):
                return JSONResponse(
                    {"error": exc.code, "code": exc.code},
                    status_code=400,
                )
            if isinstance(exc, WorkflowRerunSecretInputRequired):
                return JSONResponse({"error": str(exc)}, status_code=409)
            raise
        if status not in terminal:
            cancel_job_terminally(
                bl,
                job_id,
                reason=f"Retried by watchdog action: {reason}"[:2000],
                error_code="watchdog_retried",
            )
        enqueue_kwargs: dict[str, Any] = {
            "task_id": job["task_id"],
            "task_summary": job["task_summary"],
            "task_source": job.get("task_source", "manual"),
            "project_id": project_id,
            "project_name": project_name,
            "project_config": run_cfg,
            "mode": job.get("mode"),
            "extra_state": rerun_extra_state or None,
            "meta": rerun_meta or None,
        }
        enqueue_kwargs["test_mode"] = bool(replay_snapshot.get("test_mode"))
        enqueue_kwargs["replay_snapshot"] = replay_snapshot
        new_job_id = enqueue_task(**enqueue_kwargs)
        append_runtime_journal(
            bl,
            job_id,
            event="watchdog_retry",
            reason=reason,
            detail={
                "previous_status": status,
                "new_job_id": new_job_id,
                "force": bool(req.force),
                "requested_by": actor_username,
            },
        )
        return JSONResponse(
            {
                "status": "ok",
                "action": "retry",
                "job_id": job_id,
                "new_job_id": str(new_job_id),
            }
        )

    @router.get("/api/jobs/{job_id}/runtime-journal")
    async def api_job_runtime_journal(request: Request, job_id: str):
        """Return runtime recovery journal entries stored in jobs.meta.runtime_journal."""
        try:
            payload = job_query_service().get_runtime_journal(
                job_id,
                scope=request_scope(request),
            )
        except ServiceError as e:
            return service_error_response(e)
        except Exception as e:
            return api_error_response(e)
        return payload.dump()

    @router.get("/api/jobs/{job_id}/decision-overrides")
    async def api_job_decision_overrides(
        request: Request,
        job_id: str,
        active_only: bool = False,
        domain: str = "",
        limit: int = 200,
    ):
        """List decision overrides for a job (newest first)."""
        try:
            payload = job_query_service().list_decision_overrides(
                job_id,
                active_only=bool(active_only),
                domain=domain,
                limit=limit,
                scope=request_scope(request),
            )
        except ServiceError as e:
            return service_error_response(e)
        except Exception as e:
            return api_error_response(e)
        return payload.dump()

    @router.post("/api/jobs/{job_id}/decision-overrides")
    async def api_create_job_decision_override(
        job_id: str, body: JobDecisionOverrideRequest, request: Request
    ):
        """Create an auditable operator override for a job decision domain."""
        actor_username = resolve_actor_username(request, body.created_by, default="operator")
        try:
            payload = job_command_service().create_decision_override(
                job_id,
                JobDecisionOverrideInput(
                    domain=body.domain,
                    override_value=body.override_value,
                    original_value=body.original_value,
                    reason=body.reason,
                    created_by=actor_username,
                    active=bool(body.active),
                    message_id=body.message_id,
                    workflow_id=body.workflow_id,
                    step_id=body.step_id,
                    step_execution_id=body.step_execution_id,
                ),
                scope=request_scope(request, workflow_id=body.workflow_id or None),
            )
        except ServiceError as e:
            return service_error_response(e)
        except Exception as e:
            return api_error_response(e)
        return {"job_id": payload.job_id, "override": payload.override}

    @router.get("/api/jobs/{job_id}/errors")
    async def api_job_errors(request: Request, job_id: str, limit: int = 200):
        """Return structured runtime errors for a job (newest first)."""
        try:
            payload = job_query_service().list_errors(
                job_id,
                limit=limit,
                scope=request_scope(request),
            )
        except ServiceError as e:
            return service_error_response(e)
        except Exception as e:
            return api_error_response(e)
        return payload.dump()

    @router.get("/api/jobs/{job_id}/definition")
    async def api_job_definition(request: Request, job_id: str):
        """Return the workflow definition the job pinned (its snapshot), not the live one."""
        try:
            payload = job_query_service().get_job_definition(
                job_id,
                scope=request_scope(request),
            )
        except ServiceError as e:
            return service_error_response(e)
        except Exception as e:
            return api_error_response(e)
        return payload.dump()

    @router.get("/api/jobs/{job_id}/causal-chain")
    async def api_job_causal_chain(request: Request, job_id: str):
        """Return message lineage + Mermaid graph for runtime causal-chain visualization."""
        try:
            payload = job_query_service().get_causal_chain(
                job_id,
                scope=request_scope(request),
            )
        except ServiceError as e:
            return service_error_response(e)
        except Exception as e:
            return api_error_response(e)
        return payload.dump()

    @router.get(
        "/api/jobs/{job_id}",
        response_model=JobDetailContractView,
        operation_id="getJob",
        summary="Get a job.",
        description=(
            "Return the current top-level state for one job, including status, current step, "
            "approval information, and compact runtime context. The nested workflow_run field "
            "is the stable integration projection; other fields are product diagnostics."
        ),
        response_description="Current top-level job state and supporting execution context.",
        responses={
            200: {
                "description": "Current top-level job state and supporting execution context.",
                "content": {
                    "application/json": {
                        "example": {
                            "id": "job-987",
                            "project_id": "proj_42",
                            "thread_id": "thread-7",
                            "task_summary": "Generate a release-readiness report for version 2.4.",
                            "status": "awaiting_approval",
                            "current_step_id": "human-review",
                            "current_group_id": None,
                            "current_agent": "reviewer",
                            "status_label": "Awaiting approval",
                            "status_tone": "amber",
                            "approval_state": "pending",
                            "attention_group": "awaiting_approval",
                            "runtime": {
                                "started_at": "2026-05-27T09:12:08Z",
                                "duration_ms": 184220,
                            },
                            "job": {
                                "selected_workflow": "release-report",
                                "mode": "standard",
                            },
                            "sections": {"summary": {"title": "Summary", "items": []}},
                            "projection": {
                                "next_action": "Approve or reject the generated report."
                            },
                            "approval_gate": {"required": True, "thread_id": "thread-7"},
                            "approval_history": [],
                            "causal_chain": {},
                            "cost_breakdown_raw": {"total_cost_usd": 0.14},
                            "decision_overrides": [],
                            "incident": {},
                            "job_errors": [],
                            "runtime_journal": [],
                            "section_warnings": [],
                            "thread_state": {},
                            "lifecycle": {"queued_at": "2026-05-27T09:12:00Z"},
                            "workflow_run": {
                                "schema_version": "1",
                                "job_id": "job-987",
                                "project_id": "proj_42",
                                "workflow_id": "release-report",
                                "status": "awaiting_approval",
                                "terminal": False,
                                "active_steps": [
                                    {
                                        "step_id": "human-review",
                                        "step_execution_id": "6f1c2a1e-3b9d-4c55-9a0e-2f7f3e8d1b42",
                                        "group_id": None,
                                        "occurrence": 1,
                                        "display_name": "Human review",
                                        "step_kind": "approval",
                                        "status": "awaiting_approval",
                                        "started_at": "2026-05-27T09:15:00Z",
                                        "finished_at": None,
                                        "child_job_id": None,
                                    }
                                ],
                                "steps": [],
                                "approval": {
                                    "approval_id": "interrupt-7",
                                    "job_id": "job-987",
                                    "thread_id": "thread-7",
                                    "step_id": "human-review",
                                    "step_execution_id": "6f1c2a1e-3b9d-4c55-9a0e-2f7f3e8d1b42",
                                    "group_id": None,
                                    "message": "Approve the release report.",
                                    "content": {"summary": "Release report is ready."},
                                    "allowed_actions": ["approve", "reject"],
                                    "created_at": "2026-05-27T09:15:00Z",
                                    "expires_at": None,
                                },
                                "result": None,
                                "error": None,
                                "queued_at": "2026-05-27T09:12:00Z",
                                "started_at": "2026-05-27T09:12:08Z",
                                "updated_at": "2026-05-27T09:15:00Z",
                                "finished_at": None,
                            },
                        }
                    }
                },
            }
        },
    )
    async def api_job_status(
        request: Request,
        job_id: Annotated[
            str, Path(description="Job identifier returned when the task was submitted.")
        ],
    ):
        """Return current job status, summary, and cost metrics as JSON."""
        try:
            payload = await build_job_detail_contract(
                job_id=job_id,
                scope=request_scope(request),
                job_query_service=job_query_service(),
                approval_query_service=approval_query_service(),
                user_role=resolve_user_role(request),
                resolve_artifact=api_dependencies.resolve_artifact,
            )
        except ServiceError as e:
            return service_error_response(e)
        except Exception as e:
            return api_error_response(e)
        if isinstance(payload, dict):
            return payload
        return payload.dump()

    @router.get(
        "/api/jobs/{job_id}/status",
        responses={
            200: {
                "description": "Bounded job progress, active steps, wait state, and cost totals.",
                "content": {
                    "application/json": {
                        "example": {
                            "id": "job-987",
                            "status": "awaiting_timer",
                            "project_id": "proj_42",
                            "thread_id": "thread-7",
                            "task_summary": "Generate a release-readiness report for version 2.4.",
                            "selected_workflow": "release-report",
                            "started_at": "2026-05-27T09:12:08Z",
                            "finished_at": None,
                            "duration_ms": None,
                            "current_step_id": "wait-for-release",
                            "current_group_id": None,
                            "current_agent": "wait_step",
                            "truncated_fields": [],
                            "active_steps": [
                                {
                                    "step_id": "wait-for-release",
                                    "step_execution_id": "6f1c2a1e-3b9d-4c55-9a0e-2f7f3e8d1b42",
                                    "attempt": 1,
                                    "group_id": None,
                                    "lane_id": None,
                                    "iteration": None,
                                    "status": "awaiting_timer",
                                }
                            ],
                            "active_steps_total": 1,
                            "active_steps_truncated": False,
                            "workflow_wait": {
                                "kind": "timer",
                                "wait_id": "wait-7",
                                "step_id": "wait-for-release",
                                "group_id": None,
                                "due_at": "2026-05-27T09:15:00Z",
                                "child_job_id": None,
                                "approval_id": None,
                            },
                            "cost": {
                                "tokens_prompt": 0,
                                "tokens_completion": 0,
                                "tokens_total": 0,
                                "cost_usd": 0.0,
                                "known_cost_usd": 0.0,
                                "unknown_cli_tokens": 0,
                                "cost_knownness": "known",
                                "llm_calls": 0,
                            },
                        }
                    }
                },
            }
        },
    )
    async def api_job_polling_status(request: Request, job_id: str) -> Any:
        """Return bounded progress for polling, with the same job scope checks as detail."""
        try:
            status = await job_query_service().get_job_status(job_id, scope=request_scope(request))
            return build_job_polling_payload(status)
        except ServiceError as exc:
            return service_error_response(exc)
        except Exception as exc:
            return api_error_response(exc)

    register_job_detail_routes(
        router,
        JobDetailRouteDependencies(
            api_error_response=api_error_response,
            err_database_not_configured=err_database_not_configured,
            get_logger_only=get_logger_only,
            job_command_service=job_command_service,
            job_query_service=job_query_service,
            request_scope=request_scope,
            service_error_response=service_error_response,
        ),
    )

    @router.get("/api/jobs/{job_id}/replay-step")
    async def api_job_replay_step(
        request: Request, job_id: str, message_id: str = "", correlation_id: str = ""
    ):
        """Return causal replay trace for one step message (routing + prompt/response)."""
        try:
            payload = job_query_service().get_replay_step(
                job_id=job_id,
                message_id=message_id,
                correlation_id=correlation_id,
                scope=request_scope(request),
            )
        except ServiceError as e:
            return service_error_response(e)
        except Exception as e:
            return api_error_response(e)
        return JSONResponse(payload.dump())

    @router.get("/api/jobs/{job_id}/step-replays")
    async def api_job_step_replays(
        request: Request,
        job_id: str,
        limit: int = 25,
        message_id: str = "",
        correlation_id: str = "",
    ):
        try:
            payload = job_query_service().list_step_replays(
                job_id,
                message_id=message_id,
                correlation_id=correlation_id,
                limit=limit,
                scope=request_scope(request),
            )
        except ServiceError as e:
            return service_error_response(e)
        except Exception as e:
            return api_error_response(e)
        return payload.dump()

    @router.post("/api/jobs/{job_id}/replay-step/execute")
    async def api_job_replay_step_execute(
        job_id: str, body: JobStepReplayRequest, request: Request
    ):
        """Re-execute one step from captured prompt inputs and persist replay result."""
        requested_by = resolve_actor_username(request, body.requested_by, default="operator")
        try:
            payload = job_command_service().execute_step_replay(
                job_id,
                JobStepReplayExecutionInput(
                    message_id=str(body.message_id or "").strip(),
                    correlation_id=str(body.correlation_id or "").strip(),
                    requested_by=requested_by,
                    replay_mode=body.replay_mode,
                ),
                scope=request_scope(request),
            )
        except ServiceError as e:
            return service_error_response(e)
        except Exception as e:
            return api_error_response(e)
        return JSONResponse(payload.dump())

    @router.post("/api/jobs/{job_id}/retry-step")
    async def api_job_retry_interrupted_step(
        job_id: str,
        body: JobRetryInterruptedStepRequest,
        request: Request,
    ):
        """Re-enqueue the interrupted job so the same step reruns from checkpoint."""
        requested_by = resolve_actor_username(request, body.requested_by, default="operator")
        try:
            payload = job_command_service().retry_interrupted_step(
                job_id,
                JobRetryInterruptedStepInput(requested_by=requested_by),
                scope=request_scope(request),
            )
        except ServiceError as e:
            return service_error_response(e)
        except Exception as e:
            return api_error_response(e)
        return JSONResponse(
            {
                "job_id": payload.job_id,
                "status": payload.status,
                "enqueued": payload.enqueued,
                "thread_id": payload.thread_id,
                "queue_name": payload.queue_name,
            }
        )

    @router.get("/api/jobs/{job_id}/logs")
    async def api_job_logs(
        request: Request,
        job_id: str,
        since_id: str = "",
        before_id: str = "",
        newest: bool = False,
        limit: int = 25,
    ):
        """Return job logs with incremental and pagination filters."""
        try:
            payload = job_query_service().list_logs(
                job_id,
                since_id=since_id,
                before_id=before_id,
                newest=newest,
                limit=limit,
                scope=request_scope(request),
            )
        except ServiceError as e:
            return service_error_response(e)
        except Exception as e:
            return api_error_response(e)
        return payload.dump()

    @router.get(
        "/api/jobs/{job_id}/approval",
        response_model=ApprovalGateView,
        operation_id="getJobApproval",
    )
    async def api_job_approval(request: Request, job_id: str):
        """Return the current interrupt payload for a job awaiting approval."""
        try:
            scope = request_scope(request)
            payload = approval_query_service().get_current_gate(
                job_id,
                scope=scope,
            )
        except ServiceError as e:
            return service_error_response(e)
        except Exception as e:
            return api_error_response(e)
        if _is_token_scope(scope):
            # Returning a Response bypasses the browser response model, whose
            # default ``payload`` field would otherwise reintroduce it.
            return JSONResponse(_token_approval_gate_response(payload.dump()))
        return payload.dump()

    @router.get("/api/jobs/{job_id}/approvals")
    async def api_job_approvals(request: Request, job_id: str):
        """Return approval history for a job."""
        try:
            payload = approval_query_service().list_history(
                job_id,
                scope=request_scope(request),
            )
        except ServiceError as e:
            return service_error_response(e)
        except Exception as e:
            return api_error_response(e)
        return payload.dump()

    return router
