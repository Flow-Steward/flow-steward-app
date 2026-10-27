# ruff: noqa: F401
from __future__ import annotations

import uuid
from typing import Any

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, model_validator

DRAFT_RELEASE_NOTES_VALUE = "Draft Release Notes"
GENERATE_A_RELEASE_READINESS_REPORT_FOR_VERSION_2_MESSAGE = (
    "Generate a release-readiness report for version 2.4."
)
MESSAGE_STEP_INTAKE_RELEASE_VERSION_VALUE = "message.step.intake.release_version"
RELEASE_REPORT_VALUE = "Release Report"
RELEASE_WRITER_VALUE = "Release Writer"
REVIEW_THE_INCOMING_RELEASE_REQUEST_MESSAGE = "Review the incoming release request."
VALUE_2026_05_27T09_12_00Z_VALUE = "2026-05-27T09:12:00Z"
VALUE_2026_05_27T09_12_08Z_VALUE = "2026-05-27T09:12:08Z"
VALUE_2026_05_28T12_00_00Z_VALUE = "2026-05-28T12:00:00Z"


class ViewModel(BaseModel):
    model_config = ConfigDict(extra="allow", coerce_numbers_to_str=True)

    @model_validator(mode="before")
    @classmethod
    def _normalize_uuid_payloads(cls, value: Any) -> Any:
        return cls._normalize_uuid_values(value)

    @classmethod
    def _normalize_uuid_values(cls, value: Any) -> Any:
        if isinstance(value, uuid.UUID):
            return str(value)
        if isinstance(value, dict):
            return {key: cls._normalize_uuid_values(item) for key, item in value.items()}
        if isinstance(value, list):
            return [cls._normalize_uuid_values(item) for item in value]
        if isinstance(value, tuple):
            return tuple(cls._normalize_uuid_values(item) for item in value)
        return value

    def dump(self) -> dict[str, Any]:
        return self.model_dump(mode="json")
