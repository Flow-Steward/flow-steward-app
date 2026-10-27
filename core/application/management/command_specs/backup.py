"""Public protected PostgreSQL backup command specifications."""

from __future__ import annotations

from core.application.lifecycle.backup_service import (
    BackupCreateRequest,
    BackupListRequest,
    BackupListResult,
    BackupResult,
    BackupVerifyRequest,
)
from core.application.management.command_specs.contracts import (
    CommandSpec,
    CompactSupport,
    FieldSpec,
    FieldType,
    InteractivePolicy,
    OperationType,
)

_TOKEN_BACKUP_CREATE = "backup.create"
_TOKEN_BACKUP_LIST = "backup.list"
_TOKEN_BACKUP_VERIFY = "backup.verify"

BACKUP_COMMAND_SPECS: tuple[CommandSpec, ...] = (
    CommandSpec(
        command_id=_TOKEN_BACKUP_CREATE,
        compact_support=CompactSupport.PLATFORM,
        path=("backup", "create"),
        summary="Create and verify a protected PostgreSQL backup.",
        description=(
            "Generate an owner-only encrypted PostgreSQL custom-format backup under "
            "backups/, validate it with pg_restore -l, and publish it only after verification."
        ),
        examples=("flow-steward backup create", "flow-steward backup create --json"),
        operation_type=OperationType.CHANGE,
        interactive_policy=InteractivePolicy.IMMEDIATE,
        fields=(),
        input_model=BackupCreateRequest,
        result_model=BackupResult,
        plan_builder_ref=None,
        confirmation_policy_ref=None,
        service_ref=_TOKEN_BACKUP_CREATE,
        result_renderer_ref=_TOKEN_BACKUP_CREATE,
        lifecycle_interrupts=True,
    ),
    CommandSpec(
        command_id=_TOKEN_BACKUP_LIST,
        compact_support=CompactSupport.PLATFORM,
        path=("backup", "list"),
        summary="List protected PostgreSQL backups.",
        description=(
            "Read backup path, size, creation time, and persisted verification metadata "
            "without decrypting every artifact."
        ),
        examples=("flow-steward backup list", "flow-steward backup list --json"),
        operation_type=OperationType.READ,
        interactive_policy=InteractivePolicy.IMMEDIATE,
        fields=(),
        input_model=BackupListRequest,
        result_model=BackupListResult,
        plan_builder_ref=None,
        confirmation_policy_ref=None,
        service_ref=_TOKEN_BACKUP_LIST,
        result_renderer_ref=_TOKEN_BACKUP_LIST,
    ),
    CommandSpec(
        command_id=_TOKEN_BACKUP_VERIFY,
        compact_support=CompactSupport.PLATFORM,
        path=("backup", "verify"),
        summary="Verify one protected PostgreSQL backup.",
        description=(
            "Resolve one artifact below backups/, reject symlink or path escape, decrypt "
            "only to an owner-only temporary file, and run non-mutating pg_restore -l."
        ),
        examples=(
            "flow-steward backup verify backups/flow-steward-20260725T060000Z.dump.enc",
            "flow-steward backup verify backups/flow-steward.dump.enc --json",
        ),
        operation_type=OperationType.READ,
        interactive_policy=InteractivePolicy.IMMEDIATE,
        fields=(
            FieldSpec(
                key="backup",
                label="Backup",
                type=FieldType.PATH,
                required=True,
                positional=True,
                description="Protected backup path that must resolve below the backups/ directory.",
                examples=("backups/flow-steward-20260725T060000Z.dump.enc",),
            ),
        ),
        input_model=BackupVerifyRequest,
        result_model=BackupResult,
        plan_builder_ref=None,
        confirmation_policy_ref=None,
        service_ref=_TOKEN_BACKUP_VERIFY,
        result_renderer_ref=_TOKEN_BACKUP_VERIFY,
    ),
)

__all__ = ["BACKUP_COMMAND_SPECS"]
