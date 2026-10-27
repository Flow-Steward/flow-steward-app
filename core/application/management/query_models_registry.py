# ruff: noqa: F405, I001, RUF100
from __future__ import annotations

from .query_models_common import *  # noqa: F403,F401  # NOSONAR
from .query_models_admin import *  # noqa: F403,F401  # NOSONAR
from .query_models_jobs import *  # noqa: F403,F401  # NOSONAR
from .query_models_workflows import *  # noqa: F403,F401  # NOSONAR
from .query_models_workflow_editor import *  # noqa: F403,F401  # NOSONAR
from .query_models_project_pages import *  # noqa: F403,F401  # NOSONAR


class DatasetRecordView(ViewModel):
    dataset_id: str = ""
    dataset_key: str = ""
    tenant_id: str = ""
    account_id: str = ""
    workspace_id: str = ""
    display_name: str = ""
    description: str = ""
    connector_kind: str = ""
    connector_id: str = ""
    runtime_kind: str = ""
    query_language: str = ""
    read_only: bool = True
    enabled: bool = True
    availability_mode: str = "specific_projects_only"
    schema_snapshot_jsonb: dict[str, Any] = Field(default_factory=dict)
    resource_scope_jsonb: dict[str, Any] = Field(default_factory=dict)
    connection_schema_jsonb: dict[str, Any] = Field(default_factory=dict)
    metadata_jsonb: dict[str, Any] = Field(default_factory=dict)
    project_ids: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    workspace_unresolved: bool | None = None


class DatasetsListView(ViewModel):
    datasets: list[DatasetRecordView] = Field(default_factory=list)


class DatasetConnectorView(ViewModel):
    connector_id: str = ""
    display_name: str = ""
    description: str = ""
    connector_kind: str = ""
    extension_id: str = ""
    local_connector_id: str = ""
    source_install_id: str = ""
    source_release_id: str = ""
    runtime_adapter_ref: str = ""
    connection_schema: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)


class DatasetConnectorsListView(ViewModel):
    connectors: list[DatasetConnectorView] = Field(default_factory=list)
    count: int = 0


class DatasetConnectionView(ViewModel):
    connection_id: str = ""
    tenant_id: str = ""
    account_id: str = ""
    workspace_id: str = ""
    workspace_display_name: str = ""
    display_name: str = ""
    description: str = ""
    connector_ref_jsonb: dict[str, Any] = Field(default_factory=dict)
    source_kind: str = ""
    environment: str = "dev"
    tags: list[str] = Field(default_factory=list)
    access_mode: str = "read_only"
    enabled: bool = True
    health_status: str = "unknown"
    last_checked_at: str | None = None
    last_schema_refresh_at: str | None = None
    last_error: str = ""
    linked_dataset_count: int = 0
    connection_config_jsonb: dict[str, Any] = Field(default_factory=dict)
    secret_refs_jsonb: dict[str, Any] = Field(default_factory=dict)
    metadata_jsonb: dict[str, Any] = Field(default_factory=dict)


class DatasetDataConnectionsListView(ViewModel):
    connections: list[DatasetConnectionView] = Field(default_factory=list)
    count: int = 0


class DatasetActionResponseView(ViewModel):
    ok: bool = False
    result: dict[str, Any] = Field(default_factory=dict)
    commands: list[Any] = Field(default_factory=list)
    events: list[Any] = Field(default_factory=list)
    reload: bool = False
    errors: list[Any] = Field(default_factory=list)


class DatasetQueryTemplateView(ViewModel):
    query_template_id: str = ""
    dataset_id: str = ""
    tenant_id: str = ""
    account_id: str = ""
    workspace_id: str = ""
    display_name: str = ""
    description: str = ""
    query_text: str = ""
    query_language: str = ""
    params_schema_jsonb: dict[str, Any] = Field(default_factory=dict)
    output_schema_jsonb: dict[str, Any] = Field(default_factory=dict)
    metadata_jsonb: dict[str, Any] = Field(default_factory=dict)
    tags: list[str] = Field(default_factory=list)
    enabled: bool = True


class DatasetQueryTemplatesListView(ViewModel):
    query_templates: list[DatasetQueryTemplateView] = Field(default_factory=list)
    count: int = 0


class DatasetProjectCatalogView(ViewModel):
    project_id: str = ""
    workspace_id: str = ""
    dataset_ids: list[str] = Field(default_factory=list)
    explicit_dataset_ids: list[str] = Field(default_factory=list)
    available_datasets: list[DatasetRecordView] = Field(default_factory=list)


class DatasetProjectAssignmentCatalogView(ViewModel):
    dataset_id: str = ""
    workspace_id: str = ""
    availability_mode: str = "specific_projects_only"
    project_ids: list[str] = Field(default_factory=list)
    explicit_project_ids: list[str] = Field(default_factory=list)
    available_projects: list[dict[str, Any]] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Project connection views (Phase 5)
# ---------------------------------------------------------------------------


class ProjectConnectionView(ViewModel):
    connection_id: str = ""
    project_id: str | None = None
    extension_id: str | None = None
    account_id: str = ""
    display_name: str = ""
    connection_kind: str = "extension"
    connection_type: str = ""
    environment: str = ""
    credential_revision: int = 1
    extension_install_id: str = ""
    runtime_mode: str = ""
    remote_base_url: str = ""
    health_status: str = ""
    runtime_status: str = ""
    last_seen_ok_at: Any = None
    last_error: str = ""
    enabled: bool = True
    connection_config: dict[str, Any] = Field(default_factory=dict)
    secret_refs: dict[str, Any] = Field(default_factory=dict)
    connection_routes: list[dict[str, Any]] = Field(default_factory=list)
    ingress_config: dict[str, Any] = Field(default_factory=dict)
    workflow_mappings: dict[str, str] = Field(default_factory=dict)
    tags: list[str] = Field(default_factory=list)
    created_at: Any = None
    updated_at: Any = None


class ProjectConnectionListView(ViewModel):
    connections: list[ProjectConnectionView] = Field(default_factory=list)
    project_id: str | None = None


# ---------------------------------------------------------------------------
# Extension views (Phase 6)
# ---------------------------------------------------------------------------


class ExtensionListFilterInput(ViewModel):
    q: str = ""
    state: list[str] = Field(default_factory=list)
    status_group: list[str] = Field(default_factory=list)
    runtime_mode: list[str] = Field(default_factory=list)
    entitlement_status: list[str] = Field(default_factory=list)
    origin: list[str] = Field(default_factory=list)
    source: list[str] = Field(default_factory=list)
    visibility: list[str] = Field(default_factory=list)
    has_runtime_module: bool | None = None
    connected: bool | None = None


class ExtensionSummaryView(ViewModel):
    extension_id: str = ""
    version: str = ""
    display_name: str = ""
    description: str = ""
    vendor_name: str = ""
    category: str = ""
    tags: list[str] = Field(default_factory=list)
    support_url: str = ""
    website_url: str = ""
    extension_type: str = ""
    install_id: str = ""
    release_id: str | None = None
    descriptor_origin: str = ""
    descriptor_source: str = ""
    runtime_mode: str = ""
    remote_base_url: str = ""
    handshake_status: str = ""
    health_status: str = ""
    runtime_status: str = ""
    state: str = "active"
    visibility: str = "account"
    source_label: str = ""
    entitlement_status: str = "unknown"
    status_group: str = "unknown"
    has_runtime_module: bool = False
    connected: bool = False
    flags: dict[str, bool] = Field(default_factory=dict)
    installed_at: str | None = None
    #: The install record is there but its files are not (lost volume, manual
    #: cleanup, crashed install): only Uninstall makes sense for it.
    files_missing: bool = False
    broken: bool = False
    broken_reason: str = ""
    #: The bundle's ``icon.png``: whether there is one, and where Core serves it.
    has_icon: bool = False
    icon_url: str = ""


class ExtensionListView(ViewModel):
    extensions: list[ExtensionSummaryView] = Field(default_factory=list)
    total: int = 0


class ExtensionArchiveLifecycleAvailabilityView(ViewModel):
    available: bool = False
    reason_code: str = "unavailable"
    message: str = ""
    remediation: dict[str, list[str]] = Field(default_factory=dict)


class ExtensionMarketplaceFilterInput(ViewModel):
    q: str = ""
    category: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    vendor: list[str] = Field(default_factory=list)
    contains: list[str] = Field(default_factory=list)
    channel: list[str] = Field(default_factory=list)
    execution_mode: list[str] = Field(default_factory=list)
    allows_self_hosted: bool | None = None
    requires_connected_control_plane: bool | None = None
    import_source: list[str] = Field(default_factory=list)
    compatibility: list[str] = Field(default_factory=list)
    origin: list[str] = Field(default_factory=list)
    billing_mode: list[str] = Field(default_factory=list)
    plan_id: list[str] = Field(default_factory=list)
    installed: bool | None = None
    page: int = 1
    per_page: int = 20
    sort: str = "updated_desc"
    facets: bool = False


class ExtensionMarketplaceFacetBucketView(ViewModel):
    value: str = ""
    count: int = 0


class ExtensionMarketplaceFacetsView(ViewModel):
    category: list[ExtensionMarketplaceFacetBucketView] = Field(default_factory=list)
    tags: list[ExtensionMarketplaceFacetBucketView] = Field(default_factory=list)
    vendor: list[ExtensionMarketplaceFacetBucketView] = Field(default_factory=list)
    channel: list[ExtensionMarketplaceFacetBucketView] = Field(default_factory=list)
    execution_mode: list[ExtensionMarketplaceFacetBucketView] = Field(default_factory=list)
    compatibility: list[ExtensionMarketplaceFacetBucketView] = Field(default_factory=list)
    origin: list[ExtensionMarketplaceFacetBucketView] = Field(default_factory=list)
    contains: list[ExtensionMarketplaceFacetBucketView] = Field(default_factory=list)
    billing_mode: list[ExtensionMarketplaceFacetBucketView] = Field(default_factory=list)
    plan_id: list[ExtensionMarketplaceFacetBucketView] = Field(default_factory=list)
    import_source: list[ExtensionMarketplaceFacetBucketView] = Field(default_factory=list)


class ExtensionMarketplaceItemView(ViewModel):
    extension_id: str = ""
    release_id: str = ""
    release_version: str = ""
    display_name: str = ""
    description: str = ""
    extension_type: str = ""
    category: str = ""
    tags: list[str] = Field(default_factory=list)
    vendor: str = ""
    channel: str = ""
    execution_modes: list[str] = Field(default_factory=list)
    allows_self_hosted: bool = False
    requires_connected_control_plane: bool = False
    import_sources: list[str] = Field(default_factory=list)
    compatibility: str = "unknown"
    origin: str = ""
    source: str = ""
    source_label: str = ""
    billing_mode: str = ""
    plan_id: str = ""
    contains: list[str] = Field(default_factory=list)
    installed: bool = False
    install_id: str | None = None
    install_state: str | None = None
    entitlement_status: str | None = None
    status_group: str | None = None
    updated_at: str | None = None
    flags: dict[str, Any] = Field(default_factory=dict)


class ExtensionMarketplaceListView(ViewModel):
    extensions: list[ExtensionMarketplaceItemView] = Field(default_factory=list)
    total: int = 0
    page: int = 1
    per_page: int = 20
    total_pages: int = 1
    facets: ExtensionMarketplaceFacetsView | None = None


class ExtensionDetailView(ExtensionSummaryView):
    artifacts: list[dict[str, Any]] = Field(default_factory=list)
    entitlement: dict[str, Any] | None = None
    connections: list[ProjectConnectionView] = Field(default_factory=list)
    projections: list[dict[str, Any]] = Field(default_factory=list)
    config: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)
    compiled_manifest: dict[str, Any] = Field(default_factory=dict)
    validation_summary: dict[str, Any] = Field(default_factory=dict)
    projects: list[ExtensionPageProjectView] = Field(default_factory=list)
    selected_project_id: str | None = None


class ExtensionSafeDetailView(ViewModel):
    """Non-administrative extension identity needed by project-owned UI."""

    extension_id: str = ""
    install_id: str = ""
    display_name: str = ""
    description: str = ""
    version: str = ""
    state: str = "active"
    active: bool = False
    broken: bool = False
    category: str = ""
    tags: list[str] = Field(default_factory=list)
    vendor_name: str = ""
    support_url: str = ""
    website_url: str = ""
    has_icon: bool = False
    icon_url: str = ""


# ---------------------------------------------------------------------------
# MCP connector views (Phase 7)
# ---------------------------------------------------------------------------


class McpConnectorView(ViewModel):
    connector_id: str = ""
    project_id: str = ""
    display_name: str = ""
    description: str = ""
    transport: str = ""
    base_url: str = ""
    auth_mode: str = "none"
    namespace_prefix: str = ""
    allowlisted_tools: list[str] = Field(default_factory=list)
    discovered_tools_jsonb: list[dict[str, Any]] = Field(default_factory=list)
    projected_tools: list[dict[str, Any]] = Field(default_factory=list)
    status: str = "unchecked"
    auth_status: str = "not_configured"
    oauth_status: dict[str, Any] = Field(default_factory=dict)
    last_synced_at: str | None = None
    last_sync_status: str = "never"
    last_sync_error: str = ""
    enabled: bool = True
    created_at: str | None = None
    updated_at: str | None = None
    has_client_id: bool = False
    has_client_secret: bool = False
    oauth_credential_revision: int = 1
    oauth_authorization_parameters: dict[str, str] = Field(default_factory=dict)
    oauth_requested_scopes: list[str] = Field(default_factory=list)
    oauth_refresh_token_required: bool = False
    #: Set when the connector was installed from the MCP server catalog; never
    #: rewritten by a later catalog change.
    catalog_item_id: str = ""
    catalog_remote_id: str = ""
    catalog_item_revision: int = 0


class McpConnectorListView(ViewModel):
    connectors: list[McpConnectorView] = Field(default_factory=list)
    total: int = 0


class McpConnectorHealthView(ViewModel):
    connector_id: str = ""
    status: str = "unchecked"
    auth_status: str = "not_configured"
    latency_ms: int | None = None
    error: str | None = None
    checked_at: str | None = None


class WorkspaceRecordView(ViewModel):
    id: str = ""
    workspace_key: str = ""
    display_name: str = ""
    description: str = ""
    account_id: str = ""
    tenant_id: str = ""
    status: str = ""
    archived: bool | None = None
    created_at: str | None = None
    updated_at: str | None = None
    metadata_jsonb: dict[str, Any] = Field(default_factory=dict)


class WorkspacesListView(ViewModel):
    workspaces: list[WorkspaceRecordView] = Field(default_factory=list)


class WorkspaceProjectsView(ViewModel):
    projects: list[ProjectGridRowView] = Field(default_factory=list)


class DataContractRecordView(ViewModel):
    contract_id: str = ""
    version: str = ""
    display_name: str = ""
    description: str = ""
    kind: str = "json"
    schema_: dict[str, Any] = Field(
        default_factory=dict,
        validation_alias=AliasChoices("schema"),
        serialization_alias="schema",
    )
    enabled: bool = True
    tags: list[str] = Field(default_factory=list)
    created_at: str | None = None
    updated_at: str | None = None


class DataContractsListView(ViewModel):
    data_contracts: list[DataContractRecordView] = Field(default_factory=list)


class FieldRecordView(ViewModel):
    field_key: str = ""
    display_name: str = ""
    description: str = ""
    value_type: str = "text"
    data_contract_id: str | None = None
    created_from_kind: str = ""
    created_from_ref: str = ""
    source_hints_json: dict[str, Any] = Field(default_factory=dict)
    manual_allowed: bool = True
    enabled: bool = True
    created_at: str | None = None
    updated_at: str | None = None


class FieldsListView(ViewModel):
    fields: list[FieldRecordView] = Field(default_factory=list)


class PublishedBlockInputView(ViewModel):
    field_id: str | None = None
    field_key: str = ""
    required: bool = False
    multiple_allowed: bool = False
    manual_allowed_override: bool | None = None
    default_source_hint: str = ""
    notes: str = ""
    sort_order: int = 0


class PublishedBlockOutputView(ViewModel):
    field_id: str | None = None
    field_key: str = ""
    required_by_default: bool = False
    exposed_by_default: bool = True
    notes: str = ""
    sort_order: int = 0


class PublishedBlockRecordView(ViewModel):
    block_key: str = ""
    display_name: str = ""
    block_type: str = ""
    source_kind: str = ""
    source_ref: str = ""
    description: str = ""
    family: str = ""
    enabled: bool = True
    inputs: list[PublishedBlockInputView] = Field(default_factory=list)
    outputs: list[PublishedBlockOutputView] = Field(default_factory=list)
    created_at: str | None = None
    updated_at: str | None = None


class PublishedBlocksListView(ViewModel):
    published_blocks: list[PublishedBlockRecordView] = Field(default_factory=list)


class GuardrailRecordView(ViewModel):
    id: str = ""
    guardrail_id: str = ""
    display_name: str = ""
    description: str = ""
    enabled: bool = True
    mode: str = ""
    rule_type: str = ""
    severity: str = ""
    config: dict[str, Any] = Field(default_factory=dict)
    prompt_text: str = ""
    agent_prompt: str = ""
    project_id: str | None = None
    sort_order: int = 0
    agents: list[str] = Field(default_factory=list)
    source_install_id: str | None = None
    source_release_id: str | None = None
    projection_mode: str | None = None
    created_at: str | None = None
    updated_at: str | None = None


class GuardrailsListView(ViewModel):
    guardrails: list[GuardrailRecordView] = Field(default_factory=list)


class GuardrailHistoryEntryView(ViewModel):
    id: str | int | None = None
    entity_type: str = "guardrail"
    entity_id: str = ""
    revision: int | None = None
    action: str = ""
    author: str = ""
    created_at: str | None = None
    snapshot: dict[str, Any] = Field(default_factory=dict)
    diff_summary: dict[str, Any] = Field(default_factory=dict)


class GuardrailDiffView(ViewModel):
    entity_type: str = "guardrail"
    entity_id: str = ""
    from_entry_id: str | None = None
    to_entry_id: str | None = None
    diff: dict[str, Any] = Field(default_factory=dict)


class RegistryStatsBucketView(ViewModel):
    total: int = 0
    enabled: int = 0


class RegistryStatsView(ViewModel):
    agents: RegistryStatsBucketView = Field(default_factory=RegistryStatsBucketView)
    tools: RegistryStatsBucketView = Field(default_factory=RegistryStatsBucketView)
    skills: RegistryStatsBucketView = Field(default_factory=RegistryStatsBucketView)
    workflows: RegistryStatsBucketView = Field(default_factory=RegistryStatsBucketView)
    guardrails: RegistryStatsBucketView = Field(default_factory=RegistryStatsBucketView)


class RegistryValidationView(ViewModel):
    valid: bool = False
    errors: list[dict[str, Any]] = Field(default_factory=list)
    warnings: list[dict[str, Any]] = Field(default_factory=list)


class RegistrySearchResultView(ViewModel):
    entity_type: str = ""
    entity_id: str = ""
    display_name: str = ""
    description: str = ""
    enabled: bool | None = None


class ChangelogEntryView(ViewModel):
    id: str | int | None = None
    entity_type: str = ""
    entity_id: str = ""
    action: str = ""
    author: str = ""
    created_at: str | None = None
    revision: int | None = None
    snapshot: dict[str, Any] = Field(default_factory=dict)
    meta: dict[str, Any] = Field(default_factory=dict)


class ChangelogDiffView(ViewModel):
    entity_type: str = ""
    entity_id: str = ""
    from_entry_id: str | None = None
    to_entry_id: str | None = None
    diff: dict[str, Any] = Field(default_factory=dict)


class SecurityPolicyWithSourcesView(ViewModel):
    effective_policy: dict[str, Any] = Field(default_factory=dict)
    global_policy: dict[str, Any] = Field(default_factory=dict)
    project_override: dict[str, Any] = Field(default_factory=dict)
    source_map: dict[str, Any] = Field(default_factory=dict)


class SecurityPostureView(ViewModel):
    score: int = 100
    level: str = "hardened"
    status: str | None = None
    category_scores: dict[str, Any] = Field(default_factory=dict)
    finding_counts: dict[str, Any] = Field(default_factory=dict)
    snapshot_at: str | None = None


class SecurityEventView(ViewModel):
    id: str = ""
    account_id: str | None = None
    project_id: str | None = None
    job_id: str | None = None
    surface: str = ""
    scanner: str = ""
    rule_id: str = ""
    severity: str = ""
    action: str = ""
    redacted_preview: str = ""
    trace_id: str | None = None
    scan_id: str | None = None
    policy_level: str | None = None
    duration_ms: int | None = None
    created_at: str | None = None


class SecurityEventsListView(ViewModel):
    events: list[SecurityEventView] = Field(default_factory=list)
    page: int = 1
    per_page: int = 25
    total: int | None = None
    error: str | None = None


class SecurityFindingView(ViewModel):
    id: str = ""
    event_id: str = ""
    scanner: str = ""
    rule_id: str = ""
    severity: str = ""
    confidence: str | None = None
    context: dict[str, Any] | None = None
    redacted_snippet: str = ""
    created_at: str | None = None


class SecurityFindingsListView(ViewModel):
    findings: list[SecurityFindingView] = Field(default_factory=list)
    page: int = 1
    per_page: int = 25
    total: int | None = None
    error: str | None = None


class ProjectAdminDetailView(ViewModel):
    id: str = ""
    name: str = ""
    description: str = ""
    workspace_id: str = ""
    default_agent_models: dict[str, Any] = Field(default_factory=dict)
    available_providers: list[Any] = Field(default_factory=list)
    available_models: list[str] = Field(default_factory=list)
    providers_with_models: dict[str, Any] = Field(default_factory=dict)


class McpConnectorDeleteResultView(ViewModel):
    status: str = "deleted"
    connector_id: str = ""


class TrashListItemView(ViewModel):
    entity_type: str = ""
    entity_id: str = ""
    label: str = ""
    deleted_at: str | None = None
    deleted_by: str | None = None


class TrashListView(ViewModel):
    items: list[TrashListItemView] = Field(default_factory=list)
    total: int = 0


class WorkspaceDeletePlanProjectView(ViewModel):
    project_id: str = ""
    label: str = ""
    status: str = ""
    can_reassign: bool = False
    current_workspace_id: str = ""
    current_workspace_label: str = ""


class WorkspaceDeletePlanWorkspaceView(ViewModel):
    workspace_id: str = ""
    label: str = ""
    status: str = ""


class WorkspaceDeletePlanSummaryView(ViewModel):
    active_project_count: int = 0
    trashed_project_count: int = 0


class WorkspaceDeletePlanView(ViewModel):
    status: str = "planned"
    action_type: str = "trash"
    workspace: WorkspaceDeletePlanWorkspaceView = Field(
        default_factory=WorkspaceDeletePlanWorkspaceView
    )
    projects: list[WorkspaceDeletePlanProjectView] = Field(default_factory=list)
    target_workspaces: list[dict[str, Any]] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    validation_errors: list[str] = Field(default_factory=list)
    summary: WorkspaceDeletePlanSummaryView = Field(default_factory=WorkspaceDeletePlanSummaryView)


# ---------------------------------------------------------------------------
# Chat slash-command views (safe UI projection)
# ---------------------------------------------------------------------------


class ChatSlashCommandView(ViewModel):
    """Safe UI metadata for one slash command — no handler refs or auth details."""

    path: str = ""
    title: str = ""
    description: str = ""
    usage_hint: str = ""
    target_hint: str | None = None
    is_contextual: bool = False


class ChatSlashCommandGroupView(ViewModel):
    group: str = ""
    label: str = ""
    commands: list[ChatSlashCommandView] = Field(default_factory=list)


class ChatSlashHelpView(ViewModel):
    groups: list[ChatSlashCommandGroupView] = Field(default_factory=list)
    total_commands: int = 0
