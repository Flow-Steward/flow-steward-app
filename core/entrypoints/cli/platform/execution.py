"""Side-effecting command execution, isolated from Click and renderers."""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import io
from collections.abc import Awaitable, Callable
from types import UnionType
from typing import Any, Protocol, Union, cast, get_args, get_origin

from pydantic import BaseModel

from core.application.lifecycle.contracts import CancellationToken, ConfigurationValidationResult
from core.application.lifecycle.errors import ForcedLifecycleInterrupt, LifecycleError
from core.application.management.command_specs.contracts import (
    CLIErrorDetail,
    CommandSpec,
    ExecutionResult,
    ExitCode,
    OperationProgressEvent,
    OperationType,
)
from core.entrypoints.cli.platform.errors import map_exception
from core.entrypoints.cli.platform.interrupts import lifecycle_interrupt_scope
from core.entrypoints.cli.platform.redaction import (
    RedactionContext,
    redact,
    redact_output,
    redact_text,
)


class ServiceInvoker(Protocol):
    def invoke(
        self,
        service_ref: str,
        command_input: BaseModel,
        *,
        progress_sink: Callable[[OperationProgressEvent], None] | None = None,
        technical_sink: Callable[[str], None] | None = None,
        cancellation: CancellationToken | None = None,
    ) -> object: ...


def execute_command(
    *,
    spec: CommandSpec,
    command_input: BaseModel,
    services: ServiceInvoker,
    progress_sink: Callable[[OperationProgressEvent], None] | None = None,
    technical_sink: Callable[[str], None] | None = None,
    known_secrets: tuple[str, ...] = (),
    capture_stdout: bool = False,
    cancellation: CancellationToken | None = None,
) -> ExecutionResult:
    """Invoke exactly one registered service and normalize its outcome."""
    command_context = RedactionContext.from_command_spec(spec, command_input.model_dump())
    known_secrets = (*command_context.known_secrets, *known_secrets)
    context = RedactionContext(known_secrets=known_secrets)

    def safe_progress(event: OperationProgressEvent) -> None:
        if progress_sink is not None:
            _safe_sink_write(
                progress_sink,
                OperationProgressEvent.model_validate(redact(event.model_dump(), context=context)),
            )

    def safe_technical(message: str) -> None:
        _safe_sink_write(
            technical_sink,
            redact_text(str(message), context=context),
        )

    captured_stdout = io.StringIO()
    scoped_lifecycle = spec.lifecycle_interrupts and spec.operation_type is not OperationType.READ
    invocation_cancellation = cancellation or CancellationToken() if scoped_lifecycle else None
    try:
        stdout_context = (
            contextlib.redirect_stdout(captured_stdout)
            if capture_stdout
            else contextlib.nullcontext()
        )
        interrupt_context = (
            lifecycle_interrupt_scope(
                invocation_cancellation,
                reporter=safe_technical,
            )
            if invocation_cancellation is not None
            else contextlib.nullcontext()
        )
        with stdout_context, interrupt_context:
            if invocation_cancellation is not None:
                value = services.invoke(
                    spec.service_ref,
                    command_input,
                    progress_sink=safe_progress if progress_sink else None,
                    technical_sink=safe_technical if technical_sink else None,
                    cancellation=invocation_cancellation,
                )
            else:
                value = services.invoke(
                    spec.service_ref,
                    command_input,
                    progress_sink=safe_progress if progress_sink else None,
                    technical_sink=safe_technical if technical_sink else None,
                )
            value = _await(value)
        _log_captured_stdout(
            captured_stdout,
            technical_sink=technical_sink,
            context=context,
        )
        validated = spec.result_model.model_validate(value)
        _log_tool_streams(validated, technical_sink=technical_sink, context=context)
        if isinstance(validated, ConfigurationValidationResult) and not validated.valid:
            return ExecutionResult(
                error=CLIErrorDetail(
                    code="configuration_invalid",
                    message="Configuration validation failed.",
                    hint="Repair the reported configuration issues and rerun config validate.",
                    details={"issues": list(validated.issues)},
                ),
                exit_code=ExitCode.VALIDATION,
            )
        if failure := _result_failure(
            validated,
            operation_type=spec.operation_type,
            context=context,
        ):
            return failure
        return ExecutionResult(
            result=validated,
            public_result=_public_result_payload(
                validated,
                context=context,
                secret_fields=_result_secret_fields(spec.result_model),
            ),
            operation_id=_optional_text(getattr(validated, "operation_id", None)),
            log_path=_optional_text(getattr(validated, "log_path", None)),
        )
    except BaseException as exc:  # mapping is the public executor boundary
        _log_captured_stdout(
            captured_stdout,
            technical_sink=technical_sink,
            context=context,
        )
        lifecycle_failure: LifecycleError | None = None
        if isinstance(exc, LifecycleError):
            lifecycle_failure = exc
        elif isinstance(exc, ForcedLifecycleInterrupt) and invocation_cancellation is not None:
            lifecycle_failure = LifecycleError.interrupted(
                step="interrupt",
                recovery_command=invocation_cancellation.recovery_command,
                forced=True,
            )
        if lifecycle_failure is not None:
            _safe_sink_write(
                technical_sink,
                redact_text(
                    f"{lifecycle_failure.summary} {lifecycle_failure.cause}",
                    context=context,
                ),
            )
            return ExecutionResult(
                error=CLIErrorDetail(
                    code=lifecycle_failure.code,
                    message=redact_text(lifecycle_failure.cause, context=context),
                    hint=redact_text(lifecycle_failure.remediation, context=context),
                    retryable=lifecycle_failure.retryable,
                    details=lifecycle_failure.details,
                ),
                exit_code=lifecycle_failure.exit_code,
                operation_id=lifecycle_failure.operation_id,
                log_path=lifecycle_failure.log_path,
            )
        mapped = map_exception(
            exc,
            known_secrets=known_secrets,
            operation_type=spec.operation_type,
        )
        _safe_sink_write(technical_sink, mapped.technical_detail)
        return ExecutionResult(
            error=CLIErrorDetail(
                code=mapped.code,
                message=mapped.cause or mapped.summary,
                field_issues=mapped.field_issues,
                hint=mapped.remediation,
                retryable=mapped.retryable,
            ),
            exit_code=mapped.exit_code,
        )


def _log_captured_stdout(
    captured: io.StringIO,
    *,
    technical_sink: Callable[[str], None] | None,
    context: RedactionContext,
) -> None:
    text = captured.getvalue().rstrip("\r\n")
    if not text or technical_sink is None:
        return
    _safe_sink_write(technical_sink, "stdout:")
    _safe_sink_write(technical_sink, redact_text(text, context=context))
    captured.seek(0)
    captured.truncate(0)


def _await(value: object) -> object:
    if not inspect.isawaitable(value):
        return value
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(_await_value(value))
    raise RuntimeError("CLI execution cannot run inside an active event loop.")


async def _await_value(value: object) -> Any:
    return await cast(Awaitable[Any], value)


def _public_result_payload(
    value: BaseModel,
    *,
    context: RedactionContext,
    secret_fields: frozenset[str],
) -> dict[str, Any]:
    payload = value.model_dump(mode="json")
    redacted = redact_output(
        payload,
        context=context,
        secret_fields=secret_fields,
        public_usage_counter_paths=_result_public_usage_counter_paths(type(value)),
        server_redacted_document_paths=_result_server_redacted_document_paths(type(value)),
    )
    return redacted if isinstance(redacted, dict) else {}


def _result_server_redacted_document_paths(model: type[BaseModel]) -> frozenset[tuple[str, ...]]:
    """Top-level text fields the result model declares as server-redacted documents."""
    return frozenset(
        (name,)
        for name, field in model.model_fields.items()
        if field.annotation is str
        and isinstance(field.json_schema_extra, dict)
        and field.json_schema_extra.get("server_redacted_document") is True
        and not field.json_schema_extra.get("secret")
        and not field.json_schema_extra.get("sensitive")
    )


def _result_public_usage_counter_paths(
    model: type[BaseModel],
    seen: frozenset[type[BaseModel]] = frozenset(),
) -> frozenset[tuple[str, ...]]:
    if model in seen:
        return frozenset()
    paths: set[tuple[str, ...]] = set()
    for name, field in model.model_fields.items():
        extra = field.json_schema_extra
        if isinstance(extra, dict) and (extra.get("secret") or extra.get("sensitive")):
            continue
        annotation = field.annotation
        if (
            annotation is int
            and isinstance(extra, dict)
            and extra.get("public_usage_counter") is True
        ):
            paths.add((name,))
        elif nested_model := _result_nested_model(annotation):
            paths.update(
                (name, *path)
                for path in _result_public_usage_counter_paths(nested_model, seen | {model})
            )
    return frozenset(paths)


def _result_nested_model(annotation: object) -> type[BaseModel] | None:
    """Only declared models, optionally nullable, confer counter provenance."""
    if get_origin(annotation) in (Union, UnionType):
        alternatives = get_args(annotation)
        if len(alternatives) != 2 or type(None) not in alternatives:
            return None
        annotation = next(item for item in alternatives if item is not type(None))
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return annotation
    return None


def _result_secret_fields(model: type[BaseModel]) -> frozenset[str]:
    fields: set[str] = set()
    for name, field in model.model_fields.items():
        extra = field.json_schema_extra or {}
        if not isinstance(extra, dict):
            extra = {}
        if extra.get("secret") is True or extra.get("sensitive") is True:
            fields.add(name)
            if field.alias:
                fields.add(str(field.alias))
    return frozenset(fields)


def _optional_text(value: object) -> str | None:
    text = str(value or "").strip()
    return text or None


def _log_tool_streams(
    value: BaseModel,
    *,
    technical_sink: Callable[[str], None] | None,
    context: RedactionContext,
) -> None:
    if technical_sink is None:
        return
    stdout = str(getattr(value, "stdout_full", None) or getattr(value, "stdout", "") or "")
    stderr = str(getattr(value, "stderr_full", None) or getattr(value, "stderr", "") or "")
    if stdout:
        _safe_sink_write(technical_sink, "stdout:")
        _safe_sink_write(technical_sink, redact_text(stdout, context=context))
    if stderr:
        _safe_sink_write(technical_sink, "stderr:")
        _safe_sink_write(technical_sink, redact_text(stderr, context=context))


def _safe_sink_write(sink: Callable[[Any], None] | None, value: Any) -> None:
    """Keep optional diagnostic/progress sinks from changing command semantics."""
    if sink is None:
        return
    try:
        sink(value)
    except Exception:
        return


def _result_failure(
    value: BaseModel,
    *,
    operation_type: OperationType,
    context: RedactionContext,
) -> ExecutionResult | None:
    """Map typed operation/tool failures into the closed CLI error contract."""
    status = str(getattr(value, "status", "") or "").strip().lower()
    operation = str(getattr(value, "operation", "") or "").strip()
    raw_exit_code = getattr(value, "exit_code", None)
    raw_process_exit_code = getattr(value, "process_exit_code", None)
    reported_exit_code = raw_exit_code if isinstance(raw_exit_code, int) else raw_process_exit_code
    exit_code = int(reported_exit_code) if isinstance(reported_exit_code, int) else 0
    failed = bool(operation) and status in {
        "failed",
        "failure",
        "timeout",
        "timed_out",
        "cancelled",
        "canceled",
    }
    failed = failed or exit_code != 0
    if not failed:
        return None

    message = str(getattr(value, "message", "") or "").strip()
    stderr = str(getattr(value, "stderr", "") or "").strip()
    stdout = str(getattr(value, "stdout", "") or "").strip()
    result_details = dict(getattr(value, "details", {}) or {})
    readiness_findings = tuple(getattr(value, "findings", ()) or ())
    if readiness_findings:
        serialized_findings = [
            finding.model_dump(mode="json") if isinstance(finding, BaseModel) else dict(finding)
            for finding in readiness_findings
            if isinstance(finding, (BaseModel, dict))
        ]
        blocking_findings = [
            finding
            for finding in serialized_findings
            if str(finding.get("area") or "") == "core"
            and str(finding.get("status") or "") == "failed"
        ]
        if blocking_findings:
            serialized_findings = blocking_findings
        result_details.update(
            {
                "status": status,
                "findings": serialized_findings,
                "core_passed": int(getattr(value, "core_passed", 0) or 0),
                "core_failed": int(getattr(value, "core_failed", 0) or 0),
            }
        )
        first_remediation = next(
            (
                str(finding.get("remediation") or "").strip()
                for finding in serialized_findings
                if str(finding.get("remediation") or "").strip()
            ),
            "",
        )
        if first_remediation:
            result_details["remediation"] = first_remediation
    detail = (
        message
        or stderr
        or stdout
        or (f"Command reported {status}." if status else "Operation failed.")
    )
    detail = redact_text(detail, context=context)
    if len(detail) > 600:
        detail = f"{detail[:597]}..."

    if status in {"timeout", "timed_out"}:
        code = "tool_timeout"
        public_exit = ExitCode.RECOVERY_REQUIRED
        hint = "The operation timed out. Retry with a larger timeout or inspect the operation log."
    elif status in {"cancelled", "canceled"}:
        code = "tool_cancelled"
        public_exit = ExitCode.INTERRUPTED
        hint = "The operation was cancelled before completion."
    elif reported_exit_code is not None:
        code = str(
            result_details.get("error_code")
            or ("precondition_failed" if exit_code == int(ExitCode.PRECONDITION) else "tool_failed")
        )
        public_exit = ExitCode.RECOVERY_REQUIRED
        if exit_code == int(ExitCode.PRECONDITION):
            public_exit = ExitCode.PRECONDITION
        elif operation_type is OperationType.READ:
            public_exit = ExitCode.STABLE_FAILURE
        hint = str(result_details.get("remediation") or "").strip() or (
            f"Underlying tool exit code: {exit_code}."
            if operation_type is OperationType.READ
            else "Inspect the operation log and current state before retrying."
        )
    else:
        code = "operation_failed"
        public_exit = (
            ExitCode.STABLE_FAILURE
            if operation_type is OperationType.READ
            else ExitCode.RECOVERY_REQUIRED
        )
        hint = (
            None
            if operation_type is OperationType.READ
            else "Inspect the operation log and current state before retrying."
        )

    prefix = f"{operation}: " if operation else ""
    return ExecutionResult(
        error=CLIErrorDetail(
            code=code,
            message=f"{prefix}{detail}",
            hint=hint,
            retryable=status in {"timeout", "timed_out"},
            details=result_details,
        ),
        exit_code=public_exit,
    )
