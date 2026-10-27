# ruff: noqa: F401
from __future__ import annotations

from collections.abc import Mapping
from typing import Annotated, Any, Literal

from pydantic import AfterValidator, ConfigDict, Field, field_validator

from core.application.extensions.identity import normalize_extension_id
from core.application.registry.identifier_keys import (
    normalize_tool_identifier,
    require_tool_identifier,
)
from core.application.registry.tool_manifest import ToolInterface

from .query_models import (
    AuthAccountMembershipView,
    AuthAccountView,
    AuthTokenRecordView,
    AuthUserView,
    DatasetConnectionView,
    DatasetQueryTemplateView,
    DatasetRecordView,
    JobDetailContractView,
    JobsAttentionGroupsView,
    JobsListView,
    ProjectJobsListView,
    ProjectsListView,
    ViewModel,
)

GENERATE_A_RELEASE_READINESS_REPORT_FOR_VERSION_2_MESSAGE = (
    "Generate a release-readiness report for version 2.4."
)


class AgentMutationResultView(ViewModel):
    model_config = ConfigDict(
        extra="ignore",
        json_schema_extra={
            "description": (
                "Mutation result returned after an internal agent create, update, toggle, "
                "delete, clone, or restore action."
            ),
        },
    )

    status: str
    agent_id: str
    enabled: bool | None = None
    source_agent_id: str | None = None
    restored_from_entry_id: str | None = None
    validation: dict[str, Any] | None = None
    snapshot: dict[str, Any] | None = None


class SkillMutationResultView(ViewModel):
    model_config = ConfigDict(
        extra="ignore",
        json_schema_extra={
            "description": (
                "Mutation result returned after an internal skill create, update, toggle, "
                "delete, clone, or restore action."
            ),
        },
    )

    status: str
    skill_id: str
    enabled: bool | None = None
    source_skill_id: str | None = None
    restored_from_entry_id: str | None = None
    validation: dict[str, Any] | None = None
    snapshot: dict[str, Any] | None = None


ToolIdentifier = Annotated[str, AfterValidator(normalize_tool_identifier)]
ToolPathIdentifier = Annotated[str, AfterValidator(require_tool_identifier)]


def _validate_tool_interface_payload(value: dict[str, Any]) -> dict[str, Any]:
    # Reuse the artifact owner without changing the submitted dictionary or
    # materializing defaults before the canonical store serializes it.
    ToolInterface.model_validate(value)
    return value


ToolInterfaceInput = Annotated[dict[str, Any], AfterValidator(_validate_tool_interface_payload)]


class ToolSaveInput(ViewModel):
    tool_id: ToolIdentifier
    version: str = "1.0.0"
    display_name: str
    description: str = ""
    enabled: bool = False
    category: str = "general"
    prompt: str = ""
    interface: ToolInterfaceInput = Field(default_factory=dict)
    implementation: dict[str, Any] = Field(default_factory=dict)
    config: dict[str, Any] = Field(default_factory=dict)
    builder_metadata_jsonb: dict[str, Any] = Field(default_factory=dict)
    tags: list[str] = Field(default_factory=list)


class ToolPatchInput(ViewModel):
    display_name: str | None = None
    description: str | None = None
    enabled: bool | None = None
    category: str | None = None
    prompt: str | None = None
    interface: ToolInterfaceInput | None = None
    implementation: dict[str, Any] | None = None
    config: dict[str, Any] | None = None
    builder_metadata_jsonb: dict[str, Any] | None = None
    tags: list[str] | None = None


class ToolCloneInput(ViewModel):
    tool_id: ToolIdentifier | None = None
    display_name: str | None = None


class ToolRestoreRevisionInput(ViewModel):
    entry_id: str | int


class ToolMutationResultView(ViewModel):
    model_config = ConfigDict(
        extra="ignore",
        json_schema_extra={
            "description": (
                "Mutation result returned after an internal tool create, update, toggle, "
                "delete, clone, or restore action."
            ),
        },
    )

    status: str
    tool_id: str
    enabled: bool | None = None
    source_tool_id: str | None = None
    restored_from_entry_id: str | None = None
    validation: dict[str, Any] | None = None
    snapshot: dict[str, Any] | None = None


class ToolInvocationInput(ViewModel):
    function_name: str | None = None
    arguments: dict[str, Any] = Field(default_factory=dict)


class ToolInvocationResultView(ViewModel):
    tool_id: str
    status: str = "succeeded"
    implementation_kind: str
    function_name: str
    result: Any = None
    catalog_entry: dict[str, Any] = Field(default_factory=dict)


class ExtensionScanResultView(ViewModel):
    root: str
    extensions: list[dict[str, Any]] = Field(default_factory=list)
    scanned_count: int = 0


class ExtensionEnableResultView(ViewModel):
    extension: dict[str, Any] = Field(default_factory=dict)


class ExtensionArchiveInstallResultView(ViewModel):
    extension: dict[str, Any] = Field(default_factory=dict)
    installed_path: str = ""


class ExtensionArchiveUninstallResultView(ViewModel):
    extension_id: str
    state: str = "uninstalled"
    removed_data: list[str] = Field(default_factory=list)


class ExtensionActionResultView(ViewModel):
    extension_id: str
    action_id: str
    action: dict[str, Any] = Field(default_factory=dict)


class ExtensionQueryResultView(ViewModel):
    extension_id: str
    query_id: str
    query: dict[str, Any] = Field(default_factory=dict)


class ExtensionInvocationResultView(ViewModel):
    extension_id: str
    invocation: dict[str, Any] = Field(default_factory=dict)
    # Artifact handles created during this invocation (from artifact_output_specs).
    produced_artifacts: list[dict[str, Any]] = Field(default_factory=list)
    produced_datasets: list[dict[str, Any]] = Field(default_factory=list)


class CommandDispatchInput(ViewModel):
    command_type: str
    target: dict[str, Any] = Field(default_factory=dict)
    payload: dict[str, Any] = Field(default_factory=dict)
    source: str = "api"
    correlation_id: str = ""


class CommandDispatchResultView(ViewModel):
    command_id: str = ""
    command_type: str = ""
    status: str = "accepted"  # accepted | executed | failed | rejected
    result: dict[str, Any] = Field(default_factory=dict)
    errors: list[dict[str, Any]] = Field(default_factory=list)


class CommandBatchInput(ViewModel):
    commands: list[CommandDispatchInput] = Field(default_factory=list)


class CommandBatchResultView(ViewModel):
    results: list[CommandDispatchResultView] = Field(default_factory=list)


class ProjectConnectionCreateInput(ViewModel):
    extension_id: str | None = None  # for extension/mcp_connector connections
    connector_id: str = ""  # for mcp_connector connections
    connection_kind: str = "extension"  # "extension" | "mcp_connector" | "extension_runtime"
    display_name: str = ""
    connection_type: str = ""
    environment: str = ""
    extension_install_id: str = ""
    runtime_mode: str | None = None  # "local" | "remote_https"
    remote_base_url: str = ""
    health_status: str = ""
    runtime_status: str = ""
    last_seen_ok_at: float | None = None
    last_error: str = ""
    enabled: bool = True
    connection_config: dict[str, Any] = Field(default_factory=dict)
    secret_refs: dict[str, Any] = Field(default_factory=dict)
    connection_routes: list[dict[str, Any]] = Field(default_factory=list)
    ingress_config: dict[str, Any] = Field(default_factory=dict)
    workflow_mappings: dict[str, str] = Field(default_factory=dict)
    tags: list[str] = Field(default_factory=list)

    @field_validator("extension_id", mode="before")
    @classmethod
    def _validate_extension_id(cls, value: Any) -> str | None:
        normalized = str(value or "").strip()
        return normalize_extension_id(normalized) if normalized else None

    @field_validator("runtime_mode", mode="before")
    @classmethod
    def _validate_runtime_mode(cls, value: Any) -> str | None:
        normalized = str(value or "").strip().lower()
        if not normalized:
            return None
        if normalized not in {"local", "remote_https"}:
            raise ValueError("runtime_mode must be local or remote_https")
        return normalized

    @field_validator("secret_refs", mode="before")
    @classmethod
    def _normalize_secret_refs(cls, value: Any) -> dict[str, Any] | None:
        if value is None:
            return None
        from core.application.management.project_connection import normalize_secret_refs_payload

        return normalize_secret_refs_payload(value)


class ProjectConnectionUpdateInput(ViewModel):
    enabled: bool | None = None
    display_name: str | None = None
    connection_type: str | None = None
    environment: str | None = None
    extension_install_id: str | None = None
    runtime_mode: str | None = None
    remote_base_url: str | None = None
    health_status: str | None = None
    runtime_status: str | None = None
    last_seen_ok_at: float | None = None
    last_error: str | None = None
    connection_config: dict[str, Any] | None = None
    secret_refs: dict[str, Any] | None = None
    connection_routes: list[dict[str, Any]] | None = None
    ingress_config: dict[str, Any] | None = None
    workflow_mappings: dict[str, str] | None = None
    tags: list[str] | None = None

    @field_validator("extension_install_id", mode="before")
    @classmethod
    def _normalize_extension_install_id(cls, value: Any) -> str | None:
        normalized = str(value or "").strip()
        return normalized or None

    @field_validator("runtime_mode", mode="before")
    @classmethod
    def _validate_runtime_mode(cls, value: Any) -> str | None:
        normalized = str(value or "").strip().lower()
        if not normalized:
            return None
        if normalized not in {"local", "remote_https"}:
            raise ValueError("runtime_mode must be local or remote_https")
        return normalized

    @field_validator("secret_refs", mode="before")
    @classmethod
    def _normalize_secret_refs(cls, value: Any) -> dict[str, Any] | None:
        if value is None:
            return None
        from core.application.management.project_connection import normalize_secret_refs_payload

        return normalize_secret_refs_payload(value)
