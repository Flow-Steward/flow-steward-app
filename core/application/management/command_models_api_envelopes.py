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

GENERATE_A_RELEASE_READINESS_REPORT_FOR_VERSION_2_MESSAGE = (
    "Generate a release-readiness report for version 2.4."
)


class JobLifecycleResultView(ViewModel):
    job_id: str
    status: str
    project_id: str | None = None
    thread_id: str | None = None
    new_job_id: str | None = None
    queue_name: str | None = None
    cancelled: bool | None = None
    enqueued: bool | None = None


class JobDeleteResultView(ViewModel):
    status: str = "deleted"
    job_id: str


class ProjectJobsBulkDeleteResultView(ViewModel):
    status: str = "ok"
    requested: list[str] = Field(default_factory=list)
    deleted: list[str] = Field(default_factory=list)
    deleted_count: int = 0
    skipped: list[dict[str, Any]] = Field(default_factory=list)


class ApiEnvelopeMetaView(ViewModel):
    resource: str = "resource"
    pagination: dict[str, int] | None = None


class ApiV2JobsEnvelopeView(ViewModel):
    model_config = ConfigDict(
        title="API v2 Job List Envelope",
        json_schema_extra={
            "description": "Versioned jobs list envelope returned to internal React/API v2 consumers.",
            "example": {
                "ok": True,
                "kind": "list",
                "data": {
                    "jobs": [
                        {
                            "job_id": "job-987",
                            "project_id": "proj_42",
                            "project_name": "Payments API",
                            "status": "in_progress",
                            "task_summary": GENERATE_A_RELEASE_READINESS_REPORT_FOR_VERSION_2_MESSAGE,
                        }
                    ],
                    "total": 1,
                    "page": 1,
                    "per_page": 25,
                    "total_pages": 1,
                    "project_id": "proj_42",
                    "workspace_id": None,
                    "status": "in_progress",
                },
                "error": None,
                "meta": {
                    "resource": "jobs",
                    "pagination": {
                        "page": 1,
                        "per_page": 25,
                        "total": 1,
                        "total_pages": 1,
                    },
                },
            },
        },
    )

    ok: bool = True
    kind: str = "list"
    data: JobsListView | JobsAttentionGroupsView = Field(default_factory=JobsListView)
    error: dict[str, Any] | None = None
    meta: ApiEnvelopeMetaView = Field(default_factory=lambda: ApiEnvelopeMetaView(resource="jobs"))


class ApiV2JobDetailEnvelopeView(ViewModel):
    model_config = ConfigDict(
        title="API v2 Job Detail Envelope",
        json_schema_extra={
            "description": "Versioned job-detail envelope returned to internal React/API v2 consumers.",
            "example": {
                "ok": True,
                "kind": "detail",
                "data": {
                    "id": "job-987",
                    "project_id": "proj_42",
                    "thread_id": "thread-7",
                    "task_summary": GENERATE_A_RELEASE_READINESS_REPORT_FOR_VERSION_2_MESSAGE,
                    "status": "awaiting_approval",
                    "current_step_id": "review_report",
                    "current_group_id": None,
                    "current_agent": "reviewer",
                },
                "error": None,
                "meta": {"resource": "jobs"},
            },
        },
    )

    ok: bool = True
    kind: str = "detail"
    data: JobDetailContractView = Field(default_factory=JobDetailContractView)
    error: dict[str, Any] | None = None
    meta: ApiEnvelopeMetaView = Field(default_factory=lambda: ApiEnvelopeMetaView(resource="jobs"))


class ApiV2ProjectsEnvelopeView(ViewModel):
    model_config = ConfigDict(
        title="API v2 Project List Envelope",
        json_schema_extra={
            "description": "Versioned projects list envelope returned to internal React/API v2 consumers.",
            "example": {
                "ok": True,
                "kind": "list",
                "data": {
                    "projects": [
                        {
                            "id": "proj_42",
                            "name": "Payments API",
                            "job_count": 12,
                            "operator_default_workflow_id": "release-report",
                        }
                    ],
                    "total": 1,
                    "page": 1,
                    "per_page": 25,
                    "total_pages": 1,
                },
                "error": None,
                "meta": {
                    "resource": "projects",
                    "pagination": {
                        "page": 1,
                        "per_page": 25,
                        "total": 1,
                        "total_pages": 1,
                    },
                },
            },
        },
    )

    ok: bool = True
    kind: str = "list"
    data: ProjectsListView = Field(default_factory=ProjectsListView)
    error: dict[str, Any] | None = None
    meta: ApiEnvelopeMetaView = Field(
        default_factory=lambda: ApiEnvelopeMetaView(resource="projects")
    )


class ApiV2ProjectJobsEnvelopeView(ViewModel):
    model_config = ConfigDict(
        title="API v2 Project Job List Envelope",
        json_schema_extra={
            "description": "Versioned project-jobs envelope returned to internal React/API v2 consumers.",
            "example": {
                "ok": True,
                "kind": "list",
                "data": {
                    "jobs": [
                        {
                            "id": "job-987",
                            "project_id": "proj_42",
                            "task_summary": GENERATE_A_RELEASE_READINESS_REPORT_FOR_VERSION_2_MESSAGE,
                            "status": "queued",
                        }
                    ],
                    "total": 1,
                    "page": 1,
                    "per_page": 25,
                    "total_pages": 1,
                },
                "error": None,
                "meta": {
                    "resource": "project-jobs",
                    "pagination": {
                        "page": 1,
                        "per_page": 25,
                        "total": 1,
                        "total_pages": 1,
                    },
                },
            },
        },
    )

    ok: bool = True
    kind: str = "list"
    data: ProjectJobsListView = Field(default_factory=ProjectJobsListView)
    error: dict[str, Any] | None = None
    meta: ApiEnvelopeMetaView = Field(
        default_factory=lambda: ApiEnvelopeMetaView(resource="project-jobs")
    )


class ApiV2ProjectJobsBulkDeleteEnvelopeView(ViewModel):
    model_config = ConfigDict(
        title="API v2 Project Job Bulk Delete Envelope",
        json_schema_extra={
            "description": "Versioned bulk-delete result envelope returned to internal React/API v2 consumers.",
            "example": {
                "ok": True,
                "kind": "mutation",
                "data": {
                    "status": "ok",
                    "requested": ["job-1", "job-2"],
                    "deleted": ["job-1"],
                    "deleted_count": 1,
                    "skipped": [{"job_id": "job-2", "reason": "not_found"}],
                },
                "error": None,
                "meta": {"resource": "project-jobs"},
            },
        },
    )

    ok: bool = True
    kind: str = "mutation"
    data: ProjectJobsBulkDeleteResultView = Field(default_factory=ProjectJobsBulkDeleteResultView)
    error: dict[str, Any] | None = None
    meta: ApiEnvelopeMetaView = Field(
        default_factory=lambda: ApiEnvelopeMetaView(resource="project-jobs")
    )


class ApiV2DashboardSummaryDataView(ViewModel):
    range: str = "7d"
    range_hours: int = 168
    metrics_truncated: bool = False
    historical_metrics_source: str = "live"
    metrics_cap: int | None = None


class ApiV2DashboardSummaryEnvelopeView(ViewModel):
    model_config = ConfigDict(
        title="API v2 Dashboard Summary Envelope",
        json_schema_extra={
            "description": "Versioned dashboard summary envelope returned to internal React/API v2 consumers.",
            "example": {
                "ok": True,
                "kind": "detail",
                "data": {
                    "range": "24h",
                    "range_hours": 24,
                    "metrics_truncated": False,
                    "historical_metrics_source": "live",
                    "metrics_cap": 500,
                    "jobs_running": 2,
                },
                "error": None,
                "meta": {"resource": "dashboard-summary"},
            },
        },
    )

    ok: bool = True
    kind: str = "detail"
    data: ApiV2DashboardSummaryDataView = Field(default_factory=ApiV2DashboardSummaryDataView)
    error: dict[str, Any] | None = None
    meta: ApiEnvelopeMetaView = Field(
        default_factory=lambda: ApiEnvelopeMetaView(resource="dashboard-summary")
    )


class JobDecisionOverrideResultView(ViewModel):
    job_id: str
    override: dict[str, Any] = Field(default_factory=dict)


class JobStepReplayExecutionResultView(ViewModel):
    job_id: str
    message_id: str
    replay_id: str | None = None
    original_outcome: dict[str, Any] = Field(default_factory=dict)
    replay_outcome: dict[str, Any] = Field(default_factory=dict)
    diff_excerpt: str | None = None
    replay: dict[str, Any] | None = None


class ProjectWorkflowBindingsResultView(ViewModel):
    project_id: str
    workflow_ids: list[str]
    operator_default_workflow_id: str = ""
    project_workflows: list[dict[str, Any]] = Field(default_factory=list)


class ProjectPolicyResultView(ViewModel):
    project_id: str
    policy: dict[str, Any] = Field(default_factory=dict)


class ProjectSettingsRestoreResultView(ViewModel):
    status: str = ""
    project_id: str = ""
    revision_id: str | None = None
    new_revision_id: str | None = None


class ProjectDeleteResultView(ViewModel):
    status: str = "deleted"
    project_id: str


class ProjectMutationResultView(ViewModel):
    status: str
    project_id: str
    name: str = ""


class WorkflowRunAcceptedView(ViewModel):
    model_config = ConfigDict(
        extra="ignore",
        title="Workflow Run Result",
        json_schema_extra={
            "description": "Accepted asynchronous workflow launch with its durable job identity."
        },
    )

    project_id: str
    job_id: str
    status: str = "pending"
    queue: str = ""
    deduplicated: bool = False


class ProjectTaskEnqueueResultView(ViewModel):
    model_config = ConfigDict(
        title="Task Submission Result",
        json_schema_extra={
            "description": (
                "Accepted task submission with the created job identifier and queue details."
            ),
            "example": {
                "project_id": "proj_42",
                "task_id": "task-123",
                "job_id": "job-987",
                "status": "pending",
                "queue": "project.proj_42.manual",
                "file_count": 0,
                "project_created": False,
                "deduplicated": False,
            },
        },
    )

    project_id: str = Field(description="Project that accepted the task for execution.")
    task_id: str = Field(description="Caller-supplied task identifier echoed back for correlation.")
    job_id: str = Field(description="Platform job identifier used to monitor execution progress.")
    status: str = Field(
        default="pending",
        description="Initial job lifecycle state after the task was enqueued.",
    )
    queue: str = Field(description="Queue name that accepted the task.")
    file_count: int = Field(default=0, description="Number of uploaded files attached to the task.")
    project_created: bool = Field(
        default=False,
        description="Whether enqueueing implicitly provisioned a project before creating the job.",
    )
    deduplicated: bool = Field(
        default=False,
        description="True when an idempotent replay returns the previously accepted job.",
    )


class ExtensionProjectCreateResultView(ViewModel):
    status: str = "created"
    project_id: str
    name: str = ""
    parent_project_id: str
    managed_artifact: dict[str, Any] = Field(default_factory=dict)


class ProjectScheduledJobMutationResultView(ViewModel):
    project_id: str
    scheduled_job_id: str
    status: str
    enabled: bool | None = None
    next_run_at: Any = None
    job: dict[str, Any] | None = None


class ProjectScheduledJobDeleteResultView(ViewModel):
    project_id: str
    scheduled_job_id: str
    status: str = "deleted"


class ProjectScheduledJobRunResultView(ViewModel):
    project_id: str
    scheduled_job_id: str
    status: str
    job_id: str | None = None
    reason: str | None = None
    next_run_at: Any = None


class WorkflowMutationResultView(ViewModel):
    status: str = ""
    workflow_id: str = ""
    enabled: bool | None = None
    source_workflow_id: str | None = None
    restored_from_entry_id: str | None = None
    validation: dict[str, Any] | None = None
    snapshot: dict[str, Any] | None = None


class ProjectScheduleTimezoneResultView(ViewModel):
    status: str = ""
    schedule_timezone: str = ""
    recalculated_jobs: int = 0
