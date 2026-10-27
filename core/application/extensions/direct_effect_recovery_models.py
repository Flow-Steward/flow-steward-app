"""Closed transport contracts for an existing direct-action effect."""

from __future__ import annotations

from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from core.application.management.query_models_common import ViewModel

PositiveVersion = Annotated[int, Field(strict=True, gt=0)]


class DirectEffectResolutionRef(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    command_id: str = Field(min_length=1, max_length=200)
    effect_id: str = Field(min_length=1, max_length=200)
    fenced_version: PositiveVersion
    approval_id: Literal[""] = ""

    @field_validator("effect_id")
    @classmethod
    def valid_effect_id(cls, value: str) -> str:
        return str(UUID(value))


class DirectEffectDecisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    observed_version: PositiveVersion
    action: Literal["approve", "reject"]
    comment: str = Field(min_length=1, max_length=4000)

    @field_validator("comment")
    @classmethod
    def trim_comment(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("A comment is required")
        return value


class DirectEffectSummary(ViewModel):
    model_config = ConfigDict(extra="forbid")

    effect_id: str
    observed_version: PositiveVersion
    operation_id: str
    status: str
    decision_allowed: bool
    retry_supported: bool
    retry_requires_original_request: Literal[True] = True
    resolution_ref: DirectEffectResolutionRef | None = None
    retry_unavailable_reason: Literal["retry_in_progress"] | None = None


class DirectEffectListView(ViewModel):
    model_config = ConfigDict(extra="forbid")

    effects: list[DirectEffectSummary]
    truncated: bool


class DirectEffectDecisionView(ViewModel):
    model_config = ConfigDict(extra="forbid")

    resolution_ref: DirectEffectResolutionRef
    action: Literal["approve", "reject"]
    observed_version: PositiveVersion
    fenced_version: PositiveVersion
