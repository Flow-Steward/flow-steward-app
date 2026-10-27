"""Transport-safe models for public database tooling commands."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class _DatabaseRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DatabaseOperationResult(BaseModel):
    """Structured result for public database tooling leaves."""

    model_config = ConfigDict(extra="forbid")

    operation: str
    status: str = "completed"
    changed: bool = False
    message: str = ""
    details: dict[str, Any] = Field(default_factory=dict)
    artifacts: list[str] = Field(default_factory=list)


class DatabaseStatusRequest(_DatabaseRequest):
    dsn: str = ""


class DatabaseUpgradeRequest(_DatabaseRequest):
    dsn: str = ""
    author: str = "system"
    maintenance_confirmed: bool = False


class DatabaseBootstrapSeedRequest(_DatabaseRequest):
    dsn: str = ""
    author: str = "system"


class DatabaseGenerateMigrationRequest(_DatabaseRequest):
    name: str
    root: str = "."

    @field_validator("name")
    @classmethod
    def require_name(cls, value: str) -> str:
        normalized = str(value or "").strip()
        if not normalized:
            raise ValueError("name is required")
        return normalized

    @field_validator("root", mode="before")
    @classmethod
    def normalize_root(cls, value: object) -> str:
        return str(value or ".")


class DatabaseQueryDiagnosticsRequest(_DatabaseRequest):
    dsn: str = ""
    limit: int = 15
    sort_by: Literal["mean", "total", "max", "calls"] = "mean"
    min_rows: int = 1000
    max_query_len: int = 140

    @field_validator("limit", "max_query_len")
    @classmethod
    def normalize_positive(cls, value: int) -> int:
        return max(1, int(value or 1))

    @field_validator("min_rows")
    @classmethod
    def normalize_min_rows(cls, value: int) -> int:
        return max(0, int(value or 0))


class DatabaseTuneEnvRequest(_DatabaseRequest):
    memory_gb: float | None = None
    max_connections: int = 100
    storage: Literal["ssd", "hdd"] = "ssd"

    @field_validator("max_connections")
    @classmethod
    def normalize_connections(cls, value: int) -> int:
        return max(1, int(value or 1))


class DatabaseRestoreParityCaptureRequest(_DatabaseRequest):
    dsn: str = ""
    label: str = "baseline"
    output: str

    @field_validator("output", mode="before")
    @classmethod
    def normalize_output(cls, value: object) -> str:
        normalized = str(value or "").strip()
        if not normalized:
            raise ValueError("output is required")
        return normalized


class DatabaseRestoreParityVerifyRequest(_DatabaseRequest):
    dsn: str = ""
    label: str = "restored"
    baseline: str
    output: str = ""

    @field_validator("baseline", "output", mode="before")
    @classmethod
    def normalize_path(cls, value: object) -> str:
        return str(value or "").strip()

    @field_validator("baseline")
    @classmethod
    def require_baseline(cls, value: str) -> str:
        if not value:
            raise ValueError("baseline is required")
        return value
