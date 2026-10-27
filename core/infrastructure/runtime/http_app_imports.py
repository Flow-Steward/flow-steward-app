# ruff: noqa: F401
"""
FastAPI server for Platform orchestrator.
Provides:
  - React shell routes: /projects, /jobs/{id}, /settings, etc.
  - REST API: /api/workflow-runs, /webhook/providers/{provider_key}, /api/approvals/{thread_id}
  - JSON endpoints: /api/jobs/{id}, /api/jobs/{id}/logs, /api/jobs/{id}/approvals
  - WebSocket: /ws/jobs/{id}  — replaces polling; pushes logs/status/approval/cost

Task execution model:
  Tasks are NOT run synchronously in the web process.
  Instead they are enqueued to RabbitMQ and processed by worker/worker.py.
  Each project has its own queue. Top-level project concurrency is controlled by
  `project_policies.concurrency_cap` (default: 1), and effective parallelism also
  depends on how many worker processes/containers are running. With the default
  cap, same-project tasks stay serial; different projects can run in parallel
  when multiple workers are available.

  The web process only:
    1. Creates a job row with status='pending'
    2. Publishes the task message to the project's RabbitMQ queue
    3. Exposes API/WebSocket state for the React job pages
"""

import asyncio
import inspect
import json
import logging
import os
import re
import signal
import stat
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import replace as _dataclass_replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote

from fastapi import (
    FastAPI,
    Request,
)
from fastapi.exceptions import RequestValidationError
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.openapi.utils import get_openapi
from fastapi.responses import (
    JSONResponse,
    RedirectResponse,
)
from fastapi.routing import APIRoute
from fastapi.staticfiles import StaticFiles
from pydantic import TypeAdapter, ValidationError
from starlette.middleware.gzip import GZipMiddleware

from core.application.auth import auth_http_runtime as _auth_http_runtime
from core.application.auth import (
    serve_auth_runtime as _serve_auth_runtime,
)
from core.application.auth.config import AUTH_ADMIN_ONLY_PATTERNS as _AUTH_ADMIN_ONLY_PATTERNS
from core.application.auth.config import AUTH_ADMIN_ROLES as _AUTH_ADMIN_ROLES
from core.application.auth.config import AUTH_BOOTSTRAP_TOKEN_HEADER as _AUTH_BOOTSTRAP_TOKEN_HEADER
from core.application.auth.config import AUTH_COOKIE_NAME as _AUTH_COOKIE_NAME
from core.application.auth.config import AUTH_CSRF_COOKIE_NAME as _AUTH_CSRF_COOKIE_NAME
from core.application.auth.config import AUTH_CSRF_HEADER_NAME as _AUTH_CSRF_HEADER_NAME
from core.application.auth.config import AUTH_PASSWORD_MIN_LENGTH as _AUTH_PASSWORD_MIN_LENGTH
from core.application.auth.config import (
    AUTH_PASSWORD_RESET_LOCKOUT_SECONDS as _PASSWORD_RESET_LOCKOUT_SECONDS,
)
from core.application.auth.config import (
    AUTH_PASSWORD_RESET_MAX_ATTEMPTS as _PASSWORD_RESET_MAX_ATTEMPTS,
)
from core.application.auth.config import (
    AUTH_PASSWORD_RESET_RATE_WINDOW_SECONDS as _PASSWORD_RESET_RATE_WINDOW_SECONDS,
)
from core.application.auth.config import (
    AUTH_PASSWORD_RESET_TTL_MINUTES as _AUTH_PASSWORD_RESET_TTL_MINUTES,
)
from core.application.auth.config import AUTH_PUBLIC_PATHS as _AUTH_PUBLIC_PATHS_SOURCE
from core.application.auth.config import (
    AUTH_REMEMBER_SESSION_TTL_HOURS as _AUTH_REMEMBER_SESSION_TTL_HOURS,
)
from core.application.auth.config import AUTH_SESSION_TTL_HOURS as _AUTH_SESSION_TTL_HOURS
from core.application.auth.config import AUTH_UNSAFE_METHODS as _AUTH_UNSAFE_METHODS
from core.application.auth.config import AUTH_WRITE_ROLES as _AUTH_WRITE_ROLES
from core.application.auth.config import DEFAULT_ACCOUNT_ID
from core.application.auth.config import (
    PUBLIC_FRONTEND_PATH_PREFIXES as _PUBLIC_FRONTEND_PATH_PREFIXES,
)
from core.application.auth.config import PUBLIC_HEALTH_PATHS as _PUBLIC_HEALTH_PATHS
from core.application.auth.config import (
    PUBLIC_WEBHOOK_PATH_PREFIXES as _PUBLIC_WEBHOOK_PATH_PREFIXES,
)
from core.application.auth.password import password_hash as _password_hash_impl
from core.application.auth.password import (
    password_reset_token_hash as _password_reset_token_hash_impl,
)
from core.application.auth.password import session_token_hash as _session_token_hash_impl
from core.application.auth.password_reset_rate_limit import (
    password_reset_throttle_check as _password_reset_throttle_check_impl,
)
from core.application.auth.password_reset_rate_limit import (
    password_reset_throttle_key as _password_reset_throttle_key_impl,
)
from core.application.auth.password_reset_rate_limit import (
    password_reset_throttle_record as _password_reset_throttle_record_impl,
)
from core.application.auth.principal import ResolvedPrincipal
from core.application.auth.rate_limit import (
    auth_login_clear_failures as _auth_login_clear_failures_impl,
)
from core.application.auth.rate_limit import (
    auth_login_clear_user_failures as _auth_login_clear_user_failures_impl,
)
from core.application.auth.rate_limit import auth_login_lock_state as _auth_login_lock_state_impl
from core.application.auth.rate_limit import (
    auth_login_record_failure as _auth_login_record_failure_impl,
)
from core.application.auth.resolve import normalize_principal
from core.application.extensions import service_support as _extension_services
from core.application.extensions.artifact_dataset_plane_v2 import ArtifactDatasetPlane
from core.application.extensions.artifact_storage import create_storage_adapter
from core.application.extensions.install import DBExtensionInstallStore as _DBExtensionInstallStore
from core.application.extensions.system_email import send_password_reset_email
from core.application.jobs import job_runtime_payload as _job_runtime_payload
from core.application.jobs import job_trace_runtime as _job_trace_runtime
from core.application.jobs import task_enqueue_runtime as _task_enqueue_runtime
from core.application.jobs.job_invocation_artifacts import (
    load_replay_snapshot as _load_job_replay_snapshot,
)
from core.application.management import (
    ServiceError,
    ServiceNotFoundError,
    ServiceUnavailableError,
    ServiceValidationError,
)
from core.application.management import (
    command_service_runtime as _command_service_runtime,
)
from core.application.management import (
    query_service_runtime as _query_service_runtime,
)
from core.application.management import (
    snapshot_serializers as _serve_snapshot_serializers_runtime,
)
from core.application.management.command_models import (
    AuthAccountStatusResultView,
    AuthChangePasswordResultView,
    AuthSwitchAccountResultView,
    AuthTokenCreateResultView,
    AuthTokenStatusResultView,
    AuthUserForcePasswordResetResultView,
    AuthUserMutationResultView,
    AuthUserUnlockResultView,
    ChangelogRestoreResultView,
    CommandBatchResultView,
    CommandDispatchResultView,
    DataContractMutationResultView,
    DatasetConnectionMutationResultView,
    DatasetDatasetProjectAssignmentUpdateResultView,
    DatasetMutationResultView,
    DatasetProjectAssignmentUpdateResultView,
    DatasetQueryTemplateMutationResultView,
    EventRedriveResultView,
    ExtensionActionResultView,
    ExtensionArchiveInstallResultView,
    ExtensionBuildResultView,
    ExtensionDescriptorValidateResultView,
    ExtensionGeneratedJobResultView,
    ExtensionImportResultView,
    ExtensionInstallResultView,
    ExtensionInvocationResultView,
    ExtensionQueryResultView,
    ExtensionReleaseImportResultView,
    ExtensionRuntimeHealthResultView,
    ExtensionRuntimeTokenResultView,
    ExtensionScanResultView,
    ExtensionToggleResultView,
    ExtensionUninstallResultView,
    ExtensionValidateResultView,
    FieldMutationResultView,
    GuardrailAgentsResultView,
    GuardrailDeleteResultView,
    GuardrailMutationResultView,
    GuardrailRestoreResultView,
    HardDeleteResultView,
    JobDecisionOverrideResultView,
    JobDeleteResultView,
    JobLifecycleResultView,
    JobStepReplayExecutionResultView,
    LifecyclePlanView,
    ProjectConnectionDeleteResultView,
    ProjectCreateInput,
    ProjectDeleteResultView,
    ProjectMutationResultView,
    ProjectNotificationDispatchResultView,
    ProjectNotificationRoutesUpdateResultView,
    ProjectPolicyResultView,
    ProjectScheduledJobDeleteResultView,
    ProjectScheduledJobMutationResultView,
    ProjectScheduledJobRunResultView,
    ProjectScheduleTimezoneResultView,
    ProjectSettingsRestoreResultView,
    ProjectUpdateInput,
    ProjectWorkflowBindingsResultView,
    PublishedBlockMutationResultView,
    ReactionEventIngestResultView,
    RestoreResultView,
    SecurityCustomRuleResultView,
    SecurityPolicyUpdateResultView,
    SecurityProjectPolicyUpdateResultView,
    TrashResultView,
    WorkflowMutationResultView,
    WorkspaceDeleteExecuteResultView,
)
from core.application.management.project_grid_rows import (
    serialize_projects_grid_row as _serialize_projects_grid_row_impl,
)
from core.application.management.query_models import (
    AgentDiffView,
    AgentSkillPairListView,
    ApprovalGateView,
    ApprovalHistoryView,
    AuthAccountEnvelopeView,
    AuthAccountsListView,
    AuthMeView,
    AuthServiceAccountEnvelopeView,
    AuthServiceAccountsListView,
    AuthServiceAccountTokensListView,
    AuthTokensListView,
    AuthUsersListView,
    AwaitingApprovalJobView,
    CallableWorkflowListView,
    ChangelogDiffView,
    ChangelogEntryView,
    CoreSloMetricsView,
    DashboardSummaryView,
    DataContractRecordView,
    DatasetActionResponseView,
    DatasetConnectorsListView,
    DatasetDataConnectionsListView,
    DatasetProjectAssignmentCatalogView,
    DatasetProjectCatalogView,
    DatasetQueryTemplatesListView,
    DatasetRecordView,
    DatasetsListView,
    DomainEventDetailView,
    EventsListView,
    ExtensionArchiveLifecycleAvailabilityView,
    ExtensionDetailView,
    ExtensionHealthView,
    ExtensionListView,
    ExtensionMarketplaceListView,
    FieldRecordView,
    GuardrailDiffView,
    GuardrailHistoryEntryView,
    GuardrailRecordView,
    JobCausalChainView,
    JobCostBreakdownView,
    JobDecisionOverridesView,
    JobErrorsView,
    JobEventsView,
    JobIncidentDrilldownView,
    JobLogsView,
    JobMessageDetailView,
    JobMessagesView,
    JobReactionsView,
    JobReplayStepView,
    JobRuntimeJournalView,
    JobStepReplaysView,
    JobTraceView,
    McpConnectorDeleteResultView,
    McpConnectorHealthView,
    McpConnectorListView,
    McpConnectorView,
    ModelCatalogSettingsView,
    ModelProviderMutationResultView,
    ModelProvidersListView,
    ModelSelectionOptionsView,
    ProjectAdminDetailView,
    ProjectConnectionListView,
    ProjectConnectionView,
    ProjectCronPreviewView,
    ProjectNotificationDeliveriesView,
    ProjectNotificationOverviewView,
    ProjectNotificationRoutesView,
    ProjectPolicyView,
    ProjectProviderBindingsView,
    ProjectScheduledJobsView,
    ProjectSettingsHistoryDiffView,
    ProjectSettingsHistoryListView,
    ProjectWorkflowBindingsView,
    ProjectWorkflowPreviewView,
    ProvidersCatalogItemView,
    ProvidersCatalogView,
    ProviderSecretStatusView,
    PublishedBlockRecordView,
    RegistrySearchResultView,
    RegistryStatsView,
    RegistryValidationView,
    RetentionSettingsSaveView,
    RetentionSettingsView,
    SaveStatusView,
    SecurityEventsListView,
    SecurityFindingsListView,
    SecurityPolicyWithSourcesView,
    SecurityPostureView,
    SkillDiffView,
    ToolDiffView,
    TrashListView,
    UserEmailChangeRequestResultView,
    UserEmailChangeStatusView,
    UserLocaleResultView,
    UserProfileUpdateResultView,
    UserTimezoneResultView,
    WorkflowAdminDetailView,
    WorkflowAdminSummaryView,
    WorkflowCanvasGraphView,
    WorkflowConnectorCatalogView,
    WorkflowContractResultView,
    WorkflowDagView,
    WorkflowDiffResultView,
    WorkflowExportPolicyView,
    WorkflowExtensionFormSchemaView,
    WorkflowExtensionResourceQueryView,
    WorkflowHistoryEntryView,
    WorkflowImportApplyResultView,
    WorkflowImportDependencyOptionsView,
    WorkflowImportPreflightView,
    WorkflowIssueListView,
    WorkflowProviderTriggerDeleteResultView,
    WorkflowProviderTriggerView,
    WorkflowReactionsView,
    WorkflowStatsView,
    WorkflowSuggestionsResultView,
    WorkflowTriggerCatalogResponseView,
    WorkflowTriggerDryRunView,
    WorkflowTriggerEntrypointView,
    WorkflowTriggersView,
    WorkflowValidationResultView,
    WorkflowValidationSummaryView,
    WorkflowWebhookDeliveriesView,
    WorkflowWebhookTriggerDeleteResultView,
    WorkspaceDeletePlanView,
    WorkspaceProjectsView,
    WorkspaceRecordView,
    WorkspacesListView,
)
from core.application.management.scope import normalize_scope, scope_from_principal
from core.application.management.snapshot_serializers import (
    build_project_config_snapshot as _build_project_config_snapshot_impl,
)
from core.application.management.snapshot_serializers import (
    json_value_equal as _json_value_equal_impl,
)
from core.application.management.snapshot_serializers import (
    policy_snapshot_hash as _policy_snapshot_hash_impl,
)
from core.application.management.snapshot_serializers import (
    project_settings_diff as _project_settings_diff_impl,
)
from core.application.management.snapshot_serializers import (
    redact_snapshot_secrets as _redact_snapshot_secrets_impl,
)
from core.application.management.snapshot_serializers import (
    serialize_agent_message_row as _serialize_agent_message_row_impl,
)
from core.application.management.snapshot_serializers import (
    serialize_decision_override_row as _serialize_decision_override_row_impl,
)
from core.application.management.snapshot_serializers import (
    serialize_event_row as _serialize_event_row_impl,
)
from core.application.management.snapshot_serializers import (
    serialize_job_error_row as _serialize_job_error_row_impl,
)
from core.application.management.snapshot_serializers import (
    serialize_job_step_replay_row as _serialize_job_step_replay_row_impl,
)
from core.application.management.snapshot_serializers import (
    serialize_log_row as _serialize_log_row_impl,
)
from core.application.orchestration import interrupted_step_runtime as _interrupted_step_runtime
from core.application.orchestration import step_replay_runtime as _step_replay_runtime
from core.application.orchestration.agents.common import sanitize_external_input
from core.application.orchestration.graph_runtime import build_registry_graph
from core.application.reports import ui_page_registry as _ui_page_registry
from core.application.reports.display_text import build_display_summary
from core.application.reports.ui_projection import project_job_detail, user_role_from_auth
from core.application.settings import platform_contracts as _platform_contracts
from core.application.tooling.db.bootstrap_seed import (
    ensure_canonical_guardrails_seeded as _ensure_canonical_guardrails_seeded,
)
from core.application.workflows import domain_event_runtime as _domain_event_runtime
from core.application.workflows import reaction_runtime as _reaction_runtime
from core.application.workflows import (
    serve_workflow_helpers as _serve_workflow_helpers_runtime,
)
from core.application.workflows import workflow_override_runtime as _workflow_override_runtime
from core.application.workflows.reaction_scopes import (
    compose_scoped_reaction_key,
    normalize_reaction_scope,
    reaction_scope_matches_target,
    split_scoped_reaction_key,
)
from core.application.workflows.serve_workflow_helpers import (
    build_project_policy_from_form as _build_project_policy_from_form_impl,
)
from core.application.workflows.serve_workflow_helpers import (
    build_rerun_workflow_selection as _build_rerun_workflow_selection_impl,
)
from core.application.workflows.serve_workflow_helpers import (
    build_task_envelope_for_preview as _build_task_envelope_for_preview_impl,
)
from core.application.workflows.serve_workflow_helpers import (
    cron_human_summary as _cron_human_summary_impl,
)
from core.application.workflows.serve_workflow_helpers import (
    cron_next_run_at as _cron_next_run_at_impl,
)
from core.application.workflows.serve_workflow_helpers import (
    form_flag as _form_flag_impl,
)
from core.application.workflows.serve_workflow_helpers import (
    form_float as _form_float_impl,
)
from core.application.workflows.serve_workflow_helpers import (
    form_int as _form_int_impl,
)
from core.application.workflows.serve_workflow_helpers import (
    normalize_cron_expr as _normalize_cron_expr_impl,
)
from core.application.workflows.serve_workflow_helpers import (
    parse_agent_model_overrides as _parse_agent_model_overrides_impl,
)
from core.application.workflows.serve_workflow_helpers import (
    parse_cron_expression as _parse_cron_expression_impl,
)
from core.application.workflows.serve_workflow_helpers import (
    parse_project_policy_enum as _parse_project_policy_enum_impl,
)
from core.application.workflows.serve_workflow_helpers import (
    resolve_model_selection_context as _resolve_model_selection_context,
)
from core.infrastructure.config import CONFIG
from core.infrastructure.config.openapi_docs_config import (
    INTERNAL_DOCS_PATH,
    INTERNAL_OPENAPI_PATH,
    PUBLIC_DOCS_PATH,
    PUBLIC_OPENAPI_PATH,
    load_openapi_docs_config,
)
from core.infrastructure.config.openapi_docs_config import (
    docs_public_auth_paths as _docs_public_auth_paths,
)
from core.infrastructure.db import job_thread_store
from core.infrastructure.extension_runtime import (
    extension_runtime_helpers as _extension_runtime_helpers,
)
from core.infrastructure.observability import logger_runtime as _logger_runtime
from core.infrastructure.observability import runtime_logging as _runtime_logging
from core.infrastructure.observability.sentry import init_sentry
from core.infrastructure.runtime import app_lifecycle_runtime as _app_lifecycle_runtime
from core.infrastructure.runtime import graph_resume_runtime as _graph_resume_runtime
from core.infrastructure.runtime import process_runtime as _process_runtime
from core.infrastructure.runtime import readiness_runtime as _readiness_runtime
from core.infrastructure.runtime import serve_interrupt_helpers as _serve_interrupt_helpers
from core.infrastructure.runtime import (
    serve_request_runtime as _serve_request_runtime,
)
from core.infrastructure.runtime import (
    serve_service_wiring as _serve_service_wiring,
)
from core.infrastructure.runtime import state_reader_runtime as _state_reader_runtime
from core.infrastructure.runtime import watchdog_runtime as _watchdog_runtime
from core.infrastructure.runtime.app_factory import (
    build_runtime_container as _build_runtime_container,
)
from core.infrastructure.runtime.app_factory import (
    load_optional_private_modules as _load_optional_private_modules,
)
from core.infrastructure.runtime.control import scans as _control_scans
from core.infrastructure.runtime.control import scans_scheduled_jobs as _control_scheduled_jobs
from core.infrastructure.runtime.control.config import ControlConfig as _ControlConfig
from core.infrastructure.runtime.control.loops import (
    start_control_threads as _start_control_threads,
)
from core.infrastructure.runtime.control.loops import (
    stop_control_threads as _stop_control_threads,
)
from core.infrastructure.security.policy import SecurityPolicy


def _plan_project_workflow_preview_impl(
    envelope: dict[str, Any], state: dict[str, Any]
) -> dict[str, Any]:
    """The workflow routing would run for this task and its steps; nothing is planned."""
    from dataclasses import asdict

    from core.application.orchestration.supervisor import OrchestratorSupervisor
    from core.application.registry.registry_manager import RegistryManager

    supervisor = OrchestratorSupervisor(RegistryManager.instance())
    routing = supervisor.resolve_workflow_routing(envelope, state=state)
    steps = [
        {
            "step_id": str(getattr(step, "step_id", "") or ""),
            "step_kind": str(getattr(step, "step_kind", "") or ""),
            "display_name": str(getattr(step, "display_name", "") or ""),
            "agent": str(getattr(step, "agent", "") or ""),
            "skill": str(getattr(step, "skill", "") or ""),
            "group_id": getattr(step, "group_id", None),
        }
        for step in getattr(routing.workflow, "steps", None) or []
    ]
    return {
        "workflow_id": routing.workflow_id,
        "classification": asdict(routing.classification),
        "routing": {
            "reason": routing.routing_reason,
            "signals": routing.routing_signals,
            "reentry_context": routing.reentry_context,
        },
        "steps": steps,
    }


def _state_based_approval_payload_impl(values: dict[str, Any]) -> dict[str, Any] | None:
    from core.application.orchestration.approval_payload import state_based_approval_payload

    return state_based_approval_payload(values)


def _default_workspace_id_impl() -> str:
    from core.application.auth.config import DEFAULT_WORKSPACE_ID

    return DEFAULT_WORKSPACE_ID


def _build_workflow_availability_service_impl(*, get_logger):
    from core.application.workflows.workflow_availability_service import (
        WorkflowAvailabilityService,
    )

    return WorkflowAvailabilityService(get_logger=get_logger)


def _write_project_profile_impl(project_row: dict[str, Any], *, logger) -> None:
    from core.application.management.project_profile_kb import write_project_profile_to_kb
    from core.application.memory.project_context import sync_project_context

    return write_project_profile_to_kb(
        project_row,
        logger=logger,
        write_project_profile=sync_project_context,
    )


def _queue_delivery_summary_impl(bl) -> dict[str, Any]:
    from core.application.jobs.queue_delivery_queries import (
        build_queue_delivery_summary,
        load_queue_delivery_readiness_counts,
    )

    return build_queue_delivery_summary(load_queue_delivery_readiness_counts(bl))


__all__ = [name for name in globals() if not (name.startswith("__") and name.endswith("__"))]
