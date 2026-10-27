"""Closed, renderer-neutral contracts for the schema-driven public CLI."""

from __future__ import annotations

import re
from collections.abc import Mapping
from enum import IntEnum, StrEnum
from types import MappingProxyType
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, SerializeAsAny, model_validator


class CompactSupport(StrEnum):
    """How a command reaches a Compact installation, or why it does not.

    AC17-R113 and AC17-R115: availability was held in four hand-maintained places and
    in none of them was it a property of the command -- the launcher's recognised
    words, its Compact dispatch arcs, the argparse choices inside the image, and the
    help text that tells an operator what will not work. A command could be correct in
    the registry and refused by the launcher, or refused by the launcher and never
    mentioned in the help. Both happened.

    The value says how the command is dispatched on that profile, because that is what
    every one of those lists is really about.
    """

    PLATFORM = "platform"
    """Dispatched by the launcher as a Compact platform operation."""

    PRODUCT = "product"
    """Dispatched into the running product container."""

    TOOLING_SERVICE = "tooling_service"
    """Runs in a tooling service a Compact deployment does not include."""

    DELEGATED_SERVICE = "delegated_service"
    """Dispatched to a service this profile does not define, though the runtime could do the work."""

    RETAINED_CLI = "retained_cli"
    """Dispatched to the retained fs-cli container this profile does not have.

    AC17-R140: refused by the launcher on Compact for exactly that reason, before
    Docker is asked anything. They used to be accepted, fail to start the container,
    and advise a reinstall that cannot create one.
    """

    REFUSED_UNDECLARED = "refused_undeclared"
    """Refused by the launcher's Compact allowlist and not mentioned in its help.

    AC17-R115 found `environment` and `release` in this state: recognised as words,
    absent from the dispatch arcs, and absent from the help, so an operator meets a
    flat "Unsupported Compact platform command." This records today's behaviour rather
    than endorsing it; whether to open those commands is a separate decision.
    """


class OperationType(StrEnum):
    """The safety classification of a public command."""

    READ = "read"
    CHANGE = "change"
    DESTRUCTIVE = "destructive"


class InteractivePolicy(StrEnum):
    """When a command requires guided review."""

    IMMEDIATE = "immediate"
    WIZARD_WHEN_INCOMPLETE = "wizard_when_incomplete"
    ALWAYS_REVIEW = "always_review"


class FieldType(StrEnum):
    """Supported first-party field presentation and validation types."""

    TEXT = "text"
    SECRET = "secret"  # pragma: allowlist secret
    BOOLEAN = "boolean"
    SINGLE_SELECT = "single_select"
    MULTI_SELECT = "multi_select"
    INTEGER = "integer"
    DECIMAL = "decimal"
    PATH = "path"
    URL = "url"
    EMAIL = "email"
    DURATION = "duration"
    EXTENSION_ID = "extension_id"
    RUNTIME_IDENTIFIER = "runtime_identifier"


class ExitCode(IntEnum):
    """Stable process exit values for the public CLI."""

    SUCCESS = 0
    SYNTAX = 2
    VALIDATION = 3
    PRECONDITION = 4
    PERMISSION = 5
    CONFLICT = 6
    DEPENDENCY = 7
    STABLE_FAILURE = 8
    RECOVERY_REQUIRED = 9
    INTERRUPTED = 10
    INTERNAL = 70


class _ClosedModel(BaseModel):
    """Base model that rejects fields outside the fixed contract."""

    model_config = ConfigDict(extra="forbid")


class ChoiceItem(_ClosedModel):
    """A static selectable value."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    value: str
    label: str
    description: str = ""


def _freeze_metadata(value: object) -> object:
    """Return immutable metadata containers without changing scalar values."""
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze_metadata(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze_metadata(item) for item in value)
    if isinstance(value, tuple):
        return tuple(_freeze_metadata(item) for item in value)
    if isinstance(value, set):
        return frozenset(_freeze_metadata(item) for item in value)
    return value


class FieldSpec(_ClosedModel):
    """Renderer-neutral declaration of one public command input."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    key: str
    label: str
    type: FieldType
    required: bool = False
    description: str = ""
    choices: tuple[ChoiceItem, ...] = ()
    choice_provider: str | None = None
    default: object | None = None
    examples: tuple[str, ...] = ()
    placeholder: str = ""
    secret: bool = False
    advanced: bool = False
    depends_on: tuple[str, ...] = ()
    visible_if: Mapping[str, object] = Field(default_factory=dict)
    option_name: str | None = None
    positional: bool = False
    allow_persisted_config: bool = False
    allow_secret_file: bool = False
    allow_secret_stdin: bool = False
    deferred_until_plan: bool = False

    @model_validator(mode="after")
    def validate_invariants(self) -> Self:
        """Keep field metadata safe and unambiguous across renderers."""
        object.__setattr__(self, "default", _freeze_metadata(self.default))
        object.__setattr__(self, "visible_if", _freeze_metadata(self.visible_if))
        if self.required and self.default is not None:
            raise ValueError("required fields cannot declare defaults")
        if self.secret != (self.type is FieldType.SECRET):
            raise ValueError("secret fields must use FieldType.SECRET")
        if self.secret and self.positional:
            raise ValueError("secret fields cannot be positional")
        if self.choices and self.choice_provider:
            raise ValueError("choices and choice_provider are mutually exclusive")
        if self.choice_provider and self.type not in {
            FieldType.SINGLE_SELECT,
            FieldType.MULTI_SELECT,
        }:
            raise ValueError("choice_provider requires a select field type")
        return self


class ExactlyOneOfGroup(_ClosedModel):
    """Named cross-field requirement shared by every input renderer."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    key: str
    label: str
    field_keys: tuple[str, ...]

    @model_validator(mode="after")
    def validate_invariants(self) -> Self:
        if not self.key.strip() or not self.label.strip():
            raise ValueError("exactly-one groups require a key and label")
        if len(self.field_keys) < 2 or len(set(self.field_keys)) != len(self.field_keys):
            raise ValueError("exactly-one groups require at least two distinct fields")
        return self


_CANONICAL_PATH_SEGMENT = re.compile(r"^[a-z][a-z0-9_-]*$")


class CommandSpec(_ClosedModel):
    """Renderer-neutral declaration of one public command or command group."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    command_id: str
    path: tuple[str, ...]
    summary: str
    description: str
    examples: tuple[str, ...]
    operation_type: OperationType
    interactive_policy: InteractivePolicy
    fields: tuple[FieldSpec, ...]
    input_model: type[BaseModel]
    result_model: type[BaseModel]
    plan_builder_ref: str | None
    confirmation_policy_ref: str | None
    service_ref: str
    result_renderer_ref: str
    compact_support: CompactSupport = CompactSupport.PRODUCT
    accepts_input_json: bool = False
    exactly_one_of: tuple[ExactlyOneOfGroup, ...] = ()
    lifecycle_interrupts: bool = False

    @model_validator(mode="after")
    def validate_path_and_id(self) -> Self:
        """Require the single canonical command identity used by all adapters."""
        if not 1 <= len(self.path) <= 2:
            raise ValueError("command paths must contain one or two segments")
        if any(not _CANONICAL_PATH_SEGMENT.fullmatch(segment) for segment in self.path):
            raise ValueError("command path segments must be non-empty and canonical")
        if self.command_id != ".".join(self.path):
            raise ValueError("command_id must equal the dot-joined path")
        field_keys = {field.key for field in self.fields}
        grouped_fields: set[str] = set()
        group_keys: set[str] = set()
        for group in self.exactly_one_of:
            if group.key in group_keys:
                raise ValueError("exactly-one group keys must be unique")
            group_keys.add(group.key)
            unknown = set(group.field_keys) - field_keys
            if unknown:
                raise ValueError("exactly-one groups must reference declared fields")
            if grouped_fields.intersection(group.field_keys):
                raise ValueError("fields cannot belong to multiple exactly-one groups")
            grouped_fields.update(group.field_keys)
        return self


class FieldIssue(_ClosedModel):
    """A safe, aggregate validation issue for one named field."""

    field: str
    message: str
    code: str = "validation"


class CLIWarning(_ClosedModel):
    """A non-fatal warning returned with a successful command result."""

    code: str
    message: str


class CLIErrorDetail(_ClosedModel):
    """Safe public error detail without raw invalid input values."""

    code: str
    message: str
    field_issues: tuple[FieldIssue, ...] = ()
    hint: str | None = None
    retryable: bool = False
    details: dict[str, Any] = Field(default_factory=dict)


class CLISuccessEnvelope(_ClosedModel):
    """The fixed JSON success response emitted by JSON CLI mode."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True, serialize_by_alias=True)

    schema_: Literal["flow-steward.cli.v1"] = Field("flow-steward.cli.v1", alias="schema")
    ok: Literal[True] = True
    command: str
    result: dict[str, Any]
    warnings: list[CLIWarning] = Field(default_factory=list)
    operation_id: str | None = None
    log_path: str | None = None


class CLIFailureEnvelope(_ClosedModel):
    """The fixed JSON failure response emitted by JSON CLI mode."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True, serialize_by_alias=True)

    schema_: Literal["flow-steward.cli.v1"] = Field("flow-steward.cli.v1", alias="schema")
    ok: Literal[False] = False
    command: str
    error: CLIErrorDetail
    operation_id: str | None = None
    log_path: str | None = None


class OperationStepStatus(StrEnum):
    """Lifecycle state for structured operation progress."""

    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED = "skipped"
    CANCELLED = "cancelled"


class OperationStep(_ClosedModel):
    """A durable-safe snapshot of an operation step."""

    step_id: str
    status: OperationStepStatus
    label: str
    detail: str = ""


class OperationProgressEvent(_ClosedModel):
    """Safe structured progress information for renderers and operation logs."""

    step_id: str
    status: OperationStepStatus
    label: str
    detail: str = ""
    completed: int | float | None = None
    total: int | float | None = None
    elapsed_seconds: float | None = None
    heartbeat: bool = False


class ExecutionResult(_ClosedModel):
    """Renderer-neutral result returned by the common command executor."""

    result: SerializeAsAny[BaseModel] | None = None
    public_result: dict[str, Any] | None = None
    error: CLIErrorDetail | None = None
    warnings: tuple[CLIWarning, ...] = ()
    exit_code: ExitCode = ExitCode.SUCCESS
    operation_id: str | None = None
    log_path: str | None = None

    @model_validator(mode="after")
    def validate_outcome(self) -> Self:
        """Represent exactly one of a successful result or a structured error."""
        if (self.result is None) == (self.error is None):
            raise ValueError("execution results require exactly one result or error")
        if self.result is None and self.public_result is not None:
            raise ValueError("public result payload requires a successful typed result")
        if self.error is None and self.exit_code is not ExitCode.SUCCESS:
            raise ValueError("successful execution results require the success exit code")
        if self.error is not None and self.exit_code is ExitCode.SUCCESS:
            raise ValueError("failed execution results require a non-success exit code")
        return self
