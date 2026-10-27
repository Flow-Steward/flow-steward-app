# ruff: noqa: F405, I001, RUF100
from __future__ import annotations

from pydantic import GetJsonSchemaHandler, SerializerFunctionWrapHandler, model_serializer
from pydantic.json_schema import JsonSchemaValue
from pydantic_core import CoreSchema

from .query_models_common import *  # noqa: F403,F401  # NOSONAR
from .query_models_admin import *  # noqa: F403,F401  # NOSONAR
from core.application.jobs.workflow_run_models import PublicWorkflowResultV1, WorkflowRunStateV1
from core.application.workflows.workflow_contract_summary import WorkflowContractSummary


class AttentionGroupsView(ViewModel):
    needs_attention: list[AttentionJobView] = Field(default_factory=list)
    running: list[AttentionJobView] = Field(default_factory=list)
    blocked: list[AttentionJobView] = Field(default_factory=list)
    done: list[AttentionJobView] = Field(default_factory=list)
    acknowledged: list[AttentionJobView] = Field(default_factory=list)


class JobsAttentionGroupsView(ViewModel):
    group_by: str = "attention"
    project_id: str | None = None
    groups: AttentionGroupsView = Field(default_factory=AttentionGroupsView)


class JobCancelAcceptedView(ViewModel):
    model_config = ConfigDict(
        title="Job Cancellation Result",
        json_schema_extra={
            "description": "Acknowledgement that a cancellation request was accepted for a job.",
            "example": {"job_id": "job-987", "cancelled": True},
        },
    )

    job_id: str = Field(description="Job that received the cancellation request.")
    cancelled: bool = Field(
        default=True,
        description="Whether the platform accepted the cancellation request.",
    )


class JobCostSummaryView(ViewModel):
    tokens_prompt: int = Field(default=0, json_schema_extra={"public_usage_counter": True})
    tokens_completion: int = Field(default=0, json_schema_extra={"public_usage_counter": True})
    tokens_total: int = Field(default=0, json_schema_extra={"public_usage_counter": True})
    cost_usd: float = 0.0
    known_cost_usd: float = 0.0
    unknown_cli_tokens: int = Field(default=0, json_schema_extra={"public_usage_counter": True})
    cost_knownness: str = "known"
    llm_calls: int = 0


class JobStatusView(ViewModel):
    model_config = ConfigDict(extra="ignore", coerce_numbers_to_str=True)

    id: str
    status: str
    project_id: str | None = None
    thread_id: str | None = None
    task_summary: str
    mode: str | None = None
    selected_workflow: str | None = None
    selected_workflow_display_name: str | None = None
    selected_workflow_source: str | None = None
    workflow_selection_reason: str | None = None
    started_at: str | None = None
    finished_at: str | None = None
    duration_ms: int | None = None
    acknowledged_at: str | None = None
    acknowledged_by: str | None = None
    current_step_id: str | None = None
    current_group_id: str | None = None
    active_steps: list[dict[str, Any]] = Field(
        default_factory=list,
        description=(
            "Step executions in flight: {step_id, step_execution_id, attempt, group_id, "
            "lane_id, iteration, status}; every running lane in a parallel group, "
            "empty once the job ended."
        ),
    )
    current_agent: str | None = None
    invocation: dict[str, Any] = Field(default_factory=dict)
    trigger: dict[str, Any] = Field(default_factory=dict)
    runtime: dict[str, Any] = Field(default_factory=dict)
    cost: JobCostSummaryView = Field(default_factory=JobCostSummaryView)
    failure_summary: dict[str, Any] = Field(default_factory=dict)
    workflow_result: PublicWorkflowResultV1 | None = None


class JobDetailContractView(ViewModel):
    model_config = ConfigDict(
        title="Job Detail Response",
        json_schema_extra={
            "description": (
                "Top-level job state, approval status, and compact runtime context for one job."
            ),
            "example": {
                "id": "job-987",
                "project_id": "proj_42",
                "thread_id": "thread-7",
                "task_summary": GENERATE_A_RELEASE_READINESS_REPORT_FOR_VERSION_2_MESSAGE,
                "status": "awaiting_approval",
                "current_step_id": "review_report",
                "current_group_id": None,
                "current_agent": "reviewer",
                "status_label": "Awaiting approval",
                "status_tone": "amber",
                "step_label": "Review report",
                "approval_state": "pending",
                "attention_group": "awaiting_approval",
                "runtime": {"started_at": VALUE_2026_05_27T09_12_08Z_VALUE, "duration_ms": 184220},
                "job": {"selected_workflow": "release-report", "mode": "standard"},
                "sections": {"summary": {"title": "Summary", "items": []}},
                "projection": {"next_action": "Approve or reject the generated report."},
                "approval_gate": {"required": True, "thread_id": "thread-7"},
                "approval_history": [],
                "causal_chain": {},
                "cost_breakdown_raw": {"total_cost_usd": 0.14},
                "decision_overrides": [],
                "incident": {},
                "job_errors": [],
                "runtime_journal": [],
                "section_warnings": [],
                "thread_state": {},
                "lifecycle": {"queued_at": VALUE_2026_05_27T09_12_00Z_VALUE},
                "workflow_run": {
                    "schema_version": "1",
                    "job_id": "job-987",
                    "project_id": "proj_42",
                    "workflow_id": "release-report",
                    "status": "awaiting_approval",
                    "terminal": False,
                    "active_steps": [],
                    "steps": [],
                    "approval": None,
                    "result": None,
                    "error": None,
                    "queued_at": VALUE_2026_05_27T09_12_00Z_VALUE,
                    "started_at": VALUE_2026_05_27T09_12_08Z_VALUE,
                    "updated_at": VALUE_2026_05_27T09_12_08Z_VALUE,
                    "finished_at": None,
                },
            },
        },
    )

    id: str = Field(default="", description="Platform job identifier.")
    project_id: str | None = Field(default=None, description="Project that owns the job.")
    thread_id: str | None = Field(
        default=None,
        description="Workflow thread identifier used for approvals and resumptions when present.",
    )
    task_summary: str = Field(default="", description="Plain-language task summary for the job.")
    status: str = Field(default="", description="Current lifecycle status for the job.")
    current_step_id: str | None = Field(
        default=None, description="Workflow step the run is at, when available."
    )
    current_group_id: str | None = Field(
        default=None, description="Real group the current step belongs to, when it is inside one."
    )
    current_agent: str = Field(
        default="", description="Current workflow agent or component, when available."
    )
    status_label: str = Field(default="", description="Human-readable status label.")
    status_tone: str = Field(
        default="gray", description="UI-friendly status tone for the current state."
    )
    step_label: str = Field(default="", description="Human-readable label for the current step.")
    approval_state: str = Field(
        default="", description="Approval state when the workflow is gated on a human decision."
    )
    attention_group: str = Field(
        default="", description="High-level operational bucket for the current job state."
    )
    runtime: dict[str, Any] = Field(default_factory=dict)
    job: dict[str, Any] = Field(default_factory=dict)
    sections: dict[str, Any] = Field(default_factory=dict)
    projection: dict[str, Any] = Field(default_factory=dict)
    approval_gate: dict[str, Any] = Field(default_factory=dict)
    approval_history: list[dict[str, Any]] = Field(default_factory=list)
    causal_chain: dict[str, Any] = Field(default_factory=dict)
    cost: JobCostSummaryView | None = None
    cost_breakdown_raw: dict[str, Any] = Field(default_factory=dict)
    decision_overrides: list[dict[str, Any]] = Field(default_factory=list)
    incident: dict[str, Any] = Field(default_factory=dict)
    job_errors: list[dict[str, Any]] = Field(default_factory=list)
    runtime_journal: list[dict[str, Any]] = Field(default_factory=list)
    section_warnings: list[dict[str, str]] = Field(default_factory=list)
    thread_state: dict[str, Any] = Field(default_factory=dict)
    lifecycle: dict[str, Any] = Field(default_factory=dict)
    workflow_result: PublicWorkflowResultV1 | None = None
    workflow_run: WorkflowRunStateV1 | None = Field(
        default=None,
        description=(
            "Stable, versioned integration projection. Other job-detail fields are product and "
            "diagnostic surfaces and may evolve."
        ),
    )

    @classmethod
    def __get_pydantic_json_schema__(
        cls, schema: CoreSchema, handler: GetJsonSchemaHandler
    ) -> JsonSchemaValue:
        """Retain model fields for a serializer that only omits optional cost data."""
        schema_without_serializer = dict(schema)
        schema_without_serializer.pop("serialization", None)
        return handler(schema_without_serializer)

    @model_serializer(mode="wrap")
    def serialize_supplied_cost(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        """Type supplied summary counters without inventing absent detail data."""
        payload = handler(self)
        if "cost" not in self.model_fields_set:
            payload.pop("cost", None)
        elif isinstance(payload.get("cost"), dict) and self.cost is not None:
            payload["cost"] = {
                key: value
                for key, value in payload["cost"].items()
                if key in self.cost.model_fields_set
            }
        return payload


class JobTraceSpanView(ViewModel):
    span_id: str
    trace_id: str
    parent_span_id: str | None = None
    kind: str = "span"
    layer: str = "runtime"
    name: str = ""
    status: str | None = None
    run_id: str | None = None
    parent_run_id: str | None = None
    correlation_id: str | None = None
    started_at: str | None = None
    finished_at: str | None = None
    occurred_at: str | None = None
    duration_ms: int | None = None
    attributes: dict[str, Any] = Field(default_factory=dict)


class JobTraceView(ViewModel):
    trace_id: str
    job_id: str
    project_id: str | None = None
    account_id: str
    status: str | None = None
    root_span_id: str
    root_run_id: str | None = None
    linkage: dict[str, Any] = Field(default_factory=dict)
    spans: list[JobTraceSpanView] = Field(default_factory=list)


class JobRuntimeJournalView(ViewModel):
    job_id: str
    runtime_journal: list[dict[str, Any]] = Field(default_factory=list)


class JobDecisionOverridesView(ViewModel):
    job_id: str
    overrides: list[dict[str, Any]] = Field(default_factory=list)


class JobErrorsView(ViewModel):
    job_id: str
    errors: list[dict[str, Any]] = Field(default_factory=list)


class JobDefinitionView(ViewModel):
    """The definition a job runs: the snapshot it pinned, never the live workflow."""

    model_config = ConfigDict(
        title="Job Definition",
        json_schema_extra={
            "description": (
                "The workflow definition pinned by a job at its first dispatch. A job "
                "without a pinned snapshot reports available=false instead of showing "
                "the current definition as the one it executed."
            )
        },
    )

    job_id: str
    available: bool = False
    unavailable_reason: str | None = None
    snapshot_id: str | None = None
    workflow_id: str | None = None
    revision: int | None = None
    digest: str | None = None
    schema_version: int | None = None
    contract_summary: WorkflowContractSummary | None = None
    steps: list[dict[str, Any]] = Field(default_factory=list)
    groups: list[dict[str, Any]] = Field(default_factory=list)
    for_each: list[dict[str, Any]] = Field(default_factory=list)
    execution_graph: dict[str, Any] = Field(default_factory=dict)
    #: A protected author graph: shown read-only with sensitive values redacted.
    protected: bool = False


class JobCausalChainView(ViewModel):
    job_id: str
    nodes: list[dict[str, Any]] = Field(default_factory=list)
    edges: list[dict[str, Any]] = Field(default_factory=list)
    mermaid: str = ""


class JobCostBreakdownView(ViewModel):
    breakdown: list[dict[str, Any]] = Field(default_factory=list)
    total_cost_usd: float = 0.0
    known_cost_usd: float = 0.0
    unknown_cli_tokens: int = 0
    cost_knownness: str = "known"


class JobEventsView(ViewModel):
    events: list[dict[str, Any]] = Field(default_factory=list)
    last_id: str = ""


class JobMessagesView(ViewModel):
    messages: list[dict[str, Any]] = Field(default_factory=list)
    last_id: str = ""
    has_more: bool = False


class JobMessageDetailView(ViewModel):
    message: dict[str, Any] = Field(default_factory=dict)


class JobReplayStepView(ViewModel):
    message: dict[str, Any] = Field(default_factory=dict)
    routing_decisions: list[dict[str, Any]] = Field(default_factory=list)
    prompt_request: dict[str, Any] | None = None
    prompt_response: dict[str, Any] | None = None
    related_messages: list[dict[str, Any]] = Field(default_factory=list)


class JobStepReplaysView(ViewModel):
    job_id: str
    step_replays: list[dict[str, Any]] = Field(default_factory=list)


class JobLogsView(ViewModel):
    logs: list[dict[str, Any]] = Field(default_factory=list)
    status: str = ""
    last_id: str = ""
    next_before_id: str = ""
    has_more: bool = False


class JobLogsPageView(ViewModel):
    job: dict[str, Any] = Field(default_factory=dict)
    logs: list[dict[str, Any]] = Field(default_factory=list)
    events: list[dict[str, Any]] = Field(default_factory=list)
    messages: list[dict[str, Any]] = Field(default_factory=list)
    log_has_more: bool = False
    message_has_more: bool = False
    runtime: dict[str, Any] = Field(default_factory=dict)
    decision_overrides: list[dict[str, Any]] = Field(default_factory=list)
    job_errors: list[dict[str, Any]] = Field(default_factory=list)
    job_failure_reason: str = ""
    project_name: str = ""


class JobIncidentDrilldownView(ViewModel):
    job_id: str
    project_id: str | None = None
    status: str = ""
    root_cause: dict[str, Any] = Field(default_factory=dict)
    failure_context: dict[str, Any] = Field(default_factory=dict)
    related_events: list[dict[str, Any]] = Field(default_factory=list)
    related_dispatches: list[dict[str, Any]] = Field(default_factory=list)
    related_attempts: list[dict[str, Any]] = Field(default_factory=list)


class JobReactionsView(ViewModel):
    job_id: str
    reactions: list[dict[str, Any]] = Field(default_factory=list)


class WorkflowReactionRuleView(ViewModel):
    reaction_key: str = ""
    display_name: str = ""
    event_type: str = ""
    conditions: dict[str, Any] = Field(default_factory=dict)
    action: dict[str, Any] = Field(default_factory=dict)
    debounce_seconds: int = 0
    idempotency_key_template: str | None = None
    enabled: bool = True
    scope_kind: str = "workflow"
    scope_id: str = ""
