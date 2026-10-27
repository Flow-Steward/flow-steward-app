"""Typed semantic authentication outcome for public workflow entrypoints."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class WorkflowEntrypointAuthRejectionReason(StrEnum):
    AUTH_FAILED = "auth_failed"
    AUTH_MODE_NOT_CONFIGURED = "auth_mode_not_configured"
    CREDENTIAL_NOT_CONFIGURED = "credential_not_configured"


@dataclass(frozen=True)
class WorkflowEntrypointAuthOutcome:
    ok: bool
    error: str = ""
    reason: WorkflowEntrypointAuthRejectionReason | None = None

    @classmethod
    def accepted(cls) -> WorkflowEntrypointAuthOutcome:
        return cls(ok=True)

    @classmethod
    def rejected(
        cls,
        *,
        error: str,
        reason: WorkflowEntrypointAuthRejectionReason = (
            WorkflowEntrypointAuthRejectionReason.AUTH_FAILED
        ),
    ) -> WorkflowEntrypointAuthOutcome:
        return cls(ok=False, error=error, reason=reason)


__all__ = ["WorkflowEntrypointAuthOutcome", "WorkflowEntrypointAuthRejectionReason"]
