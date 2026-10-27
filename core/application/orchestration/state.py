"""
OrchestratorState — central state TypedDict flowing through the LangGraph StateGraph.
Every node reads from and writes to this shared state.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal, TypedDict

from pydantic import BaseModel, ConfigDict, Field, model_validator

try:
    from langchain_core.messages import BaseMessage
except Exception:  # pragma: no cover - test/runtime fallback when langchain_core is absent

    class BaseMessage:
        """Minimal fallback for type annotations in lightweight test environments."""


try:
    from langgraph.graph.message import add_messages
except Exception:  # pragma: no cover - test/runtime fallback when langgraph is absent

    def add_messages(a, b):
        return (a or []) + (b or [])


class ExternalEffectApprovalAssociation(TypedDict):
    phase: Literal["association"]
    effect: dict[str, Any]
    origin: dict[str, Any]
    gate: dict[str, Any]
    definition_ref: dict[str, Any]


class ExternalEffectIssuedRetry(TypedDict):
    phase: Literal["issued"]
    command_id: str
    effect_id: str
    fenced_version: int
    approval_id: str


_PROJECTION_ONLY_FIELDS: frozenset[str] = frozenset(
    {
        "agent_performance",
    }
)
_TRUSTED_PROJECT_SCOPE_KEYS: tuple[str, ...] = (
    "account_id",
    "tenant_id",
    "workspace_id",
    "project_id",
    "workflow_id",
)
_TRUSTED_RUNTIME_SCOPE_KEYS: tuple[str, ...] = (
    *_TRUSTED_PROJECT_SCOPE_KEYS,
    "job_id",
    "run_id",
    "actor_id",
    "user_id",
)
_EXTRA_STATE_PROTECTED_ROOTS: frozenset[str] = frozenset(
    {
        "_test_mode_suppressed_outputs",
        "workflow_run_trace",
        "workflow_container_progress",
        "project_config",
        "test_mode",
        "_trusted_runtime_context",
        "trusted_runtime_context",
        "external_effect_resolution_ref",
    }
)


#: Journal entries a checkpoint keeps. A For each over thousands of items adds
#: entries per item; the newest ones explain where a run is, and the complete
#: history lives in the job's messages and lifecycle events.
JOURNAL_RETAINED_ENTRIES = 1000


def retained_journal(current: list[dict] | None, new: list[dict] | None) -> list[dict]:
    """Append *new* entries and keep the newest ``JOURNAL_RETAINED_ENTRIES``."""
    combined = [*(current or []), *(new or [])]
    return combined[-JOURNAL_RETAINED_ENTRIES:]


class OrchestratorState(TypedDict):
    """Central state flowing through the Flow Steward LangGraph."""

    # --- Task identity ---
    task_id: str
    task_summary: str
    task_source: str
    job_id: str | None  # DB jobs.id
    test_mode: bool
    debug_test_run: bool
    workflow_test_run_execution_id: str | None
    # Kept test outputs a workflow test run returns instead of running steps.
    workflow_test_run_kept_outputs: dict | None
    step_test_run_context: dict | None

    # --- Attachments ---
    attachments: (
        list[dict] | None
    )  # [{id, filename, mime_type, size_bytes}] — metadata only, no content

    # --- Routing ---
    effort_hint: Literal["micro", "small", "standard", "major"] | None
    task_type: str | None  # transport/projection-only external task label; never authoritative
    risk_level: Literal["low", "medium", "high"] | None
    routing_reason: str | None
    routing_signals: dict | None
    reentry_context: dict | None
    resume_parent: bool | None
    feedback_type: str | None
    parent_task_id: str | None
    parent_workflow_id: str | None
    orchestrator_plan: dict | None  # OrchestratorDecision serialized; checked on recovery
    orchestrator_rationale: str | None  # why this path (shown in UI)
    workflow_path: list[str]

    # --- Project context ---
    project_name: str
    project_id: str | None  # worker-authored scope persisted by the graph
    account_id: str | None  # canonical account scope for builder/runtime helpers
    tenant_id: str | None  # canonical tenant scope for builder/runtime helpers
    workspace_id: str | None  # canonical workspace scope for builder/runtime helpers
    trusted_runtime_context: dict[str, Any] | None  # durable worker-authored scope envelope
    project_config: dict
    project_policy: dict | None

    # --- Agent artifacts (accumulated) ---
    # Step artifacts flow via agent_messages/materialized inputs contracts,
    # not dedicated top-level state fields.
    # Execution-specific review/verifier artifacts must flow through
    # agent_messages/materialized inputs rather than canonical top-level state.

    # --- Project context (persisted key retained for checkpoint compatibility) ---
    kb_context: str | None

    # --- Orchestrator decisions ---
    needs_user_approval: bool
    # Durable identity of the approval whose Command update was checkpointed.
    # It must be a declared LangGraph state channel: unknown update keys are
    # ignored by LangGraph and would allow recovery to replay an old gate.
    approval_resume_applied_id: str | None
    external_effect_resolution_ref: (
        ExternalEffectApprovalAssociation | ExternalEffectIssuedRetry | None
    )
    approval_context: str | None
    user_approved: bool | None
    # Set by approval gate on rejection; cleared by the producing agent after use.
    # Annotated with a last-write-wins reducer because LangGraph 1.x applies
    # Command.update and the goto-target node's return dict in the same step,
    # which would otherwise raise InvalidUpdateError for a plain Optional[str].
    review_feedback: Annotated[str | None, lambda a, b: b]
    reviewer_comments: Annotated[str | None, lambda a, b: b]
    risk_notes: Annotated[str | None, lambda a, b: b]

    # --- Conversation history ---
    messages: Annotated[list[BaseMessage], add_messages]

    # --- Final ---
    final_report: Annotated[str | None, lambda a, b: b]
    proof_of_work: dict | None
    unresolved_step_failure: dict | None
    last_failure_class: str | None
    last_retry_reason: str | None
    last_retry_retryable: bool | None
    retry_backoff_seconds: float | None
    retry_telemetry: dict | None
    operation_retry_state: Annotated[dict | None, lambda a, b: b]
    operation_retry_dispatch: Annotated[dict | None, lambda a, b: b]
    status: Annotated[
        Literal[
            "in_progress",
            "awaiting_approval",
            "completed",
            "failed",
            "max_iterations",
        ],
        lambda a, b: b,
    ]
    error: str | None

    # =========================================================
    # Registry runtime fields
    # =========================================================

    # --- Orchestrator Supervisor ---
    task_envelope: dict | None  # TaskEnvelope serialized
    # Run cursor runtime (checkpoint v2): the pinned definition ref, where the
    # run is, the execution last dispatched and any pending recovery approval.
    runtime_schema_version: int | None
    workflow_definition_ref: dict | None  # {snapshot_id, revision, digest}
    run_cursor: Annotated[dict | None, lambda a, b: b]
    current_step_id: str | None
    current_group_id: str | None
    dispatched_step_execution: Annotated[dict | None, lambda a, b: b]
    recovery_pending: Annotated[dict | None, lambda a, b: b]
    # Structured event log; the checkpoint keeps the newest entries (see
    # retained_journal), the durable history is the job's messages and events.
    orchestrator_journal: Annotated[list[dict], retained_journal]
    workflow_run_trace: Annotated[dict | None, lambda a, b: b]
    workflow_container_progress: Annotated[dict | None, lambda a, b: b]
    autonomy_gate_pending: dict | None  # pending step-level autonomy review context
    autonomy_gate_approvals: dict | None  # step_execution_id -> approved before dispatch
    initial_requirements: str | None  # original task description (never mutated)
    iteration_count: int  # total agent invocations in this run
    # Successful items from authored finite loops. They remain part of
    # iteration_count telemetry, but do not consume the independent runaway
    # orchestration budget.
    bounded_loop_iterations: int
    # Graph transitions the hub took after an accepted result or decision.
    # Each one is progress bounded by the graph, its loop items and the rework
    # budget, so it does not consume the runaway budget either.
    routed_transitions: int
    confidence_score: float | None  # last task classification confidence
    automation_recommendation: str | None  # classifier: auto | supervised | human_only
    guardrail_check_results: Annotated[
        dict | None, lambda a, b: b
    ]  # guardrail check key -> latest pass/fail metadata
    state_summary: str | None  # compressed summary of state (≈500 tokens)
    inputs: dict | None  # normalized public workflow inputs
    # Immutable, non-revealing capability snapshot minted by the launch boundary.
    # It must be a declared LangGraph channel or graph compilation drops it.
    _workflow_secret_ref_fingerprints: Annotated[list[str] | None, lambda a, b: b]
    # Host-authored proof that an output is absent because its producing
    # connector mutation was deliberately suppressed in Test Mode.
    _test_mode_suppressed_outputs: Annotated[dict[str, dict[str, str]] | None, lambda a, b: b]
    outputs: dict | None  # normalized public workflow variable outputs
    artifacts: dict | None  # normalized public workflow artifact outputs
    # Full output-contract validation result. Declared because it is what the
    # public run result is built from: the validation node emits it, but an
    # undeclared key never survives the LangGraph checkpoint, so completed runs
    # persisted meta.workflow_outputs = {} and callers received no outputs at all.
    workflow_outputs: Annotated[dict | None, lambda a, b: b]
    materialized_inputs: dict | None  # expected_inputs source mappings available to skills
    workflow_data: dict | None  # dynamic workflow step variables and mapped outputs
    workflow_terminal: Annotated[
        dict | None, lambda a, b: b
    ]  # explicit terminal proof set only by stop_success/stop_fail runtime steps
    step_test_run_terminal: Annotated[dict | None, lambda a, b: b]
    parallel_lanes: list[dict] | None  # computed lane state of the active parallel group
    parallel_concurrency_cap: int | None
    # The lanes body result the parallel executor hands back to the hub,
    # tagged with the group execution it belongs to.
    parallel_outcome: Annotated[dict | None, lambda a, b: b]
    # The items a For each pass walks, by For each execution id. Written once
    # when the pass starts, so the run cursor that changes on every step stays
    # small (ADR 0038).
    for_each_items: Annotated[dict | None, lambda a, b: b]
    pending_wait: Annotated[dict | None, lambda a, b: b]
    wait_resume_applied_id: Annotated[str | None, lambda a, b: b]
    parallel_execution_order: list[int] | None

    # --- Agent Registry Support ---
    agent_messages: Annotated[
        list[dict] | None, lambda a, b: b
    ]  # manual append/replace (last-write-wins)
    active_agents: list[str] | None  # agent_ids currently running
    agent_performance: (
        dict | None
    )  # agent_id → {cost, tokens, duration_ms}  # PROJECTION-ONLY — read by admin page, never populated by orchestrator

    # --- Feedback & Re-entry ---
    feedback_mode: bool  # True when processing human/reviewer rework feedback
    parent_job_id: str | None  # original job_id if this is a re-entry
    rework_instructions: str | None  # structured rework requirements from review
    feedback: Annotated[str | None, lambda a, b: b]  # current retry feedback (last-write-wins)

    # --- Guardrails ---
    guardrail_violations: list[dict] | None  # [{rule, message, rule_type}] — last check results
    guardrail_retry_counts: (
        dict | None
    )  # step:agent:rules -> retry attempts for persistent violations

    # --- Registry Metadata ---
    workflow_id: str | None  # active workflow_id from registry
    workflow_selection_source: (
        str | None
    )  # "explicit_request" | "entry_binding" | "schedule_binding" | "webhook_binding" | "parent_call" | "resume"
    invoke_child_auto_approve: bool | None  # auto-approve nested child workflow gates
    invoke_parent_job_id: str | None  # parent job for invoke_workflow child lineage
    invoke_parent_step_id: str | None  # parent step emitting the child workflow
    invoke_parent_step_agent_id: str | None  # parent step agent for child heartbeat mirroring
    invoke_parent_workflow_id: str | None  # parent workflow that emitted the child invocation
    invoke_child_label: str | None  # child workflow name, marks its steps in the parent's run
    invoke_workflow_ancestry: (
        list | None
    )  # workflow ids from the root down to this run — read by the recursion/depth guard


class OrchestratorStateModel(BaseModel):
    """Canonical runtime state schema (Pydantic) for validation/normalization.

    Flow Steward still uses TypedDict for LangGraph channel reducers, but this model is now
    the single canonical place to validate and normalize state payloads at runtime
    boundaries (worker entry, API entry, tests).
    """

    model_config = ConfigDict(
        extra="allow",
        arbitrary_types_allowed=True,
        validate_assignment=True,
    )

    # --- Task identity ---
    task_id: str = ""
    task_summary: str = ""
    task_source: str = "manual"
    job_id: str | None = None
    test_mode: bool = False
    debug_test_run: bool = False
    workflow_test_run_execution_id: str | None = None
    workflow_test_run_kept_outputs: dict | None = None
    step_test_run_context: dict | None = None

    # --- Attachments ---
    attachments: list[dict] | None = None

    # --- Routing ---
    effort_hint: str | None = None
    task_type: str | None = None  # transport/projection-only external task label
    risk_level: str | None = None
    routing_reason: str | None = None
    routing_signals: dict | None = None
    reentry_context: dict | None = None
    resume_parent: bool | None = None
    feedback_type: str | None = None
    parent_task_id: str | None = None
    parent_workflow_id: str | None = None
    orchestrator_plan: dict | None = None
    orchestrator_rationale: str | None = None
    workflow_path: list[str] = Field(default_factory=list)

    # --- Project context ---
    project_name: str = ""
    project_id: str | None = None
    account_id: str | None = None
    tenant_id: str | None = None
    workspace_id: str | None = None
    trusted_runtime_context: dict[str, Any] | None = None
    project_config: dict = Field(default_factory=dict)
    project_policy: dict | None = None

    # --- Agent artifacts ---
    # Step artifacts flow via agent_messages/materialized inputs contracts,
    # not dedicated top-level state fields.
    # --- Project context (persisted key retained for checkpoint compatibility) ---
    kb_context: str | None = None

    # --- Orchestrator decisions ---
    needs_user_approval: bool = False
    # Durable identity of the approval whose Command update was checkpointed.
    # It must be a declared LangGraph state channel: unknown update keys are
    # ignored by LangGraph and would allow recovery to replay an old gate.
    approval_resume_applied_id: str | None = None
    external_effect_resolution_ref: dict[str, Any] | None = None
    approval_context: str | None = None
    user_approved: bool | None = None
    review_feedback: str | None = None
    reviewer_comments: str | None = None
    risk_notes: str | None = None

    # --- Conversation history ---
    messages: list[Any] = Field(default_factory=list)

    # --- Final ---
    final_report: str | None = None
    proof_of_work: dict | None = None
    unresolved_step_failure: dict | None = None
    last_failure_class: str | None = None
    last_retry_reason: str | None = None
    last_retry_retryable: bool | None = None
    retry_backoff_seconds: float | None = None
    retry_telemetry: dict | None = None
    operation_retry_state: dict | None = None
    operation_retry_dispatch: dict | None = None
    status: str = "in_progress"
    error: str | None = None

    # --- Registry-mode fields ---
    task_envelope: dict | None = None
    runtime_schema_version: int | None = None
    workflow_definition_ref: dict | None = None
    run_cursor: dict | None = None
    current_step_id: str | None = None
    current_group_id: str | None = None
    dispatched_step_execution: dict | None = None
    recovery_pending: dict | None = None
    orchestrator_journal: list[dict] = Field(default_factory=list)
    workflow_run_trace: dict | None = None
    workflow_container_progress: dict | None = None
    for_each_items: dict | None = None
    autonomy_gate_pending: dict | None = None
    autonomy_gate_approvals: dict = Field(default_factory=dict)
    initial_requirements: str | None = None
    iteration_count: int = 0
    bounded_loop_iterations: int = 0
    routed_transitions: int = 0
    confidence_score: float | None = None
    automation_recommendation: str | None = None
    guardrail_check_results: dict | None = None
    state_summary: str | None = None
    inputs: dict = Field(default_factory=dict)
    workflow_secret_ref_fingerprints: list[str] = Field(
        default_factory=list,
        alias="_workflow_secret_ref_fingerprints",
    )
    test_mode_suppressed_outputs: dict[str, dict[str, str]] = Field(
        default_factory=dict,
        alias="_test_mode_suppressed_outputs",
    )
    outputs: dict = Field(default_factory=dict)
    artifacts: dict = Field(default_factory=dict)
    workflow_outputs: dict | None = None
    materialized_inputs: dict = Field(default_factory=dict)
    workflow_data: dict = Field(default_factory=dict)
    workflow_terminal: dict | None = None
    step_test_run_terminal: dict | None = None
    parallel_lanes: list[dict] | None = None
    parallel_concurrency_cap: int | None = None
    parallel_outcome: dict | None = None
    pending_wait: dict | None = None
    wait_resume_applied_id: str | None = None
    parallel_execution_order: list[int] | None = None

    # --- Agent registry support ---
    agent_messages: list[dict] = Field(default_factory=list)
    active_agents: list[str] | None = None
    agent_performance: dict | None = None  # PROJECTION-ONLY — never populated by orchestrator

    # --- Feedback & re-entry ---
    feedback_mode: bool = False
    parent_job_id: str | None = None
    rework_instructions: str | None = None
    feedback: str | None = None

    # --- Guardrails ---
    guardrail_violations: list[dict] | None = None
    guardrail_retry_counts: dict | None = None

    # --- Registry metadata ---
    workflow_id: str | None = None
    workflow_selection_source: str | None = None
    invoke_child_auto_approve: bool | None = None
    invoke_parent_job_id: str | None = None
    invoke_parent_step_id: str | None = None
    invoke_parent_step_agent_id: str | None = None
    invoke_parent_workflow_id: str | None = None
    invoke_child_label: str | None = None
    invoke_workflow_ancestry: list | None = None

    @model_validator(mode="before")
    @classmethod
    def _normalize_entity_ids(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        payload = dict(value)
        for key in ("job_id", "parent_job_id", "invoke_parent_job_id"):
            if key in payload and payload[key] not in (None, ""):
                payload[key] = str(payload[key]).strip()
        journal = payload.get("orchestrator_journal")
        if journal is not None:
            if not isinstance(journal, list):
                raise TypeError("orchestrator_journal must be a list of structured dict objects")
            for entry in journal:
                if not isinstance(entry, dict):
                    raise TypeError("orchestrator_journal entries must be structured dict objects")
        return payload

    @model_validator(mode="after")
    def _derive_project_policy(self):
        """Derive the project policy from the available project config."""
        project_cfg = self.project_config if isinstance(self.project_config, dict) else {}
        if self.project_policy is None and isinstance(project_cfg.get("project_policy"), dict):
            self.project_policy = dict(project_cfg.get("project_policy") or {})

        if not self.task_source:
            self.task_source = "manual"
        if not self.status:
            self.status = "in_progress"
        return self


# ---------------------------------------------------------------------------
# Schema sync guard — OrchestratorState TypedDict is the canonical field source.
# OrchestratorStateModel must declare every field that OrchestratorState declares so that
# callers can validate/normalize state through the model without silently
# dropping fields.  Adding a field to OrchestratorState without adding it to
# OrchestratorStateModel will raise immediately at import time.
# ---------------------------------------------------------------------------
_STATE_TYPEDDICT_FIELDS: frozenset[str] = frozenset(OrchestratorState.__annotations__.keys())
_STATE_MODEL_FIELDS: frozenset[str] = frozenset(
    {
        alias or name
        for name, field_info in OrchestratorStateModel.model_fields.items()
        for alias in (name, field_info.alias)
        if alias
    }
)
_MISSING_FROM_MODEL: frozenset[str] = _STATE_TYPEDDICT_FIELDS - _STATE_MODEL_FIELDS
assert not _MISSING_FROM_MODEL, (
    f"OrchestratorStateModel is missing fields declared in OrchestratorState (the canonical source): "
    f"{sorted(_MISSING_FROM_MODEL)}. Add the missing fields to OrchestratorStateModel to keep "
    f"the two definitions in sync."
)


def normalize_orchestrator_state(state: dict | OrchestratorStateModel | None) -> dict:
    """Normalize arbitrary state payload into canonical OrchestratorStateModel dict."""
    if isinstance(state, OrchestratorStateModel):
        model = state
    else:
        payload = dict(state or {})
        model = OrchestratorStateModel.model_validate(payload)
    normalized = model.model_dump(exclude_none=False, by_alias=True)
    # Unset backend scope is absent metadata, not a contradictory checkpoint claim.
    if normalized.get("project_id") is None:
        normalized.pop("project_id", None)
    return normalized


def _state_text(value: Any) -> str:
    return str(value or "").strip()


def _collect_callable_public_inputs(state: dict) -> dict[str, Any]:
    """Collect public callable inputs from canonical runtime containers."""
    collected: dict[str, Any] = {}
    for container_name in ("inputs", "materialized_inputs"):
        container = state.get(container_name)
        if isinstance(container, dict):
            collected.update(container)
            nested_inputs = container.get("inputs")
            if isinstance(nested_inputs, dict):
                collected.update(nested_inputs)

    task_envelope = state.get("task_envelope")
    if isinstance(task_envelope, dict):
        envelope_inputs = task_envelope.get("inputs")
        if isinstance(envelope_inputs, dict):
            collected.update(envelope_inputs)
    return collected


def promote_callable_input_state(state: dict | None) -> dict:
    """Promote public callable inputs into prompt-facing canonical state fields.

    Workflow triggers preserve public inputs in canonical inputs,
    materialized_inputs, and task_envelope.inputs containers.
    Prompt and tool configuration code consumes top-level fields, so promote the
    public request before agents run.
    """
    promoted = dict(state or {})
    public_inputs = _collect_callable_public_inputs(promoted)
    if public_inputs:
        for container_name in ("inputs", "materialized_inputs"):
            existing = promoted.get(container_name)
            merged = dict(existing) if isinstance(existing, dict) else {}
            merged.update(public_inputs)
            promoted[container_name] = merged

    public_request = _state_text(public_inputs.get("request"))
    if public_request:
        promoted["request"] = public_request
        promoted["original_request"] = public_request
        promoted["initial_requirements"] = public_request

    return promoted


def build_initial_orchestrator_state(
    *,
    task_id: str,
    task_summary: str,
    task_source: str,
    project_name: str,
    project_config: dict | None = None,
    test_mode: bool = False,
    status: str = "in_progress",
    extra_state: dict | None = None,
) -> dict:
    """Build a fully-normalized initial graph state with canonical defaults."""
    authoritative_project_config = dict(project_config or {})
    model = OrchestratorStateModel(
        task_id=task_id or "",
        task_summary=task_summary or "",
        task_source=(task_source or "manual"),
        project_name=project_name or "",
        project_config=authoritative_project_config,
        test_mode=bool(test_mode),
        status=(status or "in_progress"),
    )
    state_dict = model.model_dump(exclude_none=False, by_alias=True)
    if extra_state:
        state_dict.update(
            {
                str(key): value
                for key, value in dict(extra_state).items()
                if str(key).strip() not in _EXTRA_STATE_PROTECTED_ROOTS
            }
        )
    state_dict = promote_callable_input_state(state_dict)
    project_cfg = dict(authoritative_project_config)
    # The declared channel must not retain caller extras when legacy scope is absent.
    state_dict["project_id"] = _state_text(project_cfg.get("project_id")) or None
    for scope_key in _TRUSTED_PROJECT_SCOPE_KEYS:
        scope_value = _state_text(project_cfg.get(scope_key))
        if scope_value:
            state_dict[scope_key] = scope_value
    state_dict["project_config"] = project_cfg
    trusted_runtime_context: dict[str, Any] = {}
    for scope_key in _TRUSTED_RUNTIME_SCOPE_KEYS:
        if scope_key in _TRUSTED_PROJECT_SCOPE_KEYS:
            scope_value = _state_text(project_cfg.get(scope_key))
        else:
            scope_value = _state_text(state_dict.get(scope_key))
        if scope_value:
            trusted_runtime_context[scope_key] = scope_value
    if test_mode:
        trusted_runtime_context["test_mode"] = True
    if trusted_runtime_context:
        state_dict["trusted_runtime_context"] = trusted_runtime_context
    return normalize_orchestrator_state(state_dict)
