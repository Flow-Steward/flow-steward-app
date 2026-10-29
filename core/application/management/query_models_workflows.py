# ruff: noqa: F405, I001, RUF100
# mypy: ignore-errors
from __future__ import annotations

from .query_models_common import *  # noqa: F403,F401  # NOSONAR
from .query_models_admin import *  # noqa: F403,F401  # NOSONAR
from .query_models_jobs import *  # noqa: F403,F401  # NOSONAR


class ProjectGridRowView(ViewModel):
    id: str = ""
    name: str = ""
    workspace_id: str = ""
    workspace_key: str = ""
    workspace_name: str = ""
    updated_at: str = ""
    job_count: int = 0
    operator_default_workflow_id: str = ""
    workflow_ids: list[str] = Field(default_factory=list)


class ProjectsListView(ViewModel):
    projects: list[ProjectGridRowView] = Field(default_factory=list)
    total: int = 0
    page: int = 1
    per_page: int = 25
    total_pages: int = 1


class ProjectRuntimeConfigView(ViewModel):
    project_id: str
    project_name: str = ""
    run_config: dict[str, Any] = Field(default_factory=dict)


class ProjectWorkflowBindingsView(ViewModel):
    project_id: str
    workflow_ids: list[str]
    operator_default_workflow_id: str = ""
    project_workflows: list[dict[str, Any]] = Field(default_factory=list)


class WorkflowCatalogItemView(ViewModel):
    workflow_id: str
    display_name: str = ""
    description: str = ""
    enabled: bool = False
    availability_status: str = ""
    new_runs_blocked: bool = False
    validation: dict[str, Any] | None = None


class ProjectWorkflowCatalogView(ViewModel):
    project_id: str
    workflows: list[WorkflowCatalogItemView] = Field(default_factory=list)


class PublicWorkflowCatalogItemView(ViewModel):
    model_config = ConfigDict(
        title="Workflow Catalog Item",
        json_schema_extra={
            "description": (
                "Integration-facing summary of one workflow that can be launched for a project."
            ),
            "example": {
                "workflow_id": "release-report",
                "display_name": RELEASE_REPORT_VALUE,
                "description": "Produces a release-readiness report for a requested version.",
                "enabled": True,
                "availability_status": "available",
                "new_runs_blocked": False,
            },
        },
    )

    workflow_id: str = Field(description="Stable workflow identifier used when launching work.")
    display_name: str = Field(
        default="",
        description="Human-readable workflow name for integrator UIs.",
    )
    description: str = Field(default="", description="Short explanation of what the workflow does.")
    enabled: bool = Field(
        default=False,
        description="Whether the workflow can currently be selected for new runs.",
    )
    availability_status: str = Field(
        default="",
        description="Availability hint for the current workflow state.",
    )
    new_runs_blocked: bool = Field(
        default=False,
        description="Whether new runs are temporarily blocked for this workflow.",
    )


class PublicProjectWorkflowCatalogView(ViewModel):
    model_config = ConfigDict(
        title="Workflow Catalog Response",
        json_schema_extra={
            "description": (
                "List of workflows that an external integration can launch for a project."
            ),
            "example": {
                "project_id": "proj_42",
                "workflows": [
                    {
                        "workflow_id": "release-report",
                        "display_name": RELEASE_REPORT_VALUE,
                        "description": "Produces a release-readiness report for a requested version.",
                        "enabled": True,
                        "availability_status": "available",
                        "new_runs_blocked": False,
                    }
                ],
            },
        },
    )

    project_id: str = Field(description="Project whose callable workflows are being listed.")
    workflows: list[PublicWorkflowCatalogItemView] = Field(
        default_factory=list,
        description="Workflows that the integration can launch for this project.",
    )


class WorkflowEntrypointView(ViewModel):
    model_config = ConfigDict(
        title="Workflow Entrypoint",
        json_schema_extra={
            "description": (
                "Callable workflow entrypoint configuration exposed to external integrations."
            ),
            "example": {
                "entry_key": "release-report-webhook",
                "workflow_id": "release-report",
                "enabled": True,
                "allowed_methods": ["POST"],
                "idempotency_mode": "client_key",
                "response_mode": "accepted",
                "requires_secret": True,
                "input_mapping_keys": ["release_version", "requested_by"],
            },
        },
    )

    entry_key: str = Field(description="Stable identifier for the callable workflow entrypoint.")
    workflow_id: str = Field(
        default="",
        description="Workflow that will run when this entrypoint is invoked.",
    )
    enabled: bool = Field(
        default=False,
        description="Whether this entrypoint currently accepts new calls.",
    )
    allowed_methods: list[str] = Field(
        default_factory=list,
        description="HTTP methods accepted by the entrypoint.",
    )
    idempotency_mode: str = Field(
        default="",
        description="How callers should provide idempotency information.",
    )
    response_mode: str = Field(
        default="",
        description="How the entrypoint acknowledges accepted requests.",
    )
    requires_secret: bool = Field(
        default=False,
        description="Whether the caller must supply a configured shared secret.",
    )
    input_mapping_keys: list[str] = Field(
        default_factory=list,
        description="Top-level input fields the entrypoint expects from callers.",
    )


class ProjectWorkflowEntrypointsView(ViewModel):
    model_config = ConfigDict(
        title="Workflow Entrypoints Response",
        json_schema_extra={
            "description": ("List of callable workflow entrypoints available for a project."),
            "example": {
                "project_id": "proj_42",
                "entrypoints": [
                    {
                        "entry_key": "release-report-webhook",
                        "workflow_id": "release-report",
                        "enabled": True,
                        "allowed_methods": ["POST"],
                        "idempotency_mode": "client_key",
                        "response_mode": "accepted",
                        "requires_secret": True,
                        "input_mapping_keys": ["release_version", "requested_by"],
                    }
                ],
            },
        },
    )

    project_id: str = Field(
        description="Project whose callable workflow entrypoints are being listed."
    )
    entrypoints: list[WorkflowEntrypointView] = Field(
        default_factory=list,
        description="Entrypoints that an external integration can call for this project.",
    )


class WorkflowValidationIssueView(ViewModel):
    type: str = ""
    code: str = ""
    severity: str = ""
    message: str = ""
    path: str = ""
    field: str | None = None


class WorkflowValidationResultView(ViewModel):
    workflow_id: str
    validation: dict[str, Any] = Field(default_factory=dict)
    issues: list[WorkflowValidationIssueView] = Field(default_factory=list)


class WorkflowContractResultView(ViewModel):
    model_config = ConfigDict(
        title="Workflow Contract Response",
        json_schema_extra={
            "description": ("Machine-readable input and output contract for a workflow."),
            "example": {
                "workflow_id": "release-report",
                "display_name": RELEASE_REPORT_VALUE,
                "enabled": True,
                "interface": {
                    "input": {
                        "required": ["release_version"],
                        "properties": {
                            "release_version": {
                                "type": "string",
                                "description": "Version to evaluate.",
                            }
                        },
                    },
                    "output": {
                        "properties": {
                            "report_url": {"type": "string", "format": "uri"},
                            "status": {"type": "string"},
                        }
                    },
                },
            },
        },
    )

    workflow_id: str = Field(description="Workflow identifier for the returned contract.")
    display_name: str = Field(default="", description="Human-readable workflow name.")
    enabled: bool = Field(
        default=False, description="Whether the workflow currently accepts new runs."
    )
    interface: dict[str, Any] = Field(
        default_factory=dict,
        description="Machine-readable workflow input and output contract.",
    )
    definition_issues: list[dict[str, Any]] = Field(
        default_factory=list,
        description="Definition issues shared by global and project contract surfaces.",
    )


class WorkflowSuggestionsResultView(ViewModel):
    workflow_id: str
    suggestions: list[dict[str, Any]] = Field(default_factory=list)


class WorkflowImportDependencyOptionsView(ViewModel):
    kind: str = "workflow_import_dependency_options"
    options: dict[str, list[dict[str, Any]]] = Field(default_factory=dict)


class WorkflowImportPreflightView(ViewModel):
    schema_version: int = 1
    kind: str = "workflow_import_preflight"
    ok: bool = False
    can_apply: bool = False
    summary: dict[str, Any] = Field(default_factory=dict)
    workflows: list[dict[str, Any]] = Field(default_factory=list)
    dependencies: list[dict[str, Any]] = Field(default_factory=list)
    required_resolution_actions: list[str] = Field(default_factory=list)


class WorkflowImportApplyResultView(ViewModel):
    status: str = ""
    workflow_id: str = ""
    imported_workflows: list[dict[str, Any]] = Field(default_factory=list)
    preflight: WorkflowImportPreflightView = Field(default_factory=WorkflowImportPreflightView)


class WorkflowDagView(ViewModel):
    workflow_id: str = ""
    display_name: str = ""
    nodes: list[dict[str, Any]] = Field(default_factory=list)
    edges: list[dict[str, Any]] = Field(default_factory=list)
    protected_workflow: bool | None = None
    read_only: bool | None = None
    portability: dict[str, Any] | None = None


class WorkflowCanvasGraphView(ViewModel):
    workflow_id: str = ""
    display_name: str = ""
    nodes: list[dict[str, Any]] = Field(default_factory=list)
    edges: list[dict[str, Any]] = Field(default_factory=list)
    placeholder: bool | None = None
    reason: str | None = None
    protected_workflow: bool | None = None
    read_only: bool | None = None


class WorkflowConnectorCatalogView(ViewModel):
    connectors: list[dict[str, Any]] = Field(default_factory=list)
    count: int = 0
    project_id: str = ""


class WorkflowExtensionFormSchemaView(ViewModel):
    schema_: dict[str, Any] = Field(
        default_factory=dict,
        validation_alias=AliasChoices("schema"),
        serialization_alias="schema",
    )
    initial_values: dict[str, Any] = Field(default_factory=dict)
    field_groups: list[Any] = Field(default_factory=list)
    errors: list[Any] = Field(default_factory=list)
    warnings: list[Any] = Field(default_factory=list)


class WorkflowExtensionResourceQueryView(ViewModel):
    items: list[Any] = Field(default_factory=list)
    next_cursor: str | None = None
    count: int | None = None
    meta: dict[str, Any] = Field(default_factory=dict)


class WorkflowTriggerEntrypointView(ViewModel):
    entrypoint_id: str | None = None
    workflow_id: str = ""
    project_id: str | None = None
    entry_kind: str = "webhook"
    entry_key: str = ""
    enabled: bool = True
    allowed_methods: list[str] = Field(default_factory=list)
    auth_mode: str = ""
    auth_config: dict[str, Any] = Field(default_factory=dict)
    source_restrictions: dict[str, Any] = Field(default_factory=dict)
    allowed_content_types: list[str] = Field(default_factory=list)
    max_body_bytes: int = 0
    max_file_bytes: int = 0
    max_files: int = 0
    idempotency_mode: str = "none"
    response_mode: str = ""
    input_mapping: dict[str, Any] = Field(default_factory=dict)
    description: str = ""
    requires_secret: bool = False
    secret_ref: str | None = None
    public_path: str = ""
    public_url: str | None = None
    public_url_source: str | None = None
    readiness: dict[str, Any] = Field(default_factory=dict)
    created_at: str | None = None
    updated_at: str | None = None
    credential_reveal: dict[str, Any] | None = None


class WorkflowProviderTriggerView(ViewModel):
    trigger_id: str = ""
    workflow_id: str = ""
    project_id: str | None = None
    provider_key: str = ""
    trigger_type_id: str = ""
    display_name: str = ""
    description: str = ""
    enabled: bool = True
    event_name: str = ""
    trigger_config: dict[str, Any] = Field(default_factory=dict)
    readiness: dict[str, Any] = Field(default_factory=dict)
    config_schema: dict[str, Any] = Field(default_factory=dict)
    ui_schema: dict[str, Any] = Field(default_factory=dict)
    provider_ingress_url: str | None = None
    created_at: str | None = None
    updated_at: str | None = None


class WorkflowTriggerCatalogResponseView(ViewModel):
    workflow_id: str = ""
    triggers: list[dict[str, Any]] = Field(default_factory=list)


class WorkflowTriggersView(ViewModel):
    workflow_id: str = ""
    catalog: list[dict[str, Any]] = Field(default_factory=list)
    incoming_webhooks: list[WorkflowTriggerEntrypointView] = Field(default_factory=list)
    provider_triggers: list[WorkflowProviderTriggerView] = Field(default_factory=list)


class WorkflowWebhookTriggerDeleteResultView(ViewModel):
    status: str = ""
    entry_key: str = ""


class WorkflowTriggerDryRunView(ViewModel):
    workflow_id: str = ""
    entry_key: str = ""
    method: str = "POST"
    public_url: str = ""
    mapped_inputs: dict[str, Any] = Field(default_factory=dict)
    validation: dict[str, Any] = Field(default_factory=dict)


class WorkflowWebhookDeliveryView(ViewModel):
    id: str | int | None = None
    task_id: str | None = None
    task_summary: str = ""
    status: str = ""
    timestamp: Any = None
    request_id: str | None = None
    idempotency_key_digest: str | None = None
    reason: str | None = None
    job_id: str | None = None
    deduplicated: bool = False
    source_ip: str | None = None
    auth_mode: str | None = None
    auth_outcome: str | None = None
    auth_category: str | None = None
    http_status: int | None = None
    content_type: str | None = None
    response_mode: str | None = None
    validation_errors: list[Any] = Field(default_factory=list)
    payload_summary: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None
    result: dict[str, Any] = Field(default_factory=dict)


class WorkflowWebhookDeliveriesView(ViewModel):
    workflow_id: str = ""
    entry_key: str = ""
    deliveries: list[WorkflowWebhookDeliveryView] = Field(default_factory=list)


class WorkflowProviderTriggerDeleteResultView(ViewModel):
    status: str = ""
    trigger_id: str = ""


class WorkflowAdminSummaryView(ViewModel):
    workflow_id: str = ""
    display_name: str = ""
    description: str = ""
    enabled: bool = False
    portability: dict[str, Any] = Field(default_factory=dict)


class WorkflowAdminDetailView(WorkflowAdminSummaryView):
    settings: dict[str, Any] = Field(default_factory=dict)
    trigger: dict[str, Any] = Field(default_factory=dict)
    participants: list[dict[str, Any]] = Field(default_factory=list)
    account_id: str = ""
    source_install_id: str = ""
    revision: int | None = None
    steps: list[WorkflowEditorStepView] = Field(default_factory=list)
    groups: list[WorkflowEditorGroupView] = Field(default_factory=list)
    for_each: list[WorkflowEditorForEachView] = Field(default_factory=list)


class WorkflowExportPolicyView(ViewModel):
    export_allowed: bool = False
    clone_allowed: bool = False
    restore_allowed: bool = False
    structure_read_allowed: bool = False
    structure_change_allowed: bool = False
    step_test_run_allowed: bool = False
    source_kind: str = ""
    source_install_id: str = ""
    availability_reason_code: str = ""
    availability_reason_text: str = ""


class WorkflowValidationSummaryView(ViewModel):
    workflow_id: str = ""
    validation: dict[str, Any] = Field(default_factory=dict)


class WorkflowIssueListView(ViewModel):
    workflow_id: str = ""
    issues: list[WorkflowValidationIssueView] = Field(default_factory=list)


class WorkflowStatsView(ViewModel):
    workflow_id: str = ""
    total_runs: int = 0
    completed_count: int = 0
    failed_count: int = 0
    active_count: int = 0
    cancelled_count: int = 0
    success_rate: float | None = None
    total_cost_usd: float = 0.0
    avg_cost_per_run_usd: float = 0.0
    jobs_with_cost: int = 0
    last_run_at: str | None = None
    avg_duration_seconds: float | None = None


class WorkflowHistoryEntryView(ViewModel):
    entry_id: str | int = ""
    revision: int | None = None
    entity_type: str = "workflow"
    entity_id: str = ""
    action: str = ""
    author: str = ""
    changed_at: str | None = None
    snapshot: dict[str, Any] = Field(default_factory=dict)


class WorkflowDiffResultView(ViewModel):
    entity_type: str = "workflow"
    entity_id: str = ""
    from_entry_id: str | None = None
    to_entry_id: str | None = None
    summary: dict[str, Any] = Field(default_factory=dict)
    changes: list[dict[str, Any]] = Field(default_factory=list)


class CallableWorkflowView(ViewModel):
    workflow_id: str = ""
    display_name: str = ""
    description: str = ""
    enabled: bool = False
    trigger_channels: list[str] = Field(default_factory=list)
    input_mode: str = ""
    strict_inputs: bool = False
    output_mode: str = ""
    expected_inputs: list[RegistryContractItemView] = Field(default_factory=list)
    expected_outputs: list[RegistryContractItemView] = Field(default_factory=list)
    returned_files: list[dict[str, Any]] = Field(default_factory=list)
    input_schema: dict[str, Any] = Field(default_factory=dict)
    output_schema: dict[str, Any] = Field(default_factory=dict)


class CallableWorkflowListView(ViewModel):
    workflows: list[CallableWorkflowView] = Field(default_factory=list)
    count: int = 0


class AgentSkillPairView(ViewModel):
    agent_id: str = ""
    skill_id: str = ""
    display_name: str = ""
    agent_display_name: str = ""
    skill_display_name: str = ""
    enabled: bool = True


class AgentSkillPairListView(ViewModel):
    pairs: list[AgentSkillPairView] = Field(default_factory=list)
    count: int = 0
