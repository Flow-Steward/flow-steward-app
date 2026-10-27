# ruff: noqa: RUF022
from __future__ import annotations

import copy
import json
import logging
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, date, datetime
from typing import Any

from core.application.auth.principal import coerce_optional_project_id, coerce_optional_workspace_id
from core.application.jobs.failure_summary import build_job_failure_summary
from core.application.jobs.queue_delivery_queries import (
    build_queue_delivery_summary,
    build_queue_delivery_view,
    load_queue_delivery_counts,
)
from core.application.jobs.queue_delivery_runtime import queue_dead_letter_queue_name
from core.application.management.scope import ScopeContext, normalize_scope, scope_audit_fields
from core.application.observability_vocabulary import (
    event_status_meta,
    event_type_meta,
    humanize_observability_token,
)
from core.application.workflows.reaction_scopes import serialize_scoped_reaction_rows
from core.application.workflows.workflow_reaction_sources import (
    build_workflow_reaction_trigger_sources,
)
from core.infrastructure.extension_runtime.extension_invocation_audit import (
    log_host_extension_invocation,
)

from .errors import (
    ServiceConflictError,
    ServiceError,
    ServiceForbiddenError,
    ServiceNotFoundError,
    ServiceUnavailableError,
    ServiceValidationError,
)
from .guards import (
    is_default_workflow_catalog_account,
    require_account_access,
    require_event_access,
    require_project_access,
    require_workflow_access,
)
from .query_models import (
    ApprovalGateView,
    ApprovalHistoryView,
    AttentionGroupsView,
    AttentionJobView,
    AwaitingApprovalJobView,
    DomainEventDetailView,
    DomainEventDispatchAttemptView,
    DomainEventDispatchView,
    DomainEventSummaryView,
    EventsListView,
    ExtensionHealthView,
    ExtensionPageContextView,
    ExtensionPageProjectView,
    ExtensionsListView,
    JobCausalChainView,
    JobCostBreakdownView,
    JobCostSummaryView,
    JobDecisionOverridesView,
    JobDefinitionView,
    JobErrorsView,
    JobEventsView,
    JobIncidentDrilldownView,
    JobLogsPageView,
    JobLogsView,
    JobMessageDetailView,
    JobMessagesView,
    JobReactionsView,
    JobReplayStepView,
    JobRuntimeJournalView,
    JobsAttentionGroupsView,
    JobsListView,
    JobStatusView,
    JobStepReplaysView,
    JobTraceView,
    ProjectDetailPageView,
    ProjectEditPageView,
    ProjectGridRowView,
    ProjectJobRowView,
    ProjectJobsListView,
    ProjectNotificationDeliveriesView,
    ProjectNotificationOverviewView,
    ProjectNotificationRoutesView,
    ProjectPolicyView,
    ProjectQueueLeaseView,
    ProjectQueuePendingJobView,
    ProjectQueueView,
    ProjectReadyStateView,
    ProjectRuntimeConfigView,
    ProjectScheduledJobsView,
    ProjectSettingsHistoryDiffView,
    ProjectSettingsHistoryListView,
    ProjectSettingsHistoryPageView,
    ProjectsListView,
    ProjectWorkflowBindingsView,
    ProjectWorkflowPreviewView,
    QueueHealthView,
    ToolDiffView,
    ToolHistoryEntriesView,
    ToolRecordView,
    ToolsHealthView,
    ToolsListView,
    ToolValidationView,
    WorkflowReactionRuleView,
    WorkflowReactionsView,
)

logger = logging.getLogger(__name__)

_TERMINAL_JOB_STATUSES = frozenset(
    {
        "cancelled",
        "completed",
        "failed",
        "max_iterations",
        "triage_needed",
    }
)
DATABASE_NOT_CONFIGURED_MESSAGE = "Database not configured"
PROJECT_NOT_FOUND_MESSAGE = "Project not found"
EXTENSION_NOT_FOUND_MESSAGE = "Extension not found"


def _service_scope(
    scope: ScopeContext | None,
    *,
    project_id: Any = None,
    workspace_id: Any = None,
    workflow_id: Any = None,
) -> ScopeContext:
    return normalize_scope(
        scope,
        project_id=project_id,
        workspace_id=workspace_id,
        workflow_id=workflow_id,
    )


def _scope_audit_fields(scope: ScopeContext | None) -> dict[str, Any]:
    return scope_audit_fields(scope)


def _enrich_event_command_row(command: dict[str, Any]) -> dict[str, Any]:
    payload = dict(command)
    command_type = str(payload.get("command_type") or payload.get("type") or "").strip()
    if command_type:
        payload.setdefault("command_type", command_type)
        payload.setdefault(
            "command_type_label",
            humanize_observability_token(command_type) or command_type,
        )

    status_value = str(payload.get("status") or payload.get("state") or "").strip().lower()
    if status_value:
        status_meta = event_status_meta(status_value)
        payload.setdefault("status", status_meta["code"])
        payload.setdefault("status_label", str(status_meta.get("label") or ""))
        payload.setdefault("status_tone", str(status_meta.get("tone") or "gray"))
    else:
        raw_state = str(payload.get("state") or "").strip()
        if raw_state:
            payload.setdefault("status_label", humanize_observability_token(raw_state) or raw_state)

    return payload


__all__ = [
    "Any",
    "ApprovalGateView",
    "ApprovalHistoryView",
    "AttentionGroupsView",
    "AttentionJobView",
    "Awaitable",
    "AwaitingApprovalJobView",
    "Callable",
    "DATABASE_NOT_CONFIGURED_MESSAGE",
    "DomainEventDetailView",
    "DomainEventDispatchAttemptView",
    "DomainEventDispatchView",
    "DomainEventSummaryView",
    "EXTENSION_NOT_FOUND_MESSAGE",
    "EventsListView",
    "ExtensionHealthView",
    "ExtensionPageContextView",
    "ExtensionPageProjectView",
    "ExtensionsListView",
    "JobCausalChainView",
    "JobCostBreakdownView",
    "JobCostSummaryView",
    "JobDecisionOverridesView",
    "JobDefinitionView",
    "JobErrorsView",
    "JobEventsView",
    "JobIncidentDrilldownView",
    "JobLogsPageView",
    "JobLogsView",
    "JobMessageDetailView",
    "JobMessagesView",
    "JobReactionsView",
    "JobReplayStepView",
    "JobRuntimeJournalView",
    "JobStatusView",
    "JobStepReplaysView",
    "JobTraceView",
    "JobsAttentionGroupsView",
    "JobsListView",
    "PROJECT_NOT_FOUND_MESSAGE",
    "ProjectDetailPageView",
    "ProjectEditPageView",
    "ProjectGridRowView",
    "ProjectJobRowView",
    "ProjectJobsListView",
    "ProjectNotificationDeliveriesView",
    "ProjectNotificationOverviewView",
    "ProjectNotificationRoutesView",
    "ProjectPolicyView",
    "ProjectQueueLeaseView",
    "ProjectQueuePendingJobView",
    "ProjectQueueView",
    "ProjectReadyStateView",
    "ProjectRuntimeConfigView",
    "ProjectScheduledJobsView",
    "ProjectSettingsHistoryDiffView",
    "ProjectSettingsHistoryListView",
    "ProjectSettingsHistoryPageView",
    "ProjectWorkflowBindingsView",
    "ProjectWorkflowPreviewView",
    "ProjectsListView",
    "QueueHealthView",
    "ScopeContext",
    "ServiceConflictError",
    "ServiceError",
    "ServiceForbiddenError",
    "ServiceNotFoundError",
    "ServiceUnavailableError",
    "ServiceValidationError",
    "ToolDiffView",
    "ToolHistoryEntriesView",
    "ToolRecordView",
    "ToolValidationView",
    "ToolsHealthView",
    "ToolsListView",
    "UTC",
    "WorkflowReactionRuleView",
    "WorkflowReactionsView",
    "_TERMINAL_JOB_STATUSES",
    "_enrich_event_command_row",
    "_scope_audit_fields",
    "_service_scope",
    "annotations",
    "build_job_failure_summary",
    "build_queue_delivery_summary",
    "build_queue_delivery_view",
    "build_workflow_reaction_trigger_sources",
    "coerce_optional_project_id",
    "coerce_optional_workspace_id",
    "copy",
    "date",
    "datetime",
    "event_status_meta",
    "event_type_meta",
    "humanize_observability_token",
    "is_default_workflow_catalog_account",
    "json",
    "load_queue_delivery_counts",
    "log_host_extension_invocation",
    "logger",
    "logging",
    "normalize_scope",
    "queue_dead_letter_queue_name",
    "require_event_access",
    "require_project_access",
    "require_account_access",
    "require_workflow_access",
    "scope_audit_fields",
    "serialize_scoped_reaction_rows",
    "uuid",
]
