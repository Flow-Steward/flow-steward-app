# mypy: ignore-errors
"""Skill and tool relationship routes for admin agents."""

from __future__ import annotations

from collections.abc import Callable
from typing import Annotated, Any

from fastapi import APIRouter, Body, HTTPException, Request

from core.application.management.command_models import AgentMutationResultView
from core.application.management.errors import ServiceError
from core.application.management.query_models import (
    AgentSkillAssignmentView,
    AgentToolAssignmentView,
)
from core.entrypoints.admin._registry_scope import assert_row_in_account_scope, request_scope
from core.entrypoints.admin._service_errors import raise_service_http_error
from core.entrypoints.admin.agents_models import (
    AgentRelationshipsReplace,
    PromptUpdate,
    ReorderRequest,
    SkillAdd,
    ToolAdd,
)

_AGENT_PROMPT_REQUEST_EXAMPLE = {
    "skill_prompt": "Prefer customer-facing wording and highlight risks."
}
_AGENT_PROMPT_RESPONSE_EXAMPLE = {
    "status": "updated",
    "agent_id": "release_writer",
}


def register_agent_relationship_routes(
    router: APIRouter,
    *,
    get_db: Callable[[], Any],
    get_agent_commands: Callable[[], Any],
) -> None:
    def _require_agent_in_scope(agent_id: str, request: Request) -> None:
        """Load the agent and enforce caller account scope, else fail closed (404).

        These skill/tool relationship subroutes key only on ``agent_id``; without
        this guard a scoped admin could read or mutate another account's agent
        contract and capability mappings.
        """
        db = get_db()
        agent = db.get_agent(agent_id)
        if not agent:
            raise HTTPException(status_code=404, detail=f"Agent '{agent_id}' not found")
        assert_row_in_account_scope(
            agent, request_scope(request), detail=f"Agent '{agent_id}' not found"
        )

    # --------------------------------------------------------------------------- #
    # Skill relationships                                                     #
    # --------------------------------------------------------------------------- #

    @router.get(
        "/{agent_id}/skills",
        response_model=list[AgentSkillAssignmentView],
        responses={
            400: {"description": "Bad request"},
            403: {"description": "Forbidden"},
            404: {"description": "Not found"},
            409: {"description": "Conflict"},
            500: {"description": "Internal server error"},
            503: {"description": "Service unavailable"},
        },
    )
    def get_agent_skills(agent_id: str, request: Request):
        """Get skills assigned to an agent (with prompts and sort order)."""
        _require_agent_in_scope(agent_id, request)
        db = get_db()
        return db.get_agent_skills(agent_id)

    @router.post(
        "/{agent_id}/skills",
        response_model=list[AgentSkillAssignmentView],
        responses={
            400: {"description": "Bad request"},
            403: {"description": "Forbidden"},
            404: {"description": "Not found"},
            409: {"description": "Conflict"},
            500: {"description": "Internal server error"},
            503: {"description": "Service unavailable"},
        },
    )
    def add_agent_skill(agent_id: str, request: Request, body: SkillAdd):
        """Add a skill to an agent."""
        try:
            return get_agent_commands().add_agent_skill(
                agent_id, body.skill_id, body.skill_prompt, scope=request_scope(request)
            )
        except ServiceError as exc:
            raise_service_http_error(exc)

    @router.delete(
        "/{agent_id}/skills/{skill_id}",
        response_model=list[AgentSkillAssignmentView],
        responses={
            400: {"description": "Bad request"},
            403: {"description": "Forbidden"},
            404: {"description": "Not found"},
            409: {"description": "Conflict"},
            500: {"description": "Internal server error"},
            503: {"description": "Service unavailable"},
        },
    )
    def remove_agent_skill(agent_id: str, skill_id: str, request: Request):
        """Remove a skill from an agent."""
        try:
            return get_agent_commands().remove_agent_skill(
                agent_id, skill_id, scope=request_scope(request)
            )
        except ServiceError as exc:
            raise_service_http_error(exc)

    @router.put(
        "/{agent_id}/skills/reorder",
        response_model=list[AgentSkillAssignmentView],
        responses={
            400: {"description": "Bad request"},
            403: {"description": "Forbidden"},
            404: {"description": "Not found"},
            409: {"description": "Conflict"},
            500: {"description": "Internal server error"},
            503: {"description": "Service unavailable"},
        },
    )
    def reorder_agent_skills(agent_id: str, request: Request, body: ReorderRequest):
        """Reorder agent skills."""
        try:
            return get_agent_commands().reorder_agent_skills(
                agent_id, body.ordered_ids, scope=request_scope(request)
            )
        except ServiceError as exc:
            raise_service_http_error(exc)

    @router.put(
        "/{agent_id}/skills/{skill_id}/prompt",
        response_model=AgentMutationResultView,
        responses={
            200: {"content": {"application/json": {"example": _AGENT_PROMPT_RESPONSE_EXAMPLE}}},
            400: {"description": "Bad request"},
            403: {"description": "Forbidden"},
            404: {"description": "Not found"},
            409: {"description": "Conflict"},
            500: {"description": "Internal server error"},
            503: {"description": "Service unavailable"},
        },
    )
    def update_agent_skill_prompt(
        agent_id: str,
        skill_id: str,
        request: Request,
        body: Annotated[
            PromptUpdate,
            Body(
                openapi_examples={
                    "updateSkillPrompt": {
                        "summary": "Replace the per-skill prompt for one agent/skill mapping.",
                        "value": _AGENT_PROMPT_REQUEST_EXAMPLE,
                    }
                }
            ),
        ],
    ):
        """Update the per-skill prompt for an agent."""
        try:
            return get_agent_commands().update_agent_skill_prompt(
                agent_id, skill_id, body.skill_prompt, scope=request_scope(request)
            )
        except ServiceError as exc:
            raise_service_http_error(exc)

    # --------------------------------------------------------------------------- #
    # Tool relationships                                                           #
    # --------------------------------------------------------------------------- #

    @router.get(
        "/{agent_id}/tools",
        response_model=list[AgentToolAssignmentView],
        responses={
            400: {"description": "Bad request"},
            403: {"description": "Forbidden"},
            404: {"description": "Not found"},
            409: {"description": "Conflict"},
            500: {"description": "Internal server error"},
            503: {"description": "Service unavailable"},
        },
    )
    def get_agent_tools(agent_id: str, request: Request):
        """Get tools directly assigned to an agent."""
        _require_agent_in_scope(agent_id, request)
        db = get_db()
        return db.get_agent_tools(agent_id)

    @router.get(
        "/{agent_id}/effective-tools",
        response_model=list[AgentToolAssignmentView],
        responses={
            400: {"description": "Bad request"},
            403: {"description": "Forbidden"},
            404: {"description": "Not found"},
            409: {"description": "Conflict"},
            500: {"description": "Internal server error"},
            503: {"description": "Service unavailable"},
        },
    )
    def get_effective_tools(agent_id: str, request: Request):
        """Get all tools for an agent (direct assignments only)."""
        _require_agent_in_scope(agent_id, request)
        db = get_db()
        return db.get_effective_tools(agent_id)

    @router.put(
        "/{agent_id}/relationships",
        responses={
            400: {"description": "Invalid relationship request"},
            404: {"description": "Agent or relationship target not found"},
            409: {"description": "Relationship revision conflict"},
        },
    )
    def replace_agent_relationships(
        agent_id: str, request: Request, body: AgentRelationshipsReplace
    ):
        """Atomically replace the exact skill/tool snapshot with CAS protection."""
        try:
            return get_agent_commands().replace_agent_relationships(
                agent_id,
                expected_skill_ids=body.expected_skills,
                expected_tool_ids=body.expected_tools,
                skill_ids=body.skills,
                tool_ids=body.tools,
                scope=request_scope(request),
            )
        except ServiceError as exc:
            raise_service_http_error(exc)

    @router.post(
        "/{agent_id}/tools",
        response_model=list[AgentToolAssignmentView],
        responses={
            400: {"description": "Bad request"},
            403: {"description": "Forbidden"},
            404: {"description": "Not found"},
            409: {"description": "Conflict"},
            500: {"description": "Internal server error"},
            503: {"description": "Service unavailable"},
        },
    )
    def add_agent_tool(agent_id: str, request: Request, body: ToolAdd):
        """Add a tool to an agent."""
        try:
            return get_agent_commands().add_agent_tool(
                agent_id, body.tool_id, scope=request_scope(request)
            )
        except ServiceError as exc:
            raise_service_http_error(exc)

    @router.delete(
        "/{agent_id}/tools/{tool_id}",
        response_model=list[AgentToolAssignmentView],
        responses={
            400: {"description": "Bad request"},
            403: {"description": "Forbidden"},
            404: {"description": "Not found"},
            409: {"description": "Conflict"},
            500: {"description": "Internal server error"},
            503: {"description": "Service unavailable"},
        },
    )
    def remove_agent_tool(agent_id: str, tool_id: str, request: Request):
        """Remove a tool from an agent."""
        try:
            return get_agent_commands().remove_agent_tool(
                agent_id, tool_id, scope=request_scope(request)
            )
        except ServiceError as exc:
            raise_service_http_error(exc)
