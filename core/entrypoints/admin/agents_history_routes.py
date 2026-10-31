# ruff: noqa: B904
# mypy: ignore-errors
"""History, clone, and restore routes for admin agents."""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Annotated, Any

from fastapi import APIRouter, Body, HTTPException, Query, Request

from core.application.management.command_models import AgentMutationResultView
from core.application.management.command_services_agent import AgentCommandService
from core.application.management.errors import ServiceError
from core.application.management.query_models import AgentDiffView, AgentRecordView
from core.entrypoints.admin._service_errors import raise_service_http_error, service_db_getter
from core.entrypoints.admin.agents_models import AgentCloneRequest, RestoreRevisionRequest
from core.entrypoints.api.error_safety import safe_error_response

logger = logging.getLogger(__name__)

_AGENT_CLONE_REQUEST_EXAMPLE = {
    "agent_id": "release_writer_copy",
    "display_name": "Release Writer (Copy)",
}
_AGENT_RESTORE_REQUEST_EXAMPLE = {"entry_id": "188"}
_AGENT_CLONE_RESPONSE_EXAMPLE = {
    "status": "cloned",
    "agent_id": "release_writer_copy",
    "source_agent_id": "release_writer",
}
_AGENT_RESTORE_RESPONSE_EXAMPLE = {
    "status": "restored",
    "agent_id": "release_writer",
    "restored_from_entry_id": "188",
    "validation": {"valid": True, "errors": [], "warnings": []},
    "snapshot": {"agent_id": "release_writer", "display_name": "Release Writer"},
}


def register_agent_history_routes(
    router: APIRouter,
    *,
    get_db: Callable[[], Any],
    get_registry: Callable[[], Any],
    get_agent_validation: Callable[[Any, str], dict],
    get_scope: Callable[[Request], Any],
    assert_in_scope: Callable[[dict, Any, str], None],
    clone_agent_command: Callable[..., dict[str, Any]],
    restore_agent_command: Callable[..., Any] | None = None,
) -> None:
    if restore_agent_command is None:
        restore_agent_command = AgentCommandService(
            get_db=service_db_getter(get_db),
            reload_registry=lambda: get_registry().reload_all(),
            get_agent_validation=lambda db, agent_id, **_kwargs: get_agent_validation(db, agent_id),
        ).restore_agent_revision

    @router.post(
        "/{agent_id}/clone",
        response_model=AgentMutationResultView,
        responses={
            200: {"content": {"application/json": {"example": _AGENT_CLONE_RESPONSE_EXAMPLE}}},
            400: {"description": "Bad request"},
            403: {"description": "Forbidden"},
            404: {"description": "Not found"},
            409: {"description": "Conflict"},
            500: {"description": "Internal server error"},
            503: {"description": "Service unavailable"},
        },
    )
    def clone_agent(
        agent_id: str,
        request: Request,
        body: Annotated[
            AgentCloneRequest,
            Body(
                openapi_examples={
                    "cloneAgent": {
                        "summary": "Clone an agent under a new identifier.",
                        "value": _AGENT_CLONE_REQUEST_EXAMPLE,
                    }
                }
            ),
        ],
    ):
        """Clone an existing agent into a new agent_id."""
        scope = get_scope(request)
        try:
            return clone_agent_command(
                source_agent_id=agent_id,
                target_agent_id=body.agent_id,
                display_name=body.display_name,
                author=scope.actor_name,
                scope=scope,
            )
        except ServiceError as exc:
            if int(exc.status_code or 500) >= 500:
                return safe_error_response(logger, exc, message="Failed to clone agent")
            detail = exc.payload if set(exc.payload) != {"error"} else str(exc)
            raise HTTPException(status_code=exc.status_code, detail=detail) from exc
        except Exception as exc:
            return safe_error_response(logger, exc, message="Failed to clone agent")

    @router.get(
        "/{agent_id}/history",
        response_model=list[AgentRecordView],
        responses={
            400: {"description": "Bad request"},
            403: {"description": "Forbidden"},
            404: {"description": "Not found"},
            409: {"description": "Conflict"},
            500: {"description": "Internal server error"},
            503: {"description": "Service unavailable"},
        },
    )
    def get_agent_history(
        agent_id: str, request: Request, limit: Annotated[int, Query(ge=1, le=200)] = 50
    ):
        db = get_db()
        source = db.get_agent(agent_id)
        if not source:
            raise HTTPException(status_code=404, detail=f"Agent '{agent_id}' not found")
        assert_in_scope(source, get_scope(request), detail=f"Agent '{agent_id}' not found")
        return db.get_entity_history(entity_type="agent", entity_id=agent_id, limit=limit)

    @router.get(
        "/{agent_id}/history/{revision}",
        response_model=AgentRecordView,
        responses={
            400: {"description": "Bad request"},
            403: {"description": "Forbidden"},
            404: {"description": "Not found"},
            409: {"description": "Conflict"},
            500: {"description": "Internal server error"},
            503: {"description": "Service unavailable"},
        },
    )
    def get_agent_revision(agent_id: str, revision: int, request: Request):
        db = get_db()
        source = db.get_agent(agent_id)
        if not source:
            raise HTTPException(status_code=404, detail=f"Agent '{agent_id}' not found")
        assert_in_scope(source, get_scope(request), detail=f"Agent '{agent_id}' not found")
        entry = db.get_entity_revision(entity_type="agent", entity_id=agent_id, revision=revision)
        if not entry:
            raise HTTPException(
                status_code=404, detail=f"Revision {revision} not found for agent '{agent_id}'"
            )
        return entry

    @router.get(
        "/{agent_id}/diff",
        response_model=AgentDiffView,
        responses={
            400: {"description": "Bad request"},
            403: {"description": "Forbidden"},
            404: {"description": "Not found"},
            409: {"description": "Conflict"},
            500: {"description": "Internal server error"},
            503: {"description": "Service unavailable"},
        },
    )
    def get_agent_diff(
        agent_id: str,
        request: Request,
        from_entry_id: Annotated[str | None, Query()] = None,
        to_entry_id: Annotated[str | None, Query()] = None,
    ):
        db = get_db()
        source = db.get_agent(agent_id)
        if not source:
            raise HTTPException(status_code=404, detail=f"Agent '{agent_id}' not found")
        assert_in_scope(source, get_scope(request), detail=f"Agent '{agent_id}' not found")
        try:
            return db.diff_entity_history(
                entity_type="agent",
                entity_id=agent_id,
                from_entry_id=from_entry_id,
                to_entry_id=to_entry_id,
            )
        except KeyError as e:
            raise HTTPException(status_code=404, detail=str(e))

    @router.post(
        "/{agent_id}/restore",
        response_model=AgentMutationResultView,
        responses={
            200: {"content": {"application/json": {"example": _AGENT_RESTORE_RESPONSE_EXAMPLE}}},
            400: {"description": "Bad request"},
            403: {"description": "Forbidden"},
            404: {"description": "Not found"},
            409: {"description": "Conflict"},
            500: {"description": "Internal server error"},
            503: {"description": "Service unavailable"},
        },
    )
    def restore_agent_revision(
        agent_id: str,
        request: Request,
        body: Annotated[
            RestoreRevisionRequest,
            Body(
                openapi_examples={
                    "restoreAgent": {
                        "summary": "Restore a previously recorded agent revision.",
                        "value": _AGENT_RESTORE_REQUEST_EXAMPLE,
                    }
                }
            ),
        ],
    ):
        # The service checks account scope and attributes the change to the actor.
        scope = get_scope(request)
        try:
            return restore_agent_command(agent_id=agent_id, entry_id=body.entry_id, scope=scope)
        except ServiceError as exc:
            raise_service_http_error(exc)
