# ruff: noqa: F405, I001, RUF100
from __future__ import annotations

from .query_models_common import *  # noqa: F403,F401  # NOSONAR
from .query_models_admin import *  # noqa: F403,F401  # NOSONAR
from .query_models_jobs import *  # noqa: F403,F401  # NOSONAR
from .query_models_workflows import *  # noqa: F403,F401  # NOSONAR
from .query_models_workflow_editor import *  # noqa: F403,F401  # NOSONAR


class ProjectPolicyView(ViewModel):
    project_id: str
    policy: dict[str, Any] = Field(default_factory=dict)


class ProjectSettingsHistoryListView(ViewModel):
    project_id: str
    revisions: list[dict[str, Any]] = Field(default_factory=list)


class ProjectSettingsHistoryDiffView(ViewModel):
    project_id: str
    selected_revision: dict[str, Any] = Field(default_factory=dict)
    compare_to: dict[str, Any] = Field(default_factory=dict)
    summary: dict[str, Any] = Field(
        default_factory=lambda: {"total": 0, "added": 0, "removed": 0, "changed": 0}
    )
    changes: list[dict[str, Any]] = Field(default_factory=list)


class ProjectSettingsHistoryPageView(ViewModel):
    project: dict[str, Any] = Field(default_factory=dict)
    revisions: list[dict[str, Any]] = Field(default_factory=list)
    selected_revision: dict[str, Any] | None = None
    compare_entry: dict[str, Any] = Field(default_factory=dict)
    diff_payload: dict[str, Any] = Field(
        default_factory=lambda: {
            "changes": [],
            "summary": {"total": 0, "added": 0, "removed": 0, "changed": 0},
        }
    )


class ProjectScheduledJobsView(ViewModel):
    project_id: str
    timezone_context: dict[str, Any] = Field(default_factory=dict)
    jobs: list[dict[str, Any]] = Field(default_factory=list)
    runs: list[dict[str, Any]] = Field(default_factory=list)


class ProjectWorkflowPreviewView(ViewModel):
    project_id: str
    selected_workflow_id: str = ""
    requested_workflow_id: str | None = None
    selection_source: str = "explicit_request"
    selection_reason: str = ""
    classification: dict[str, Any] = Field(default_factory=dict)
    step_plan: list[dict[str, Any]] = Field(default_factory=list)
    step_count: int = 0
    availability_status: str = "available"
    availability_reason_code: str = "none"
    availability_reason_text: str = ""
    new_runs_blocked: bool = False
    blocked_dependencies: list[str] = Field(default_factory=list)
    missing_connections: list[str] = Field(default_factory=list)


class ProjectCronPreviewView(ViewModel):
    cron_expr: str = ""
    timezone: str = "UTC"
    summary: str = ""
    next_runs: list[str] = Field(default_factory=list)
    next_runs_utc: list[str] = Field(
        default_factory=list,
        description="UTC instants for upcoming runs; next_runs retains schedule-local wall times.",
    )


class WorkflowAvailabilityView(ViewModel):
    workflow_id: str = ""
    account_id: str = ""
    project_id: str | None = None
    owner_install_id: str = ""
    availability_status: str = "available"
    availability_reason_code: str = "none"
    availability_reason_text: str = ""
    new_runs_blocked: bool = False
    visible_in_catalog: bool = True
    blocked_dependencies: list[str] = Field(default_factory=list)
    missing_connections: list[str] = Field(default_factory=list)


class WorkflowChoiceView(ViewModel):
    workflow_id: str = ""
    display_name: str = ""
    description: str = ""
    enabled: bool = True
    scope_kind: str = "system"
    account_id: str = ""
    availability_status: str = "available"
    availability_reason_code: str = "none"
    availability_reason_text: str = ""
    new_runs_blocked: bool = False
    visible_in_catalog: bool = True
    owner_install_id: str = ""


class PublishedBlockChoiceView(ViewModel):
    block_key: str = ""
    display_name: str = ""
    block_type: str = ""
    enabled: bool = True
    scope_kind: str = "system"
    account_id: str = ""
    availability_status: str = "available"
    availability_reason_code: str = "none"
    availability_reason_text: str = ""
    new_runs_blocked: bool = False
    visible_in_catalog: bool = True
    owner_install_id: str = ""


class WorkflowDraftPreviewView(ViewModel):
    project_id: str
    raw_request: str = ""
    validation_status: str = "ready"
    selected_workflow_ids: list[str] = Field(default_factory=list)
    operator_default_workflow_id: str = ""
    current_workflow_ids: list[str] = Field(default_factory=list)
    current_operator_default_workflow_id: str = ""
    impact_summary: str = ""
    warnings: list[str] = Field(default_factory=list)
    deep_links: list[dict[str, Any]] = Field(default_factory=list)


class ExtensionProjectPreviewView(ViewModel):
    parent_project_id: str
    parent_project_name: str = ""
    project_name: str = ""
    request_summary: str = ""
    artifact_kind: str = "extension"
    artifact_metadata: dict[str, Any] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)
    next_step: str = ""
    deep_links: list[dict[str, Any]] = Field(default_factory=list)


class ProjectDetailPageView(ViewModel):
    project: dict[str, Any] = Field(default_factory=dict)
    jobs: list[dict[str, Any]] = Field(default_factory=list)
    workflows: list[dict[str, Any]] = Field(default_factory=list)
    default_workflow: dict[str, Any] | None = None
    project_policy: dict[str, Any] = Field(default_factory=dict)
    scheduled_jobs: list[dict[str, Any]] = Field(default_factory=list)
    scheduled_runs: list[dict[str, Any]] = Field(default_factory=list)
    page: int = 1
    per_page: int = 25
    total_jobs: int = 0
    total_pages: int = 1


class ProjectCliDetailView(ProjectDetailPageView):
    connection_schema_page: dict[str, Any] = Field(default_factory=dict)


class ProjectEditPageView(ViewModel):
    project: dict[str, Any] = Field(default_factory=dict)
    workflows: list[dict[str, Any]] = Field(default_factory=list)
    project_workflows: list[dict[str, Any]] = Field(default_factory=list)
    project_workflow_ids: list[str] = Field(default_factory=list)
    project_policy: dict[str, Any] = Field(default_factory=dict)
    project_settings_revisions: list[dict[str, Any]] = Field(default_factory=list)


class ProjectQueueLeaseView(ViewModel):
    project_id: str
    worker_instance_id: str = ""
    lease_token: str = ""
    heartbeat_at: str | None = None
    expires_at: str | None = None
    acquired_at: str | None = None
    active_job_id: str | None = None
    drain_status: str = ""
    heartbeat_age_sec: int | None = None


class ProjectReadyStateView(ViewModel):
    is_ready: bool = False
    reason: str = "idle"


class QueueHealthView(ViewModel):
    state: str = ""
    state_label: str = ""
    state_tone: str = "gray"
    state_message: str = ""
    attention_state: str = ""
    attention_label: str = ""
    attention_tone: str = "gray"
    attention_message: str = ""
    requires_intervention: bool = False
    queued_pending_jobs: int = 0
    retrying_delivery_jobs: int = 0
    dead_lettered_jobs: int = 0
    post_claim_execution_failed_jobs: int = 0


class ProjectQueuePendingJobView(ViewModel):
    job_id: str
    task_id: str = ""
    task_summary: str = ""
    task_source: str = ""
    queued_at: str | None = None
    queue_delivery_state: str = ""
    queue_delivery_state_label: str = ""
    queue_delivery_state_tone: str = "gray"
    queue_health_state: str = ""
    queue_health_label: str = ""
    queue_health_tone: str = "gray"


class ProjectQueueView(ViewModel):
    project_id: str
    pending_count: int = 0
    project_backlog: int = 0
    queue_depth: int = -1
    ready_queue_depth: int = -1
    queue_name: str = ""
    dead_letter_queue_name: str = ""
    broker_available: bool | None = None
    queue_transport_state: str = ""
    queue_transport_label: str = ""
    queue_transport_message: str = ""
    lease_holder: str | None = None
    project_lease: ProjectQueueLeaseView | None = None
    ready_state: ProjectReadyStateView = Field(default_factory=ProjectReadyStateView)
    queue_health: QueueHealthView = Field(default_factory=QueueHealthView)
    pending_jobs: list[ProjectQueuePendingJobView] = Field(default_factory=list)


class ApprovalGateView(ViewModel):
    job_id: str
    thread_id: str = ""
    task_summary: str = ""
    project_id: str | None = None
    approval_id: str | None = None
    expires_at: str | None = None
    allowed_actions: list[str] = Field(default_factory=list)
    payload: dict[str, Any] = Field(default_factory=dict)


class ApprovalHistoryView(ViewModel):
    job_id: str
    approvals: list[dict[str, Any]] = Field(default_factory=list)


class DomainEventSummaryView(ViewModel):
    id: str | None = None
    event_id: str = ""
    event_type: str = ""
    event_type_label: str | None = None
    event_family: str | None = None
    event_family_label: str | None = None
    event_category: str | None = None
    event_category_label: str | None = None
    severity: str = "info"
    status: str = "recorded"
    status_label: str | None = None
    status_tone: str | None = None
    title: str = ""
    summary: str = ""
    detail: str = ""
    project_id: str | None = None
    project_name: str | None = None
    workflow_id: str | None = None
    job_id: str | None = None
    group_id: str | None = None
    step_id: str | None = None
    step_execution_id: str | None = None
    created_at: str | None = None
    updated_at: str | None = None


class DomainEventDispatchAttemptView(ViewModel):
    id: str | None = None
    dispatch_id: str = ""
    status: str = "queued"
    status_label: str | None = None
    status_tone: str | None = None
    handler: str | None = None
    error_text: str | None = None
    attempt_no: int | None = None
    attempted_at: str | None = None
    created_at: str | None = None


class DomainEventDispatchView(ViewModel):
    id: str | None = None
    dispatch_id: str = ""
    target: str = ""
    target_type: str | None = None
    target_id: str | None = None
    status: str = "matched"
    status_label: str | None = None
    status_tone: str | None = None
    reason_code: str | None = None
    reason_detail: str | None = None
    attempt_count: int | None = None
    max_attempts: int | None = None
    created_at: str | None = None
    attempts: list[DomainEventDispatchAttemptView] = Field(default_factory=list)


class DomainEventDetailView(DomainEventSummaryView):
    payload: dict[str, Any] = Field(default_factory=dict)
    actor: dict[str, Any] = Field(default_factory=dict)
    urls: dict[str, Any] = Field(default_factory=dict)
    envelope: dict[str, Any] = Field(default_factory=dict)
    source: str | None = None
    aggregate_type: str | None = None
    aggregate_id: str | None = None
    correlation_id: str | None = None
    dispatches: list[DomainEventDispatchView] = Field(default_factory=list)
    commands: list[dict[str, Any]] = Field(default_factory=list)


class EventsListView(ViewModel):
    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "events": [
                    {
                        "event_id": "evt-123",
                        "event_type": "job.failed",
                        "severity": "error",
                        "status": "recorded",
                        "title": "Job failed",
                    }
                ],
                "total": 1,
                "page": 1,
                "per_page": 25,
                "total_pages": 1,
                "facets": {"status": [{"value": "recorded", "count": 1}]},
            }
        }
    )

    events: list[DomainEventSummaryView] = Field(default_factory=list)
    total: int = 0
    page: int = 1
    per_page: int = 25
    total_pages: int = 1
    facets: dict[str, Any] = Field(default_factory=dict)


class WorkflowReactionsView(ViewModel):
    workflow_id: str
    scope_kind: str = "workflow"
    scope_id: str = ""
    project_id: str | None = None
    available_trigger_sources: list[dict[str, Any]] = Field(default_factory=list)
    reactions: list[WorkflowReactionRuleView] = Field(default_factory=list)


class ExtensionsListView(ViewModel):
    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "extensions": [
                    {
                        "extension_id": "slack_notifier",
                        "display_name": "Slack Notifier",
                        "enabled": True,
                    }
                ]
            }
        }
    )

    extensions: list[dict[str, Any]] = Field(default_factory=list)


class ExtensionHealthView(ViewModel):
    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "extension_id": "slack_notifier",
                "health": {
                    "status": "healthy",
                    "last_checked_at": VALUE_2026_05_28T12_00_00Z_VALUE,
                },
            }
        }
    )

    extension_id: str
    health: dict[str, Any] = Field(default_factory=dict)


class ToolRecordView(ViewModel):
    model_config = ConfigDict(
        title="Tool Record",
        json_schema_extra={
            "description": "One tool definition as returned by the internal registry admin API.",
            "example": {
                "tool_id": "github_issue_lookup",
                "version": "1.0.0",
                "display_name": "GitHub Issue Lookup",
                "description": "Fetches issue details from GitHub for workflow steps.",
                "enabled": True,
                "category": "integration",
                "prompt": "Look up a GitHub issue by owner, repo, and issue number.",
                "interface": {
                    "function_name": "lookup_issue",
                    "arguments": {
                        "owner": {"type": "string"},
                        "repo": {"type": "string"},
                        "issue_number": {"type": "integer"},
                    },
                },
                "implementation": {"kind": "python", "module": "extensions.github.tools.issue"},
                "config": {"timeout_seconds": 30},
                "builder_metadata_jsonb": {"origin": "seed"},
                "tags": ["github", "issues"],
            },
        },
    )

    tool_id: str | None = None
    version: str | None = None
    display_name: str | None = None
    description: str | None = None
    enabled: bool | None = None
    category: str | None = None
    prompt: str | None = None
    interface: dict[str, Any] = Field(default_factory=dict)
    implementation: dict[str, Any] = Field(default_factory=dict)
    config: dict[str, Any] = Field(default_factory=dict)
    builder_metadata_jsonb: dict[str, Any] = Field(default_factory=dict)
    tags: list[str] = Field(default_factory=list)
    availability_status: str = "available"
    availability_reason_code: str = "none"
    availability_reason_text: str = ""
    agent_relationship_assignable: bool | None = None
    agent_relationship_reason_code: str = "unknown"
    agent_relationship_reason_text: str = ""


class ToolsHealthView(ViewModel):
    registered: dict[str, Any] = Field(default_factory=dict)
    errors: list[Any] = Field(default_factory=list)


class ToolsListView(ViewModel):
    tools: list[ToolRecordView] = Field(default_factory=list)


class ToolValidationView(ViewModel):
    tool_id: str
    validation: dict[str, Any] = Field(default_factory=dict)


class ToolHistoryEntriesView(ViewModel):
    model_config = ConfigDict(
        title="Tool History Entries",
        json_schema_extra={
            "description": "Ordered tool revision history returned by the internal registry admin API.",
            "example": {
                "entries": [
                    {
                        "tool_id": "github_issue_lookup",
                        "version": "1.0.0",
                        "display_name": "GitHub Issue Lookup",
                        "description": "Fetches issue details from GitHub for workflow steps.",
                        "enabled": True,
                        "category": "integration",
                        "tags": ["github", "issues"],
                    }
                ]
            },
        },
    )

    entries: list[ToolRecordView] = Field(default_factory=list)


class ToolDiffView(ViewModel):
    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "status": "different",
                "fields": {
                    "prompt": {"before": "Old prompt", "after": "New prompt"},
                    "enabled": {"before": False, "after": True},
                },
            }
        }
    )


class ModelProviderRecordView(ViewModel):
    provider_id: str | None = None
    provider_key: str = ""
    display_name: str = ""
    provider_type_key: str = ""
    account_id: str | None = None
    enabled: bool | None = None
    config: dict[str, Any] = Field(default_factory=dict)


class ModelProvidersListView(ViewModel):
    providers: list[ModelProviderRecordView] = Field(default_factory=list)


class ModelProviderMutationResultView(ModelProviderRecordView):
    status: str = "saved"


class DomainEventDetailEnvelopeView(ViewModel):
    event: DomainEventDetailView = Field(default_factory=DomainEventDetailView)


class DashboardSummaryView(ViewModel):
    range: str = "7d"
    range_hours: int = 168
    metrics_truncated: bool = False
    historical_metrics_source: str = "live"
    metrics_cap: int | None = None


class CoreSloMetricsView(ViewModel):
    window_hours: int = 24
    account_id: str = "acc_default"
    generated_at: str = VALUE_2026_05_28T12_00_00Z_VALUE


class ExtensionPageProjectView(ViewModel):
    id: str
    name: str


class ExtensionPageContextView(ViewModel):
    compiled_manifest: dict[str, Any] = Field(default_factory=dict)
    preview_mode: bool = False
    extension_id: str = ""
    extension_name: str = ""
    extension_kind: str = ""
    extension_version: str = ""
    extension_description: str = ""
    compile_status: str = "ok"
    current_state: str = "active"
    author: str = ""
    vendor_name: str = ""
    support_url: str = ""
    website_url: str = ""
    broken: bool = False
    broken_reason: str = ""
    validation_summary: dict[str, Any] = Field(default_factory=dict)
    health_status: str = ""
    projects: list[ExtensionPageProjectView] = Field(default_factory=list)
    selected_project_id: str | None = None
