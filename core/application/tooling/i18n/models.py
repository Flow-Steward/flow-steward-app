"""Typed request contracts for localization tooling operations."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, field_validator


class _LocalizationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


class LocalizationBootstrapCatalogRequest(_LocalizationRequest):
    source: str = "core/application/settings/ui_i18n.py"
    timeout_seconds: int = 300


class LocalizationRebuildPotRequest(_LocalizationRequest):
    timeout_seconds: int = 300


class LocalizationInitLocaleRequest(_LocalizationRequest):
    code: str
    label: str = ""
    native_label: str = ""
    timeout_seconds: int = 300

    @field_validator("code")
    @classmethod
    def require_code(cls, value: str) -> str:
        normalized = str(value or "").strip().lower()
        if not normalized:
            raise ValueError("code is required")
        return normalized


class LocalizationUpdateRequest(_LocalizationRequest):
    timeout_seconds: int = 300


class LocalizationCompileRequest(_LocalizationRequest):
    force: bool = False
    timeout_seconds: int = 300


class LocalizationValidateKeysRequest(_LocalizationRequest):
    timeout_seconds: int = 300
