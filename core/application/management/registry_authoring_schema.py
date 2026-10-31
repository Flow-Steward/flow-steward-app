"""What a client may send to create or change an agent or a skill.

The JSON Schemas come from the request models the admin routes validate with,
so they cannot drift from what the routes accept. The semantics lists describe
the behaviour of :class:`AgentCommandService` and :class:`SkillCommandService`
as it is; they document it and change nothing.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from .command_models_registry_agents_skills import (
    AgentPatch,
    AgentSave,
    InputItem,
    OutputItem,
    SkillCreate,
    SkillPatch,
)

_NAME_CONFLICT = (
    "A display_name that an agent/skill visible to the caller already uses (compared "
    "case-insensitively, trimmed, inner whitespace collapsed) is refused with 409 "
    "registry_name_conflict: the payload lists the visible matches with where they are "
    "used and a suggested_display_name. Keeping the current name is never refused."
)
_LAST_WRITE_WINS = (
    "There is no version check: the last write wins. Read the object right before "
    "changing it, and after an uncertain result read it again instead of retrying blindly."
)

_AGENT_CREATE_RULES = [
    "agent_id is required and must match [a-z][a-z0-9_]*.",
    "A taken agent_id (also by an object the caller cannot see, or one in Trash) is "
    "refused with 409 registry_id_conflict and nothing is changed; suggested_id is only "
    "a hint and is not reserved. Nothing is ever renamed automatically.",
    _NAME_CONFLICT,
    "Omitted fields take the defaults shown in the schema; enabled defaults to false.",
    "An empty config.provider/config.model means the agent inherits the project route.",
    "Saving an enabled agent requires a valid readiness result; otherwise 400 "
    "'Contract validation failed' with the issues, and nothing is saved.",
]
_AGENT_PATCH_RULES = [
    "Only the fields present in the request change; omitted fields keep their stored values.",
    "null never clears a field: a null value is ignored like an omitted one.",
    "config and prompts merge shallowly into the stored objects: the keys sent replace "
    "those keys, every other stored key stays.",
    "An empty config.provider or config.model removes that route field, so the agent "
    "inherits the project's provider/model route.",
    "local_instructions replaces prompts.local_instructions as a whole list; every other "
    "list or object field replaces the stored value as a whole.",
    "skills and tools are refused here; assign skills with the skill assignment routes.",
    "enabled is the desired state. Setting it to false is refused with 409 while enabled "
    "workflows run the agent. When the saved agent is enabled, it must pass readiness, "
    "otherwise 400 'Contract validation failed' and nothing is saved.",
    _NAME_CONFLICT,
    _LAST_WRITE_WINS,
]
_SKILL_CREATE_RULES = [
    "skill_id is required. A taken skill_id (also by an object the caller cannot see, or "
    "one in Trash) is refused with 409 registry_id_conflict and nothing is changed; "
    "suggested_id is only a hint and is not reserved. Nothing is ever renamed automatically.",
    _NAME_CONFLICT,
    "Omitted fields take the defaults shown in the schema; enabled defaults to false.",
    "expected_inputs/expected_outputs items follow the contract item schemas below.",
    "Saving an enabled skill requires a valid readiness result; otherwise 400 "
    "'Contract validation failed' with the issues, and nothing is saved.",
]
_SKILL_PATCH_RULES = [
    "Only the fields present in the request change; omitted fields keep their stored values.",
    "null never clears a field: a null value is ignored like an omitted one.",
    "Every field sent replaces the stored value as a whole; objects are not merged.",
    "expected_inputs/expected_outputs are not part of the patch; replace them with the "
    "skill inputs/outputs routes.",
    "enabled is the desired state. Setting it to false is refused with 409 while enabled "
    "workflows depend on an agent using the skill. When the saved skill is enabled, it "
    "must pass readiness, otherwise 400 'Contract validation failed' and nothing is saved.",
    _NAME_CONFLICT,
    _LAST_WRITE_WINS,
]


class RegistryAuthoringSchemaView(BaseModel):
    """Request schemas and change semantics for one registry object type."""

    entity_type: Literal["agent", "skill"] = Field(description="The object type described.")
    create: dict[str, Any] = Field(
        description="Create route, the JSON Schema of its body and its rules."
    )
    patch: dict[str, Any] = Field(
        description="Partial-update route, the JSON Schema of its body and its semantics."
    )
    related: dict[str, Any] = Field(
        description="Readiness, enablement and relationship routes for this object type."
    )


def agent_authoring_schema() -> RegistryAuthoringSchemaView:
    """Schemas and rules for creating and changing an agent."""
    return RegistryAuthoringSchemaView(
        entity_type="agent",
        create={
            "route": "POST /api/agents",
            "json_schema": AgentSave.model_json_schema(),
            "rules": list(_AGENT_CREATE_RULES),
        },
        patch={
            "route": "PATCH /api/agents/{agent_id}",
            "json_schema": AgentPatch.model_json_schema(),
            "semantics": list(_AGENT_PATCH_RULES),
        },
        related={
            "read": "GET /api/agents/{agent_id} (disabled agents included)",
            "readiness": "GET /api/agents/{agent_id}/contract-validation",
            "enable": "PATCH /api/agents/{agent_id} with {'enabled': true|false}",
            "assign_skill": "POST /api/agents/{agent_id}/skills {skill_id, skill_prompt}",
            "unassign_skill": "DELETE /api/agents/{agent_id}/skills/{skill_id}",
            "assignment_instructions": (
                "PUT /api/agents/{agent_id}/skills/{skill_id}/prompt {skill_prompt}"
            ),
        },
    )


def skill_authoring_schema() -> RegistryAuthoringSchemaView:
    """Schemas and rules for creating and changing a skill."""
    return RegistryAuthoringSchemaView(
        entity_type="skill",
        create={
            "route": "POST /api/skills",
            "json_schema": SkillCreate.model_json_schema(),
            "rules": list(_SKILL_CREATE_RULES),
        },
        patch={
            "route": "PATCH /api/skills/{skill_id}",
            "json_schema": SkillPatch.model_json_schema(),
            "semantics": list(_SKILL_PATCH_RULES),
        },
        related={
            "read": "GET /api/skills/{skill_id} (disabled skills included)",
            "readiness": "GET /api/skills/{skill_id}/contract-validation",
            "enable": "PATCH /api/skills/{skill_id} with {'enabled': true|false}",
            "inputs": {
                "route": "PUT /api/skills/{skill_id}/inputs (replaces the whole list)",
                "item_json_schema": InputItem.model_json_schema(),
            },
            "outputs": {
                "route": "PUT /api/skills/{skill_id}/outputs (replaces the whole list)",
                "item_json_schema": OutputItem.model_json_schema(),
            },
        },
    )


__all__ = [
    "RegistryAuthoringSchemaView",
    "agent_authoring_schema",
    "skill_authoring_schema",
]
