"""Request bodies shared by every workflow authoring transport (HTTP, MCP, CLI).

The HTTP routes and the CLI commands validate the same models, so a body one
transport accepts is never refused by another for a different reason.
"""

from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

_WORKFLOW_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")


def validate_workflow_id(value: str | None, *, field_name: str) -> str | None:
    """A new workflow id: lowercase letters, digits and underscores."""
    if value is None:
        return None
    normalized = str(value).strip()
    if not _WORKFLOW_ID_PATTERN.fullmatch(normalized):
        raise ValueError(f"{field_name} must match [a-z][a-z0-9_]* (lowercase, no spaces)")
    return normalized


class WorkflowDraftInput(BaseModel):
    """The existing YAML bundle and bounded application-owned resolutions."""

    model_config = ConfigDict(extra="forbid")
    bundle_yaml: str
    resolutions: list[dict[str, Any]] | None = None


class WorkflowChangeSet(BaseModel):
    """One atomic set of definition changes against the revision the caller loaded."""

    model_config = ConfigDict(extra="forbid")

    expected_revision: int = Field(ge=1)
    operations: list[dict[str, Any]] = Field(min_length=1)
    dry_run: bool = Field(
        default=False,
        description="Judge the set exactly as Apply would and save nothing.",
    )


class WorkflowImportRequest(BaseModel):
    bundle_yaml: str | None = None
    yaml: str | None = None
    target_workflow_id: str | None = None
    #: The project the operator chose to import into. Absent for callers that
    #: never pick one, which keeps the request scope's project as before.
    project_id: str | None = None
    resolutions: list[dict] = []

    @field_validator("resolutions", mode="before")
    @classmethod
    def validate_resolutions(cls, value: object) -> object:
        # `{}` is the natural first guess for "no resolutions"; the bare
        # "Input should be a valid list" never said what was wanted instead.
        if value is None:
            return []
        if not isinstance(value, list):
            raise ValueError(
                "resolutions must be a list of resolution objects; send [] when there "
                "is nothing to resolve"
            )
        return value

    @field_validator("target_workflow_id")
    @classmethod
    def validate_target_workflow_id(cls, value: str | None) -> str | None:
        if value is None or not str(value).strip():
            return None
        return validate_workflow_id(value, field_name="target_workflow_id")


__all__ = [
    "WorkflowChangeSet",
    "WorkflowDraftInput",
    "WorkflowImportRequest",
    "validate_workflow_id",
]
