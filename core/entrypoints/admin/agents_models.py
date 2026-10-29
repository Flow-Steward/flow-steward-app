"""Request models for admin agent routes."""

from __future__ import annotations

import re

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from core.application.registry.agent_manifest import refuse_agent_phases
from core.application.registry.catalog_categories import catalog_category
from core.application.registry.prompt_contracts import validate_skill_prompt

_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")


class AgentSave(BaseModel):
    @model_validator(mode="before")
    @classmethod
    def _reject_phases(cls, values):
        refuse_agent_phases(values)
        return values

    agent_id: str
    version: str = "1.0.0"
    display_name: str
    category: str = ""
    base_skill: str = ""
    secondary_skills: list[str] = []
    local_instructions: list[str] = []

    @field_validator("agent_id")
    @classmethod
    def validate_agent_id(cls, v: str) -> str:
        if not _ID_PATTERN.match(v):
            raise ValueError("agent_id must match [a-z][a-z0-9_]* (lowercase, no spaces)")
        return v

    @field_validator("category")
    @classmethod
    def validate_category(cls, v: str) -> str:
        return catalog_category(v)

    description: str = ""
    enabled: bool = False
    prompts: dict = {}
    config: dict = {}
    quality: dict = {}
    inputs: dict = {"required": [], "optional": []}
    outputs: list[str] = []
    builder_metadata_jsonb: dict = {}
    depends_on: dict = {"soft": [], "hard": []}
    tags: list[str] = []
    # Relationships (for create/full-update)
    skills: list[dict] = []  # [{skill_id, skill_prompt, sort_order}]
    tools: list[str] = []


#: An agent's skills and tools are relationships with their own endpoints —
#: `/{agent_id}/skills`, `/{agent_id}/tools` and their reorder and prompt routes —
#: because order and per-binding settings cannot be expressed by replacing a
#: list. A PATCH carrying them used to drop them and answer `no_change`, so a
#: caller was told nothing had changed while believing it had asked for a change.
_AGENT_RELATIONSHIP_FIELDS = {
    "skills": "/api/agents/{agent_id}/skills",
    "tools": "/api/agents/{agent_id}/tools",
}


class AgentPatch(BaseModel):
    """Partial update — only provided fields are changed."""

    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="before")
    @classmethod
    def _reject_relationship_fields(cls, values):
        if not isinstance(values, dict):
            return values
        refuse_agent_phases(values)
        named = [name for name in _AGENT_RELATIONSHIP_FIELDS if name in values]
        if named:
            routes = ", ".join(_AGENT_RELATIONSHIP_FIELDS[name] for name in named)
            raise ValueError(
                f"{' and '.join(named)} cannot be changed through this patch; use {routes}"
            )
        return values

    base_skill: str | None = None
    secondary_skills: list[str] | None = None
    local_instructions: list[str] | None = None
    display_name: str | None = None
    category: str | None = None
    description: str | None = None
    enabled: bool | None = None
    prompts: dict | None = None
    config: dict | None = None
    quality: dict | None = None
    inputs: dict | None = None
    outputs: list[str] | None = None
    builder_metadata_jsonb: dict | None = None
    depends_on: dict | None = None
    tags: list[str] | None = None

    @field_validator("category")
    @classmethod
    def validate_category(cls, v: str | None) -> str | None:
        return None if v is None else catalog_category(v)


class SkillAdd(BaseModel):
    skill_id: str
    skill_prompt: str = ""

    @field_validator("skill_prompt")
    @classmethod
    def validate_skill_prompt_text(cls, v: str) -> str:
        errors = validate_skill_prompt(v)
        if errors:
            raise ValueError(errors[0])
        return v


class ToolAdd(BaseModel):
    tool_id: str


class PromptUpdate(BaseModel):
    skill_prompt: str

    @field_validator("skill_prompt")
    @classmethod
    def validate_skill_prompt_text(cls, v: str) -> str:
        errors = validate_skill_prompt(v)
        if errors:
            raise ValueError(errors[0])
        return v


class ReorderRequest(BaseModel):
    ordered_ids: list[str]


class AgentRelationshipsReplace(BaseModel):
    """Exact relationship snapshot used for transactional CAS replacement."""

    expected_skills: list[str]
    expected_tools: list[str]
    skills: list[str]
    tools: list[str]


class RestoreRevisionRequest(BaseModel):
    entry_id: str | int


class AgentCloneRequest(BaseModel):
    agent_id: str | None = None
    display_name: str | None = None


class AgentInputItem(BaseModel):
    name: str
    type: str = "text"
    description: str = ""
    required: bool = False


class AgentOutputItem(BaseModel):
    name: str
    type: str = "text"
    description: str = ""
    required: bool = True
