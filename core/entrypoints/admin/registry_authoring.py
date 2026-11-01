"""Read-only routes that publish the agent and skill request schemas."""

from __future__ import annotations

from fastapi import APIRouter

from core.application.management.registry_authoring_schema import (
    RegistryAuthoringSchemaView,
    agent_authoring_schema,
    skill_authoring_schema,
)

router = APIRouter(tags=["registry-authoring"])

_RESPONSES: dict[int | str, dict[str, str]] = {
    401: {"description": "Authentication required"},
    403: {"description": "Forbidden"},
}


@router.get(
    "/api/agent-authoring/schema",
    responses=_RESPONSES,
)
def get_agent_authoring_schema() -> RegistryAuthoringSchemaView:
    """Body schemas and change semantics for creating and patching agents."""
    return agent_authoring_schema()


@router.get(
    "/api/skill-authoring/schema",
    responses=_RESPONSES,
)
def get_skill_authoring_schema() -> RegistryAuthoringSchemaView:
    """Body schemas and change semantics for creating and patching skills."""
    return skill_authoring_schema()


__all__ = ["router"]
