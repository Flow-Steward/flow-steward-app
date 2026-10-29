"""
Skill manifest — describes an outcome an agent can deliver.
A skill knows nothing about which agent delivers it.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from core.application.registry.builder_metadata import normalize_builder_metadata
from core.application.registry.catalog_categories import DEFAULT_CATALOG_CATEGORY


@dataclass
class SkillManifest:
    skill_id: str
    version: str
    display_name: str
    description: str
    enabled: bool
    category: str
    instructions: list[str]  # step-by-step guide for the LLM
    expected_inputs: list[dict]  # [{name, description, required}]
    expected_outputs: list[dict]  # [{name, description, required}]
    input_schema: dict  # canonical input contract schema
    output_schema: dict  # canonical output contract schema
    skill_type: str = "secondary"  # base | secondary
    prompt_template: str = ""
    examples: list[dict] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    builder_metadata: dict = field(default_factory=dict)

    @classmethod
    def from_db_row(cls, row: dict) -> SkillManifest:
        """Construct from a RegistryDBStore.get_skill() result."""
        expected_inputs = [
            {
                "name": i["name"],
                "type": i.get("type", "text"),
                "description": i.get("description", ""),
                "required": i.get("required", False),
                "contract_id": i.get("contract_id"),
            }
            for i in row.get("_inputs", [])
        ]
        expected_outputs = [
            {
                "name": o["name"],
                "type": o.get("type", "text"),
                "description": o.get("description", ""),
                "required": o.get("required", True),
                "contract_id": o.get("contract_id"),
            }
            for o in row.get("_outputs", [])
        ]
        return cls(
            skill_id=row["skill_id"],
            version=row.get("version", "1.0.0"),
            display_name=row["display_name"],
            description=row.get("description", ""),
            enabled=row.get("enabled", True),
            category=str(row.get("category") or DEFAULT_CATALOG_CATEGORY),
            skill_type=str(row.get("skill_type") or "secondary"),
            instructions=list(row.get("instructions") or []),
            prompt_template=str(row.get("prompt_template") or ""),
            expected_inputs=expected_inputs,
            expected_outputs=expected_outputs,
            input_schema=dict(row.get("input_schema") or {}),
            output_schema=dict(row.get("output_schema") or {}),
            examples=list(row.get("examples") or []),
            tags=list(row.get("tags") or []),
            builder_metadata=normalize_builder_metadata(row.get("builder_metadata_jsonb")),
        )

    def to_dict(self) -> dict:
        return {
            "skill_id": self.skill_id,
            "version": self.version,
            "display_name": self.display_name,
            "description": self.description,
            "enabled": self.enabled,
            "category": self.category,
            "skill_type": self.skill_type,
            "instructions": self.instructions,
            "prompt_template": self.prompt_template,
            "expected_inputs": self.expected_inputs,
            "expected_outputs": self.expected_outputs,
            "input_schema": self.input_schema,
            "output_schema": self.output_schema,
            "examples": self.examples,
            "tags": self.tags,
            "builder_metadata_jsonb": normalize_builder_metadata(self.builder_metadata),
        }
