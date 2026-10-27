# ruff: noqa: F401
from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal

from pydantic import ConfigDict, Field, field_validator

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

_TOKEN_A_F0_9_64 = "^(?:|[a-f0-9]{64})$"

GENERATE_A_RELEASE_READINESS_REPORT_FOR_VERSION_2_MESSAGE = (
    "Generate a release-readiness report for version 2.4."
)


class ExtensionInstallInput(ViewModel):
    extension_id: str
    version: str = "1.0.0"
    config: dict[str, Any] = Field(default_factory=dict)


class ExtensionValidateResultView(ViewModel):
    valid: bool = False
    extension_id: str = ""
    errors: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class ExtensionBuildResultView(ViewModel):
    status: str = "built"
    extension_id: str = ""
    version: str = ""
    output_path: str = ""
    errors: list[str] = Field(default_factory=list)


class ExtensionImportResultView(ViewModel):
    status: str = "imported"
    extension_id: str = ""
    version: str = ""
    imported: list[dict[str, Any]] = Field(default_factory=list)
    skipped: list[dict[str, Any]] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)


class ExtensionDescriptorValidateInput(ViewModel):
    source_type: str = "file"  # file | url | builtin | payload
    descriptor_path: str = ""
    descriptor_url: str = ""
    descriptor_payload: dict[str, Any] = Field(default_factory=dict)
    source_root: str = ""
    platform_version: str = "1.0.0"
    allow_insecure_remote: bool = False


class ExtensionDescriptorImportFileInput(ViewModel):
    model_config = ConfigDict(extra="forbid", coerce_numbers_to_str=True)

    descriptor_path: str
    source_ref: str = ""
    install_after_import: bool = False
    runtime_mode: str = ""
    remote_base_url: str = ""
    approved_browser_authority_impact_hash: str = Field(
        default="", max_length=64, pattern=_TOKEN_A_F0_9_64
    )


class ExtensionDescriptorImportUrlInput(ViewModel):
    descriptor_url: str
    install_after_import: bool = False
    runtime_mode: str = ""
    remote_base_url: str = ""


class ExtensionReleaseInstallInput(ViewModel):
    model_config = ConfigDict(extra="forbid", coerce_numbers_to_str=True)

    release_id: str
    runtime_mode: str = ""
    remote_base_url: str = ""
    approved_oauth_impact_hash: str = Field(default="", max_length=64, pattern=_TOKEN_A_F0_9_64)
    approved_browser_authority_impact_hash: str = Field(
        default="", max_length=64, pattern=_TOKEN_A_F0_9_64
    )
    entitlement_status: Literal[
        "trial_active",
        "active",
        "grace_period",
        "trial_expired",
        "suspended_nonpayment",
        "disabled",
    ] = "trial_active"


class ExtensionRuntimeTokenInput(ViewModel):
    install_id: str
    connection_id: str = ""
    project_id: Any = None
    ttl_seconds: int = 3600


class ExtensionGeneratedJobCreateInput(ViewModel):
    source_job_id: Any = None
    request_payload: dict[str, Any] = Field(default_factory=dict)
    visibility: str = "private"


class ExtensionDescriptorValidateResultView(ViewModel):
    valid: bool = False
    extension_id: str = ""
    release_version: str = ""
    execution_modes: list[str] = Field(default_factory=list)
    normalized: dict[str, Any] = Field(default_factory=dict)
    errors: list[dict[str, Any]] = Field(default_factory=list)
    warnings: list[dict[str, Any]] = Field(default_factory=list)


class ExtensionReleaseImportResultView(ViewModel):
    status: str = "imported"
    release_id: str = ""
    extension_id: str = ""
    release_version: str = ""
    imported_artifacts: list[dict[str, Any]] = Field(default_factory=list)
    install: dict[str, Any] | None = None
    errors: list[dict[str, Any]] = Field(default_factory=list)


class ExtensionInstallResultView(ViewModel):
    status: str = "installed"
    extension_id: str = ""
    version: str = ""
    account_id: str = ""
    install_id: str = ""
    release_id: str | None = None
    dependency_warnings: list[dict[str, Any]] = Field(default_factory=list)


class ExtensionToggleResultView(ViewModel):
    status: str = "ok"
    extension_id: str = ""
    state: str = ""  # active | disabled


class ExtensionUninstallResultView(ViewModel):
    status: str = "uninstalled"
    extension_id: str = ""


class ExtensionRuntimeHealthResultView(ViewModel):
    status: str = "ok"
    install_id: str = ""
    handshake_status: str = "unknown"
    health_status: str = "unknown"
    runtime_status: str = "unknown"
    last_error: str = ""


class ExtensionRuntimeTokenResultView(ViewModel):
    issued: bool = False
    token: str = ""
    token_type: str = "Bearer"
    install_id: str = ""
    expires_at: float | None = None
    ttl_seconds: int = 0
    reason: str = ""


class ExtensionGeneratedJobResultView(ViewModel):
    status: str = "created"
    generated_job_id: str = ""
    account_id: str = ""
    source_job_id: str | None = None
    visibility: str = "private"


class McpConnectorCreateInput(ViewModel):
    display_name: str
    description: str = ""
    transport: str = "streamable_http"
    base_url: str
    auth_mode: str = "none"
    headers: dict[str, str] = Field(default_factory=dict)
    oauth_config: dict[str, Any] = Field(default_factory=dict)
    allowlisted_tools: list[str] = Field(default_factory=list)
    namespace_prefix: str = ""
    enabled: bool = True
    #: Provenance of an install from the MCP server catalog. Set only by the
    #: catalog install service; the public create request does not carry them.
    catalog_item_id: str = ""
    catalog_remote_id: str = ""
    catalog_item_revision: int = 0


class McpConnectorUpdateInput(ViewModel):
    display_name: str | None = None
    description: str | None = None
    transport: str | None = None
    base_url: str | None = None
    auth_mode: str | None = None
    headers: dict[str, str] | None = None
    headers_mode: Literal["keep", "replace", "clear"] | None = None
    oauth_config: dict[str, Any] | None = None
    allowlisted_tools: list[str] | None = None
    namespace_prefix: str | None = None
    enabled: bool | None = None


class WorkspaceMutationResultView(ViewModel):
    id: str = ""
    workspace_key: str = ""
    display_name: str = ""
    description: str = ""
    account_id: str = ""
    tenant_id: str = ""
    status: str = ""
    archived: bool | None = None
    metadata_jsonb: dict[str, Any] = Field(default_factory=dict)


class DataContractMutationResultView(ViewModel):
    status: str = ""
    contract_id: str = ""
    enabled: bool | None = None


class FieldMutationResultView(ViewModel):
    status: str = ""
    field_key: str = ""
    enabled: bool | None = None


class PublishedBlockMutationResultView(ViewModel):
    status: str = ""
    block_key: str = ""
    enabled: bool | None = None


class GuardrailMutationResultView(ViewModel):
    status: str = ""
    id: str = ""
    enabled: bool | None = None


class GuardrailDeleteResultView(ViewModel):
    status: str = "deleted"


class GuardrailAgentsResultView(ViewModel):
    id: str = ""
    agents: list[str] = Field(default_factory=list)


class GuardrailRestoreResultView(ViewModel):
    status: str = "restored"
    guardrail_id: str = ""
    restored_from_entry_id: str = ""
    validation: dict[str, Any] = Field(default_factory=dict)
    snapshot: dict[str, Any] | None = None


class ChangelogRestoreResultView(ViewModel):
    status: str = "restored"
    entity_type: str = ""
    entity_id: str = ""
    restored_from_entry_id: str = ""
    new_snapshot: dict[str, Any] | None = None
    validation: dict[str, Any] = Field(default_factory=dict)


class SecurityPolicyUpdateResultView(ViewModel):
    status: str = "updated"
    policy: dict[str, Any] = Field(default_factory=dict)


class SecurityProjectPolicyUpdateResultView(ViewModel):
    status: str = "updated"
    project_id: str = ""


class SecurityCustomRuleResultView(ViewModel):
    status: str = "added"
    rule: dict[str, Any] = Field(default_factory=dict)


class ProjectConnectionDeleteResultView(ViewModel):
    status: str = "deleted"
    connection_id: str = ""


class WorkspaceDeleteExecuteResultView(ViewModel):
    status: str = "trashed"
    entity_type: str = "workspace"
    entity_id: str | int = ""
    reassigned_project_ids: list[str] = Field(default_factory=list)
    trashed_project_ids: list[str] = Field(default_factory=list)


class TrashResultView(ViewModel):
    status: str = "trashed"
    entity_type: str = ""
    entity_id: str | int = ""


class RestoreResultView(ViewModel):
    status: str = "restored"
    entity_type: str = ""
    entity_id: str | int = ""


class HardDeleteResultView(ViewModel):
    status: str = "hard_deleted"
    entity_type: str = ""
    entity_id: str | int = ""


class LifecyclePlanSummaryView(ViewModel):
    total_nodes: int = 0
    selectable_nodes: int = 0
    selected_nodes: int = 0
    projects: int = 0
    workflows: int = 0
    datasets: int = 0
    connections: int = 0


class LifecycleTreeNodeView(ViewModel):
    node_id: str = ""
    entity_type: str = ""
    entity_id: str = ""
    label: str = ""
    selectable: bool = False
    selected: bool = False
    status: str = ""
    children: list[dict[str, Any]] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class LifecyclePlanView(ViewModel):
    status: str = "planned"
    action_type: str = ""
    plan_version: str = ""
    root_entity: dict[str, Any] = Field(default_factory=dict)
    tree: dict[str, Any] = Field(default_factory=dict)
    impact_summary: dict[str, Any] = Field(default_factory=dict)
    auto_managed_cleanup_groups: list[dict[str, Any]] = Field(default_factory=list)
    selectable_product_nodes: list[dict[str, Any]] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    direct_dependency_conflicts: list[dict[str, Any]] = Field(default_factory=list)
    validation_errors: list[str] = Field(default_factory=list)
