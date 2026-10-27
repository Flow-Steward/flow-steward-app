"""Project settings history API routes."""

from __future__ import annotations

from collections.abc import Callable
from typing import Annotated, Any

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse

from core.application.management import ServiceError
from core.application.management.command_models import ProjectSettingsRestoreInput


def create_project_history_router(
    *,
    project_query_service: Callable[[], Any],
    project_command_service: Callable[[], Any],
    request_scope: Callable[..., Any],
    resolve_actor_username: Callable[..., str],
    service_error_response: Callable[..., JSONResponse],
    api_error_response: Callable[..., JSONResponse],
) -> APIRouter:
    router = APIRouter(tags=["project-history"])

    @router.get("/api/projects/{project_id}/history")
    async def api_project_settings_history(
        request: Request,
        project_id: str,
        limit: Annotated[int, Query(ge=1, le=500)] = 50,
    ):
        try:
            payload = project_query_service().list_history(
                project_id,
                limit=int(limit),
                scope=request_scope(request, project_id=project_id, default_actor="system"),
            )
        except ServiceError as e:
            return service_error_response(e)
        except Exception as e:
            return api_error_response(e)
        return payload.dump()

    @router.get("/api/projects/{project_id}/history/{revision_id}/diff")
    async def api_project_settings_history_diff(
        request: Request,
        project_id: str,
        revision_id: str,
        compare_to_revision_id: Annotated[str | None, Query()] = None,
    ):
        try:
            payload = project_query_service().get_history_diff(
                project_id,
                revision_id,
                compare_to_revision_id=compare_to_revision_id,
                scope=request_scope(request, project_id=project_id, default_actor="system"),
            )
        except ServiceError as e:
            return service_error_response(e)
        except Exception as e:
            return api_error_response(e)
        return payload.dump()

    @router.post("/api/projects/{project_id}/history/{revision_id}/restore")
    async def api_project_settings_history_restore(
        request: Request, project_id: str, revision_id: str
    ):
        actor = resolve_actor_username(request, "", default="system")
        try:
            payload = project_command_service().restore_project_settings_revision(
                project_id,
                revision_id,
                ProjectSettingsRestoreInput(author=actor),
                scope=request_scope(
                    request, project_id=project_id, actor_id=actor, default_actor="system"
                ),
            )
        except ServiceError as e:
            return service_error_response(e)
        except Exception as e:
            return api_error_response(e)
        return payload.dump()

    return router
