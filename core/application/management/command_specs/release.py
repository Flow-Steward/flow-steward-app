"""Public pinned-release command specifications."""

from __future__ import annotations

from core.application.management.command_specs.contracts import (
    CommandSpec,
    CompactSupport,
    FieldSpec,
    FieldType,
    InteractivePolicy,
    OperationType,
)
from core.application.release.service import (
    ReleaseCheckRequest,
    ReleaseInfoRequest,
    ReleaseModulesApplyRequest,
    ReleaseModulesListRequest,
    ReleaseModulesResult,
    ReleaseResult,
    ReleaseUpgradeRequest,
)

_TOKEN_RELEASE_INFO = "release.info"
_TOKEN_RELEASE_CHECK = "release.check"
_TOKEN_RELEASE_UPGRADE = "release.upgrade"
_TOKEN_RELEASE_MODULES_LIST = "release.modules-list"
_TOKEN_RELEASE_MODULES_APPLY = "release.modules-apply"

RELEASE_COMMAND_SPECS: tuple[CommandSpec, ...] = (
    CommandSpec(
        command_id=_TOKEN_RELEASE_INFO,
        compact_support=CompactSupport.PLATFORM,
        path=("release", "info"),
        summary="Show the installed source or official release origin.",
        description=(
            "Show durable source commit provenance or the applied official version and "
            "immutable image identity without reading checkout VERSION or changing runtime state."
        ),
        examples=("flow-steward release info", "flow-steward release info --json"),
        operation_type=OperationType.READ,
        interactive_policy=InteractivePolicy.IMMEDIATE,
        fields=(),
        input_model=ReleaseInfoRequest,
        result_model=ReleaseResult,
        plan_builder_ref=None,
        confirmation_policy_ref=None,
        service_ref=_TOKEN_RELEASE_INFO,
        result_renderer_ref=_TOKEN_RELEASE_INFO,
    ),
    CommandSpec(
        command_id=_TOKEN_RELEASE_CHECK,
        compact_support=CompactSupport.PLATFORM,
        path=("release", "check"),
        summary="Check published stable GitHub Releases.",
        description=(
            "Compare an official installation with, or offer a source installation an explicit "
            "transition to, the newest usable stable GitHub Release for this profile and platform."
        ),
        examples=("flow-steward release check", "flow-steward release check --json"),
        operation_type=OperationType.READ,
        interactive_policy=InteractivePolicy.IMMEDIATE,
        fields=(),
        input_model=ReleaseCheckRequest,
        result_model=ReleaseResult,
        plan_builder_ref=None,
        confirmation_policy_ref=None,
        service_ref=_TOKEN_RELEASE_CHECK,
        result_renderer_ref=_TOKEN_RELEASE_CHECK,
    ),
    CommandSpec(
        command_id=_TOKEN_RELEASE_UPGRADE,
        compact_support=CompactSupport.PLATFORM,
        path=("release", "upgrade"),
        summary="Upgrade to an exact official release.",
        description=(
            "Preflight an exact release, enable maintenance, create a verified database "
            "backup, apply migrations and activate the release. Disable maintenance after "
            "healthy completion; leave it enabled on failure."
        ),
        examples=(
            "flow-steward release upgrade",
            "flow-steward release upgrade --target-version 2.0.0",
            "flow-steward release upgrade --dry-run",
        ),
        operation_type=OperationType.CHANGE,
        interactive_policy=InteractivePolicy.ALWAYS_REVIEW,
        fields=(
            FieldSpec(
                key="target_version",
                label="Target version",
                type=FieldType.TEXT,
                default="",
                option_name="--target-version",
                description="Optional stable X.Y.Z version; defaults to latest.",
                placeholder="2.0.0",
                examples=("2.0.0",),
            ),
            FieldSpec(
                key="dry_run",
                label="Dry run",
                type=FieldType.BOOLEAN,
                default=False,
                option_name="--dry-run",
                description="Run read-only preflight and show the exact reviewed plan.",
                examples=("true",),
            ),
            FieldSpec(
                key="skip_db_backup",
                label="Skip database backup",
                type=FieldType.BOOLEAN,
                default=False,
                option_name="--skip-db-backup",
                description="Accept the risk of upgrading without creating a database backup.",
                examples=("true",),
            ),
            FieldSpec(
                key="timeout_seconds",
                label="Timeout seconds",
                type=FieldType.INTEGER,
                default=ReleaseUpgradeRequest.model_fields["timeout_seconds"].default,
                option_name="--timeout-seconds",
                advanced=True,
                description="Timeout for the complete installer handoff, including service startup.",
            ),
        ),
        input_model=ReleaseUpgradeRequest,
        result_model=ReleaseResult,
        plan_builder_ref="release.upgrade.preview",
        confirmation_policy_ref=None,
        service_ref=_TOKEN_RELEASE_UPGRADE,
        result_renderer_ref=_TOKEN_RELEASE_UPGRADE,
        lifecycle_interrupts=True,
    ),
    CommandSpec(
        command_id=_TOKEN_RELEASE_MODULES_LIST,
        compact_support=CompactSupport.PLATFORM,
        path=("release", "modules-list"),
        summary="Show the active and staged platform modules.",
        description=(
            "Show the platform-module set every process role loads and each module "
            "version the deployment manager staged, without changing anything."
        ),
        examples=(
            "flow-steward release modules-list",
            "flow-steward release modules-list --json",
        ),
        operation_type=OperationType.READ,
        interactive_policy=InteractivePolicy.IMMEDIATE,
        fields=(),
        input_model=ReleaseModulesListRequest,
        result_model=ReleaseModulesResult,
        plan_builder_ref=None,
        confirmation_policy_ref=None,
        service_ref=_TOKEN_RELEASE_MODULES_LIST,
        result_renderer_ref=_TOKEN_RELEASE_MODULES_LIST,
    ),
    CommandSpec(
        command_id=_TOKEN_RELEASE_MODULES_APPLY,
        compact_support=CompactSupport.PLATFORM,
        path=("release", "modules-apply"),
        summary="Activate staged platform modules on the installed release.",
        description=(
            "Make the highest staged version of every module, or the named versions, the "
            "active set through the release upgrade: maintenance, a verified backup, the "
            "roles stopped, the list written, database migrations, the roles restarted "
            "and health checks. Maintenance stays enabled on failure."
        ),
        examples=(
            "flow-steward release modules-apply",
            "flow-steward release modules-apply --modules acme.ledger@1.2.0",
            "flow-steward release modules-apply --restore-previous",
        ),
        operation_type=OperationType.CHANGE,
        interactive_policy=InteractivePolicy.ALWAYS_REVIEW,
        fields=(
            FieldSpec(
                key="modules",
                label="Modules",
                type=FieldType.TEXT,
                default="",
                option_name="--modules",
                description=(
                    "Optional id@version items separated by commas; defaults to the "
                    "highest staged version of every module."
                ),
                placeholder="acme.ledger@1.2.0",
                examples=("acme.ledger@1.2.0",),
            ),
            FieldSpec(
                key="restore_previous",
                label="Restore previous modules",
                type=FieldType.BOOLEAN,
                default=False,
                option_name="--restore-previous",
                description=(
                    "End an unfinished module change by activating its previous set again."
                ),
                examples=("true",),
            ),
        ),
        input_model=ReleaseModulesApplyRequest,
        result_model=ReleaseResult,
        plan_builder_ref="release.modules-apply.preview",
        confirmation_policy_ref=None,
        service_ref=_TOKEN_RELEASE_MODULES_APPLY,
        result_renderer_ref=_TOKEN_RELEASE_MODULES_APPLY,
        lifecycle_interrupts=True,
    ),
)
