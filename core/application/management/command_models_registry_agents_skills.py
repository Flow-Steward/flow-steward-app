"""Request models for creating and changing agents and skills.

The admin HTTP routes validate their bodies with these models, and the
registry authoring schema publishes their JSON Schema, so a client that reads
the schema sees exactly what the routes accept.
"""

from __future__ import annotations

import re
from typing import Annotated, Any

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from core.application.registry.agent_manifest import refuse_agent_phases
from core.application.registry.catalog_categories import CatalogCategory

_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")


class AgentSave(BaseModel):
    @model_validator(mode="before")
    @classmethod
    def _reject_phases(cls, values: Any) -> Any:
        refuse_agent_phases(values)
        return values

    agent_id: str
    version: str = "1.0.0"
    display_name: str
    category: CatalogCategory = ""
    base_skill: str = ""
    secondary_skills: list[str] = []
    local_instructions: list[str] = []

    @field_validator("agent_id")
    @classmethod
    def validate_agent_id(cls, v: str) -> str:
        if not _ID_PATTERN.match(v):
            raise ValueError("agent_id must match [a-z][a-z0-9_]* (lowercase, no spaces)")
        return v

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
AGENT_RELATIONSHIP_FIELDS = {
    "skills": "/api/agents/{agent_id}/skills",
    "tools": "/api/agents/{agent_id}/tools",
}


class AgentPatch(BaseModel):
    """Partial update — only provided fields are changed."""

    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="before")
    @classmethod
    def _reject_relationship_fields(cls, values: Any) -> Any:
        if not isinstance(values, dict):
            return values
        refuse_agent_phases(values)
        named = [name for name in AGENT_RELATIONSHIP_FIELDS if name in values]
        if named:
            routes = ", ".join(AGENT_RELATIONSHIP_FIELDS[name] for name in named)
            raise ValueError(
                f"{' and '.join(named)} cannot be changed through this patch; use {routes}"
            )
        return values

    base_skill: str | None = None
    secondary_skills: list[str] | None = None
    local_instructions: list[str] | None = None
    display_name: str | None = None
    category: CatalogCategory | None = None
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


class SkillSave(BaseModel):
    skill_id: str | None = None
    version: str = "1.0.0"
    display_name: str
    description: str = ""
    enabled: bool = False
    category: CatalogCategory = ""
    skill_type: str = "secondary"
    instructions: list[str] = []
    prompt_template: str = ""
    expected_inputs: list[dict] = []
    expected_outputs: list[dict] = []
    input_schema: dict = {}
    output_schema: dict = {}
    examples: list[dict] = []
    builder_metadata_jsonb: dict = {}
    tags: list[str] = []


class SkillCreate(SkillSave):
    skill_id: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)] = Field(...)


class SkillPatch(BaseModel):
    display_name: str | None = None
    description: str | None = None
    enabled: bool | None = None
    category: CatalogCategory | None = None
    skill_type: str | None = None
    instructions: list[str] | None = None
    prompt_template: str | None = None
    input_schema: dict | None = None
    output_schema: dict | None = None
    examples: list[dict] | None = None
    builder_metadata_jsonb: dict | None = None
    tags: list[str] | None = None


class InputItem(BaseModel):
    name: str
    type: str = "text"
    description: str = ""
    required: bool = False
    contract_id: str | None = None


class OutputItem(BaseModel):
    name: str
    type: str = "text"
    description: str = ""
    required: bool = True
    contract_id: str | None = None


__all__ = [
    "AGENT_RELATIONSHIP_FIELDS",
    "AgentPatch",
    "AgentSave",
    "InputItem",
    "OutputItem",
    "SkillCreate",
    "SkillPatch",
    "SkillSave",
]
