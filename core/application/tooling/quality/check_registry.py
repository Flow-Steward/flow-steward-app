"""Typed ownership registry for quality-gate checks."""

from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator


class CheckDefinition(BaseModel):
    """One independently fingerprintable quality check."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(min_length=1, pattern=r"^[a-z0-9][a-z0-9-]*$")
    owner_tier: Literal[1, 2, 3]
    command: tuple[str, ...] = Field(min_length=1)
    inputs: tuple[str, ...] = Field(min_length=1)
    input_mode: Literal["content", "paths"] = "content"
    runtime_inputs: tuple[str, ...] = ()
    prerequisites: tuple[str, ...] = ()
    repeat_count: int = Field(default=1, ge=1)
    repeat_reason: str = ""
    trusted_ci_required: bool = False
    selection_required: bool = False
    execution: Literal["orchestrator", "external"] = "orchestrator"
    # "commit": evidence counts only for the commit that produced it.
    # "inputs": evidence counts for any commit whose fingerprint matches. Only for
    # checks whose `inputs` name every file the tool reads, so the fingerprint is
    # the whole proof; a later commit that leaves them untouched skips the run.
    reuse_scope: Literal["commit", "inputs"] = "commit"

    @model_validator(mode="after")
    def validate_repetition(self) -> Self:
        if self.repeat_count > 1 and not self.repeat_reason.strip():
            raise ValueError("repeat_reason is required when repeat_count is greater than one")
        if self.repeat_count == 1 and self.repeat_reason:
            raise ValueError("repeat_reason requires repeat_count greater than one")
        return self


class CheckRegistry(BaseModel):
    """Canonical collection of quality checks and their owning tiers."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1]
    checks: tuple[CheckDefinition, ...]

    def ids_for_tier(self, tier: int) -> tuple[str, ...]:
        """Return check ids owned by ``tier`` in declared execution order."""
        return tuple(check.id for check in self.checks if check.owner_tier == tier)

    def validate_contract(self) -> None:
        """Reject ambiguous ownership and dangling prerequisite references."""
        counts = Counter(check.id for check in self.checks)
        duplicates = sorted(check_id for check_id, count in counts.items() if count > 1)
        if duplicates:
            raise ValueError(f"duplicate check ids: {', '.join(duplicates)}")

        known_ids = set(counts)
        for check in self.checks:
            missing = sorted(set(check.prerequisites) - known_ids)
            if missing:
                raise ValueError(f"{check.id}: unknown prerequisites: {', '.join(missing)}")
            if check.id in check.prerequisites:
                raise ValueError(f"{check.id}: check cannot depend on itself")


def load_check_registry(path: Path) -> CheckRegistry:
    """Load and validate a registry from a UTF-8 JSON file."""
    registry = CheckRegistry.model_validate_json(path.read_text(encoding="utf-8"))
    registry.validate_contract()
    return registry
