"""Project API routes extracted from serve.py."""

from __future__ import annotations

import datetime
import decimal
import json
from collections.abc import Callable
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from core.application.management import ServiceError
from core.application.management.command_models import (
    ProjectJobsBulkDeleteInput,
    ProjectJobsBulkDeleteResultView,
)
from core.application.management.project_cli_projection import summarize_data_connection_rows
from core.application.management.query_models import (
    ProjectJobsListView,
    ProjectQueueView,
    ProjectsListView,
)
from core.application.management.requests import ProjectJobsBulkDeleteRequest
from core.application.settings.model_catalog import ModelCatalogUnavailableError

PROJECT_NOT_FOUND_MESSAGE = "Project not found"
REQUEST_BODY_MUST_BE_VALID_JSON_VALUE = "Request body must be valid JSON"
WORKSPACE_ID_IS_REQUIRED_MESSAGE = "'workspace_id' is required"


def create_api_projects_router(
    *,
    job_query_service: Callable[[], Any],
    job_command_service: Callable[[], Any],
    project_query_service: Callable[[], Any],
    project_command_service: Callable[[], Any],
    request_scope: Callable[..., Any],
    require_authenticated_user: Callable[..., Any],
    local_auth_enabled: Callable[[], bool],
    external_auth_enabled: bool,
    auth_admin_roles: set[str],
    resolve_actor_username: Callable[..., str],
    project_create_input_cls: type,
    project_update_input_cls: type,
    service_error_cls: type[Exception],
    service_not_found_error_cls: type[Exception],
    service_error_response: Callable[..., JSONResponse],
    api_error_response: Callable[..., JSONResponse],
    err_admin_role_required: str,
    err_database_not_configured: str,
    default_agent_models: Callable[..., dict[str, Any]],
    available_providers: Callable[..., list[tuple[str, str]]],
    available_models: Callable[..., list[str]],
    providers_with_models: Callable[..., dict[str, Any]] | None = None,
    resolve_model_selection_context: Callable[..., dict[str, Any]] | None = None,
) -> APIRouter:
    router = APIRouter(tags=["api-projects"])
    safe_error_messages = {
        "create": "Project creation failed.",
        "detail": "Project detail unavailable.",
        "update": "Project update failed.",
        "delete": "Project deletion failed.",
        "create_payload": "Invalid project payload.",
        "update_payload": "Invalid project update payload.",
    }

    def _json_safe(obj: Any) -> Any:
        if obj is None or isinstance(obj, (bool, int, float, str)):
            return obj
        if isinstance(obj, (datetime.datetime, datetime.date, datetime.time)):
            return obj.isoformat()
        if isinstance(obj, decimal.Decimal):
            return float(obj)
        if isinstance(obj, (frozenset, set)):
            return [_json_safe(v) for v in sorted(str(v) for v in obj)]
        if isinstance(obj, (list, tuple)):
            return [_json_safe(v) for v in obj]
        if isinstance(obj, dict):
            return {str(k): _json_safe(v) for k, v in obj.items()}
        try:
            json.dumps(obj)
            return obj
        except (TypeError, ValueError):
            return str(obj)

    def _log_unexpected(message: str, *args: Any) -> None:
        _ = message
        del args

    def _safe_project_error_message(
        action: str,
        *,
        status_code: int | None = None,
        service_message: str = "",
    ) -> str:
        normalized = str(service_message or "").strip()
        if normalized == err_database_not_configured:
            return normalized
        if status_code is not None and status_code < 500 and normalized:
            return normalized
        return safe_error_messages[action]

    def _api_svc_error(
        exc: Exception,
        *,
        action: str,
        default_status: int = 422,
    ) -> HTTPException:
        sc = int(getattr(exc, "status_code", default_status) or default_status)
        detail = _safe_project_error_message(
            action,
            status_code=sc,
            service_message=str(getattr(exc, "message", "")),
        )
        return HTTPException(status_code=sc, detail=detail)

    def _invoke_request_callable(source: Callable[..., Any] | Any, request: Request) -> Any:
        if not callable(source):
            return source
        try:
            from inspect import Parameter, signature

            params = signature(source).parameters
        except (TypeError, ValueError):
            return source()
        if "request" in params:
            return source(request=request)
        required_positional = [
            param
            for param in params.values()
            if param.kind in (Parameter.POSITIONAL_ONLY, Parameter.POSITIONAL_OR_KEYWORD)
            and param.default is Parameter.empty
        ]
        if required_positional:
            return source(request)
        return source()

    def _resolve_model_selection_payload(request: Request) -> dict[str, Any]:
        payload = {
            "default_agent_models": _invoke_request_callable(default_agent_models, request),
            "available_providers": _invoke_request_callable(available_providers, request),
            "available_models": _invoke_request_callable(available_models, request),
            "providers_with_models": _invoke_request_callable(providers_with_models, request)
            if providers_with_models
            else {},
        }
        if not callable(resolve_model_selection_context):
            return payload
        resolved = _invoke_request_callable(resolve_model_selection_context, request)
        if not isinstance(resolved, dict):
            return payload
        for key in tuple(payload):
            if key in resolved:
                payload[key] = resolved[key]
        return payload

    _api_project_create_fields: set[str] = {
        "data_connection_ids",
        "default_model",
        "default_provider",
        "description",
        "name",
        "operator_default_workflow_id",
        "project_rules",
        "workflow_ids",
        "workspace_id",
    }
    _api_project_update_fields: set[str] = set(_api_project_create_fields)

    def _api_reject_unknown_project_fields(raw: dict[str, Any], allowed: set[str]) -> None:
        unknown = sorted(str(key) for key in raw if str(key) not in allowed)
        if unknown:
            raise HTTPException(
                status_code=422,
                detail=f"Unknown project field(s): {', '.join(unknown)}",
            )

    @router.get(
        "/api/projects",
        response_model=ProjectsListView,
        responses={
            404: {"description": "Not found"},
            422: {"description": "Validation error"},
            500: {"description": "Internal server error"},
        },
    )
    async def api_projects_list(
        request: Request,
        page: Annotated[int, Query()] = 1,
        per_page: Annotated[int, Query()] = 25,
        search: Annotated[str, Query()] = "",
        workspace_id: Annotated[str, Query()] = "",
    ):
        """Paginated JSON list of projects for the projects grid."""
        normalized_page = max(1, int(page or 1))
        normalized_per_page = max(1, min(int(per_page or 25), 200))
        try:
            try:
                resolved_scope = request_scope(request, workspace_id=workspace_id)
            except Exception:
                resolved_scope = request_scope(request)
            payload = project_query_service().list_projects(
                page=page,
                per_page=per_page,
                search=search,
                scope=resolved_scope,
            )
        except Exception:
            return JSONResponse(
                {
                    "projects": [],
                    "total": 0,
                    "page": normalized_page,
                    "per_page": normalized_per_page,
                    "total_pages": 1,
                }
            )
        dumped = payload.dump() if hasattr(payload, "dump") else {}
        if isinstance(dumped, dict) and dumped:
            return JSONResponse(dumped)
        return JSONResponse(
            {
                "projects": [],
                "total": 0,
                "page": normalized_page,
                "per_page": normalized_per_page,
                "total_pages": 1,
            }
        )

    @router.post(
        "/api/projects",
        responses={
            404: {"description": "Not found"},
            422: {"description": "Validation error"},
            500: {"description": "Internal server error"},
        },
    )
    async def api_projects_create(request: Request):
        try:
            raw: dict = dict(await request.json())
        except Exception as exc:
            raise HTTPException(
                status_code=422, detail=REQUEST_BODY_MUST_BE_VALID_JSON_VALUE
            ) from exc
        name = str(raw.get("name") or "").strip()
        if not name:
            raise HTTPException(status_code=422, detail="'name' is required and must be non-empty")
        if not str(raw.get("workspace_id") or "").strip():
            raise HTTPException(status_code=422, detail=WORKSPACE_ID_IS_REQUIRED_MESSAGE)
        _api_reject_unknown_project_fields(raw, _api_project_create_fields)
        actor = resolve_actor_username(request, "", default="system")
        try:
            cmd = project_create_input_cls(author=actor, form_data=raw, **raw)
        except Exception as exc:
            _log_unexpected("api_projects_create rejected payload")
            raise HTTPException(
                status_code=422,
                detail=_safe_project_error_message("create_payload"),
            ) from exc
        try:
            payload = project_command_service().create_project(
                cmd,
                scope=request_scope(request, actor_id=actor, default_actor="system"),
            )
        except service_error_cls as exc:
            raise _api_svc_error(exc, action="create") from exc
        except Exception as exc:
            _log_unexpected("api_projects_create failed unexpectedly")
            raise HTTPException(
                status_code=500, detail=_safe_project_error_message("create")
            ) from exc
        data = payload.dump() if hasattr(payload, "dump") else {}
        if "project_id" in data and "id" not in data:
            data["id"] = data["project_id"]
        return JSONResponse(data, status_code=201)

    @router.get(
        "/api/projects/{project_id}",
        responses={
            404: {"description": "Not found"},
            422: {"description": "Validation error"},
            500: {"description": "Internal server error"},
            503: {"description": "Service unavailable"},
        },
    )
    async def api_project_detail(request: Request, project_id: str):
        try:
            payload = project_query_service().get_project_edit_form(
                project_id,
                scope=request_scope(request, project_id=project_id),
            )
            project_data = (
                dict(payload.project)
                if hasattr(payload, "project") and isinstance(payload.project, dict)
                else {}
            )
            project_data.update(_resolve_model_selection_payload(request))
            project_data.setdefault("id", project_id)
            # Data connections carry an inlined schema_snapshot whose size grows
            # with the source database; one project detail was 298KB, 292KB of it
            # snapshots. Serve the same bounded summary the CLI projection uses.
            for key in ("assigned_data_connections", "available_data_connections"):
                if key in project_data:
                    project_data[key] = summarize_data_connection_rows(project_data[key])
            return JSONResponse(_json_safe(project_data))
        except service_not_found_error_cls as exc:
            raise HTTPException(status_code=404, detail=PROJECT_NOT_FOUND_MESSAGE) from exc
        except ModelCatalogUnavailableError as exc:
            raise HTTPException(status_code=503, detail="Model catalog is unavailable.") from exc
        except service_error_cls as exc:
            raise _api_svc_error(exc, action="detail") from exc
        except Exception as exc:
            _log_unexpected("api_project_detail %s failed unexpectedly", project_id)
            raise HTTPException(
                status_code=500, detail=_safe_project_error_message("detail")
            ) from exc

    @router.put(
        "/api/projects/{project_id}",
        responses={
            404: {"description": "Not found"},
            422: {"description": "Validation error"},
            500: {"description": "Internal server error"},
        },
    )
    async def api_project_update(request: Request, project_id: str):
        try:
            raw: dict = dict(await request.json())
        except Exception as exc:
            raise HTTPException(
                status_code=422, detail=REQUEST_BODY_MUST_BE_VALID_JSON_VALUE
            ) from exc
        _api_reject_unknown_project_fields(raw, _api_project_update_fields)
        actor = resolve_actor_username(request, "", default="system")
        try:
            existing_form = project_query_service().get_project_edit_form(
                project_id,
                scope=request_scope(request, project_id=project_id),
            )
            existing_proj = (
                dict(existing_form.project)
                if hasattr(existing_form, "project") and isinstance(existing_form.project, dict)
                else {}
            )
            existing_wf_ids = list(getattr(existing_form, "project_workflow_ids", None) or [])
            existing_default_wf = str(
                existing_proj.get("operator_default_workflow_id") or ""
            ).strip()
        except Exception:
            existing_proj = {}
            existing_wf_ids = []
            existing_default_wf = ""
        if not raw.get("workspace_id"):
            raw.setdefault("workspace_id", existing_proj.get("workspace_id", ""))
        if not str(raw.get("workspace_id") or "").strip():
            raise HTTPException(status_code=422, detail=WORKSPACE_ID_IS_REQUIRED_MESSAGE)
        if not raw.get("name"):
            raw.setdefault("name", existing_proj.get("name", ""))
        if not raw.get("workflow_ids") and existing_wf_ids:
            raw.setdefault("workflow_ids", existing_wf_ids)
        if "operator_default_workflow_id" not in raw and existing_default_wf:
            raw.setdefault("operator_default_workflow_id", existing_default_wf)
        try:
            cmd = project_update_input_cls(author=actor, form_data=raw, **raw)
        except Exception as exc:
            _log_unexpected("api_project_update rejected payload for %s", project_id)
            raise HTTPException(
                status_code=422,
                detail=_safe_project_error_message("update_payload"),
            ) from exc
        try:
            payload = project_command_service().update_project(
                project_id,
                cmd,
                scope=request_scope(
                    request, project_id=project_id, actor_id=actor, default_actor="system"
                ),
            )
        except service_not_found_error_cls as exc:
            raise HTTPException(status_code=404, detail=PROJECT_NOT_FOUND_MESSAGE) from exc
        except service_error_cls as exc:
            raise _api_svc_error(exc, action="update") from exc
        except Exception as exc:
            _log_unexpected("api_project_update %s failed unexpectedly", project_id)
            raise HTTPException(
                status_code=500, detail=_safe_project_error_message("update")
            ) from exc
        data = payload.dump() if hasattr(payload, "dump") else {}
        if "project_id" in data and "id" not in data:
            data["id"] = data["project_id"]
        return JSONResponse(data, status_code=200)

    @router.post(
        "/api/projects/{project_id}/move-workspace",
        responses={
            404: {"description": "Not found"},
            422: {"description": "Validation error"},
            500: {"description": "Internal server error"},
        },
    )
    async def api_project_move_workspace(request: Request, project_id: str):
        try:
            raw: dict = dict(await request.json())
        except Exception as exc:
            raise HTTPException(
                status_code=422, detail=REQUEST_BODY_MUST_BE_VALID_JSON_VALUE
            ) from exc
        workspace_id = str(raw.get("workspace_id") or "").strip()
        if not workspace_id:
            raise HTTPException(status_code=422, detail=WORKSPACE_ID_IS_REQUIRED_MESSAGE)
        actor = resolve_actor_username(request, "", default="system")
        try:
            payload = project_command_service().move_project_to_workspace(
                project_id,
                workspace_id,
                scope=request_scope(
                    request, project_id=project_id, actor_id=actor, default_actor="system"
                ),
            )
        except service_not_found_error_cls as exc:
            raise HTTPException(status_code=404, detail=PROJECT_NOT_FOUND_MESSAGE) from exc
        except service_error_cls as exc:
            raise _api_svc_error(exc, action="update") from exc
        except Exception as exc:
            _log_unexpected("api_project_move_workspace %s failed unexpectedly", project_id)
            raise HTTPException(
                status_code=500, detail=_safe_project_error_message("update")
            ) from exc
        data = payload.dump() if hasattr(payload, "dump") else {}
        if "project_id" in data and "id" not in data:
            data["id"] = data["project_id"]
        return JSONResponse(data, status_code=200)

    @router.delete(
        "/api/projects/{project_id}",
        responses={
            404: {"description": "Not found"},
            422: {"description": "Validation error"},
            500: {"description": "Internal server error"},
        },
    )
    async def api_project_delete(request: Request, project_id: str):
        actor = resolve_actor_username(request, "", default="system")
        try:
            payload = project_command_service().delete_project(
                project_id,
                scope=request_scope(
                    request, project_id=project_id, actor_id=actor, default_actor="system"
                ),
            )
        except service_not_found_error_cls as exc:
            raise HTTPException(status_code=404, detail=PROJECT_NOT_FOUND_MESSAGE) from exc
        except service_error_cls as exc:
            raise _api_svc_error(exc, action="delete") from exc
        except Exception as exc:
            _log_unexpected("api_project_delete %s failed unexpectedly", project_id)
            raise HTTPException(
                status_code=500, detail=_safe_project_error_message("delete")
            ) from exc
        data = (
            payload.dump()
            if hasattr(payload, "dump")
            else {"status": "deleted", "project_id": project_id}
        )
        return JSONResponse(data, status_code=200)

    @router.get(
        "/api/projects/{project_id}/jobs",
        response_model=ProjectJobsListView,
        responses={
            404: {"description": "Not found"},
            422: {"description": "Validation error"},
            500: {"description": "Internal server error"},
        },
    )
    async def api_project_jobs_list(
        request: Request,
        project_id: str,
        page: Annotated[int, Query()] = 1,
        per_page: Annotated[int, Query()] = 25,
        status: Annotated[str, Query()] = "",
    ):
        """List jobs for a project (paginated, JSON)."""
        try:
            payload = job_query_service().list_project_jobs(
                project_id,
                page=page,
                per_page=per_page,
                status=status,
                scope=request_scope(request, project_id=project_id),
            )
        except ServiceError as e:
            return service_error_response(e)
        except Exception as e:
            return api_error_response(e)
        return JSONResponse(payload.dump())

    @router.post(
        "/api/projects/{project_id}/jobs/bulk-delete",
        response_model=ProjectJobsBulkDeleteResultView,
        responses={
            404: {"description": "Not found"},
            422: {"description": "Validation error"},
            500: {"description": "Internal server error"},
        },
    )
    async def api_project_jobs_bulk_delete(
        request: Request, project_id: str, req: ProjectJobsBulkDeleteRequest
    ):
        """Trash (soft-delete) selected jobs for a project. Requires confirm='DELETE'."""
        if local_auth_enabled() or external_auth_enabled:
            try:
                require_authenticated_user(request, required_roles=auth_admin_roles)
            except PermissionError:
                return JSONResponse({"error": err_admin_role_required}, status_code=403)
        try:
            payload = job_command_service().bulk_delete_project_jobs(
                ProjectJobsBulkDeleteInput(
                    project_id=project_id,
                    ids=req.ids or [],
                    confirm=req.confirm or "",
                ),
                scope=request_scope(request, project_id=project_id),
            )
        except ServiceError as e:
            return service_error_response(e)
        except Exception as e:
            return api_error_response(e)
        return JSONResponse(
            {
                "status": payload.status,
                "requested": payload.requested,
                "deleted": payload.deleted,
                "deleted_count": payload.deleted_count,
                "skipped": payload.skipped,
            }
        )

    @router.get(
        "/api/projects/{project_id}/queue",
        response_model=ProjectQueueView,
        responses={
            404: {"description": "Not found"},
            422: {"description": "Validation error"},
            500: {"description": "Internal server error"},
        },
    )
    async def api_project_queue(request: Request, project_id: str):
        """Return queue + scheduler status for a project."""
        try:
            payload = project_query_service().get_project_queue(
                project_id,
                scope=request_scope(request, project_id=project_id),
            )
        except ServiceError as e:
            return service_error_response(e)
        except Exception as e:
            return api_error_response(e)
        return payload.dump()

    return router
