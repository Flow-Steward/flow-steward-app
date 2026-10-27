"""Renderer-neutral core readiness contracts."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class _ClosedModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ReadinessArea(StrEnum):
    CORE = "core"
    OPTIONAL = "optional"
    RECOMMENDATION = "recommendation"


class ReadinessStatus(StrEnum):
    PASSED = "passed"
    WARNING = "warning"
    FAILED = "failed"
    INFO = "info"


class ReadinessPrerequisiteMissing(RuntimeError):
    """A readiness probe cannot run until an operator supplies something.

    Doctor exists to name what is missing. Letting the prerequisite escape as an
    ordinary exception turned "this host has no installer configuration yet"
    into a generic internal_error, which tells an operator nothing and hides the
    one answer the command was asked for.
    """

    def __init__(self, detail: str, *, remediation: str = "") -> None:
        super().__init__(detail)
        self.detail = detail
        self.remediation = remediation


class ReadinessFinding(_ClosedModel):
    check_id: str
    area: ReadinessArea
    title: str
    status: ReadinessStatus
    detail: str = ""
    remediation: str = ""


class ReadinessReport(_ClosedModel):
    status: str
    findings: tuple[ReadinessFinding, ...] = ()
    core_passed: int = 0
    core_failed: int = 0
    optional_warnings: int = 0
    recommendations: int = 0
    process_exit_code: int = Field(default=0, ge=0, le=70)
