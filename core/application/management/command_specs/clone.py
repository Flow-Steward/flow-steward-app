"""Public installation Clone command specifications."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from core.application.clone.operation import CloneRestoreResult
from core.application.clone.planning import PreparedCloneRestore
from core.application.clone.service import CloneCreateResult
from core.application.management.command_specs.contracts import (
    CommandSpec,
    CompactSupport,
    FieldSpec,
    FieldType,
    InteractivePolicy,
    OperationType,
)

_CLONE_CREATE_COMMAND = "clone.create"
_CLONE_RESTORE_COMMAND = "clone.restore"


class _ClosedModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class CloneCreateCommandInput(_ClosedModel):
    output_directory: str = "backups"
    passphrase_stdin: bool = False


class CloneRestoreCommandInput(_ClosedModel):
    archive_path: str = Field(min_length=1, max_length=4096)
    passphrase_stdin: bool = False
    operation_id: str | None = Field(
        default=None,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$",
    )


class CloneRestoreExecutionInput(_ClosedModel):
    prepared: PreparedCloneRestore
    target_installation_id: str
    target_profile: Literal["compact", "scalable"]
    public_host: str
    plan_digest: str


_PASSPHRASE_STDIN = FieldSpec(
    key="passphrase_stdin",
    label="Read passphrase from stdin",
    type=FieldType.BOOLEAN,
    default=False,
    option_name="--passphrase-stdin",
    description="Read the Clone passphrase from dedicated stdin instead of a secure prompt.",
    examples=("true", "false"),
)

CLONE_COMMAND_SPECS: tuple[CommandSpec, ...] = (
    CommandSpec(
        command_id=_CLONE_CREATE_COMMAND,
        compact_support=CompactSupport.PLATFORM,
        path=("clone", "create"),
        summary="Create an authenticated point-in-time installation Clone.",
        description="Capture allowlisted product state while manual maintenance is enabled.",
        examples=(
            "flow-steward clone create",
            "flow-steward clone create --passphrase-stdin --confirm",
        ),
        operation_type=OperationType.CHANGE,
        interactive_policy=InteractivePolicy.ALWAYS_REVIEW,
        fields=(
            FieldSpec(
                key="output_directory",
                label="Output directory",
                type=FieldType.PATH,
                default="backups",
                option_name="--output-directory",
                description="Configured directory for the single owner-only Clone artifact.",
                examples=("backups",),
            ),
            _PASSPHRASE_STDIN,
        ),
        input_model=CloneCreateCommandInput,
        result_model=CloneCreateResult,
        plan_builder_ref=None,
        confirmation_policy_ref=None,
        service_ref=_CLONE_CREATE_COMMAND,
        result_renderer_ref=_CLONE_CREATE_COMMAND,
        lifecycle_interrupts=True,
    ),
    CommandSpec(
        command_id=_CLONE_RESTORE_COMMAND,
        compact_support=CompactSupport.PLATFORM,
        path=("clone", "restore"),
        summary="Destructively replace an installation from an authenticated Clone.",
        description="Authenticate and review the exact target-bound replacement before mutation.",
        examples=(
            "flow-steward clone restore backups/source.fsclone",
            "flow-steward clone restore x.fsclone --passphrase-stdin",
        ),
        operation_type=OperationType.CHANGE,
        interactive_policy=InteractivePolicy.ALWAYS_REVIEW,
        fields=(
            FieldSpec(
                key="archive_path",
                label="Clone archive",
                type=FieldType.PATH,
                positional=True,
                required=True,
                description="Authenticated .fsclone archive to stage and restore.",
                examples=("backups/source.fsclone",),
            ),
            _PASSPHRASE_STDIN,
            FieldSpec(
                key="operation_id",
                label="Operation ID",
                type=FieldType.RUNTIME_IDENTIFIER,
                option_name="--operation-id",
                description="Resume the matching interrupted Clone restore operation.",
                examples=("clone-restore-0123456789abcdef",),
            ),
        ),
        input_model=CloneRestoreCommandInput,
        result_model=CloneRestoreResult,
        plan_builder_ref=f"{_CLONE_RESTORE_COMMAND}.preview",
        confirmation_policy_ref=(
            "match:target_installation_id,target_profile,public_host,plan_digest"
        ),
        service_ref=_CLONE_RESTORE_COMMAND,
        result_renderer_ref=_CLONE_RESTORE_COMMAND,
        lifecycle_interrupts=True,
    ),
)

__all__ = [
    "CLONE_COMMAND_SPECS",
    "CloneCreateCommandInput",
    "CloneRestoreCommandInput",
    "CloneRestoreExecutionInput",
]
