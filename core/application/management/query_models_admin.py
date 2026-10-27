# ruff: noqa: F405, RUF100
from __future__ import annotations

from datetime import datetime

from .query_models_common import *  # noqa: F403,F401  # NOSONAR


class AttentionJobView(ViewModel):
    model_config = ConfigDict(
        title="Job List Item",
        json_schema_extra={
            "description": ("Compact job record used in paginated public job listings."),
            "example": {
                "job_id": "job-987",
                "project_id": "proj_42",
                "project_name": "Payments API",
                "task_id": "task-123",
                "task_summary": GENERATE_A_RELEASE_READINESS_REPORT_FOR_VERSION_2_MESSAGE,
                "display_summary": "Release-readiness report for version 2.4",
                "status": "in_progress",
                "selected_workflow": "release-report",
                "current_step_id": "draft_report",
                "current_group_id": None,
                "current_agent": "planner",
                "queued_at": VALUE_2026_05_27T09_12_00Z_VALUE,
                "started_at": VALUE_2026_05_27T09_12_08Z_VALUE,
                "finished_at": None,
            },
        },
    )

    job_id: str | None = Field(default=None, description="Platform job identifier.")
    project_id: str | None = Field(default=None, description="Project that owns the job.")
    project_name: str | None = Field(default=None, description="Human-readable project name.")
    task_id: str | None = Field(
        default=None, description="Caller task identifier associated with the job."
    )
    task_summary: str | None = Field(
        default=None,
        description="Plain-language task summary submitted when the job was created.",
    )
    task_summary_key: str | None = Field(
        default=None,
        description="Explicit localization key for a platform-generated task summary.",
    )
    task_summary_params: dict[str, str] = Field(default_factory=dict)
    display_summary: str | None = Field(
        default=None,
        description="Short display label for the job when one is available.",
    )
    status: str | None = Field(default=None, description="Current lifecycle status for the job.")
    task_source: str | None = None
    duration_ms: int | None = None
    worker_id: str | None = None
    selected_workflow: str | None = None
    selected_workflow_display_name: str | None = None
    current_step_id: str | None = None
    current_group_id: str | None = None
    current_agent: str | None = None
    last_error: str | None = None
    queued_at: datetime | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    heartbeat_at: datetime | None = None
    acknowledged_at: str | None = None
    acknowledged_by: str | None = None


class AwaitingApprovalJobView(ViewModel):
    id: str | None = None
    project_id: str | None = None
    project_name: str | None = None
    task_summary: str | None = None
    display_summary: str | None = None
    selected_workflow: str | None = None
    started_at: str | None = None
    thread_id: str | None = None
    status: str | None = None


class JobsListView(ViewModel):
    model_config = ConfigDict(
        title="Job List Response",
        json_schema_extra={
            "description": (
                "Paginated list of jobs visible to the caller for one integration scope."
            ),
            "example": {
                "jobs": [
                    {
                        "job_id": "job-987",
                        "project_id": "proj_42",
                        "project_name": "Payments API",
                        "task_id": "task-123",
                        "task_summary": GENERATE_A_RELEASE_READINESS_REPORT_FOR_VERSION_2_MESSAGE,
                        "display_summary": "Release-readiness report for version 2.4",
                        "status": "in_progress",
                        "selected_workflow": "release-report",
                        "current_step_id": "draft_report",
                        "current_group_id": None,
                        "current_agent": "planner",
                        "queued_at": VALUE_2026_05_27T09_12_00Z_VALUE,
                        "started_at": VALUE_2026_05_27T09_12_08Z_VALUE,
                        "finished_at": None,
                    }
                ],
                "total": 1,
                "page": 1,
                "per_page": 25,
                "offset": 0,
                "total_pages": 1,
                "project_id": "proj_42",
                "workspace_id": None,
                "status": "in_progress",
            },
        },
    )

    jobs: list[AttentionJobView] = Field(
        default_factory=list,
        description="Jobs visible to the caller for the requested page.",
    )
    total: int = Field(default=0, description="Total number of jobs that match the current filter.")
    page: int = Field(default=1, description="Current result page number.")
    per_page: int = Field(default=25, description="Maximum number of jobs returned in this page.")
    offset: int = Field(default=0, description="Exact zero-based offset used for this result set.")
    total_pages: int = Field(
        default=1,
        description="Total number of pages available for the current filter.",
    )
    project_id: str | None = Field(
        default=None,
        description="Project filter echoed back when the request targeted one project.",
    )
    workspace_id: str | None = Field(
        default=None,
        description="Workspace filter echoed back when present.",
    )
    status: str | None = Field(
        default=None,
        description="Status filter echoed back when present; several statuses are comma-separated.",
    )


class ProjectJobRowView(ViewModel):
    model_config = ConfigDict(extra="ignore", coerce_numbers_to_str=True)

    id: str | None = None
    project_id: str | None = None
    task_id: str | None = None
    task_summary: str | None = None
    display_summary: str | None = None
    task_source: str | None = None
    status: str | None = None
    queued_at: str | None = None
    started_at: str | None = None
    finished_at: str | None = None
    duration_ms: int | None = None


class ProjectJobsListView(ViewModel):
    model_config = ConfigDict(extra="ignore", coerce_numbers_to_str=True)

    jobs: list[ProjectJobRowView] = Field(default_factory=list)
    total: int = 0
    page: int = 1
    per_page: int = 25
    total_pages: int = 1


class ProjectNotificationRoutesView(ViewModel):
    project_id: str
    notification_routes: list[dict[str, Any]] = Field(default_factory=list)


class ProjectNotificationOverviewView(ViewModel):
    """A project's automatic notifications, read from the live settings."""

    project_id: str
    entries: list[dict[str, Any]] = Field(default_factory=list)


class ProjectNotificationDeliveriesView(ViewModel):
    project_id: str
    job_id: str | None = None
    notification_deliveries: list[dict[str, Any]] = Field(default_factory=list)


class ProjectProviderBindingsView(ViewModel):
    project_id: str = ""
    project_providers: list[dict[str, Any]] = Field(default_factory=list)


class ProvidersCatalogItemView(ViewModel):
    provider_key: str = ""
    provider_type_key: str = ""
    display_name: str = ""
    implementation_kind: str = ""
    integration_key: str = ""
    config: dict[str, Any] = Field(default_factory=dict)
    health: dict[str, Any] = Field(default_factory=dict)


class ProvidersCatalogView(ViewModel):
    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "provider_types": [{"provider_type_key": "llm", "display_name": "LLM"}],
                "providers": [
                    {
                        "provider_key": "openai",
                        "provider_type_key": "llm",
                        "display_name": "OpenAI",
                        "implementation_kind": "native",
                        "integration_key": "openai",
                        "config": {"base_url": "https://api.openai.com/v1"},
                        "health": {"status": "healthy"},
                    }
                ],
            }
        }
    )

    provider_types: list[dict[str, Any]] = Field(default_factory=list)
    providers: list[ProvidersCatalogItemView] = Field(default_factory=list)


class ProviderSecretStatusView(ViewModel):
    status: str = "saved"
    provider_key: str = ""
    secret_key: str = ""
    project_id: str | None = None
    active: bool = True


class RegistryContractItemView(ViewModel):
    name: str = ""
    type: str = "text"
    description: str = ""
    required: bool | None = None
    contract_id: str | None = None
    source: str | None = None
    source_id: str | None = None


class AuthAccountMembershipView(ViewModel):
    account_id: str = ""
    display_name: str = ""
    is_active: bool = True
    is_default: bool = False


class AuthUserView(ViewModel):
    id: str = ""
    email: str = ""
    username: str = ""
    display_name: str = ""
    roles: list[str] = Field(default_factory=list)
    account_id: str = ""
    account_ids: list[str] = Field(default_factory=list)
    account_memberships: list[AuthAccountMembershipView] = Field(default_factory=list)
    is_active: bool = True
    must_change_password: bool = False
    timezone: str = ""
    locale: str = ""


class AuthMeView(ViewModel):
    authenticated: bool = False
    local_auth_enabled: bool = False
    external_auth_enabled: bool = False
    auth_source: str | None = None
    csrf_token: str = ""
    user: AuthUserView | None = None


class UserProfileUpdateResultView(ViewModel):
    status: str = "ok"
    display_name: str = "Release Manager"


class UserEmailChangeStatusView(ViewModel):
    pending: bool = False
    new_email: str | None = None
    expires_at: str | None = None
    created_at: str | None = None


class UserEmailChangeRequestResultView(ViewModel):
    status: str = "verification_sent"
    email: str = "owner@example.com"
    expires_at: str | None = "2026-05-28T18:45:00Z"
    expires_in_minutes: int = 30


class UserTimezoneResultView(ViewModel):
    status: str = "ok"
    timezone: str = "Europe/Vilnius"


class UserLocaleResultView(ViewModel):
    status: str = "ok"
    locale: str = "en"


class AuthUsersListView(ViewModel):
    users: list[AuthUserView] = Field(default_factory=list)
    total: int = 0
    page: int = 1
    per_page: int = 25
    total_pages: int = 1


class AuthTokenRecordView(ViewModel):
    id: str = ""
    label: str = ""
    scopes: list[str] = Field(default_factory=list)
    allowed_project_ids: list[str] = Field(default_factory=list)
    allowed_workflow_ids: list[str] = Field(default_factory=list)
    created_at: str | None = None
    expires_at: str | None = None
    last_used_at: str | None = None
    revoked: bool | None = None
    is_active: bool | None = None
    created_by: str | None = None


class AuthTokensListView(ViewModel):
    tokens: list[AuthTokenRecordView] = Field(default_factory=list)
    total: int = 0
    page: int = 1
    per_page: int = 25
    total_pages: int = 1


class AuthAccountView(ViewModel):
    account_id: str = ""
    display_name: str = ""
    is_active: bool = True


class AuthAccountsListView(ViewModel):
    accounts: list[AuthAccountView] = Field(default_factory=list)


class AuthAccountEnvelopeView(ViewModel):
    account: AuthAccountView = Field(default_factory=AuthAccountView)


class AuthServiceAccountView(ViewModel):
    service_account_key: str = ""
    display_name: str = ""
    description: str = ""
    scopes: list[str] = Field(default_factory=list)
    allowed_project_ids: list[str] = Field(default_factory=list)
    allowed_workflow_ids: list[str] = Field(default_factory=list)
    is_active: bool = True
    created_at: str | None = None
    created_by: str | None = None


class AuthServiceAccountsListView(ViewModel):
    service_accounts: list[AuthServiceAccountView] = Field(default_factory=list)


class AuthServiceAccountEnvelopeView(ViewModel):
    service_account: AuthServiceAccountView = Field(default_factory=AuthServiceAccountView)


class AuthServiceAccountTokensListView(ViewModel):
    tokens: list[AuthTokenRecordView] = Field(default_factory=list)


class ModelSelectionOptionsView(ViewModel):
    provider_models: dict[str, Any] = Field(default_factory=dict)
    all_models: list[Any] = Field(default_factory=list)
    # Additive provider display metadata (built_in + extension_llm_provider).
    # Existing consumers that read only provider_models/all_models are unaffected.
    provider_options: list[Any] = Field(default_factory=list)


class ModelCatalogSettingsView(ViewModel):
    provider_models: dict[str, Any] = Field(default_factory=dict)
    all_models: list[Any] = Field(default_factory=list)
    visibility_policy: dict[str, Any] = Field(default_factory=dict)


class SaveStatusView(ViewModel):
    status: str = "saved"


class RetentionSettingsView(ViewModel):
    settings: dict[str, Any] = Field(default_factory=dict)
    available: bool = True
    defaults: dict[str, Any] = Field(default_factory=dict)


class RetentionSettingsSaveView(ViewModel):
    status: str = "saved"
    settings: dict[str, Any] = Field(default_factory=dict)


class AgentTypeView(ViewModel):
    id: str | None = None
    key: str | None = None
    display_name: str | None = None
    description: str | None = None
    enabled: bool | None = None
    skill_count: int | None = None
    tool_count: int | None = None


class AgentSkillAssignmentView(ViewModel):
    skill_id: str | None = None
    skill_prompt: str | None = None
    sort_order: int | None = None


class AgentToolAssignmentView(ViewModel):
    tool_id: str | None = None
    sort_order: int | None = None


class AgentRecordView(ViewModel):
    agent_id: str | None = None
    version: str | None = None
    display_name: str | None = None
    description: str | None = None
    enabled: bool | None = None
    agent_type_id: str | None = None
    agent_type_key: str | None = None
    role_type: str | None = None
    base_skill: str | None = None
    secondary_skills: list[str] = Field(default_factory=list)
    local_instructions: list[str] = Field(default_factory=list)
    prompts: dict[str, Any] = Field(default_factory=dict)
    config: dict[str, Any] = Field(default_factory=dict)
    quality: dict[str, Any] = Field(default_factory=dict)
    inputs: dict[str, Any] = Field(default_factory=dict)
    outputs: list[Any] = Field(default_factory=list)
    builder_metadata_jsonb: dict[str, Any] = Field(default_factory=dict)
    depends_on: dict[str, Any] = Field(default_factory=dict)
    tags: list[str] = Field(default_factory=list)
    skills: list[AgentSkillAssignmentView] = Field(default_factory=list)
    tools: list[AgentToolAssignmentView] = Field(default_factory=list)


class AgentsListView(ViewModel):
    agents: list[AgentRecordView] = Field(default_factory=list)


class AgentValidationView(ViewModel):
    agent_id: str
    validation: dict[str, Any] = Field(default_factory=dict)


class AgentPerformanceView(ViewModel):
    agent_id: str
    invocations: int = 0
    avg_duration_secs: float | None = None
    total_cost_usd: float | None = None
    error: str | None = None


class AgentEffectiveIOView(ViewModel):
    inputs: list[RegistryContractItemView] = Field(default_factory=list)
    outputs: list[RegistryContractItemView] = Field(default_factory=list)


class AgentTemplateVariableView(ViewModel):
    name: str = ""
    type: str = "text"
    description: str = ""
    group: str = ""
    source: str = ""
    blocked: bool | None = None


class AgentOutputFormatPreviewView(ViewModel):
    response_contract: str = ""
    output_format: str = ""


class SkillRecordView(ViewModel):
    skill_id: str | None = None
    version: str | None = None
    display_name: str | None = None
    description: str | None = None
    enabled: bool | None = None
    category: str | None = None
    skill_type: str | None = None
    instructions: list[str] = Field(default_factory=list)
    prompt_template: str | None = None
    expected_inputs: list[RegistryContractItemView] = Field(default_factory=list)
    expected_outputs: list[RegistryContractItemView] = Field(default_factory=list)
    input_schema: dict[str, Any] = Field(default_factory=dict)
    output_schema: dict[str, Any] = Field(default_factory=dict)
    examples: list[dict[str, Any]] = Field(default_factory=list)
    builder_metadata_jsonb: dict[str, Any] = Field(default_factory=dict)
    tags: list[str] = Field(default_factory=list)
    supported_role_types: list[str] = Field(default_factory=list)


class SkillsListView(ViewModel):
    skills: list[SkillRecordView] = Field(default_factory=list)


class SkillValidationView(ViewModel):
    skill_id: str
    validation: dict[str, Any] = Field(default_factory=dict)


class AgentDiffView(ViewModel):
    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "status": "different",
                "fields": {
                    "description": {"before": "Old copy", "after": "New copy"},
                    "enabled": {"before": False, "after": True},
                },
            }
        }
    )


class SkillDiffView(ViewModel):
    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "status": "different",
                "fields": {
                    "display_name": {"before": "Old skill", "after": "Release notes"},
                    "enabled": {"before": False, "after": True},
                },
            }
        }
    )
