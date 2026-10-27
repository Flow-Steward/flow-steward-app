"""First-party command specification contracts for the public CLI."""

from typing import Any

from core.application.management.command_specs.contracts import (
    ChoiceItem,
    CLIErrorDetail,
    CLIFailureEnvelope,
    CLISuccessEnvelope,
    CLIWarning,
    CommandSpec,
    ExactlyOneOfGroup,
    ExecutionResult,
    ExitCode,
    FieldIssue,
    FieldSpec,
    FieldType,
    InteractivePolicy,
    OperationProgressEvent,
    OperationStep,
    OperationStepStatus,
    OperationType,
)


def get_command_spec(path: str | tuple[str, ...]) -> CommandSpec:
    """Lazily resolve command specs without importing registry for contracts-only users."""
    from core.application.management.command_specs.registry import get_command_spec as _get

    normalized = tuple(path.split(".")) if isinstance(path, str) else path
    return _get(normalized)


def __getattr__(name: str) -> Any:
    if name == "PUBLIC_COMMAND_SPECS":
        from core.application.management.command_specs.registry import PUBLIC_COMMAND_SPECS

        return PUBLIC_COMMAND_SPECS
    raise AttributeError(name)


__all__ = [
    "PUBLIC_COMMAND_SPECS",
    "CLIErrorDetail",
    "CLIFailureEnvelope",
    "CLISuccessEnvelope",
    "CLIWarning",
    "ChoiceItem",
    "CommandSpec",
    "ExactlyOneOfGroup",
    "ExecutionResult",
    "ExitCode",
    "FieldIssue",
    "FieldSpec",
    "FieldType",
    "InteractivePolicy",
    "OperationProgressEvent",
    "OperationStep",
    "OperationStepStatus",
    "OperationType",
    "get_command_spec",
]
