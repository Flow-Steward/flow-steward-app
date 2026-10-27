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
    ReleaseResult,
    ReleaseUpgradeRequest,
)

_TOKEN_RELEASE_INFO = "release.info"
_TOKEN_RELEASE_CHECK = "release.check"
_TOKEN_RELEASE_UPGRADE = "release.upgrade"

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
            "Require enabled maintenance, preflight an exact stable release and official "
            "image set, then hand activation to the installer. Database backup and migration "
            "remain explicit operator actions."
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
                key="timeout_seconds",
                label="Timeout seconds",
                type=FieldType.INTEGER,
                default=60,
                option_name="--timeout-seconds",
                advanced=True,
                description="Timeout for individual release runtime operations.",
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
)
