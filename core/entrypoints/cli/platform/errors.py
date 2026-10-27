"""The single CLI exception-to-exit mapping registry."""

from __future__ import annotations

import traceback
from collections.abc import Iterable
from dataclasses import dataclass

import click
from pydantic import ValidationError

from core.application.extensions.scaffold_contracts import ExtensionScaffoldConflictError
from core.application.lifecycle.errors import DatabaseSchemaRecoveryRequiredError, LifecycleError
from core.application.management.command_specs.contracts import (
    ExitCode,
    FieldIssue,
    OperationType,
)
from core.application.management.errors import (
    ServiceConflictError,
    ServiceError,
    ServiceForbiddenError,
    ServiceNotFoundError,
    ServicePreconditionError,
    ServiceUnavailableError,
    ServiceValidationError,
)
from core.application.readiness.contracts import ReadinessPrerequisiteMissing
from core.entrypoints.cli.platform.operation_log import OperationLogUnavailable
from core.entrypoints.cli.platform.redaction import RedactionContext, redact_text


@dataclass(frozen=True)
class MappedCLIError:
    """Safe structured error that the future CLI adapter can render consistently."""

    exit_code: ExitCode
    code: str
    summary: str
    cause: str
    remediation: str
    retryable: bool
    field_issues: tuple[FieldIssue, ...] = ()
    technical_detail: str = ""


def map_exception(
    exc: BaseException,
    *,
    known_secrets: Iterable[str] = (),
    verbose: bool = False,
    operation_type: OperationType | None = None,
) -> MappedCLIError:
    """Convert an exception to the closed public CLI error contract."""
    context = RedactionContext(known_secrets=tuple(known_secrets))
    technical_detail = _technical_detail(exc, context=context, verbose=verbose)
    if isinstance(exc, click.UsageError):
        return _mapped(
            ExitCode.SYNTAX,
            "syntax_error",
            exc,
            "Correct the command syntax and try again.",
            False,
            technical_detail,
            context=context,
        )
    if isinstance(exc, ValidationError):
        issues = tuple(
            FieldIssue(
                field=".".join(map(str, item.get("loc", ()))) or "input",
                message=str(item["msg"]),
                code=str(item["type"]),
            )
            for item in exc.errors(include_input=False)
        )
        return _mapped(
            ExitCode.VALIDATION,
            "validation_failed",
            exc,
            "Correct the invalid fields and try again.",
            False,
            technical_detail,
            issues,
            context,
        )
    if isinstance(exc, LifecycleError):
        return MappedCLIError(
            exit_code=exc.exit_code,
            code=redact_text(exc.code, context=context),
            summary=redact_text(exc.summary, context=context),
            cause=redact_text(exc.cause, context=context),
            remediation=redact_text(exc.remediation, context=context),
            retryable=exc.retryable,
            technical_detail=technical_detail,
        )
    if isinstance(exc, ServiceValidationError):
        return _mapped(
            ExitCode.VALIDATION,
            "validation_failed",
            exc,
            "Correct the invalid fields and try again.",
            False,
            technical_detail,
            _service_issues(exc, context),
            context,
        )
    if isinstance(exc, ServicePreconditionError):
        return _mapped(
            ExitCode.PRECONDITION,
            "precondition_failed",
            exc,
            "Resolve the reported precondition and retry.",
            True,
            technical_detail,
            context=context,
        )
    if isinstance(exc, ServiceNotFoundError):
        return _mapped(
            ExitCode.PRECONDITION,
            "precondition_failed",
            exc,
            "Check that the referenced resource exists.",
            False,
            technical_detail,
            context=context,
        )
    if isinstance(exc, OperationLogUnavailable):
        return _mapped(
            ExitCode.PRECONDITION,
            "operation_log_unavailable",
            exc,
            (
                "Make the state directory writable for the user running this command, "
                "or point FS_INSTALLER_STATE_DIR at a writable directory."
            ),
            False,
            technical_detail,
            context=context,
        )
    if isinstance(exc, ReadinessPrerequisiteMissing):
        # Naming what is missing is the whole purpose of the command that raised
        # this. The generic branch below turns it into `internal_error` with a
        # placeholder cause, and picks exit 8 or 70 depending on the kind of
        # command — three different answers to one situation, none of which say
        # an operator has not run the installer yet.
        return _mapped(
            ExitCode.PRECONDITION,
            "precondition_failed",
            exc,
            exc.remediation
            or "Run the installer on this host, or point the CLI at an installed stack.",
            False,
            technical_detail,
            context=context,
        )
    if isinstance(exc, (ServiceForbiddenError, PermissionError)):
        return _mapped(
            ExitCode.PERMISSION,
            "permission_denied",
            exc,
            "Use an authorized account or request access.",
            False,
            technical_detail,
            context=context,
        )
    if isinstance(exc, (ServiceConflictError, ExtensionScaffoldConflictError)):
        return _mapped(
            ExitCode.CONFLICT,
            "conflict",
            exc,
            "Resolve the conflict and try again.",
            False,
            technical_detail,
            context=context,
        )
    if isinstance(exc, ServiceUnavailableError):
        return _mapped(
            ExitCode.DEPENDENCY,
            "dependency_unavailable",
            exc,
            "Check the dependent service and retry.",
            True,
            technical_detail,
            context=context,
        )
    if isinstance(exc, ServiceError):
        return _mapped(
            ExitCode.STABLE_FAILURE,
            "operation_failed",
            exc,
            "Review the operation inputs and try again.",
            False,
            technical_detail,
            _service_issues(exc, context),
            context,
        )
    if isinstance(exc, DatabaseSchemaRecoveryRequiredError):
        return MappedCLIError(
            exit_code=ExitCode.RECOVERY_REQUIRED,
            code="schema_recovery_required",
            summary="Database recovery is required.",
            cause="The database schema is not ready for this Flow Steward release.",
            remediation=(
                "Run flow-steward database status, verify a current backup, then run "
                "flow-steward database upgrade with the matching release."
            ),
            retryable=False,
            technical_detail=technical_detail if verbose else "",
        )
    if isinstance(exc, (KeyboardInterrupt, click.Abort)):
        return _mapped(
            ExitCode.INTERRUPTED,
            "interrupted",
            exc,
            "Run the command again when ready.",
            False,
            technical_detail,
            context=context,
        )
    exit_code = ExitCode.INTERNAL
    remediation = "Try again; contact an operator if the problem persists."
    retryable = True
    if operation_type is OperationType.READ:
        exit_code = ExitCode.STABLE_FAILURE
    elif operation_type in (OperationType.CHANGE, OperationType.DESTRUCTIVE):
        exit_code = ExitCode.RECOVERY_REQUIRED
        remediation = "Inspect the current operation state before retrying."
        retryable = False
    return _mapped(
        exit_code,
        "internal_error",
        exc,
        remediation,
        retryable,
        technical_detail,
        context=context,
    )


def _service_issues(exc: BaseException, context: RedactionContext) -> tuple[FieldIssue, ...]:
    """The items a service refusal names: what to fix, each with its own reason.

    A blocked import lists its blocking dependencies and the resolutions each
    allows; a refused hook binding lists its issues. Only these fixed fields
    are shown, redacted like every other message.
    """
    payload = getattr(exc, "payload", None)
    if not isinstance(payload, dict):
        return ()
    rows = (
        payload.get("errors") if isinstance(payload.get("errors"), list) else payload.get("issues")
    )
    issues: list[FieldIssue] = []
    for item in rows[:50] if isinstance(rows, list) else []:
        if not isinstance(item, dict):
            continue
        field = str(item.get("dependency_key") or item.get("path") or item.get("ref") or "input")
        message = str(item.get("message") or item.get("code") or "")
        actions = item.get("resolution_actions")
        if isinstance(actions, list) and actions:
            message = f"{message} Resolutions: {', '.join(str(action) for action in actions)}."
        issues.append(
            FieldIssue(
                field=redact_text(field, context=context),
                message=redact_text(message.strip(), context=context),
                code=redact_text(
                    str(item.get("status") or item.get("code") or "issue"), context=context
                ),
            )
        )
    return tuple(issues)


def _mapped(
    exit_code: ExitCode,
    code: str,
    exc: BaseException,
    remediation: str,
    retryable: bool,
    technical_detail: str,
    issues: tuple[FieldIssue, ...] = (),
    context: RedactionContext | None = None,
) -> MappedCLIError:
    cause = (
        "An unexpected internal error occurred."
        if code == "internal_error"
        else redact_text(str(exc) or exc.__class__.__name__, context=context)
    )
    return MappedCLIError(
        exit_code,
        code,
        "Command could not be completed.",
        cause,
        remediation,
        retryable,
        issues,
        technical_detail,
    )


def _technical_detail(exc: BaseException, *, context: RedactionContext, verbose: bool) -> str:
    if verbose:
        return redact_text("".join(traceback.format_exception(exc)), context=context)
    return redact_text(str(exc), context=context)
