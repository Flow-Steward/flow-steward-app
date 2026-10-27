"""Project schedule timezone, cron preview, and scheduled job routes extracted from serve.py."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import suppress
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from core.application.management import ServiceError, ServiceForbiddenError
from core.application.management.command_models import (
    ProjectScheduledJobCreateInput,
    ProjectScheduledJobToggleInput,
    ProjectScheduledJobUpdateInput,
)
from core.application.management.guards import require_project_access


class ApiScheduledJobCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workflow_id: str
    cron_expr: str
    schedule_timezone: str = ""
    inputs: dict[str, Any] = Field(default_factory=dict)
    enabled: bool = False


class ApiScheduledJobUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workflow_id: str
    cron_expr: str
    schedule_timezone: str | None = None
    inputs: dict[str, Any] | None = None
    enabled: bool | None = None


class ApiScheduledJobToggleRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool | None = None


def create_project_schedules_router(
    *,
    local_auth_enabled: Callable[[], bool],
    external_auth_enabled: Callable[[], bool],
    require_authenticated_user: Callable[..., dict],
    get_logger_only: Callable[[], Any],
    request_scope: Callable[..., Any],
    project_command_service: Callable[[], Any],
    project_query_service: Callable[[], Any],
    resolve_actor_username: Callable[..., str],
    service_error_response: Callable[..., JSONResponse],
    api_error_response: Callable[..., JSONResponse],
    cron_next_run_at: Callable[..., datetime],
    normalize_cron_expr: Callable[[str], str],
    parse_cron_expression: Callable[[str], dict],
    cron_human_summary: Callable[[str, str], str],
    err_authentication_required: str,
    err_invalid_json_body: str,
    err_database_not_configured: str,
    err_project_not_found: str,
) -> APIRouter:
    router = APIRouter(tags=["project-schedules"])

    def _payload_dict(
        payload: Any,
        *,
        fallback: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if hasattr(payload, "dump"):
            with suppress(Exception):
                dumped = payload.dump()
                if isinstance(dumped, dict):
                    return dumped
        if isinstance(payload, dict):
            return dict(payload)
        if payload is None:
            return dict(fallback or {})
        if hasattr(payload, "__dict__"):
            return {
                key: value
                for key, value in vars(payload).items()
                if not key.startswith("_") and not callable(value)
            } or dict(fallback or {})
        return dict(fallback or {})

    @router.patch("/api/projects/{project_id}/schedule-timezone")
    async def api_project_update_schedule_timezone(request: Request, project_id: str):
        """Update a project's schedule timezone and recalculate all active cron next_run_at."""
        from core.application.settings.timezone_service import validate_timezone as _validate_tz

        if local_auth_enabled() or external_auth_enabled():
            try:
                require_authenticated_user(request)
            except PermissionError:
                return JSONResponse({"error": err_authentication_required}, status_code=401)

        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": err_invalid_json_body}, status_code=400)
        raw_tz = body.get("schedule_timezone") if isinstance(body, dict) else None

        try:
            clean_tz = _validate_tz(raw_tz)
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=422)

        bl = get_logger_only()
        if not bl:
            return JSONResponse({"error": err_database_not_configured}, status_code=503)

        scope = request_scope(request, project_id=project_id)
        project = bl.get_project(project_id) if hasattr(bl, "get_project") else None
        if not project:
            return JSONResponse({"error": err_project_not_found}, status_code=404)
        try:
            require_project_access(scope, project)
        except ServiceForbiddenError as exc:
            return service_error_response(exc)

        scope_account_id = ""
        if isinstance(scope, dict):
            scope_account_id = str(scope.get("account_id") or "").strip()
        else:
            scope_account_id = str(getattr(scope, "account_id", "") or "").strip()

        if hasattr(bl, "list_scheduled_jobs"):
            try:
                all_jobs = (
                    bl.list_scheduled_jobs(
                        project_id,
                        enabled_only=False,
                        account_id=scope_account_id or None,
                    )
                    or []
                )
            except TypeError:
                all_jobs = bl.list_scheduled_jobs(project_id, enabled_only=False) or []
        else:
            all_jobs = []

        recalculated_updates: list[tuple[str, datetime]] = []
        failed_jobs: list[dict[str, Any]] = []
        for job in all_jobs:
            sj = job if isinstance(job, dict) else {}
            if not sj.get("enabled") or str(sj.get("status") or "") != "active":
                continue
            cron_expr = str(sj.get("cron_expr") or "").strip()
            if not cron_expr:
                continue
            sj_id = str(sj.get("id") or "").strip()
            if not sj_id:
                failed_jobs.append({"scheduled_job_id": "", "error": "missing scheduled job id"})
                continue
            try:
                schedule_tz = str(sj.get("schedule_timezone") or "").strip() or clean_tz
                new_next = cron_next_run_at(cron_expr, datetime.now(UTC), schedule_tz=schedule_tz)
                recalculated_updates.append((sj_id, new_next))
            except Exception as exc:
                failed_jobs.append({"scheduled_job_id": sj_id, "error": str(exc)})

        if failed_jobs:
            return JSONResponse(
                {
                    "error": "Schedule timezone update aborted because one or more active jobs could not be recalculated.",
                    "failed_jobs": failed_jobs,
                },
                status_code=409,
            )

        try:
            with bl.conn.cursor() as cur:
                cur.execute(
                    "UPDATE projects SET schedule_timezone = %s WHERE id = %s",
                    (clean_tz, project_id),
                )
                for sj_id, new_next in recalculated_updates:
                    cur.execute(
                        "UPDATE scheduled_jobs SET next_run_at = %s WHERE id = %s AND project_id = %s",
                        (new_next, sj_id, project_id),
                    )
            bl.conn.commit()
        except Exception:
            with suppress(Exception):
                bl.conn.rollback()
            return JSONResponse({"error": "Failed to update schedule timezone"}, status_code=500)

        return {
            "status": "ok",
            "schedule_timezone": clean_tz,
            "recalculated_jobs": len(recalculated_updates),
        }

    @router.post("/api/projects/{project_id}/scheduled-jobs")
    async def api_project_create_scheduled_job(
        request: Request,
        project_id: str,
        body: ApiScheduledJobCreateRequest,
    ):
        actor = resolve_actor_username(request, None, default="operator")
        try:
            payload = project_command_service().create_scheduled_job(
                project_id,
                ProjectScheduledJobCreateInput(
                    workflow_id=body.workflow_id,
                    cron_expr=body.cron_expr,
                    schedule_timezone=body.schedule_timezone,
                    inputs=body.inputs,
                    enabled=body.enabled,
                    created_by=actor,
                ),
                scope=request_scope(
                    request,
                    project_id=project_id,
                    workflow_id=body.workflow_id,
                    actor_id=actor,
                ),
            )
        except ServiceError as e:
            return service_error_response(e, request=request)
        except Exception as e:
            return api_error_response(e, request=request)

        return JSONResponse(
            _payload_dict(
                payload,
                fallback={
                    "project_id": str(project_id),
                    "scheduled_job_id": 0,
                    "status": "created",
                },
            ),
            status_code=201,
        )

    @router.put("/api/projects/{project_id}/scheduled-jobs/{scheduled_job_id}")
    async def api_project_edit_scheduled_job(
        request: Request,
        project_id: str,
        scheduled_job_id: str,
        body: ApiScheduledJobUpdateRequest,
    ):
        actor = resolve_actor_username(request, None, default="operator")
        try:
            payload = project_command_service().update_scheduled_job(
                project_id,
                scheduled_job_id,
                ProjectScheduledJobUpdateInput(**body.model_dump()),
                scope=request_scope(
                    request, project_id=project_id, workflow_id=body.workflow_id, actor_id=actor
                ),
            )
        except ServiceError as exc:
            return service_error_response(exc, request=request)
        except Exception as exc:
            return api_error_response(exc, request=request)
        return _payload_dict(payload)

    @router.patch("/api/projects/{project_id}/scheduled-jobs/{scheduled_job_id}/toggle")
    async def api_project_toggle_scheduled_job(
        request: Request,
        project_id: str,
        scheduled_job_id: str,
        body: ApiScheduledJobToggleRequest,
    ):
        try:
            payload = project_command_service().toggle_scheduled_job(
                project_id,
                scheduled_job_id,
                ProjectScheduledJobToggleInput(enabled=body.enabled),
                scope=request_scope(request, project_id=project_id),
            )
        except ServiceError as e:
            return service_error_response(e, request=request)
        except Exception as e:
            return api_error_response(e, request=request)

        return _payload_dict(
            payload,
            fallback={
                "project_id": str(project_id),
                "scheduled_job_id": str(scheduled_job_id),
                "status": "updated",
                "enabled": body.enabled,
            },
        )

    @router.delete("/api/projects/{project_id}/scheduled-jobs/{scheduled_job_id}")
    async def api_project_delete_scheduled_job(
        request: Request,
        project_id: str,
        scheduled_job_id: str,
    ):
        try:
            payload = project_command_service().delete_scheduled_job(
                project_id,
                scheduled_job_id,
                scope=request_scope(request, project_id=project_id),
            )
        except ServiceError as e:
            return service_error_response(e, request=request)
        except Exception as e:
            return api_error_response(e, request=request)

        return _payload_dict(
            payload,
            fallback={
                "project_id": str(project_id),
                "scheduled_job_id": str(scheduled_job_id),
                "status": "deleted",
            },
        )

    @router.post("/api/projects/{project_id}/scheduled-jobs/{scheduled_job_id}/run-now")
    async def api_project_run_scheduled_job_now(
        request: Request,
        project_id: str,
        scheduled_job_id: str,
    ):
        try:
            payload = project_command_service().run_scheduled_job_now(
                project_id,
                scheduled_job_id,
                scope=request_scope(request, project_id=project_id),
            )
        except ServiceError as e:
            return service_error_response(e, request=request)
        except Exception as e:
            return api_error_response(e, request=request)

        return _payload_dict(
            payload,
            fallback={
                "project_id": str(project_id),
                "scheduled_job_id": str(scheduled_job_id),
                "status": "accepted",
            },
        )

    @router.get("/api/projects/{project_id}/scheduled-jobs")
    async def api_project_scheduled_jobs(
        request: Request,
        project_id: str,
        limit_runs: Annotated[int, Query(ge=1, le=200)] = 20,
    ):
        try:
            payload = project_query_service().list_scheduled_jobs(
                project_id,
                limit_runs=limit_runs,
                scope=request_scope(request, project_id=project_id, default_actor="system"),
            )
        except ServiceError as e:
            return service_error_response(e)
        except Exception as e:
            return api_error_response(e)
        return payload.dump()

    @router.get("/api/projects/{project_id}/cron-preview")
    async def api_project_cron_preview(
        request: Request,
        project_id: str,
        cron_expr: Annotated[str, Query(alias="cron_expr")] = "",
        count: Annotated[int, Query(ge=1, le=20)] = 5,
        schedule_timezone: Annotated[str | None, Query()] = None,
    ):
        """Return human-readable summary and next N run timestamps for a cron expression."""
        if local_auth_enabled() or external_auth_enabled():
            try:
                require_authenticated_user(request)
            except PermissionError:
                return JSONResponse({"error": err_authentication_required}, status_code=401)
        bl = get_logger_only()
        if not bl:
            return JSONResponse({"error": err_database_not_configured}, status_code=503)
        project = bl.get_project(project_id) if bl else None
        if not project:
            return JSONResponse({"error": err_project_not_found}, status_code=404)
        scope = request_scope(request, project_id=project_id)
        try:
            require_project_access(scope, project)
        except ServiceForbiddenError as exc:
            return service_error_response(exc)
        from core.application.settings.timezone_service import validate_timezone

        try:
            schedule_tz = validate_timezone(
                schedule_timezone
                if schedule_timezone is not None
                else str(project.get("schedule_timezone") or "").strip() or "UTC"
            )
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        clean_cron = normalize_cron_expr(cron_expr)
        if not clean_cron:
            return JSONResponse({"error": "cron_expr is required"}, status_code=400)
        try:
            parse_cron_expression(clean_cron)
        except ValueError as exc:
            return JSONResponse({"error": f"Invalid cron expression: {exc}"}, status_code=400)
        try:
            from zoneinfo import ZoneInfo

            tz = ZoneInfo(schedule_tz)
            runs_utc: list[datetime] = []
            ref = datetime.now(UTC)
            for _ in range(count):
                nxt = cron_next_run_at(clean_cron, ref, schedule_tz=schedule_tz)
                runs_utc.append(nxt)
                ref = nxt
            runs_local = [r.astimezone(tz).strftime("%Y-%m-%d %H:%M") for r in runs_utc]
            summary = cron_human_summary(clean_cron, schedule_tz)
            return {
                "cron_expr": clean_cron,
                "timezone": schedule_tz,
                "summary": summary,
                "next_runs": runs_local,
                "next_runs_utc": [
                    r.astimezone(UTC).isoformat().replace("+00:00", "Z") for r in runs_utc
                ],
            }
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        except Exception as exc:
            return JSONResponse({"error": f"Preview failed: {exc}"}, status_code=500)

    return router
