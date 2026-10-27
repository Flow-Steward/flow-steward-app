"""Public environment transition command specifications."""

from __future__ import annotations

from core.application.lifecycle.environment_service import (
    EnvironmentResult,
    EnvironmentShowRequest,
    EnvironmentSwitchRequest,
)
from core.application.management.command_specs.contracts import (
    ChoiceItem,
    CommandSpec,
    CompactSupport,
    FieldSpec,
    FieldType,
    InteractivePolicy,
    OperationType,
)

_TOKEN_ENVIRONMENT_SHOW = "environment.show"
_TOKEN_ENVIRONMENT_SWITCH = "environment.switch"

_ENVIRONMENT_CHOICES = tuple(
    ChoiceItem(value=value, label=label)
    for value, label in (
        ("development", "Development"),
        ("production", "Production"),
    )
)
_FILE_STORAGE_CHOICES = tuple(
    ChoiceItem(value=value, label=label)
    for value, label in (
        ("keep", "Keep current storage"),
        ("bundled", "Bundled storage"),
        ("external_s3", "External S3-compatible storage"),
    )
)
_ARTIFACT_DECISION_CHOICES = tuple(
    ChoiceItem(value=value, label=label)
    for value, label in (
        ("already_copied", "Required artifacts are already copied"),
        ("accept_unavailable", "Accept that existing artifacts may be unavailable"),
    )
)


ENVIRONMENT_COMMAND_SPECS: tuple[CommandSpec, ...] = (
    CommandSpec(
        command_id=_TOKEN_ENVIRONMENT_SHOW,
        compact_support=CompactSupport.REFUSED_UNDECLARED,
        path=("environment", "show"),
        summary="Show the active deployment environment.",
        description=(
            "Show the active environment, installed release, selected storage identity, "
            "and retained durable resources without mutation."
        ),
        examples=("flow-steward environment show", "flow-steward environment show --json"),
        operation_type=OperationType.READ,
        interactive_policy=InteractivePolicy.IMMEDIATE,
        fields=(),
        input_model=EnvironmentShowRequest,
        result_model=EnvironmentResult,
        plan_builder_ref=None,
        confirmation_policy_ref=None,
        service_ref=_TOKEN_ENVIRONMENT_SHOW,
        result_renderer_ref=_TOKEN_ENVIRONMENT_SHOW,
    ),
    CommandSpec(
        command_id=_TOKEN_ENVIRONMENT_SWITCH,
        compact_support=CompactSupport.REFUSED_UNDECLARED,
        path=("environment", "switch"),
        summary="Switch deployment environment without changing release.",
        description=(
            "Quiesce job admission, create and verify a protected backup, validate the "
            "target, require any storage availability decision, preserve the installed "
            "release, and restore the exact previous runtime on failure."
        ),
        examples=(
            "flow-steward environment switch production",
            "flow-steward environment switch development",
            "flow-steward environment switch production --file-storage external_s3 "
            "--s3-endpoint https://objects.example.test --s3-bucket artifacts "
            "--s3-region eu-west-1 --s3-credentials-file .secrets/artifacts.json "
            "--artifact-decision already_copied",
        ),
        operation_type=OperationType.CHANGE,
        interactive_policy=InteractivePolicy.ALWAYS_REVIEW,
        fields=(
            FieldSpec(
                key="target",
                label="Target environment",
                type=FieldType.SINGLE_SELECT,
                choices=_ENVIRONMENT_CHOICES,
                required=True,
                positional=True,
            ),
            FieldSpec(
                key="file_storage",
                label="File storage",
                type=FieldType.SINGLE_SELECT,
                choices=_FILE_STORAGE_CHOICES,
                default="keep",
                option_name="--file-storage",
            ),
            FieldSpec(
                key="s3_endpoint",
                label="S3 endpoint",
                type=FieldType.URL,
                required=True,
                option_name="--s3-endpoint",
                visible_if={"file_storage": "external_s3"},
            ),
            FieldSpec(
                key="s3_bucket",
                label="S3 bucket",
                type=FieldType.TEXT,
                required=True,
                option_name="--s3-bucket",
                visible_if={"file_storage": "external_s3"},
            ),
            FieldSpec(
                key="s3_region",
                label="S3 region",
                type=FieldType.TEXT,
                required=True,
                option_name="--s3-region",
                visible_if={"file_storage": "external_s3"},
            ),
            FieldSpec(
                key="s3_credentials_file",
                label="Owner-only S3 credentials file",
                type=FieldType.PATH,
                required=True,
                option_name="--s3-credentials-file",
                visible_if={"file_storage": "external_s3"},
                allow_secret_file=True,
            ),
            FieldSpec(
                key="allow_insecure_s3_http",
                label="Allow insecure development S3 HTTP",
                type=FieldType.BOOLEAN,
                default=False,
                option_name="--allow-insecure-s3-http",
                visible_if={"file_storage": "external_s3", "target": "development"},
                advanced=True,
            ),
            FieldSpec(
                key="artifact_decision",
                label="Existing artifact decision",
                type=FieldType.SINGLE_SELECT,
                choices=_ARTIFACT_DECISION_CHOICES,
                option_name="--artifact-decision",
                deferred_until_plan=True,
            ),
        ),
        input_model=EnvironmentSwitchRequest,
        result_model=EnvironmentResult,
        plan_builder_ref="environment.switch.preview",
        confirmation_policy_ref=None,
        service_ref=_TOKEN_ENVIRONMENT_SWITCH,
        result_renderer_ref=_TOKEN_ENVIRONMENT_SWITCH,
        lifecycle_interrupts=True,
    ),
)
