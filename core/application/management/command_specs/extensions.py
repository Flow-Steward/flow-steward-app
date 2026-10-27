"""Extension-domain public command specifications."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from core.application.extensions.identity import normalize_extension_id
from core.application.extensions.scaffold_contracts import (
    ExtensionScaffoldCreateInput,
    ExtensionScaffoldResult,
)
from core.application.management.command_models import (
    ExtensionArchiveInstallResultView,
    ExtensionArchiveUninstallResultView,
    ExtensionEnableResultView,
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
from core.application.management.query_models_project_pages import ExtensionHealthView
from core.application.management.query_models_registry import (
    ExtensionDetailView,
    ExtensionListView,
)
from core.application.tooling.extensions.models import (
    ExtensionBundleRequest,
    ExtensionBundleResult,
    ExtensionDependencyLockRequest,
    ExtensionDependencyLockResult,
    ExtensionTestRequest,
    ExtensionTestResult,
    ExtensionValidationRequest,
    ExtensionValidationResult,
)

_TOKEN_EXTENSIONS_LIST = "extensions.list"
_TOKEN_EXTENSIONS_SHOW = "extensions.show"
_TOKEN_EXTENSIONS_HEALTH = "extensions.health"
_TOKEN_EXTENSIONS_INSTALL = "extensions.install"
_TOKEN_EXTENSIONS_UNINSTALL = "extensions.uninstall"
_TOKEN_EXTENSIONS_ENABLE = "extensions.enable"
_TOKEN_EXTENSIONS_DISABLE = "extensions.disable"
_TOKEN_EXTENSIONS_CREATE = "extensions.create"
_TOKEN_EXTENSIONS_VALIDATE = "extensions.validate"
_TOKEN_EXTENSIONS_TEST = "extensions.test"
_TOKEN_EXTENSIONS_BUNDLE = "extensions.bundle"
_TOKEN_EXTENSIONS_LOCK_DEPS = "extensions.lock-deps"
_TOKEN_WORKSPACE_EXTENSIONS = "workspace/extensions"


class ExtensionsListCommandInput(BaseModel):
    """Validated input for extension inventory listing."""

    model_config = ConfigDict(extra="forbid")

    state: str = "all"


class ExtensionShowCommandInput(BaseModel):
    """Validated input for extension detail display."""

    model_config = ConfigDict(extra="forbid")

    extension_id: str

    @field_validator("extension_id")
    @classmethod
    def validate_extension_id(cls, value: str) -> str:
        return normalize_extension_id(value)


class ExtensionInstallCommandInput(BaseModel):
    """Validated input for installing an extension archive."""

    model_config = ConfigDict(extra="forbid")

    archive: Path
    approved_browser_authority_impact_hash: str = Field(
        default="", max_length=64, pattern=r"^(?:|[a-f0-9]{64})$"
    )


class ExtensionUninstallRequestInput(BaseModel):
    """Public input for preparing an extension uninstall."""

    model_config = ConfigDict(extra="forbid")

    extension_id: str

    @field_validator("extension_id")
    @classmethod
    def validate_extension_id(cls, value: str) -> str:
        return normalize_extension_id(value)


class ExtensionUninstallCommandInput(ExtensionUninstallRequestInput):
    """Internal execution input bound to the freshly prepared plan revision."""

    plan_revision: str = Field(min_length=1)


class ExtensionToggleCommandInput(BaseModel):
    """Validated input for enabling or disabling an extension."""

    model_config = ConfigDict(extra="forbid")

    extension_id: str
    version: str = ""
    config: dict[str, Any] = Field(default_factory=dict)
    approved_browser_authority_impact_hash: str = Field(
        default="", max_length=64, pattern=r"^(?:|[a-f0-9]{64})$"
    )

    @field_validator("extension_id")
    @classmethod
    def validate_extension_id(cls, value: str) -> str:
        return normalize_extension_id(value)


STATE_CHOICES = (
    ChoiceItem(value="all", label="All"),
    ChoiceItem(value="active", label="Active"),
    ChoiceItem(value="disabled", label="Disabled"),
)


def _extension_id_field(*, positional: bool = True) -> FieldSpec:
    return FieldSpec(
        key="extension_id",
        label="Extension ID",
        type=FieldType.EXTENSION_ID,
        required=True,
        description="Canonical lowercase dot-separated package ID (for example, acme.example).",
        examples=("acme.crm-sync", "com.example.notifications"),
        positional=positional,
        option_name=None if positional else "--extension-id",
    )


EXTENSIONS_LIST_SPEC = CommandSpec(
    command_id=_TOKEN_EXTENSIONS_LIST,
    path=("extensions", "list"),
    summary="List installed extensions.",
    description="List extension inventory visible to the configured management runtime.",
    examples=("flow-steward extensions list --state all",),
    operation_type=OperationType.READ,
    interactive_policy=InteractivePolicy.IMMEDIATE,
    fields=(
        FieldSpec(
            key="state",
            label="State",
            type=FieldType.SINGLE_SELECT,
            choices=STATE_CHOICES,
            default="all",
            description="Installed extension state used to filter the inventory.",
            examples=("all", "active", "disabled"),
            option_name="--state",
        ),
    ),
    input_model=ExtensionsListCommandInput,
    result_model=ExtensionListView,
    plan_builder_ref=None,
    confirmation_policy_ref=None,
    service_ref=_TOKEN_EXTENSIONS_LIST,
    result_renderer_ref=_TOKEN_EXTENSIONS_LIST,
)


EXTENSIONS_SHOW_SPEC = CommandSpec(
    command_id=_TOKEN_EXTENSIONS_SHOW,
    path=("extensions", "show"),
    summary="Show extension details.",
    description="Show runtime, manifest, validation, and project context for one extension.",
    examples=("flow-steward extensions show acme.crm-sync",),
    operation_type=OperationType.READ,
    interactive_policy=InteractivePolicy.IMMEDIATE,
    fields=(_extension_id_field(),),
    input_model=ExtensionShowCommandInput,
    result_model=ExtensionDetailView,
    plan_builder_ref=None,
    confirmation_policy_ref=None,
    service_ref=_TOKEN_EXTENSIONS_SHOW,
    result_renderer_ref=_TOKEN_EXTENSIONS_SHOW,
)


EXTENSIONS_HEALTH_SPEC = CommandSpec(
    command_id=_TOKEN_EXTENSIONS_HEALTH,
    path=("extensions", "health"),
    summary="Probe extension runtime health.",
    description="Run the canonical runtime health probe for one installed extension.",
    examples=("flow-steward extensions health acme.crm-sync",),
    operation_type=OperationType.READ,
    interactive_policy=InteractivePolicy.IMMEDIATE,
    fields=(_extension_id_field(),),
    input_model=ExtensionShowCommandInput,
    result_model=ExtensionHealthView,
    plan_builder_ref=None,
    confirmation_policy_ref=None,
    service_ref=_TOKEN_EXTENSIONS_HEALTH,
    result_renderer_ref=_TOKEN_EXTENSIONS_HEALTH,
)


EXTENSIONS_INSTALL_SPEC = CommandSpec(
    command_id=_TOKEN_EXTENSIONS_INSTALL,
    compact_support=CompactSupport.DELEGATED_SERVICE,
    path=("extensions", "install"),
    summary="Install an extension archive.",
    description="Install a validated extension .zip archive through the shared lifecycle service.",
    examples=("flow-steward extensions install --archive dist/extensions/acme.crm-sync.zip",),
    operation_type=OperationType.CHANGE,
    interactive_policy=InteractivePolicy.ALWAYS_REVIEW,
    fields=(
        FieldSpec(
            key="archive",
            label="Archive",
            type=FieldType.PATH,
            required=True,
            description="Path to the validated extension ZIP archive to install.",
            examples=("dist/extensions/acme.crm-sync.zip",),
            option_name="--archive",
        ),
        FieldSpec(
            key="approved_browser_authority_impact_hash",
            label="Approved browser authority impact hash",
            type=FieldType.TEXT,
            advanced=True,
            description=(
                "Exact impact hash returned by a browser_authority_approval_required retry."
            ),
            examples=("a" * 64,),
            option_name="--approve-browser-authority",
        ),
    ),
    input_model=ExtensionInstallCommandInput,
    result_model=ExtensionArchiveInstallResultView,
    plan_builder_ref=None,
    confirmation_policy_ref=None,
    service_ref=_TOKEN_EXTENSIONS_INSTALL,
    result_renderer_ref=_TOKEN_EXTENSIONS_INSTALL,
)


EXTENSIONS_UNINSTALL_SPEC = CommandSpec(
    command_id=_TOKEN_EXTENSIONS_UNINSTALL,
    compact_support=CompactSupport.DELEGATED_SERVICE,
    path=("extensions", "uninstall"),
    summary="Uninstall an extension.",
    description="Uninstall an extension after explicit review of a current impact plan.",
    examples=("flow-steward extensions uninstall acme.crm-sync",),
    operation_type=OperationType.DESTRUCTIVE,
    interactive_policy=InteractivePolicy.ALWAYS_REVIEW,
    fields=(_extension_id_field(),),
    input_model=ExtensionUninstallRequestInput,
    result_model=ExtensionArchiveUninstallResultView,
    plan_builder_ref="extensions.uninstall.preview",
    confirmation_policy_ref="match:extension_id",
    service_ref=_TOKEN_EXTENSIONS_UNINSTALL,
    result_renderer_ref=_TOKEN_EXTENSIONS_UNINSTALL,
)


EXTENSIONS_ENABLE_SPEC = CommandSpec(
    command_id=_TOKEN_EXTENSIONS_ENABLE,
    path=("extensions", "enable"),
    summary="Enable an extension.",
    description="Enable an installed extension for the current management scope.",
    examples=("flow-steward extensions enable acme.crm-sync",),
    operation_type=OperationType.CHANGE,
    interactive_policy=InteractivePolicy.ALWAYS_REVIEW,
    fields=(
        _extension_id_field(),
        FieldSpec(
            key="version",
            label="Version",
            type=FieldType.TEXT,
            advanced=True,
            description="Installed extension version to enable when multiple versions are present.",
            examples=("1.4.0",),
            option_name="--extension-version",
        ),
        FieldSpec(
            key="approved_browser_authority_impact_hash",
            label="Approved browser authority impact hash",
            type=FieldType.TEXT,
            advanced=True,
            description=(
                "Exact impact hash returned by a browser_authority_approval_required retry."
            ),
            examples=("a" * 64,),
            option_name="--approve-browser-authority",
        ),
    ),
    input_model=ExtensionToggleCommandInput,
    result_model=ExtensionEnableResultView,
    plan_builder_ref=None,
    confirmation_policy_ref=None,
    service_ref=_TOKEN_EXTENSIONS_ENABLE,
    result_renderer_ref=_TOKEN_EXTENSIONS_ENABLE,
)


EXTENSIONS_DISABLE_SPEC = CommandSpec(
    command_id=_TOKEN_EXTENSIONS_DISABLE,
    path=("extensions", "disable"),
    summary="Disable an extension.",
    description="Disable an installed extension for the current management scope.",
    examples=("flow-steward extensions disable acme.crm-sync",),
    operation_type=OperationType.CHANGE,
    interactive_policy=InteractivePolicy.ALWAYS_REVIEW,
    fields=(
        _extension_id_field(),
        FieldSpec(
            key="version",
            label="Version",
            type=FieldType.TEXT,
            advanced=True,
            description="Installed extension version to disable when multiple versions are present.",
            examples=("1.4.0",),
            option_name="--extension-version",
        ),
    ),
    input_model=ExtensionToggleCommandInput,
    result_model=ExtensionEnableResultView,
    plan_builder_ref=None,
    confirmation_policy_ref=None,
    service_ref=_TOKEN_EXTENSIONS_DISABLE,
    result_renderer_ref=_TOKEN_EXTENSIONS_DISABLE,
)


EXTENSIONS_CREATE_SPEC = CommandSpec(
    command_id=_TOKEN_EXTENSIONS_CREATE,
    path=("extensions", "create"),
    summary="Create an extension scaffold.",
    description="Create a canonical extension scaffold without overwriting an existing target.",
    examples=(
        "flow-steward extensions create acme.crm-sync",
        "flow-steward extensions create com.example.notifications",
    ),
    operation_type=OperationType.CHANGE,
    interactive_policy=InteractivePolicy.ALWAYS_REVIEW,
    fields=(
        _extension_id_field(),
        FieldSpec(
            key="output_root",
            label="Output root",
            type=FieldType.PATH,
            default="extensions",
            description="Directory under which the new extension scaffold is created.",
            examples=("extensions", _TOKEN_WORKSPACE_EXTENSIONS),
            option_name="--output-root",
        ),
        FieldSpec(
            key="display_name",
            label="Display name",
            type=FieldType.TEXT,
            description="Human-readable product name written to the extension manifest.",
            examples=("CRM Sync",),
            option_name="--display-name",
        ),
        FieldSpec(
            key="description",
            label="Description",
            type=FieldType.TEXT,
            description="Short operator-facing summary written to the extension manifest.",
            examples=("Synchronizes customer records with the CRM.",),
            option_name="--description",
        ),
        FieldSpec(
            key="vendor_name",
            label="Vendor name",
            type=FieldType.TEXT,
            default="Your Organization",
            description="Publisher or organization name written to extension vendor metadata.",
            examples=("Acme Integrations",),
            option_name="--vendor-name",
        ),
        FieldSpec(
            key="category",
            label="Category",
            type=FieldType.TEXT,
            default="other",
            description="Catalog category used to classify the generated extension.",
            examples=("integration", "productivity"),
            option_name="--category",
        ),
    ),
    input_model=ExtensionScaffoldCreateInput,
    result_model=ExtensionScaffoldResult,
    plan_builder_ref=None,
    confirmation_policy_ref=None,
    service_ref=_TOKEN_EXTENSIONS_CREATE,
    result_renderer_ref=_TOKEN_EXTENSIONS_CREATE,
)


EXTENSIONS_VALIDATE_SPEC = CommandSpec(
    command_id=_TOKEN_EXTENSIONS_VALIDATE,
    path=("extensions", "validate"),
    summary="Validate an extension bundle.",
    description="Run manifest and optional health/marketplace validation for one extension.",
    examples=("flow-steward extensions validate acme.crm-sync --health",),
    operation_type=OperationType.READ,
    interactive_policy=InteractivePolicy.IMMEDIATE,
    fields=(
        _extension_id_field(),
        FieldSpec(
            key="root",
            label="Root",
            type=FieldType.PATH,
            description="Root directory containing the extension bundle to validate.",
            examples=("extensions", _TOKEN_WORKSPACE_EXTENSIONS),
            option_name="--root",
        ),
        FieldSpec(
            key="health",
            label="Health",
            type=FieldType.BOOLEAN,
            description="Run the extension health check in addition to manifest validation.",
            examples=("true", "false"),
            option_name="--health",
        ),
        FieldSpec(
            key="marketplace",
            label="Marketplace",
            type=FieldType.BOOLEAN,
            description="Apply marketplace publication requirements during validation.",
            examples=("true", "false"),
            option_name="--marketplace",
        ),
    ),
    input_model=ExtensionValidationRequest,
    result_model=ExtensionValidationResult,
    plan_builder_ref=None,
    confirmation_policy_ref=None,
    service_ref=_TOKEN_EXTENSIONS_VALIDATE,
    result_renderer_ref=_TOKEN_EXTENSIONS_VALIDATE,
)


EXTENSIONS_TEST_SPEC = CommandSpec(
    command_id=_TOKEN_EXTENSIONS_TEST,
    compact_support=CompactSupport.TOOLING_SERVICE,
    path=("extensions", "test"),
    summary="Run focused extension tests.",
    description="Run validation, smoke checks, and local pytest targets for one extension.",
    examples=("flow-steward extensions test acme.crm-sync --allow-no-pytest",),
    operation_type=OperationType.READ,
    interactive_policy=InteractivePolicy.IMMEDIATE,
    fields=(
        _extension_id_field(),
        FieldSpec(
            key="root",
            label="Root",
            type=FieldType.PATH,
            description="Root directory containing the extension bundle under test.",
            examples=("extensions", _TOKEN_WORKSPACE_EXTENSIONS),
            option_name="--root",
        ),
        FieldSpec(
            key="skip_validate",
            label="Skip validation",
            type=FieldType.BOOLEAN,
            description="Skip manifest and bundle validation before other extension tests.",
            examples=("true", "false"),
            option_name="--skip-validate",
        ),
        FieldSpec(
            key="skip_smoke",
            label="Skip smoke",
            type=FieldType.BOOLEAN,
            description="Skip the extension runtime smoke check.",
            examples=("true", "false"),
            option_name="--skip-smoke",
        ),
        FieldSpec(
            key="skip_pytest",
            label="Skip pytest",
            type=FieldType.BOOLEAN,
            description="Skip extension-local pytest targets.",
            examples=("true", "false"),
            option_name="--skip-pytest",
        ),
        FieldSpec(
            key="allow_no_pytest",
            label="Allow no pytest",
            type=FieldType.BOOLEAN,
            description="Treat an extension with no pytest targets as a valid test run.",
            examples=("true", "false"),
            option_name="--allow-no-pytest",
        ),
        FieldSpec(
            key="test_env",
            label="Test environment variable",
            type=FieldType.MULTI_SELECT,
            default=(),
            description=(
                "Name of an environment variable to hand this test run from your own "
                "environment. Repeatable. Values are read at invocation and never stored; "
                "platform variables are refused."
            ),
            examples=("ACME_SANDBOX_DSN",),
            option_name="--test-env",
            advanced=True,
        ),
        FieldSpec(
            key="test_paths",
            label="Test path",
            type=FieldType.MULTI_SELECT,
            default=(),
            description=(
                "Pytest target inside the bundle, such as tests/test_api.py or a node id. "
                "Repeatable. A selection stops at the first failure; without one the whole "
                "suite runs."
            ),
            examples=("tests/test_api.py",),
            option_name="--test-path",
            advanced=True,
        ),
    ),
    input_model=ExtensionTestRequest,
    result_model=ExtensionTestResult,
    plan_builder_ref=None,
    confirmation_policy_ref=None,
    service_ref=_TOKEN_EXTENSIONS_TEST,
    result_renderer_ref=_TOKEN_EXTENSIONS_TEST,
)


EXTENSIONS_BUNDLE_SPEC = CommandSpec(
    command_id=_TOKEN_EXTENSIONS_BUNDLE,
    path=("extensions", "bundle"),
    summary="Bundle an extension archive.",
    description="Create a distributable .zip archive for one extension bundle.",
    examples=("flow-steward extensions bundle acme.crm-sync --output-dir dist/extensions",),
    operation_type=OperationType.CHANGE,
    interactive_policy=InteractivePolicy.ALWAYS_REVIEW,
    fields=(
        _extension_id_field(),
        FieldSpec(
            key="root",
            label="Root",
            type=FieldType.PATH,
            description="Root directory containing the extension bundle to package.",
            examples=("extensions", _TOKEN_WORKSPACE_EXTENSIONS),
            option_name="--root",
        ),
        FieldSpec(
            key="out_dir",
            label="Output directory",
            type=FieldType.PATH,
            default="dist/extensions",
            description="Directory where the distributable extension archive is written.",
            examples=("dist/extensions", "release/extensions"),
            option_name="--output-dir",
        ),
        FieldSpec(
            key="version",
            label="Version",
            type=FieldType.TEXT,
            description="Release version assigned to the generated extension archive.",
            examples=("1.4.0",),
            option_name="--extension-version",
        ),
        FieldSpec(
            key="runtime_target",
            label="Runtime target",
            type=FieldType.TEXT,
            advanced=True,
            description="Linux target for a native executable archive; not a Python wheel tag.",
            examples=("linux-amd64", "linux-arm64"),
            option_name="--runtime-target",
        ),
        FieldSpec(
            key="with_wheels",
            label="With wheels",
            type=FieldType.BOOLEAN,
            advanced=True,
            description="Download hash-locked wheels for both default Linux architectures before packaging.",
            examples=("true", "false"),
            option_name="--with-wheels",
        ),
        FieldSpec(
            key="platforms",
            label="Platforms",
            type=FieldType.TEXT,
            advanced=True,
            description="Comma-separated target platform tags for bundled wheel artifacts.",
            examples=("manylinux2014_x86_64,manylinux2014_aarch64",),
            option_name="--platforms",
        ),
    ),
    input_model=ExtensionBundleRequest,
    result_model=ExtensionBundleResult,
    plan_builder_ref=None,
    confirmation_policy_ref=None,
    service_ref=_TOKEN_EXTENSIONS_BUNDLE,
    result_renderer_ref=_TOKEN_EXTENSIONS_BUNDLE,
)


EXTENSIONS_LOCK_DEPS_SPEC = CommandSpec(
    command_id=_TOKEN_EXTENSIONS_LOCK_DEPS,
    path=("extensions", "lock-deps"),
    summary="Lock extension Python dependencies.",
    description="Resolve direct Python requirements and write manifest dependency metadata.",
    examples=("flow-steward extensions lock-deps acme.crm-sync --input requirements.in",),
    operation_type=OperationType.CHANGE,
    interactive_policy=InteractivePolicy.ALWAYS_REVIEW,
    fields=(
        _extension_id_field(),
        FieldSpec(
            key="root",
            label="Root",
            type=FieldType.PATH,
            description="Root directory containing the extension whose dependencies are locked.",
            examples=("extensions", _TOKEN_WORKSPACE_EXTENSIONS),
            option_name="--root",
        ),
        FieldSpec(
            key="input",
            label="Input",
            type=FieldType.PATH,
            description="Direct Python requirements file used to resolve the dependency lock.",
            examples=("requirements.in", "config/extension-requirements.in"),
            option_name="--input",
        ),
        FieldSpec(
            key="platforms",
            label="Platforms",
            type=FieldType.TEXT,
            advanced=True,
            description="Wheel platform tags to lock; defaults to both supported Linux architectures.",
            examples=("manylinux2014_x86_64,manylinux2014_aarch64",),
            option_name="--platforms",
        ),
    ),
    input_model=ExtensionDependencyLockRequest,
    result_model=ExtensionDependencyLockResult,
    plan_builder_ref=None,
    confirmation_policy_ref=None,
    service_ref=_TOKEN_EXTENSIONS_LOCK_DEPS,
    result_renderer_ref=_TOKEN_EXTENSIONS_LOCK_DEPS,
)


EXTENSION_COMMAND_SPECS = (
    EXTENSIONS_LIST_SPEC,
    EXTENSIONS_SHOW_SPEC,
    EXTENSIONS_HEALTH_SPEC,
    EXTENSIONS_INSTALL_SPEC,
    EXTENSIONS_UNINSTALL_SPEC,
    EXTENSIONS_ENABLE_SPEC,
    EXTENSIONS_DISABLE_SPEC,
    EXTENSIONS_CREATE_SPEC,
    EXTENSIONS_VALIDATE_SPEC,
    EXTENSIONS_TEST_SPEC,
    EXTENSIONS_BUNDLE_SPEC,
    EXTENSIONS_LOCK_DEPS_SPEC,
)
