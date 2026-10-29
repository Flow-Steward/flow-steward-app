# ruff: noqa: F405, I001, RUF100
# mypy: ignore-errors
from .http_app_imports import *  # noqa: F403,F401  # NOSONAR


API_DATASETS_DATASET_ID_PATH = "/api/datasets/{dataset_id}"
API_DATA_CONTRACTS_CONTRACT_ID_PATH = "/api/data-contracts/{contract_id}"
API_FIELDS_FIELD_KEY_PATH = "/api/fields/{field_key}"
API_GUARDRAILS_GUARDRAIL_ID_PATH = "/api/guardrails/{guardrail_id}"
API_MCP_CONNECTOR_ID_PATH = "/api/mcp/{connector_id}"
API_PROJECTS_PROJECT_ID_CONNECTIONS_CONNECTION_ID_PATH = (
    "/api/projects/{project_id}/connections/{connection_id}"
)
API_PROJECTS_PROJECT_ID_PATH = "/api/projects/{project_id}"
API_PUBLISHED_BLOCKS_BLOCK_KEY_PATH = "/api/published-blocks/{block_key}"
API_WORKFLOWS_WORKFLOW_ID_PATH = "/api/workflows/{workflow_id}"

HTTP_APP_MODULE = "platform.http"
logger = logging.getLogger(HTTP_APP_MODULE)

_AnyCallable = Callable[..., Any]

_get_logger_only: _AnyCallable
_serialize_domain_event_datetime: _AnyCallable
_workflow_exists: _AnyCallable
_start_graph_resume_thread: _AnyCallable
_enqueue_task: _AnyCallable
_evaluate_workflow_reactions: _AnyCallable
_reaction_action_result_executed: _AnyCallable
_watchdog_scan: _AnyCallable
_scheduled_jobs_scan: _AnyCallable
_dashboard_rollup_scan: _AnyCallable
_is_public_auth_path: _AnyCallable
_auth_tables_available: _AnyCallable
_get_user_by_session_token: _AnyCallable
_auth_account_tables_available: _AnyCallable
_is_admin_only_route: _AnyCallable
_is_registry_read_route: _AnyCallable
_is_self_service_auth_write_route: _AnyCallable
_sanitize_retryable_interrupted_step_snapshot: _AnyCallable
_project_query_service: _AnyCallable
_project_command_service: _AnyCallable
_job_query_service: _AnyCallable
_job_command_service: _AnyCallable
_cron_next_run_at: _AnyCallable
_normalize_cron_expr: _AnyCallable
_parse_cron_expression: _AnyCallable
_cron_human_summary: _AnyCallable
_run_scheduled_job_once: _AnyCallable


app: FastAPI

_CONTROL_PARSE_WORKER_PID = _control_scans._parse_worker_pid
_CONTROL_SIGNALABLE_WORKER_PID = _control_scans.signalable_worker_pid
_CONTROL_PID_ALIVE = _control_scans._pid_alive
_CONTROL_LEASE_HEARTBEAT_AGE_SECONDS = _control_scans._lease_heartbeat_age_seconds
_CONTROL_LEASE_TIMESTAMP_AGE_SECONDS = _control_scans._lease_timestamp_age_seconds
_CONTROL_LEASE_IS_LIVE = _control_scans._lease_is_live
_CONTROL_INDEX_ACTIVE_PROJECT_LEASES = _control_scans._index_active_project_leases
_CONTROL_LIST_WATCHDOG_RECOVERY_CANDIDATES = _control_scans._list_watchdog_recovery_candidates
_CONTROL_CLASSIFY_RUNTIME_HEALTH = _control_scans._classify_runtime_health
_CONTROL_CLASSIFY_WATCHDOG_RECOVERY = _control_scans._classify_watchdog_recovery
_CONTROL_UPDATE_JOB_STATUS_WITH_REASON = _control_scans._update_job_status_with_reason
_CONTROL_APPEND_RUNTIME_JOURNAL = _control_scans._append_runtime_journal
_CONTROL_DETECT_RETRYABLE_INTERRUPTED_STEP_SNAPSHOT = (
    _control_scans._detect_retryable_interrupted_step_snapshot
)
_CONTROL_WATCHDOG_SCAN = _control_scans.watchdog_scan
_CONTROL_RECORD_SCHEDULED_JOB_OUTCOME = _control_scheduled_jobs._record_scheduled_job_outcome
_CONTROL_RUN_SCHEDULED_JOB_ONCE = _control_scheduled_jobs._run_scheduled_job_once
_CONTROL_SCHEDULED_JOBS_SCAN = _control_scheduled_jobs.scheduled_jobs_scan

_PROJECT_CONFIG_SECRET_KEY_RE = re.compile(
    r"(api[_-]?key|token|secret|password|passphrase|private[_-]?key|dsn)",
    re.IGNORECASE,
)
_AUTH_PUBLIC_PATHS_BASE = {"/"} | set(_AUTH_PUBLIC_PATHS_SOURCE)
PATH_LOGIN = "/login"
PATH_BOOTSTRAP = "/bootstrap"
PATH_PROJECTS = "/projects"
DEFAULT_MODEL_GPT_4_1_NANO = "gpt-4.1-nano"

ERR_REQUEST_FAILED = "Request failed"
ERR_VALIDATION = "Validation error"
ERR_DATABASE_NOT_CONFIGURED = "Database not configured"
ERR_AUTH_TABLES_NOT_AVAILABLE = "Auth tables are not available"
ERR_AUTHENTICATION_REQUIRED = "Authentication required"
ERR_ACCOUNT_DISABLED = "Account is disabled"
ERR_ADMIN_ROLE_REQUIRED = "Admin role required"
ERR_OPERATOR_ROLE_REQUIRED = "Operator role required"
ERR_PROJECT_NOT_FOUND = "Project not found"
ERR_JOB_NOT_FOUND_LOWER = "job not found"
ERR_NOT_FOUND = "Not found"
ERR_LOCAL_AUTH_DISABLED = "Local auth is disabled"
ERR_USERNAME_REQUIRED = "username is required"
ERR_TOO_MANY_LOGIN_ATTEMPTS = "Too many login attempts. Try again later."
ERR_INVALID_JSON_BODY = "Invalid JSON body"
ERR_PAT_TABLES_NOT_AVAILABLE = "Personal access token tables are not available"
ERR_TOKEN_NOT_FOUND = "Token not found"
ERR_ACCOUNT_TABLES_NOT_AVAILABLE = "Account tables are not available"
ERR_SERVICE_ACCOUNT_KEY_REQUIRED = "service_account_key is required"  # pragma: allowlist secret
ERR_MACHINE_TOKEN_TABLES_NOT_AVAILABLE = "Machine token tables are not available"
ERR_PASSWORD_RESET_FLOW_DISABLED = "Password reset flow is disabled"  # pragma: allowlist secret
ERR_PASSWORD_RESET_TABLES_NOT_AVAILABLE = (
    "Password reset tables are not available"  # pragma: allowlist secret
)
ERR_TOO_MANY_PASSWORD_RESET_REQUESTS = (
    "Too many password reset requests"  # pragma: allowlist secret
)
_INTERNAL_OPENAPI_NON_JSON_SUCCESS_OVERRIDES: dict[tuple[str, str], dict[str, Any]] = {
    (
        "GET",
        "/api/events/stream",
    ): {
        "content": {
            "text/event-stream": {
                "schema": {
                    "type": "string",
                    "description": "Server-sent events stream payload.",
                }
            }
        }
    },
    (
        "GET",
        "/api/jobs/{job_id}/attachments/{attachment_id}",
    ): {
        "content": {
            "application/octet-stream": {
                "schema": {
                    "type": "string",
                    "format": "binary",
                    "description": "Binary attachment download.",
                }
            }
        }
    },
    (
        "GET",
        "/api/workflows/{workflow_id}/export.yaml",
    ): {
        "content": {
            "application/x-yaml": {
                "schema": {
                    "type": "string",
                    "description": "Workflow export bundle serialized as YAML.",
                }
            }
        }
    },
}
_INTERNAL_OPENAPI_SUCCESS_MODEL_OVERRIDES: dict[tuple[str, str], Any] = {
    ("GET", "/api/auth/me"): AuthMeView,
    ("GET", "/api/auth/users"): AuthUsersListView,
    ("POST", "/api/auth/users"): AuthUserMutationResultView,
    ("PUT", "/api/auth/users/{user_id}"): AuthUserMutationResultView,
    ("POST", "/api/auth/users/{user_id}/unlock"): AuthUserUnlockResultView,
    (
        "POST",
        "/api/auth/users/{user_id}/force-password-reset",
    ): AuthUserForcePasswordResetResultView,
    ("GET", "/api/auth/tokens"): AuthTokensListView,
    ("POST", "/api/auth/tokens"): AuthTokenCreateResultView,
    ("POST", "/api/auth/tokens/{token_id}/revoke"): AuthTokenStatusResultView,
    ("GET", "/api/auth/accounts"): AuthAccountsListView,
    ("POST", "/api/auth/accounts"): AuthAccountEnvelopeView,
    ("GET", "/api/auth/accounts/current"): AuthAccountEnvelopeView,
    ("PATCH", "/api/auth/accounts/current"): AuthAccountStatusResultView,
    ("POST", "/api/auth/accounts/{account_id}/disable"): AuthAccountStatusResultView,
    ("GET", "/api/auth/service-accounts"): AuthServiceAccountsListView,
    ("POST", "/api/auth/service-accounts"): AuthServiceAccountEnvelopeView,
    ("PATCH", "/api/auth/service-accounts/{service_account_key}"): AuthServiceAccountEnvelopeView,
    ("DELETE", "/api/auth/service-accounts/{service_account_key}"): AuthServiceAccountEnvelopeView,
    (
        "GET",
        "/api/auth/service-accounts/{service_account_key}/tokens",
    ): AuthServiceAccountTokensListView,
    (
        "POST",
        "/api/auth/service-accounts/{service_account_key}/tokens",
    ): AuthTokenCreateResultView,
    (
        "PATCH",
        "/api/auth/service-accounts/{service_account_key}/tokens/{token_id}",
    ): AuthTokenStatusResultView,
    (
        "POST",
        "/api/auth/service-accounts/{service_account_key}/tokens/{token_id}/revoke",
    ): AuthTokenStatusResultView,
    (
        "DELETE",
        "/api/auth/service-accounts/{service_account_key}/tokens/{token_id}",
    ): AuthTokenStatusResultView,
    ("POST", "/api/auth/change-password"): AuthChangePasswordResultView,
    ("POST", "/api/auth/switch-account"): AuthSwitchAccountResultView,
    ("PATCH", "/api/users/me/profile"): UserProfileUpdateResultView,
    ("GET", "/api/users/me/email-change-status"): UserEmailChangeStatusView,
    ("POST", "/api/users/me/email-change/request"): UserEmailChangeRequestResultView,
    ("PATCH", "/api/users/me/locale"): UserLocaleResultView,
    ("PATCH", "/api/users/me/timezone"): UserTimezoneResultView,
    ("POST", "/api/users/me/detect-timezone"): UserTimezoneResultView,
    ("GET", "/api/datasets"): DatasetsListView,
    ("POST", "/api/datasets"): DatasetMutationResultView,
    ("GET", "/api/datasets/connectors"): DatasetConnectorsListView,
    ("GET", "/api/datasets/data-connections"): DatasetDataConnectionsListView,
    ("POST", "/api/datasets/data-connections"): DatasetConnectionMutationResultView,
    (
        "PATCH",
        "/api/datasets/data-connections/{connection_id}/toggle",
    ): DatasetConnectionMutationResultView,
    (
        "DELETE",
        "/api/datasets/data-connections/{connection_id}",
    ): DatasetConnectionMutationResultView,
    (
        "POST",
        "/api/datasets/data-connections/{connection_id}/test",
    ): DatasetActionResponseView,
    (
        "POST",
        "/api/datasets/data-connections/{connection_id}/introspect",
    ): DatasetActionResponseView,
    ("GET", "/api/datasets/query-templates"): DatasetQueryTemplatesListView,
    ("POST", "/api/datasets/query-templates"): DatasetQueryTemplateMutationResultView,
    (
        "PUT",
        "/api/datasets/query-templates/{query_template_id}",
    ): DatasetQueryTemplateMutationResultView,
    (
        "PATCH",
        "/api/datasets/query-templates/{query_template_id}/toggle",
    ): DatasetQueryTemplateMutationResultView,
    (
        "DELETE",
        "/api/datasets/query-templates/{query_template_id}",
    ): DatasetQueryTemplateMutationResultView,
    (
        "POST",
        "/api/datasets/query-templates/{query_template_id}/validate",
    ): DatasetActionResponseView,
    (
        "POST",
        "/api/datasets/query-templates/{query_template_id}/preview",
    ): DatasetActionResponseView,
    (
        "POST",
        "/api/datasets/query-templates/{query_template_id}/infer-output-schema",
    ): DatasetActionResponseView,
    ("GET", "/api/datasets/project/{project_id}"): DatasetProjectCatalogView,
    (
        "PUT",
        "/api/datasets/project/{project_id}",
    ): DatasetProjectAssignmentUpdateResultView,
    ("GET", "/api/datasets/{dataset_id}/projects"): DatasetProjectAssignmentCatalogView,
    (
        "PUT",
        "/api/datasets/{dataset_id}/projects",
    ): DatasetDatasetProjectAssignmentUpdateResultView,
    ("GET", API_DATASETS_DATASET_ID_PATH): DatasetRecordView,
    ("PUT", API_DATASETS_DATASET_ID_PATH): DatasetMutationResultView,
    ("PATCH", API_DATASETS_DATASET_ID_PATH): DatasetMutationResultView,
    ("PATCH", "/api/datasets/{dataset_id}/toggle"): DatasetMutationResultView,
    ("DELETE", API_DATASETS_DATASET_ID_PATH): DatasetMutationResultView,
    ("POST", "/api/jobs/{job_id}/acknowledge"): JobLifecycleResultView,
    ("POST", "/api/jobs/{job_id}/unacknowledge"): JobLifecycleResultView,
    ("POST", "/api/jobs/{job_id}/restart"): JobLifecycleResultView,
    ("GET", "/api/jobs/awaiting-approval"): list[AwaitingApprovalJobView],
    ("GET", "/api/jobs/{job_id}/runtime-journal"): JobRuntimeJournalView,
    ("GET", "/api/jobs/{job_id}/decision-overrides"): JobDecisionOverridesView,
    ("POST", "/api/jobs/{job_id}/decision-overrides"): JobDecisionOverrideResultView,
    ("GET", "/api/jobs/{job_id}/errors"): JobErrorsView,
    ("GET", "/api/jobs/{job_id}/causal-chain"): JobCausalChainView,
    ("DELETE", "/api/jobs/{job_id}"): JobDeleteResultView,
    ("GET", "/api/jobs/{job_id}/trace"): JobTraceView,
    ("GET", "/api/jobs/{job_id}/incident"): JobIncidentDrilldownView,
    ("GET", "/api/jobs/{job_id}/cost-breakdown"): JobCostBreakdownView,
    ("GET", "/api/jobs/{job_id}/events"): JobEventsView,
    ("GET", "/api/jobs/{job_id}/messages"): JobMessagesView,
    ("GET", "/api/messages/{message_id}"): JobMessageDetailView,
    ("GET", "/api/events"): EventsListView,
    ("GET", "/api/events/{event_ref}"): DomainEventDetailView,
    ("POST", "/api/events/{event_ref}/redrive"): EventRedriveResultView,
    ("GET", "/api/dashboard/summary"): DashboardSummaryView,
    ("GET", "/api/slo/core"): CoreSloMetricsView,
    ("GET", "/api/jobs/{job_id}/replay-step"): JobReplayStepView,
    ("GET", "/api/jobs/{job_id}/step-replays"): JobStepReplaysView,
    ("POST", "/api/jobs/{job_id}/replay-step/execute"): JobStepReplayExecutionResultView,
    ("POST", "/api/jobs/{job_id}/retry-step"): JobLifecycleResultView,
    ("GET", "/api/jobs/{job_id}/logs"): JobLogsView,
    ("GET", "/api/jobs/{job_id}/approval"): ApprovalGateView,
    ("GET", "/api/jobs/{job_id}/approvals"): ApprovalHistoryView,
    ("POST", "/api/projects"): ProjectMutationResultView,
    ("GET", "/api/projects/{project_id}/providers"): ProjectProviderBindingsView,
    ("POST", "/api/projects/{project_id}/providers/bind"): ProjectProviderBindingsView,
    ("GET", "/api/projects/{project_id}/notification-routes"): ProjectNotificationRoutesView,
    (
        "GET",
        "/api/projects/{project_id}/notification-overview",
    ): ProjectNotificationOverviewView,
    (
        "POST",
        "/api/projects/{project_id}/notification-routes",
    ): ProjectNotificationRoutesUpdateResultView,
    (
        "GET",
        "/api/projects/{project_id}/notification-deliveries",
    ): ProjectNotificationDeliveriesView,
    (
        "POST",
        "/api/projects/{project_id}/notifications/dispatch",
    ): ProjectNotificationDispatchResultView,
    ("GET", "/api/providers"): ProvidersCatalogView,
    ("POST", "/api/providers"): ProvidersCatalogItemView,
    ("POST", "/api/providers/{provider_key}/secrets"): ProviderSecretStatusView,
    ("POST", "/api/reactions/ingest"): ReactionEventIngestResultView,
    ("GET", "/api/projects/{project_id}/connections"): ProjectConnectionListView,
    ("POST", "/api/projects/{project_id}/connections"): ProjectConnectionView,
    ("GET", API_PROJECTS_PROJECT_ID_CONNECTIONS_CONNECTION_ID_PATH): ProjectConnectionView,
    ("PUT", API_PROJECTS_PROJECT_ID_CONNECTIONS_CONNECTION_ID_PATH): ProjectConnectionView,
    (
        "DELETE",
        API_PROJECTS_PROJECT_ID_CONNECTIONS_CONNECTION_ID_PATH,
    ): ProjectConnectionDeleteResultView,
    ("GET", API_PROJECTS_PROJECT_ID_PATH): ProjectAdminDetailView,
    ("PUT", API_PROJECTS_PROJECT_ID_PATH): ProjectMutationResultView,
    ("DELETE", API_PROJECTS_PROJECT_ID_PATH): ProjectDeleteResultView,
    ("POST", "/api/projects/{project_id}/move-workspace"): ProjectMutationResultView,
    ("GET", "/api/projects/{project_id}/workflows"): ProjectWorkflowBindingsView,
    ("PUT", "/api/projects/{project_id}/workflows"): ProjectWorkflowBindingsResultView,
    (
        "GET",
        "/api/projects/{project_id}/workflows/{workflow_id}/validate",
    ): WorkflowValidationResultView,
    (
        "GET",
        "/api/projects/{project_id}/workflows/{workflow_id}/suggestions",
    ): WorkflowSuggestionsResultView,
    ("GET", "/api/projects/{project_id}/policy"): ProjectPolicyView,
    ("PUT", "/api/projects/{project_id}/policy"): ProjectPolicyResultView,
    ("GET", "/api/projects/{project_id}/history"): ProjectSettingsHistoryListView,
    (
        "GET",
        "/api/projects/{project_id}/history/{revision_id}/diff",
    ): ProjectSettingsHistoryDiffView,
    (
        "POST",
        "/api/projects/{project_id}/history/{revision_id}/restore",
    ): ProjectSettingsRestoreResultView,
    (
        "PATCH",
        "/api/projects/{project_id}/schedule-timezone",
    ): ProjectScheduleTimezoneResultView,
    ("POST", "/api/projects/{project_id}/scheduled-jobs"): ProjectScheduledJobMutationResultView,
    ("GET", "/api/projects/{project_id}/scheduled-jobs"): ProjectScheduledJobsView,
    (
        "PATCH",
        "/api/projects/{project_id}/scheduled-jobs/{scheduled_job_id}/toggle",
    ): ProjectScheduledJobMutationResultView,
    (
        "DELETE",
        "/api/projects/{project_id}/scheduled-jobs/{scheduled_job_id}",
    ): ProjectScheduledJobDeleteResultView,
    (
        "POST",
        "/api/projects/{project_id}/scheduled-jobs/{scheduled_job_id}/run-now",
    ): ProjectScheduledJobRunResultView,
    ("POST", "/api/projects/{project_id}/workflow-preview"): ProjectWorkflowPreviewView,
    ("GET", "/api/projects/{project_id}/cron-preview"): ProjectCronPreviewView,
    ("GET", "/api/workflows"): list[WorkflowAdminSummaryView],
    ("POST", "/api/workflows"): WorkflowMutationResultView,
    ("POST", "/api/workflows/import/preflight"): WorkflowImportPreflightView,
    ("GET", "/api/workflows/import/options"): WorkflowImportDependencyOptionsView,
    ("POST", "/api/workflows/import/apply"): WorkflowImportApplyResultView,
    ("GET", API_WORKFLOWS_WORKFLOW_ID_PATH): WorkflowAdminDetailView,
    ("PUT", API_WORKFLOWS_WORKFLOW_ID_PATH): WorkflowMutationResultView,
    ("PATCH", API_WORKFLOWS_WORKFLOW_ID_PATH): WorkflowMutationResultView,
    ("PATCH", "/api/workflows/{workflow_id}/toggle"): WorkflowMutationResultView,
    ("DELETE", API_WORKFLOWS_WORKFLOW_ID_PATH): WorkflowMutationResultView,
    ("POST", "/api/workflows/{workflow_id}/clone"): WorkflowMutationResultView,
    ("POST", "/api/workflows/{workflow_id}/restore"): WorkflowMutationResultView,
    ("GET", "/api/workflows/{workflow_id}/dag"): WorkflowDagView,
    ("GET", "/api/workflows/{workflow_id}/canvas-graph"): WorkflowCanvasGraphView,
    ("GET", "/api/workflows/{workflow_id}/export-policy"): WorkflowExportPolicyView,
    ("GET", "/api/workflows/{workflow_id}/contract-validation"): WorkflowValidationSummaryView,
    ("POST", "/api/workflows/{workflow_id}/validate"): WorkflowIssueListView,
    ("GET", "/api/workflows/{workflow_id}/stats"): WorkflowStatsView,
    ("GET", "/api/workflows/{workflow_id}/history"): list[WorkflowHistoryEntryView],
    ("GET", "/api/workflows/{workflow_id}/history/{revision}"): WorkflowHistoryEntryView,
    ("GET", "/api/workflows/{workflow_id}/diff"): WorkflowDiffResultView,
    ("GET", "/api/workflows/meta/callable-workflows"): CallableWorkflowListView,
    ("GET", "/api/workflows/meta/agent-skill-pairs"): AgentSkillPairListView,
    ("GET", "/api/workflows/{workflow_id}/contract"): WorkflowContractResultView,
    ("GET", "/api/workflows/{workflow_id}/suggestions"): WorkflowSuggestionsResultView,
    ("GET", "/api/workflows/{workflow_id}/reactions"): WorkflowReactionsView,
    ("POST", "/api/workflows/{workflow_id}/reactions"): WorkflowReactionsView,
    ("GET", "/api/workflows/meta/connector-catalog"): WorkflowConnectorCatalogView,
    ("GET", "/api/workflows/meta/extension-form-schema"): WorkflowExtensionFormSchemaView,
    ("POST", "/api/workflows/meta/extension-resource-query"): WorkflowExtensionResourceQueryView,
    ("GET", "/api/model-selection/options"): ModelSelectionOptionsView,
    ("GET", "/api/settings/model-catalog"): ModelCatalogSettingsView,
    ("PUT", "/api/settings/model-catalog"): SaveStatusView,
    ("GET", "/api/model-providers"): ModelProvidersListView,
    ("POST", "/api/model-providers"): ModelProviderMutationResultView,
    ("PUT", "/api/model-providers/{provider_id}"): ModelProviderMutationResultView,
    ("DELETE", "/api/model-providers/{provider_id}"): ModelProviderMutationResultView,
    ("GET", "/api/settings/retention"): RetentionSettingsView,
    ("POST", "/api/settings/retention"): RetentionSettingsSaveView,
    ("GET", "/api/registry/stats"): RegistryStatsView,
    ("GET", "/api/validate"): RegistryValidationView,
    ("GET", "/api/agents/{agent_id}/diff"): AgentDiffView,
    ("GET", "/api/skills/{skill_id}/diff"): SkillDiffView,
    ("GET", "/api/tools/{tool_id}/diff"): ToolDiffView,
    ("GET", "/api/workflows/{workflow_id}/triggers/catalog"): WorkflowTriggerCatalogResponseView,
    ("GET", "/api/workflows/{workflow_id}/triggers"): WorkflowTriggersView,
    (
        "POST",
        "/api/workflows/{workflow_id}/triggers/webhooks",
    ): WorkflowTriggerEntrypointView,
    (
        "PATCH",
        "/api/workflows/{workflow_id}/triggers/webhooks/{entry_key}",
    ): WorkflowTriggerEntrypointView,
    (
        "DELETE",
        "/api/workflows/{workflow_id}/triggers/webhooks/{entry_key}",
    ): WorkflowWebhookTriggerDeleteResultView,
    (
        "POST",
        "/api/workflows/{workflow_id}/triggers/webhooks/{entry_key}/rotate-secret",
    ): WorkflowTriggerEntrypointView,
    (
        "POST",
        "/api/workflows/{workflow_id}/triggers/webhooks/{entry_key}/regenerate-entry-key",
    ): WorkflowTriggerEntrypointView,
    (
        "POST",
        "/api/workflows/{workflow_id}/triggers/webhooks/{entry_key}/dry-run",
    ): WorkflowTriggerDryRunView,
    (
        "GET",
        "/api/workflows/{workflow_id}/triggers/webhooks/{entry_key}/deliveries",
    ): WorkflowWebhookDeliveriesView,
    (
        "POST",
        "/api/workflows/{workflow_id}/triggers/providers",
    ): WorkflowProviderTriggerView,
    (
        "PATCH",
        "/api/workflows/{workflow_id}/triggers/providers/{trigger_id}",
    ): WorkflowProviderTriggerView,
    (
        "DELETE",
        "/api/workflows/{workflow_id}/triggers/providers/{trigger_id}",
    ): WorkflowProviderTriggerDeleteResultView,
    ("POST", "/api/extensions/descriptors/validate"): ExtensionDescriptorValidateResultView,
    (
        "POST",
        "/api/extensions/descriptors/import/file",
    ): ExtensionReleaseImportResultView,
    (
        "POST",
        "/api/extensions/descriptors/import/url",
    ): ExtensionReleaseImportResultView,
    ("GET", "/api/extensions"): ExtensionListView,
    ("GET", "/api/extensions/marketplace"): ExtensionMarketplaceListView,
    ("GET", "/api/extensions/installs/{install_id}"): ExtensionDetailView,
    ("GET", "/api/extensions/{extension_id}"): ExtensionDetailView,
    ("POST", "/api/extensions/releases/{release_id}/install"): ExtensionInstallResultView,
    ("POST", "/api/extensions/installs/{install_id}/handshake"): ExtensionRuntimeHealthResultView,
    ("POST", "/api/extensions/{extension_id}/install"): ExtensionInstallResultView,
    ("POST", "/api/extensions/runtime/token"): ExtensionRuntimeTokenResultView,
    ("POST", "/api/extensions/generated-jobs"): ExtensionGeneratedJobResultView,
    ("POST", "/api/extensions/{extension_id}/enable"): ExtensionToggleResultView,
    ("POST", "/api/extensions/{extension_id}/disable"): ExtensionToggleResultView,
    ("GET", "/api/workspaces"): WorkspacesListView,
    ("POST", "/api/workspaces"): WorkspaceRecordView,
    ("GET", "/api/workspaces/{workspace_id}"): WorkspaceRecordView,
    ("PUT", "/api/workspaces/{workspace_id}"): WorkspaceRecordView,
    ("PATCH", "/api/workspaces/{workspace_id}/restore"): WorkspaceRecordView,
    ("GET", "/api/workspaces/{workspace_id}/projects"): WorkspaceProjectsView,
    ("GET", "/api/data-contracts"): list[DataContractRecordView],
    ("POST", "/api/data-contracts"): DataContractMutationResultView,
    ("GET", API_DATA_CONTRACTS_CONTRACT_ID_PATH): DataContractRecordView,
    ("PUT", API_DATA_CONTRACTS_CONTRACT_ID_PATH): DataContractMutationResultView,
    ("PATCH", API_DATA_CONTRACTS_CONTRACT_ID_PATH): DataContractMutationResultView,
    ("DELETE", API_DATA_CONTRACTS_CONTRACT_ID_PATH): DataContractMutationResultView,
    ("PATCH", "/api/data-contracts/{contract_id}/toggle"): DataContractMutationResultView,
    ("GET", "/api/fields"): list[FieldRecordView],
    ("POST", "/api/fields"): FieldMutationResultView,
    ("GET", API_FIELDS_FIELD_KEY_PATH): FieldRecordView,
    ("PUT", API_FIELDS_FIELD_KEY_PATH): FieldMutationResultView,
    ("PATCH", API_FIELDS_FIELD_KEY_PATH): FieldMutationResultView,
    ("DELETE", API_FIELDS_FIELD_KEY_PATH): FieldMutationResultView,
    ("PATCH", "/api/fields/{field_key}/toggle"): FieldMutationResultView,
    ("GET", "/api/published-blocks"): list[PublishedBlockRecordView],
    ("POST", "/api/published-blocks"): PublishedBlockMutationResultView,
    ("GET", API_PUBLISHED_BLOCKS_BLOCK_KEY_PATH): PublishedBlockRecordView,
    ("PUT", API_PUBLISHED_BLOCKS_BLOCK_KEY_PATH): PublishedBlockMutationResultView,
    ("PATCH", API_PUBLISHED_BLOCKS_BLOCK_KEY_PATH): PublishedBlockMutationResultView,
    ("DELETE", API_PUBLISHED_BLOCKS_BLOCK_KEY_PATH): PublishedBlockMutationResultView,
    ("PATCH", "/api/published-blocks/{block_key}/toggle"): PublishedBlockMutationResultView,
    ("GET", "/api/guardrails"): list[GuardrailRecordView],
    ("POST", "/api/guardrails"): GuardrailMutationResultView,
    ("GET", API_GUARDRAILS_GUARDRAIL_ID_PATH): GuardrailRecordView,
    ("PUT", API_GUARDRAILS_GUARDRAIL_ID_PATH): GuardrailMutationResultView,
    ("PATCH", API_GUARDRAILS_GUARDRAIL_ID_PATH): GuardrailMutationResultView,
    ("DELETE", API_GUARDRAILS_GUARDRAIL_ID_PATH): GuardrailDeleteResultView,
    ("PATCH", "/api/guardrails/{guardrail_id}/toggle"): GuardrailMutationResultView,
    ("PUT", "/api/guardrails/{guardrail_id}/agents"): GuardrailAgentsResultView,
    ("GET", "/api/guardrails/{guardrail_id}/history"): list[GuardrailHistoryEntryView],
    ("GET", "/api/guardrails/{guardrail_id}/history/{revision}"): GuardrailHistoryEntryView,
    ("GET", "/api/guardrails/{guardrail_id}/diff"): GuardrailDiffView,
    ("POST", "/api/guardrails/{guardrail_id}/restore"): GuardrailRestoreResultView,
    ("GET", "/api/changelog"): list[ChangelogEntryView],
    ("GET", "/api/changelog/stats"): RegistryStatsView,
    ("GET", "/api/changelog/validate"): RegistryValidationView,
    ("GET", "/api/changelog/search"): list[RegistrySearchResultView],
    ("GET", "/api/changelog/{entity_type}/{entity_id}/history"): list[ChangelogEntryView],
    ("GET", "/api/changelog/{entity_type}/{entity_id}/history/{revision}"): ChangelogEntryView,
    ("GET", "/api/changelog/{entity_type}/{entity_id}/diff"): ChangelogDiffView,
    ("POST", "/api/changelog/{entity_type}/{entity_id}/restore"): ChangelogRestoreResultView,
    ("GET", "/api/security/policy"): SecurityPolicy,
    ("PUT", "/api/security/policy"): SecurityPolicyUpdateResultView,
    ("GET", "/api/security/projects/{project_id}/policy"): SecurityPolicy
    | SecurityPolicyWithSourcesView,
    ("PUT", "/api/security/projects/{project_id}/policy"): SecurityProjectPolicyUpdateResultView,
    ("POST", "/api/security/projects/{project_id}/rules"): SecurityCustomRuleResultView,
    ("GET", "/api/security/posture"): SecurityPostureView,
    ("GET", "/api/security/events"): SecurityEventsListView,
    ("GET", "/api/security/findings"): SecurityFindingsListView,
    ("GET", "/api/mcp"): McpConnectorListView,
    ("POST", "/api/mcp"): McpConnectorView,
    ("GET", API_MCP_CONNECTOR_ID_PATH): McpConnectorView,
    ("PUT", API_MCP_CONNECTOR_ID_PATH): McpConnectorView,
    ("DELETE", API_MCP_CONNECTOR_ID_PATH): McpConnectorDeleteResultView,
    ("POST", "/api/mcp/{connector_id}/health"): McpConnectorHealthView,
    ("POST", "/api/mcp/{connector_id}/sync"): McpConnectorView,
    ("GET", "/api/trash"): TrashListView,
    ("POST", "/api/trash/{entity_type}/{entity_id}/trash"): TrashResultView,
    ("POST", "/api/trash/workspace/{workspace_id}/delete-plan"): WorkspaceDeletePlanView,
    (
        "POST",
        "/api/trash/workspace/{workspace_id}/delete-execute",
    ): WorkspaceDeleteExecuteResultView,
    ("POST", "/api/trash/{entity_type}/{entity_id}/restore"): RestoreResultView,
    ("POST", "/api/trash/{entity_type}/{entity_id}/plan"): LifecyclePlanView,
    ("POST", "/api/trash/{entity_type}/{entity_id}/execute"): RestoreResultView
    | HardDeleteResultView,
    ("POST", "/api/trash/{entity_type}/{entity_id}/hard-delete"): HardDeleteResultView,
    ("GET", "/api/jobs/{job_id}/reactions"): JobReactionsView,
    ("POST", "/api/extensions/install"): ExtensionArchiveInstallResultView,
    (
        "GET",
        "/api/extensions/capabilities/archive-lifecycle",
    ): ExtensionArchiveLifecycleAvailabilityView,
    ("POST", "/api/extensions/scan"): ExtensionScanResultView,
    ("GET", "/api/extensions/{extension_id}/health"): ExtensionHealthView,
    ("POST", "/api/extensions/{extension_id}/query/{query_id}"): ExtensionQueryResultView,
    ("POST", "/api/extensions/{extension_id}/action/{action_id}"): ExtensionActionResultView,
    ("POST", "/api/extensions/{extension_id}/invoke"): ExtensionInvocationResultView,
    ("POST", "/api/commands"): CommandDispatchResultView,
    ("POST", "/api/commands/batch"): CommandBatchResultView,
}
_INTERNAL_OPENAPI_SUCCESS_EXAMPLE_OVERRIDES: dict[tuple[str, str], Any] = {
    ("POST", "/api/registry/reload"): {
        "status": "reloaded",
        "stats": {"agents": 12, "skills": 34, "tools": 21, "workflows": 8},
    },
    ("GET", "/api/graph"): {
        "nodes": {
            "fixed": ["task_intake", "orchestrator"],
            "agents": ["release_writer", "reviewer"],
            "total": 7,
        }
    },
    ("POST", "/api/processes/{pid}/kill"): {"killed": True, "pid": 4242, "signal": "SIGTERM"},
    ("GET", "/api/preview/agent/{agent_id}"): {
        "agent_id": "release_writer",
        "prompt_preview": "Draft release notes for version 2.4.",
        "variables": ["release_version", "changes"],
    },
    ("POST", "/api/preview/agent/{agent_id}"): {
        "agent_id": "release_writer",
        "prompt_preview": "Draft release notes for version 2.4 using the provided state.",
        "variables": ["release_version", "changes"],
        "state": {"release_version": "2.4.0"},
    },
    ("GET", "/api/preview/workflow/{workflow_id}"): {
        "workflow_id": "release-report",
        "preview": {"task_summary": "Generate a release-readiness report.", "steps": 4},
    },
    ("GET", "/api/workload"): {
        "recommendation": "normal",
        "active_jobs": 2,
        "queue_depth": 0,
        "db_connections": 5,
    },
    ("GET", "/api/extensions/{extension_id}/preview"): {
        "parent_project_id": "proj_42",
        "project_name": "Slack Notifier",
        "artifact_kind": "extension",
        "warnings": [],
        "next_step": "create_extension",
    },
    ("GET", "/api/extensions/{extension_id}/compiled-manifest"): {
        "extension_id": "slack_notifier",
        "compile_status": "ok",
        "manifest": {"version": "1.0.0"},
    },
    ("GET", "/api/watchdog/jobs/scan"): {
        "status": "ok",
        "stuck_jobs": [],
        "orphaned_jobs": [],
        "recoverable_jobs": [],
    },
    ("POST", "/api/watchdog/jobs/{job_id}/recover"): {
        "status": "recovered",
        "job_id": "job-987",
        "action": "requeue",
    },
    ("POST", "/api/reports/generate"): {
        "report_id": "rpt-42",
        "status": "ready",
        "report_type": "job_summary",
    },
    ("GET", "/api/reports/{report_id}"): {
        "report_id": "rpt-42",
        "status": "ready",
        "content": {"title": "Job Summary"},
    },
    ("GET", "/api/search"): {
        "category": "all",
        "results": [{"label": "release-report", "type": "workflow", "id": "release-report"}],
    },
    ("POST", "/api/external-agents/register"): {
        "status": "registered",
        "agent_id": "agent-runner-1",
        "lease_id": "lease-123",
    },
    ("POST", "/api/external-agents/heartbeat"): {
        "status": "alive",
        "agent_id": "agent-runner-1",
        "next_heartbeat_at": "2026-05-28T12:05:00Z",
    },
    ("POST", "/api/external-agents/claim"): {
        "status": "claimed",
        "agent_id": "agent-runner-1",
        "job_id": "job-987",
    },
    ("POST", "/api/external-agents/report"): {
        "status": "accepted",
        "agent_id": "agent-runner-1",
        "job_id": "job-987",
    },
    ("GET", "/api/system/monitoring/summary"): {
        "status": "ready",
        "ready": True,
        "checks": {"postgres": {"ok": True}},
        "summary": {"checks_total": 1, "checks_ok": 1, "checks_failed": 0},
    },
    ("GET", "/api/system/monitoring/storage"): {
        "generated_at": "2026-05-28T12:00:00Z",
        "backend_matrix": {},
        "summary": {},
        "memory": {"available": True},
    },
    ("GET", "/api/processes"): {
        "jobs": [],
        "subprocesses": [],
        "suspected_orphans": [],
        "project_leases": [],
        "project_lease_evidence_unavailable": False,
        "workers": [],
        "reaped_worker_subprocess_count": 0,
    },
    ("POST", "/api/extensions/{extension_id}/callbacks/{callback_id}"): {
        "registration": {"registration_id": "reg-123", "callback_id": "incoming_webhook"},
        "callback_url": "https://example.com/webhook/extensions/slack_notifier/incoming_webhook/<redacted>",
        "callback_token": "redacted",
    },
    ("POST", "/api/extensions/{extension_id}/callbacks/{registration_id}/rotate"): {
        "registration": {"registration_id": "reg-123", "callback_id": "incoming_webhook"},
        "callback_url": "https://example.com/webhook/extensions/slack_notifier/incoming_webhook/<redacted>",
        "callback_token": "redacted",
    },
    ("POST", "/api/extensions/{extension_id}/callbacks/{registration_id}/status"): {
        "ok": True,
        "webhook_status": {"reachable": True},
        "status": {"ok": True, "result": {"reachable": True}},
    },
}
_INTERNAL_OPENAPI_DESCRIPTION = (
    "Authenticated internal JSON/API surface under `/api/*`, excluding HTML/UI "
    "surfaces, health and metrics endpoints, and raw webhook or callback ingress."
)
_INTERNAL_OPENAPI_EXCLUDED_PREFIXES = ("/api/health",)
ERR_WEBHOOK_PROCESSING_FAILED = "Webhook processing failed"

SQL_FROM_USERS = "FROM users u"
SQL_LIMIT_1 = "LIMIT 1"
SQL_USER_USERNAME_COLUMN = "    u.username,"
SQL_UPDATE_USER_PASSWORD_BY_ID = "UPDATE users SET password_hash = %s WHERE id = %s"
_APP_GZIP_ENABLED = bool(CONFIG.get("app_gzip_enabled", True))
_GZIP_MINIMUM_SIZE = max(256, int(CONFIG.get("gzip_minimum_size") or 512))
_STATIC_CACHE_MAX_AGE_SECONDS = max(0, int(CONFIG.get("static_cache_max_age_seconds") or 300))
_STATIC_ASSET_VERSION = str(CONFIG.get("static_asset_version") or "").strip() or str(
    int(time.time())
)
_STATIC_ROOT = Path("assets").resolve()
# Registry & config routes: operators get read (GET/HEAD/OPTIONS) access,
# writes (POST/PUT/PATCH/DELETE) still require admin.
_AUTH_REGISTRY_READ_PATTERNS = (
    re.compile(r"^/api/changelog(?:/|$)"),
    re.compile(r"^/api/agents(?:/|$)"),
    re.compile(r"^/api/catalog-categories(?:/|$)"),
    re.compile(r"^/api/tools(?:/|$)"),
    re.compile(r"^/api/skills(?:/|$)"),
    re.compile(r"^/api/workflows(?:/|$)"),
    re.compile(r"^/api/data-contracts(?:/|$)"),
    re.compile(r"^/api/fields(?:/|$)"),
    re.compile(r"^/api/published-blocks(?:/|$)"),
    re.compile(r"^/api/providers(?:/|$)"),
    re.compile(r"^/api/extensions(?:/|$)"),
    re.compile(r"^/api/model-selection/options$"),
    re.compile(r"^/api/workload$"),
    re.compile(r"^/api/runs$"),
    re.compile(r"^/api/security/posture$"),
    re.compile(r"^/health/deep$"),
)

# Module-level Flow Steward logger singleton — reused across all requests in the same process.
# Avoids opening a new psycopg_runtime connection (+ running DDL) on every API call.
_platform_logger_singleton: Any | None = None
_platform_logger_singleton_dsn: str = ""
_state_reader_graph_lock = threading.Lock()
_state_reader_graph = None
_state_reader_graph_dsn: str = ""
_OWNER_DIR_MODE = stat.S_IRWXU
_password_reset_lock = threading.Lock()
_password_reset_attempts: dict[str, dict[str, Any]] = {}
_DASHBOARD_RANGE_HOURS: dict[str, int] = {
    "24h": 24,
    "7d": 24 * 7,
    "30d": 24 * 30,
}
_DASHBOARD_DEFAULT_RANGE = "7d"
_DASHBOARD_METRICS_PAGE_SIZE = max(
    100, min(int(CONFIG.get("dashboard_metrics_page_size") or 1000), 5000)
)
_DASHBOARD_METRICS_MAX_ROWS = max(0, int(CONFIG.get("dashboard_metrics_max_rows") or 0))
_DASHBOARD_TRIAGE_LIMIT = max(10, min(int(CONFIG.get("dashboard_triage_limit") or 25), 200))
_DASHBOARD_ROLLUP_READ_ENABLED = bool(CONFIG.get("dashboard_rollup_read_enabled", True))

__all__ = [name for name in globals() if not (name.startswith("__") and name.endswith("__"))]
