# ruff: noqa: B904
# mypy: ignore-errors
"""History, clone, and restore routes for admin agents."""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Annotated, Any

from fastapi import APIRouter, Body, HTTPException, Query, Request

from core.application.management.command_models import AgentMutationResultView
from core.application.management.query_models import AgentDiffView, AgentRecordView
from core.application.registry.contract_validation import format_validation_error_detail
from core.application.registry.db_store import RestoreValidationError
from core.application.registry.identifier_keys import unique_text_key
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
) -> None:
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
        db = get_db()
        source = db.get_agent(agent_id)
        if not source:
            raise HTTPException(status_code=404, detail=f"Agent '{agent_id}' not found")
        assert_in_scope(source, get_scope(request), detail=f"Agent '{agent_id}' not found")
        target_id = unique_text_key(
            body.agent_id or f"{agent_id}_copy",
            lambda candidate: db.get_agent(candidate) is not None,
        )

        clone_data = {
            "agent_id": target_id,
            "account_id": source.get("account_id"),
            "display_name": body.display_name or f"{source.get('display_name', agent_id)} (Copy)",
            "description": source.get("description", ""),
            "agent_type_id": source.get("agent_type_key") or source.get("agent_type_id"),
            "prompts": source.get("prompts") or {},
            "skills": [
                c.get("skill_id") if isinstance(c, dict) else c
                for c in (source.get("skills") or [])
            ],
            "tools": [
                t.get("tool_id") if isinstance(t, dict) else t for t in (source.get("tools") or [])
            ],
            "inputs": source.get("inputs") or {"required": [], "optional": []},
            "outputs": list(source.get("outputs") or []),
            "depends_on": source.get("depends_on") or {"soft": [], "hard": []},
            "config": dict(source.get("config") or {}),
            "quality": dict(source.get("quality") or {}),
            "enabled": False,
            "version": source.get("version", "1.0.0"),
            "tags": list(source.get("tags") or []),
        }
        try:
            db.save_agent(clone_data, author=get_scope(request).actor_name)
        except Exception as e:
            return safe_error_response(logger, e, message="Failed to clone agent")
        get_registry().reload_all()
        return {"status": "cloned", "agent_id": target_id, "source_agent_id": agent_id}

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
        db = get_db()
        source = db.get_agent(agent_id)
        if not source:
            raise HTTPException(status_code=404, detail=f"Agent '{agent_id}' not found")
        assert_in_scope(source, get_scope(request), detail=f"Agent '{agent_id}' not found")
        try:
            outcome = db.restore_entity_revision(
                entity_type="agent",
                entity_id=agent_id,
                entry_id=str(body.entry_id),
                author=f"{get_scope(request).actor_name}_restore",
                validate_restored=lambda _entity_type, _entity_id, _restored: get_agent_validation(
                    db, agent_id
                ),
            )
        except RestoreValidationError as e:
            validation = (
                e.validation
                if isinstance(e.validation, dict)
                else {
                    "valid": False,
                    "errors": [],
                    "warnings": [],
                }
            )
            raise HTTPException(
                status_code=400,
                detail=format_validation_error_detail(
                    entity_type="agent",
                    entity_id=agent_id,
                    action="restore",
                    validation=validation,
                ),
            )
        except KeyError as e:
            raise HTTPException(status_code=404, detail=str(e))
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

        validation = outcome.get("validation")
        if not isinstance(validation, dict):
            validation = get_agent_validation(db, agent_id)
        get_registry().reload_all()
        return {
            "status": "restored",
            "agent_id": agent_id,
            "restored_from_entry_id": str(body.entry_id),
            "validation": validation,
            "snapshot": outcome.get("restored"),
        }
