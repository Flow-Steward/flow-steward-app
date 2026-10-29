"""
Agent manifest — describes a worker that connects tools and skills via prompts.
Most agents need zero Python code; they are fully defined by their manifest
and executed by the generic PromptAgent class.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from core.application.registry.builder_metadata import normalize_builder_metadata

AGENT_PHASES_REMOVED_MESSAGE = (
    "Agent field 'phases' is not accepted: workflows have no phases, and an agent runs "
    "wherever a step names it"
)


def refuse_agent_phases(data: Any) -> None:
    """An agent manifest from the phase era is an error, never silently ignored."""
    if isinstance(data, Mapping) and "phases" in data:
        raise ValueError(AGENT_PHASES_REMOVED_MESSAGE)


@dataclass
class AgentPrompts:
    system: str  # agent identity — NEVER overridden
    skill_prompts: dict[str, str]  # skill_id → prompt text
    local_instructions: list[str] = field(default_factory=list)  # agent-only guidance
    context_template: str = ""  # how to assemble user message
    output_format: str = ""  # retained prompt field for stored manifest payloads
    context_sections: dict[str, list[str]] = field(
        default_factory=dict
    )  # agent/skill → context sections
    examples: dict[str, list[dict]] = field(
        default_factory=dict
    )  # skill_id → [{input, output, notes}]

    @classmethod
    def from_dict(cls, d: dict) -> AgentPrompts:
        return cls(
            system=d.get("system", ""),
            skill_prompts=d.get("skill_prompts", {}),
            local_instructions=list(d.get("local_instructions") or []),
            context_template=d.get("context_template", ""),
            output_format=d.get("output_format", ""),
            context_sections=d.get("context_sections", {}),
            examples=d.get("examples", {}),
        )


@dataclass
class AgentManifest:
    agent_id: str
    version: str
    display_name: str
    description: str
    enabled: bool
    prompts: AgentPrompts
    base_skill: str  # primary skill executor for this agent
    skills: list[str]  # skill_ids this agent can deliver
    tools: list[str]  # tool_ids this agent has access to
    secondary_skills: list[str] = field(default_factory=list)
    inputs: dict = field(
        default_factory=dict
    )  # snapshot/editor payload: {required: [...], optional: [...]}
    outputs: list[str] = field(default_factory=list)  # snapshot/editor payload output keys
    structured_inputs: list[dict] = field(
        default_factory=list
    )  # [{name, type, description, required}]
    structured_outputs: list[dict] = field(
        default_factory=list
    )  # [{name, type, description, required}]
    depends_on: dict = field(default_factory=dict)  # {soft: [...], hard: [...]}
    config: dict = field(default_factory=dict)  # model, max_iterations, timeout, etc.
    quality: dict = field(default_factory=dict)  # retry_on_failure, validation_hook
    builder_metadata: dict = field(default_factory=dict)
    category: str = ""

    @classmethod
    def from_db_row(cls, row: dict) -> AgentManifest:
        """Construct from a RegistryDBStore.get_agent() result."""
        prompts_raw = row.get("prompts") or {}
        if isinstance(prompts_raw, str):
            import json

            prompts_raw = json.loads(prompts_raw)

        # Skill prompts are stored canonically on registry_agent_skills.
        prompts_raw["skill_prompts"] = {
            cap["skill_id"]: cap.get("skill_prompt", "")
            for cap in row.get("_skills", [])
            if cap.get("skill_id")
        }
        if "local_instructions" not in prompts_raw:
            prompts_raw["local_instructions"] = list(row.get("local_instructions") or [])

        config = row.get("config") or {}
        if isinstance(config, str):
            import json

            config = json.loads(config)

        quality = row.get("quality") or {}
        if isinstance(quality, str):
            import json

            quality = json.loads(quality)

        inputs = row.get("inputs") or {}
        if isinstance(inputs, str):
            import json

            inputs = json.loads(inputs)

        depends_on = row.get("depends_on") or {}
        if isinstance(depends_on, str):
            import json

            depends_on = json.loads(depends_on)

        ordered_skill_ids = [c["skill_id"] for c in row.get("_skills", [])]
        base_skill = ordered_skill_ids[0] if ordered_skill_ids else ""
        secondary_skills = ordered_skill_ids[1:] if len(ordered_skill_ids) > 1 else []

        return cls(
            agent_id=row["agent_id"],
            version=row.get("version", "1.0.0"),
            display_name=row["display_name"],
            description=row.get("description", ""),
            enabled=row.get("enabled", True),
            prompts=AgentPrompts.from_dict(prompts_raw),
            base_skill=base_skill,
            secondary_skills=secondary_skills,
            skills=ordered_skill_ids,
            tools=[t["tool_id"] for t in row.get("_tools", [])],
            inputs=inputs,
            outputs=list(row.get("outputs") or []),
            structured_inputs=[
                {
                    "name": i["name"],
                    "type": i.get("type", "text"),
                    "description": i.get("description", ""),
                    "required": i.get("required", False),
                }
                for i in row.get("_inputs", [])
            ],
            structured_outputs=[
                {
                    "name": o["name"],
                    "type": o.get("type", "text"),
                    "description": o.get("description", ""),
                    "required": o.get("required", True),
                }
                for o in row.get("_outputs", [])
            ],
            depends_on=depends_on,
            config=config,
            quality=quality,
            builder_metadata=normalize_builder_metadata(row.get("builder_metadata_jsonb")),
            category=str(row.get("category") or ""),
        )

    def to_dict(self) -> dict:
        return {
            "agent_id": self.agent_id,
            "version": self.version,
            "display_name": self.display_name,
            "description": self.description,
            "enabled": self.enabled,
            "prompts": {
                "system": self.prompts.system,
                "skill_prompts": self.prompts.skill_prompts,
                "local_instructions": self.prompts.local_instructions,
                "context_template": self.prompts.context_template,
                "output_format": self.prompts.output_format,
                "context_sections": self.prompts.context_sections,
                "examples": self.prompts.examples,
            },
            "base_skill": self.base_skill,
            "secondary_skills": self.secondary_skills,
            "skills": self.skills,
            "tools": self.tools,
            "inputs": self.inputs,
            "outputs": self.outputs,
            "structured_inputs": self.structured_inputs,
            "structured_outputs": self.structured_outputs,
            "depends_on": self.depends_on,
            "config": self.config,
            "quality": self.quality,
            "builder_metadata_jsonb": normalize_builder_metadata(self.builder_metadata),
            "category": self.category,
        }

    def has_skill(self, skill_id: str) -> bool:
        return skill_id in self.skills

    def has_skill_prompt(self, skill_id: str) -> bool:
        return skill_id in self.prompts.skill_prompts

    @property
    def model(self) -> str:
        return self.config.get("model", "claude-sonnet-4-6")

    @property
    def max_iterations(self) -> int:
        return self.config.get("max_iterations", 3)

    @property
    def timeout_minutes(self) -> int:
        return self.config.get("timeout_minutes", 30)
