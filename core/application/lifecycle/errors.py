"""Stable lifecycle failure contract."""

from __future__ import annotations

from collections.abc import Mapping

from core.application.management.command_specs.contracts import ExitCode


class DatabaseSchemaRecoveryRequiredError(RuntimeError):
    """The connected database cannot safely serve the current release."""


class CandidateReadinessProbePendingError(RuntimeError):
    """The candidate readiness route is not registered yet during startup."""


class LifecycleError(RuntimeError):
    """One typed error surface for lifecycle services and transport mapping."""

    def __init__(
        self,
        *,
        code: str,
        summary: str,
        cause: str,
        remediation: str,
        retryable: bool,
        step: str,
        exit_code: ExitCode,
        operation_id: str | None = None,
        log_path: str | None = None,
        recovery_command: str | None = None,
        details: Mapping[str, object] | None = None,
    ) -> None:
        super().__init__(cause)
        self.code = code
        self.summary = summary
        self.cause = cause
        self.remediation = remediation
        self.retryable = retryable
        self.step = step
        self.exit_code = exit_code
        self.operation_id = operation_id
        self.log_path = log_path
        self.recovery_command = recovery_command
        self.details = dict(details or {})

    @classmethod
    def conflict(cls, *, operation_id: str | None, cause: str) -> LifecycleError:
        return cls(
            code="lifecycle_operation_active",
            summary="Another lifecycle mutation is active.",
            cause=cause,
            remediation="Wait for the active operation to finish, then retry.",
            retryable=True,
            step="lock",
            exit_code=ExitCode.CONFLICT,
            operation_id=operation_id,
        )

    @classmethod
    def dependency(cls, *, code: str, cause: str, remediation: str, step: str) -> LifecycleError:
        return cls(
            code=code,
            summary="A lifecycle dependency is unavailable.",
            cause=cause,
            remediation=remediation,
            retryable=True,
            step=step,
            exit_code=ExitCode.DEPENDENCY,
        )

    @classmethod
    def stable_failure(
        cls,
        *,
        code: str,
        cause: str,
        remediation: str,
        step: str,
        details: Mapping[str, object] | None = None,
    ) -> LifecycleError:
        return cls(
            code=code,
            summary="The lifecycle operation failed.",
            cause=cause,
            remediation=remediation,
            retryable=False,
            step=step,
            exit_code=ExitCode.STABLE_FAILURE,
            details=details,
        )

    @classmethod
    def recovery_required(
        cls,
        *,
        code: str,
        cause: str,
        remediation: str,
        step: str,
        recovery_command: str,
        details: Mapping[str, object] | None = None,
    ) -> LifecycleError:
        return cls(
            code=code,
            summary="The lifecycle operation requires recovery.",
            cause=cause,
            remediation=remediation,
            retryable=False,
            step=step,
            exit_code=ExitCode.RECOVERY_REQUIRED,
            recovery_command=recovery_command,
            details=details,
        )

    @classmethod
    def interrupted(cls, *, step: str, recovery_command: str, forced: bool) -> LifecycleError:
        if forced:
            cause = "The lifecycle operation was forcibly interrupted."
            remediation = f"Inspect the journal, then run: {recovery_command}"
            code = "lifecycle_forced_interrupt"
        else:
            cause = "The lifecycle operation was cancelled at a safe boundary."
            remediation = f"Resume when ready with: {recovery_command}"
            code = "lifecycle_cancelled"
        return cls(
            code=code,
            summary="The lifecycle operation was interrupted.",
            cause=cause,
            remediation=remediation,
            retryable=not forced,
            step=step,
            exit_code=ExitCode.INTERRUPTED,
            recovery_command=recovery_command,
        )

    @classmethod
    def internal(cls, *, step: str, recovery_command: str) -> LifecycleError:
        return cls(
            code="lifecycle_internal_error",
            summary="An unexpected lifecycle error occurred.",
            cause="An unexpected lifecycle error occurred.",
            remediation="Inspect the operation log before retrying.",
            retryable=False,
            step=step,
            exit_code=ExitCode.INTERNAL,
            recovery_command=recovery_command,
        )


class ForcedLifecycleInterrupt(BaseException):
    """Internal control flow raised only by the scoped signal adapter."""
