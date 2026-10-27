"""Trash lifecycle API routes extracted from serve.py."""

from __future__ import annotations

from collections.abc import Callable
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from core.application.management import ServiceConflictError, ServiceError
from core.application.management.trash_tool_scope import tool_trash_scope_predicates
from core.entrypoints.admin._registry_scope import require_default_registry_account


class TrashLifecyclePlanRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action_type: str = Field(default="")


class TrashLifecycleExecuteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action_type: str = Field(default="")
    selected_node_ids: list[str] = Field(default_factory=list)
    plan_version: str = Field(min_length=1)


class WorkspaceDeleteProjectActionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_id: str = Field(default="")
    action: str = Field(default="")
    target_workspace_id: str = Field(default="")


class WorkspaceDeleteDataConnectionActionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    connection_id: str = Field(default="")
    action: str = Field(default="")
    target_workspace_id: str = Field(default="")


class WorkspaceDeleteExecuteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_actions: list[WorkspaceDeleteProjectActionRequest] = Field(default_factory=list)
    connection_actions: list[WorkspaceDeleteDataConnectionActionRequest] = Field(
        default_factory=list
    )


def create_api_trash_router(
    *,
    require_authenticated_user: Callable[..., dict],
    auth_admin_roles: set[str] | tuple[str, ...] | list[str],
    get_logger_only: Callable[[], Any],
    real_dict_cursor_kwargs: Callable[[], dict[str, Any]],
    request_scope: Callable[..., Any],
    trash_lifecycle_service: Callable[[], Any],
    service_error_response: Callable[..., JSONResponse],
    api_error_response: Callable[..., JSONResponse],
    err_admin_role_required: str,
    err_database_not_configured: str,
) -> APIRouter:
    router = APIRouter(
        tags=["api-trash"],
        responses={403: {"description": "Trash operation is not permitted"}},
    )

    def _request_scope(request: Request, project_id: str | None = None):
        normalized_project_id = str(project_id or "").strip()
        if normalized_project_id:
            scope = request_scope(request, project_id=normalized_project_id)
            allowed_project_ids = frozenset(
                str(item).strip()
                for item in (getattr(scope, "allowed_project_ids", None) or ())
                if str(item).strip()
            )
            if allowed_project_ids and normalized_project_id not in allowed_project_ids:
                raise HTTPException(status_code=403, detail="Project access denied")
            return scope
        return request_scope(request)

    _trash_tables = [
        ("workspace", "workspaces", "id", "display_name"),
        ("project", "projects", "id", "name"),
        ("job", "jobs", "id", "task_summary"),
        ("user", "users", "id", "username"),
        ("registry_agent", "registry_agents", "agent_id", "display_name"),
        ("registry_tool", "registry_tools", "tool_id", "display_name"),
        ("mcp_connector", "mcp_connectors", "connector_id", "display_name"),
        ("registry_skill", "registry_skills", "skill_id", "display_name"),
        ("registry_workflow", "registry_workflows", "workflow_id", "display_name"),
        ("registry_guardrail", "registry_guardrails", "id", "display_name"),
        ("registry_data_contract", "registry_data_contracts", "contract_id", "display_name"),
        ("dataset", "registry_datasets", "dataset_id", "display_name"),
        (
            "workspace_data_connection",
            "workspace_data_connections",
            "connection_id",
            "display_name",
        ),
        ("project_settings_revision", "project_settings_revisions", "id", "action"),
    ]
    _trash_categories = {
        "workspace": "application",
        "project": "application",
        "job": "application",
        "user": "admin",
        "registry_agent": "registry",
        "registry_tool": "registry",
        "mcp_connector": "registry",
        "registry_skill": "registry",
        "registry_workflow": "registry",
        "registry_guardrail": "registry",
        "registry_data_contract": "registry",
        "dataset": "data",
        "workspace_data_connection": "data",
        "project_settings_revision": "application",
    }

    @router.get(
        "/api/trash",
        responses={403: {"description": "Project or trash access denied"}},
    )
    async def api_trash_list(
        request: Request,
        entity_type: Annotated[str | None, Query()] = None,
        project_id: Annotated[str | None, Query()] = None,
        limit: Annotated[int, Query(ge=1, le=1000)] = 200,
    ):
        """List all soft-deleted entities across core tables."""
        try:
            require_authenticated_user(request, required_roles=auth_admin_roles)
        except PermissionError:
            return JSONResponse({"error": err_admin_role_required}, status_code=403)
        resolved_scope = _request_scope(request, project_id)
        require_default_registry_account(resolved_scope)
        bl = get_logger_only()
        if not bl:
            return JSONResponse({"error": err_database_not_configured}, status_code=503)
        normalized_entity_type = str(entity_type or "").strip().lower()
        if normalized_entity_type:
            supported_types = {row[0] for row in _trash_tables}
            if normalized_entity_type not in supported_types:
                return JSONResponse(
                    {
                        "error": "Unsupported trash entity_type",
                        "supported_entity_types": sorted(supported_types),
                    },
                    status_code=400,
                )
        items: list[dict[str, Any]] = []
        try:
            table_exists = getattr(bl, "_table_exists", None)
            column_exists = getattr(bl, "_column_exists", None)
            with bl.conn.cursor(**real_dict_cursor_kwargs()) as cur:
                for entity_type, table, id_col, label_col in _trash_tables:
                    if normalized_entity_type and entity_type != normalized_entity_type:
                        continue
                    if callable(table_exists):
                        try:
                            if not bool(table_exists(table)):
                                continue
                        except Exception:
                            continue
                    has_deleted_by = False
                    if callable(column_exists):
                        try:
                            has_deleted_by = bool(column_exists(table, "deleted_by"))
                        except Exception:
                            has_deleted_by = False
                    deleted_by_sql = "deleted_by" if has_deleted_by else "NULL::text AS deleted_by"
                    has_project_id = bool(
                        callable(column_exists) and column_exists(table, "project_id")
                    )
                    has_tenant_id = bool(
                        callable(column_exists) and column_exists(table, "tenant_id")
                    )
                    project_id_sql = (
                        "project_id::text AS project_id"
                        if has_project_id
                        else "NULL::text AS project_id"
                    )
                    tenant_id_sql = "NULL::text AS tenant_id"
                    if has_tenant_id:
                        tenant_id_sql = "tenant_id::text AS tenant_id"
                    elif entity_type == "registry_tool":
                        tenant_id_sql = (
                            "COALESCE(implementation->>'owner_tenant_id', "
                            "config->>'owner_tenant_id', (SELECT p.tenant_id::text "
                            "FROM projects p WHERE p.id = registry_tools.project_id)) AS tenant_id"
                        )
                    where_clauses = ["deleted_at IS NOT NULL"]
                    query_params: list[Any] = []
                    if callable(column_exists):
                        try:
                            if bool(column_exists(table, "account_id")):
                                where_clauses.append("(account_id = %s OR account_id IS NULL)")
                                query_params.append(str(getattr(resolved_scope, "account_id", "")))
                        except Exception:
                            pass
                    resolved_tenant_id = str(getattr(resolved_scope, "tenant_id", "") or "").strip()
                    resolved_project_id = getattr(resolved_scope, "project_id", None)
                    allowed_project_ids = frozenset(
                        getattr(resolved_scope, "allowed_project_ids", frozenset())
                    )
                    if entity_type == "registry_tool" and has_project_id and not has_tenant_id:
                        tool_clauses, tool_params = tool_trash_scope_predicates(resolved_scope)
                        where_clauses.extend(tool_clauses)
                        query_params.extend(tool_params)
                    elif has_tenant_id and resolved_tenant_id:
                        where_clauses.append("tenant_id = %s")
                        query_params.append(resolved_tenant_id)
                    if has_project_id and not (
                        entity_type == "registry_tool" and not has_tenant_id
                    ):
                        if resolved_project_id is not None:
                            where_clauses.append("project_id = %s")
                            query_params.append(str(resolved_project_id))
                        elif allowed_project_ids:
                            where_clauses.append("project_id = ANY(%s)")
                            query_params.append(sorted(allowed_project_ids))
                    if entity_type == "registry_workflow":
                        # A hook's storage workflow is a place inside another
                        # workflow, never a deleted item a person restores.
                        where_clauses.append("NOT (settings ? 'hook_storage')")
                    if entity_type == "registry_workflow" and callable(column_exists):
                        try:
                            if bool(column_exists(table, "visible_in_catalog")):
                                where_clauses.append("COALESCE(visible_in_catalog, true) = true")
                        except Exception:
                            pass
                    query = "".join(
                        [
                            "SELECT ",
                            id_col,
                            " AS entity_id, COALESCE(",
                            label_col,
                            "::text, '') AS label, deleted_at, ",
                            deleted_by_sql,
                            ", ",
                            project_id_sql,
                            ", ",
                            tenant_id_sql,
                            " FROM ",
                            table,
                            " WHERE ",
                            " AND ".join(where_clauses),
                            " ORDER BY deleted_at DESC LIMIT ",
                            str(int(limit)),
                        ]
                    )
                    cur.execute(query, tuple(query_params))
                    for row in cur.fetchall() or []:
                        record = dict(row)
                        record["entity_type"] = entity_type
                        record["category"] = _trash_categories.get(entity_type, "system")
                        record["deleted_at"] = (
                            str(record["deleted_at"]) if record.get("deleted_at") else None
                        )
                        items.append(record)
        except Exception as e:
            return api_error_response(e)
        items.sort(key=lambda item: item.get("deleted_at") or "", reverse=True)
        return {"items": items, "total": len(items)}

    @router.post("/api/trash/{entity_type}/{entity_id}/trash")
    async def api_trash_entity(request: Request, entity_type: str, entity_id: str):
        try:
            scope = request_scope(request)
            payload = trash_lifecycle_service().trash(
                entity_type,
                entity_id,
                deleted_by=scope.actor_name,
                scope=scope,
            )
        except ServiceError as e:
            return service_error_response(e)
        except Exception as e:
            return api_error_response(e)
        return payload.dump()

    @router.post("/api/trash/workspace/{workspace_id}/delete-plan")
    async def api_workspace_delete_plan(request: Request, workspace_id: str):
        try:
            payload = trash_lifecycle_service().workspace_delete_plan(
                workspace_id,
                scope=request_scope(request),
            )
        except ServiceError as e:
            return service_error_response(e)
        except Exception as e:
            return api_error_response(e)
        return payload

    @router.post("/api/trash/workspace/{workspace_id}/delete-execute")
    async def api_workspace_delete_execute(
        request: Request,
        workspace_id: str,
        body: WorkspaceDeleteExecuteRequest,
    ):
        try:
            scope = request_scope(request)
            payload = trash_lifecycle_service().trash_workspace_with_plan(
                workspace_id,
                deleted_by=scope.actor_name,
                project_actions=[item.model_dump() for item in (body.project_actions or [])],
                connection_actions=[item.model_dump() for item in (body.connection_actions or [])],
                scope=scope,
            )
        except ServiceError as e:
            return service_error_response(e)
        except Exception as e:
            return api_error_response(e)
        return payload

    @router.post(
        "/api/trash/{entity_type}/{entity_id}/restore",
        responses={403: {"description": "Project access denied"}},
    )
    async def api_restore_entity(
        request: Request,
        entity_type: str,
        entity_id: str,
        project_id: Annotated[str | None, Query()] = None,
    ):
        try:
            payload = trash_lifecycle_service().restore(
                entity_type,
                entity_id,
                scope=_request_scope(request, project_id),
            )
        except HTTPException:
            raise
        except ServiceError as e:
            return service_error_response(e)
        except Exception as e:
            return api_error_response(e)
        return payload.dump()

    @router.post(
        "/api/trash/{entity_type}/{entity_id}/plan",
        responses={403: {"description": "Project access denied"}},
    )
    async def api_lifecycle_plan(
        request: Request,
        entity_type: str,
        entity_id: str,
        body: TrashLifecyclePlanRequest,
        project_id: Annotated[str | None, Query()] = None,
    ):
        try:
            payload = trash_lifecycle_service().plan(
                entity_type,
                entity_id,
                action_type=body.action_type,
                scope=_request_scope(request, project_id),
            )
        except HTTPException:
            raise
        except ServiceError as e:
            return service_error_response(e)
        except Exception as e:
            return api_error_response(e)
        return payload.dump()

    @router.post(
        "/api/trash/{entity_type}/{entity_id}/execute",
        responses={403: {"description": "Project access denied"}},
    )
    async def api_lifecycle_execute(
        request: Request,
        entity_type: str,
        entity_id: str,
        body: TrashLifecycleExecuteRequest,
        project_id: Annotated[str | None, Query()] = None,
    ):
        try:
            payload = trash_lifecycle_service().execute_plan(
                entity_type,
                entity_id,
                action_type=body.action_type,
                selection={
                    "selected_node_ids": body.selected_node_ids,
                    "plan_version": body.plan_version,
                },
                scope=_request_scope(request, project_id),
            )
        except HTTPException:
            raise
        except ServiceError as e:
            return service_error_response(e)
        except Exception as e:
            return api_error_response(e)
        return payload.dump()

    @router.post(
        "/api/trash/{entity_type}/{entity_id}/hard-delete",
        responses={403: {"description": "Project access denied"}},
    )
    async def api_hard_delete_entity(
        request: Request,
        entity_type: str,
        entity_id: str,
        project_id: Annotated[str | None, Query()] = None,
    ):
        try:
            scope = _request_scope(request, project_id)
            normalized_entity_type = str(entity_type or "").strip()
            if normalized_entity_type in {"project", "workspace", "mcp_connector"}:
                raise ServiceConflictError(
                    "Use the lifecycle plan and revision-bound execute endpoint to purge "
                    f"{normalized_entity_type}"
                )
            payload = trash_lifecycle_service().hard_delete(
                entity_type,
                entity_id,
                scope=scope,
            )
        except HTTPException:
            raise
        except ServiceError as e:
            return service_error_response(e)
        except Exception as e:
            return api_error_response(e)
        return payload.dump()

    return router
