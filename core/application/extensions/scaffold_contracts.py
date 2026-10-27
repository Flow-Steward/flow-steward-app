"""Transport-safe extension scaffold command and result contracts."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, field_validator

from core.application.extensions.identity import normalize_extension_id


class ExtensionScaffoldError(Exception):
    """Base error for extension scaffold creation."""


class ExtensionScaffoldConflictError(ExtensionScaffoldError):
    """Raised when a scaffold target already exists."""


class ExtensionScaffoldCreateInput(BaseModel):
    """Validated command for creating an extension scaffold."""

    model_config = ConfigDict(extra="forbid")

    extension_id: str
    output_root: Path
    display_name: str = ""
    description: str = ""
    vendor_name: str = "Your Organization"
    category: str = "other"

    @field_validator("extension_id")
    @classmethod
    def validate_extension_id(cls, value: str) -> str:
        return normalize_extension_id(value)


class ExtensionScaffoldResult(BaseModel):
    """Result returned after an extension scaffold is created."""

    status: Literal["created"]
    extension_id: str
    path: str
    files_created: int


__all__ = [
    "ExtensionScaffoldConflictError",
    "ExtensionScaffoldCreateInput",
    "ExtensionScaffoldError",
    "ExtensionScaffoldResult",
]
