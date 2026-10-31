"""Request models for admin agent routes."""

from __future__ import annotations

from pydantic import BaseModel, field_validator

from core.application.registry.prompt_contracts import validate_skill_prompt


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
