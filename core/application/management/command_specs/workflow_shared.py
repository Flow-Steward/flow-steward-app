"""Fields and inputs shared by the workflow command families."""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, ConfigDict, field_validator

from core.application.management.command_specs.contracts import FieldSpec, FieldType


class WorkflowTargetCommandInput(BaseModel):
    """Validated input for one project workflow target."""

    model_config = ConfigDict(extra="forbid")

    project_id: str
    workflow_id: str

    @field_validator("workflow_id")
    @classmethod
    def require_workflow_id(cls, value: str) -> str:
        normalized = str(value or "").strip()
        if not normalized:
            raise ValueError("workflow_id is required")
        return normalized


def _mapping_value(value: Any) -> dict[str, str]:
    """A ``name → name`` mapping, given as an object or as its JSON text (CLI)."""
    if isinstance(value, str):
        value = json.loads(value or "{}")
    if not isinstance(value, dict) or not all(
        isinstance(key, str) and isinstance(item, str) for key, item in value.items()
    ):
        raise ValueError("A mapping is a JSON object of names to names")
    return dict(value)


PROJECT_ID_FIELD = FieldSpec(
    key="project_id",
    label="Project",
    type=FieldType.SINGLE_SELECT,
    required=True,
    description="Project whose linked workflows or entrypoints should be queried.",
    examples=("proj_01J8K4M2Q7",),
    choice_provider="projects.available",
    option_name="--project-id",
)

WORKFLOW_ID_FIELD = FieldSpec(
    key="workflow_id",
    label="Workflow id",
    type=FieldType.RUNTIME_IDENTIFIER,
    required=True,
    description="Identifier of the linked workflow to inspect.",
    examples=("release-report",),
    positional=True,
)


__all__ = [
    "PROJECT_ID_FIELD",
    "WORKFLOW_ID_FIELD",
    "WorkflowTargetCommandInput",
    "_mapping_value",
]
