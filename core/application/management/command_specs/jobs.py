"""Job-domain public command specifications."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from core.application.management.command_models import (
    ApprovalResumeAcceptedView,
    JobLifecycleResultView,
)
from core.application.management.command_specs.contracts import (
    CommandSpec,
    FieldSpec,
    FieldType,
    InteractivePolicy,
    OperationType,
)
from core.application.management.query_models import (
    JobDetailContractView,
    JobErrorsView,
    JobLogsView,
    JobMessagesView,
    JobsListView,
    JobStatusView,
    ViewModel,
)

_TOKEN_JOBS_LIST = "jobs.list"
_TOKEN_JOBS_SHOW = "jobs.show"
_TOKEN_JOBS_STATUS = "jobs.status"
_TOKEN_JOBS_LOGS = "jobs.logs"
_TOKEN_JOBS_MESSAGES = "jobs.messages"
_TOKEN_JOBS_ERRORS = "jobs.errors"
_TOKEN_JOBS_APPROVE = "jobs.approve"
_TOKEN_JOBS_REJECT = "jobs.reject"
_TOKEN_JOBS_ACK = "jobs.ack"
_TOKEN_JOBS_UNACK = "jobs.unack"
_TOKEN_JOBS_CANCEL = "jobs.cancel"
_TOKEN_JOBS_RESTART = "jobs.restart"
_TOKEN_JOBS_HEALTH_SNAPSHOT = "jobs.health-snapshot"


class JobsListCommandInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_id: str = ""
    status: str = ""
    limit: int = 25
    offset: int = 0

    @field_validator("limit")
    @classmethod
    def normalize_limit(cls, value: int) -> int:
        return max(1, min(int(value or 25), 500))

    @field_validator("offset")
    @classmethod
    def normalize_offset(cls, value: int) -> int:
        return max(0, int(value or 0))


class JobIdCommandInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    job_id: str

    @field_validator("job_id")
    @classmethod
    def require_job_id(cls, value: str) -> str:
        normalized = str(value or "").strip()
        if not normalized:
            raise ValueError("job_id is required")
        return normalized


class JobShowCommandInput(JobIdCommandInput):
    include_details: bool = False


class JobLogsCommandInput(JobIdCommandInput):
    since_id: str = ""
    before_id: str = ""
    limit: int = 25
    newest: bool = False

    @field_validator("limit")
    @classmethod
    def normalize_limit(cls, value: int) -> int:
        return max(1, min(int(value or 25), 2000))


class JobMessagesCommandInput(JobLogsCommandInput):
    step_id: str = ""
    producer: str = ""
    skill: str = ""
    status: str = ""


class JobErrorsCommandInput(JobIdCommandInput):
    limit: int = 25

    @field_validator("limit")
    @classmethod
    def normalize_limit(cls, value: int) -> int:
        return max(1, min(int(value or 25), 1000))


class JobApprovalCommandInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    thread_id: str
    approval_id: str
    comment: str = ""
    action: str = ""

    @field_validator("thread_id", "approval_id")
    @classmethod
    def require_thread_id(cls, value: str) -> str:
        normalized = str(value or "").strip()
        if not normalized:
            raise ValueError("value is required")
        return normalized


class JobAcknowledgeCommandInput(JobIdCommandInput):
    actor: str = "cli"


class JobCancelCommandInput(JobIdCommandInput):
    actor: str = "operator"
    comment: str = "Cancelled from CLI"


class JobHealthSnapshotCommandInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    base_url: str = "https://localhost:8099"
    verify_ssl: bool = False
    timeout_seconds: int = 10
    dsn: str = ""
    stale_heartbeat_seconds: int = 120

    @field_validator("timeout_seconds", "stale_heartbeat_seconds")
    @classmethod
    def normalize_positive(cls, value: int) -> int:
        return max(1, int(value or 1))


class JobHealthSnapshotView(ViewModel):
    ok: bool = False
    captured_at: str = ""
    service: dict[str, Any] = Field(default_factory=dict)
    database: dict[str, Any] = Field(default_factory=dict)


_PROJECT_ID_FIELD = FieldSpec(
    key="project_id",
    label="Project",
    type=FieldType.SINGLE_SELECT,
    choice_provider="projects.available",
    option_name="--project-id",
)
_JOB_ID_FIELD = FieldSpec(
    key="job_id",
    label="Job id",
    type=FieldType.RUNTIME_IDENTIFIER,
    required=True,
    positional=True,
)
_THREAD_ID_FIELD = FieldSpec(
    key="thread_id",
    label="Approval thread id",
    type=FieldType.RUNTIME_IDENTIFIER,
    required=True,
    positional=True,
)


def _pagination_fields(default_limit: int = 25) -> tuple[FieldSpec, ...]:
    return (
        FieldSpec(
            key="since_id",
            label="Since id",
            type=FieldType.RUNTIME_IDENTIFIER,
            option_name="--since-id",
        ),
        FieldSpec(
            key="before_id",
            label="Before id",
            type=FieldType.RUNTIME_IDENTIFIER,
            option_name="--before-id",
        ),
        FieldSpec(
            key="limit",
            label="Limit",
            type=FieldType.INTEGER,
            default=default_limit,
            option_name="--limit",
        ),
        FieldSpec(
            key="newest",
            label="Newest first",
            type=FieldType.BOOLEAN,
            default=False,
            option_name="--newest",
        ),
    )


JOB_COMMAND_SPECS: tuple[CommandSpec, ...] = (
    CommandSpec(
        command_id=_TOKEN_JOBS_LIST,
        path=("jobs", "list"),
        summary="List jobs.",
        description="List recent jobs through the shared job query service.",
        examples=(
            "flow-steward jobs list --project-id proj_123 --status in_progress",
            "flow-steward jobs list --status failed,stuck",
        ),
        operation_type=OperationType.READ,
        interactive_policy=InteractivePolicy.IMMEDIATE,
        fields=(
            _PROJECT_ID_FIELD,
            FieldSpec(
                key="status",
                label="Status",
                type=FieldType.TEXT,
                option_name="--status",
                description="One status or a comma list (failed,stuck); any of them matches.",
            ),
            FieldSpec(
                key="limit",
                label="Limit",
                type=FieldType.INTEGER,
                default=25,
                option_name="--limit",
            ),
            FieldSpec(
                key="offset",
                label="Offset",
                type=FieldType.INTEGER,
                default=0,
                option_name="--offset",
            ),
        ),
        input_model=JobsListCommandInput,
        result_model=JobsListView,
        plan_builder_ref=None,
        confirmation_policy_ref=None,
        service_ref=_TOKEN_JOBS_LIST,
        result_renderer_ref=_TOKEN_JOBS_LIST,
    ),
    CommandSpec(
        command_id=_TOKEN_JOBS_SHOW,
        path=("jobs", "show"),
        summary="Show job details.",
        description=(
            "Show a bounded job summary. Use --include-details for the larger UI detail contract; "
            "logs, messages and errors remain separate paginated commands."
        ),
        examples=(
            "flow-steward jobs show job_123",
            "flow-steward jobs show job_123 --include-details",
        ),
        operation_type=OperationType.READ,
        interactive_policy=InteractivePolicy.IMMEDIATE,
        fields=(
            _JOB_ID_FIELD,
            FieldSpec(
                key="include_details",
                label="Include details",
                type=FieldType.BOOLEAN,
                description=(
                    "Return the larger UI-oriented detail contract instead of the bounded summary."
                ),
                examples=("true", "false"),
                default=False,
                option_name="--include-details",
            ),
        ),
        input_model=JobShowCommandInput,
        result_model=JobDetailContractView,
        plan_builder_ref=None,
        confirmation_policy_ref=None,
        service_ref=_TOKEN_JOBS_SHOW,
        result_renderer_ref=_TOKEN_JOBS_SHOW,
    ),
    CommandSpec(
        command_id=_TOKEN_JOBS_STATUS,
        path=("jobs", "status"),
        summary="Show job status.",
        description="Show compact job status through the shared job query service.",
        examples=("flow-steward jobs status job_123",),
        operation_type=OperationType.READ,
        interactive_policy=InteractivePolicy.IMMEDIATE,
        fields=(_JOB_ID_FIELD,),
        input_model=JobIdCommandInput,
        result_model=JobStatusView,
        plan_builder_ref=None,
        confirmation_policy_ref=None,
        service_ref=_TOKEN_JOBS_STATUS,
        result_renderer_ref=_TOKEN_JOBS_STATUS,
    ),
    CommandSpec(
        command_id=_TOKEN_JOBS_LOGS,
        path=("jobs", "logs"),
        summary="Show job logs.",
        description="List job logs through the shared job query service.",
        examples=("flow-steward jobs logs job_123 --limit 50",),
        operation_type=OperationType.READ,
        interactive_policy=InteractivePolicy.IMMEDIATE,
        fields=(_JOB_ID_FIELD, *_pagination_fields(default_limit=25)),
        input_model=JobLogsCommandInput,
        result_model=JobLogsView,
        plan_builder_ref=None,
        confirmation_policy_ref=None,
        service_ref=_TOKEN_JOBS_LOGS,
        result_renderer_ref=_TOKEN_JOBS_LOGS,
    ),
    CommandSpec(
        command_id=_TOKEN_JOBS_MESSAGES,
        path=("jobs", "messages"),
        summary="Show job messages.",
        description="List agent messages for a job through the shared job query service.",
        examples=("flow-steward jobs messages job_123 --producer planner",),
        operation_type=OperationType.READ,
        interactive_policy=InteractivePolicy.IMMEDIATE,
        fields=(
            _JOB_ID_FIELD,
            *_pagination_fields(default_limit=25),
            FieldSpec(
                key="step_id",
                label="Step id",
                type=FieldType.RUNTIME_IDENTIFIER,
                option_name="--step-id",
            ),
            FieldSpec(
                key="producer", label="Producer", type=FieldType.TEXT, option_name="--producer"
            ),
            FieldSpec(key="skill", label="Skill", type=FieldType.TEXT, option_name="--skill"),
            FieldSpec(key="status", label="Status", type=FieldType.TEXT, option_name="--status"),
        ),
        input_model=JobMessagesCommandInput,
        result_model=JobMessagesView,
        plan_builder_ref=None,
        confirmation_policy_ref=None,
        service_ref=_TOKEN_JOBS_MESSAGES,
        result_renderer_ref=_TOKEN_JOBS_MESSAGES,
    ),
    CommandSpec(
        command_id=_TOKEN_JOBS_ERRORS,
        path=("jobs", "errors"),
        summary="Show job errors.",
        description="List structured job errors through the shared job query service.",
        examples=("flow-steward jobs errors job_123 --limit 50",),
        operation_type=OperationType.READ,
        interactive_policy=InteractivePolicy.IMMEDIATE,
        fields=(
            _JOB_ID_FIELD,
            FieldSpec(
                key="limit",
                label="Limit",
                type=FieldType.INTEGER,
                default=25,
                option_name="--limit",
            ),
        ),
        input_model=JobErrorsCommandInput,
        result_model=JobErrorsView,
        plan_builder_ref=None,
        confirmation_policy_ref=None,
        service_ref=_TOKEN_JOBS_ERRORS,
        result_renderer_ref=_TOKEN_JOBS_ERRORS,
    ),
    CommandSpec(
        command_id=_TOKEN_JOBS_APPROVE,
        path=("jobs", "approve"),
        summary="Approve an approval gate.",
        description="Resume an approval thread using its explicit approval ID.",
        examples=(
            "flow-steward jobs approve thread_123 --approval-id approval_123 --comment 'Approved'",
        ),
        operation_type=OperationType.CHANGE,
        interactive_policy=InteractivePolicy.ALWAYS_REVIEW,
        fields=(
            _THREAD_ID_FIELD,
            FieldSpec(
                key="approval_id",
                label="Approval ID",
                type=FieldType.RUNTIME_IDENTIFIER,
                required=True,
                description="Current approval gate identifier returned by jobs show or the API.",
                examples=("approval_123",),
                option_name="--approval-id",
            ),
            FieldSpec(key="comment", label="Comment", type=FieldType.TEXT, option_name="--comment"),
            FieldSpec(
                key="action",
                label="Action",
                type=FieldType.RUNTIME_IDENTIFIER,
                default="approve",
                option_name="--action",
            ),
        ),
        input_model=JobApprovalCommandInput,
        result_model=ApprovalResumeAcceptedView,
        plan_builder_ref=None,
        confirmation_policy_ref=None,
        service_ref=_TOKEN_JOBS_APPROVE,
        result_renderer_ref=_TOKEN_JOBS_APPROVE,
    ),
    CommandSpec(
        command_id=_TOKEN_JOBS_REJECT,
        path=("jobs", "reject"),
        summary="Reject an approval gate.",
        description="Reject an approval thread using its explicit approval ID.",
        examples=(
            "flow-steward jobs reject thread_123 --approval-id approval_123 --comment 'Needs changes'",
        ),
        operation_type=OperationType.CHANGE,
        interactive_policy=InteractivePolicy.ALWAYS_REVIEW,
        fields=(
            _THREAD_ID_FIELD,
            FieldSpec(
                key="approval_id",
                label="Approval ID",
                type=FieldType.RUNTIME_IDENTIFIER,
                required=True,
                description="Current approval gate identifier returned by jobs show or the API.",
                examples=("approval_123",),
                option_name="--approval-id",
            ),
            FieldSpec(key="comment", label="Comment", type=FieldType.TEXT, option_name="--comment"),
            FieldSpec(
                key="action",
                label="Action",
                type=FieldType.RUNTIME_IDENTIFIER,
                default="reject",
                option_name="--action",
            ),
        ),
        input_model=JobApprovalCommandInput,
        result_model=ApprovalResumeAcceptedView,
        plan_builder_ref=None,
        confirmation_policy_ref=None,
        service_ref=_TOKEN_JOBS_REJECT,
        result_renderer_ref=_TOKEN_JOBS_REJECT,
    ),
    CommandSpec(
        command_id=_TOKEN_JOBS_ACK,
        path=("jobs", "ack"),
        summary="Acknowledge a job.",
        description="Acknowledge a job through the shared job command service.",
        examples=("flow-steward jobs ack job_123 --actor cli",),
        operation_type=OperationType.CHANGE,
        interactive_policy=InteractivePolicy.ALWAYS_REVIEW,
        fields=(
            _JOB_ID_FIELD,
            FieldSpec(
                key="actor",
                label="Actor",
                type=FieldType.TEXT,
                default="cli",
                option_name="--actor",
            ),
        ),
        input_model=JobAcknowledgeCommandInput,
        result_model=JobLifecycleResultView,
        plan_builder_ref=None,
        confirmation_policy_ref=None,
        service_ref=_TOKEN_JOBS_ACK,
        result_renderer_ref=_TOKEN_JOBS_ACK,
    ),
    CommandSpec(
        command_id=_TOKEN_JOBS_UNACK,
        path=("jobs", "unack"),
        summary="Remove job acknowledgment.",
        description="Remove job acknowledgment through the shared job command service.",
        examples=("flow-steward jobs unack job_123",),
        operation_type=OperationType.CHANGE,
        interactive_policy=InteractivePolicy.ALWAYS_REVIEW,
        fields=(_JOB_ID_FIELD,),
        input_model=JobIdCommandInput,
        result_model=JobLifecycleResultView,
        plan_builder_ref=None,
        confirmation_policy_ref=None,
        service_ref=_TOKEN_JOBS_UNACK,
        result_renderer_ref=_TOKEN_JOBS_UNACK,
    ),
    CommandSpec(
        command_id=_TOKEN_JOBS_CANCEL,
        path=("jobs", "cancel"),
        summary="Cancel a job.",
        description="Cancel a job through the shared job command service.",
        examples=("flow-steward jobs cancel job_123 --comment 'Stop this run'",),
        operation_type=OperationType.CHANGE,
        interactive_policy=InteractivePolicy.ALWAYS_REVIEW,
        fields=(
            _JOB_ID_FIELD,
            FieldSpec(
                key="actor",
                label="Actor",
                type=FieldType.TEXT,
                default="operator",
                option_name="--actor",
            ),
            FieldSpec(
                key="comment",
                label="Comment",
                type=FieldType.TEXT,
                default="Cancelled from CLI",
                option_name="--comment",
            ),
        ),
        input_model=JobCancelCommandInput,
        result_model=JobLifecycleResultView,
        plan_builder_ref=None,
        confirmation_policy_ref=None,
        service_ref=_TOKEN_JOBS_CANCEL,
        result_renderer_ref=_TOKEN_JOBS_CANCEL,
    ),
    CommandSpec(
        command_id=_TOKEN_JOBS_RESTART,
        path=("jobs", "restart"),
        summary="Restart a job.",
        description="Re-enqueue a job through the shared job command service.",
        examples=("flow-steward jobs restart job_123",),
        operation_type=OperationType.CHANGE,
        interactive_policy=InteractivePolicy.ALWAYS_REVIEW,
        fields=(_JOB_ID_FIELD,),
        input_model=JobIdCommandInput,
        result_model=JobLifecycleResultView,
        plan_builder_ref=None,
        confirmation_policy_ref=None,
        service_ref=_TOKEN_JOBS_RESTART,
        result_renderer_ref=_TOKEN_JOBS_RESTART,
    ),
    CommandSpec(
        command_id=_TOKEN_JOBS_HEALTH_SNAPSHOT,
        path=("jobs", "health-snapshot"),
        summary="Collect an ops health snapshot.",
        description="Collect service and database health through the shared ops health collector.",
        examples=("flow-steward jobs health-snapshot --base-url https://localhost:8099",),
        operation_type=OperationType.READ,
        interactive_policy=InteractivePolicy.IMMEDIATE,
        fields=(
            FieldSpec(
                key="base_url",
                label="Base URL",
                type=FieldType.URL,
                default="https://localhost:8099",
                option_name="--base-url",
            ),
            FieldSpec(
                key="verify_ssl",
                label="Verify SSL",
                type=FieldType.BOOLEAN,
                default=False,
                option_name="--verify-ssl",
            ),
            FieldSpec(
                key="timeout_seconds",
                label="Timeout seconds",
                type=FieldType.INTEGER,
                default=10,
                option_name="--timeout-seconds",
            ),
            FieldSpec(
                key="dsn",
                label="DSN",
                type=FieldType.SECRET,
                secret=True,
                allow_secret_file=True,
                allow_secret_stdin=True,
                advanced=True,
            ),
            FieldSpec(
                key="stale_heartbeat_seconds",
                label="Stale heartbeat seconds",
                type=FieldType.INTEGER,
                default=120,
                option_name="--stale-heartbeat-seconds",
            ),
        ),
        input_model=JobHealthSnapshotCommandInput,
        result_model=JobHealthSnapshotView,
        plan_builder_ref=None,
        confirmation_policy_ref=None,
        service_ref=_TOKEN_JOBS_HEALTH_SNAPSHOT,
        result_renderer_ref=_TOKEN_JOBS_HEALTH_SNAPSHOT,
    ),
)
