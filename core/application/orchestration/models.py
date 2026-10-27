"""
Pydantic models for structured LLM output in Flow Steward agent nodes.

These models are used with `llm.with_structured_output(Model)` to enforce
schema on agent outputs — no free-text parsing.
"""

import uuid
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from core.application.registry.output_contract_fields import (
    response_field_json_schema as _contract_field_json_schema,
)

CANONICAL_PHASE_STATUSES = ("pass", "fail", "blocked", "escalate")
PROJECT_ID_POSITIVE_ERROR = "project_id must be > 0"
STEP_EXECUTION_ID_UUID_ERROR = "step_execution_id must be a UUID"


def _coerce_positive_int_field(value: Any, *, field_name: str, positive_error: str) -> int:
    try:
        normalized = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be an integer") from exc
    if normalized <= 0:
        raise ValueError(positive_error)
    return normalized


def _coerce_required_entity_id(value: Any, *, field_name: str) -> str:
    if isinstance(value, (dict, list, tuple, set)):
        raise ValueError(f"{field_name} must be a scalar identifier")
    normalized = str(value or "").strip()
    if not normalized:
        raise ValueError(f"{field_name} is required")
    return normalized


def normalize_phase_status(value: Any, *, default: str = "fail") -> str:
    """Return a canonical phase status or the provided default."""
    text = str(value or "").strip().lower()
    if text in CANONICAL_PHASE_STATUSES:
        return text
    return default


def is_pass_phase_status(value: Any) -> bool:
    """True when a phase/agent status semantically means success."""
    return normalize_phase_status(value) == "pass"


def phase_next_action(status: Any) -> str:
    """Derive orchestrator action from canonical phase status."""
    normalized = normalize_phase_status(status)
    if normalized == "pass":
        return "next_phase"
    if normalized == "fail":
        return "retry_phase"
    if normalized in ("blocked", "escalate"):
        return "escalate_to_human"
    return "none"


# ---------------------------------------------------------------------------
# Phase Result Envelope (Phase 3)
# ---------------------------------------------------------------------------


class PhaseResultV2(BaseModel):
    """Strict envelope passed between phase boundaries."""

    schema_version: Literal["phase_result_v2"] = Field(
        default="phase_result_v2",
        description="Schema discriminator for strict phase-result payloads.",
    )
    status: Literal["pass", "fail", "blocked", "escalate"] = Field(
        description="Execution status for this phase step."
    )
    summary: str = Field(description="Concise human-readable summary of what was done.")
    artifacts: dict[str, Any] = Field(
        default_factory=dict,
        description="Structured published block outputs (contract fields live here).",
    )
    evidence: list[Any] = Field(
        default_factory=list, description="Evidence pointers/snippets that justify the result."
    )
    errors: list[str] = Field(
        default_factory=list, description="Machine-actionable error list (empty on success)."
    )
    files_to_touch: list[str] = Field(
        default_factory=list, description="Files modified or expected to be modified by this step."
    )
    next_action: Literal["next_phase", "retry_phase", "escalate_to_human", "none"] = Field(
        default="next_phase", description="Suggested next action for router logic."
    )
    confidence: float = Field(
        default=0.0, ge=0.0, le=1.0, description="Confidence score in [0.0, 1.0]."
    )

    @field_validator("summary")
    @classmethod
    def _summary_non_empty(cls, value: str) -> str:
        if not value or not value.strip():
            raise ValueError("summary must be non-empty")
        return value.strip()

    @field_validator("status", mode="before")
    @classmethod
    def _normalize_status(cls, value: Any) -> str:
        normalized = normalize_phase_status(value, default="")
        if not normalized:
            raise ValueError("status must be one of pass, fail, blocked, escalate")
        return normalized


def validate_phase_result_payload(payload: dict | None) -> tuple[PhaseResultV2 | None, list[str]]:
    """Validate dict payload as PhaseResultV2 and return normalized model + errors."""
    if not isinstance(payload, dict):
        return None, ["phase_result_v2 payload must be an object"]
    try:
        normalized_payload = dict(payload)
        normalized_payload["schema_version"] = "phase_result_v2"
        normalized_payload["status"] = normalize_phase_status(payload.get("status"), default="")
        if (
            "next_action" not in normalized_payload
            or not str(normalized_payload.get("next_action") or "").strip()
        ):
            normalized_payload["next_action"] = phase_next_action(normalized_payload["status"])
        model = PhaseResultV2.model_validate(normalized_payload)
        return model, []
    except ValidationError as exc:
        return None, [f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors()]


def build_phase_result_json_schema(outputs: list[dict] | None = None) -> dict:
    """Build a strict JSON schema for phase_result_v2 using published block outputs."""
    output_rows = [dict(row) for row in (outputs or []) if isinstance(row, dict)]
    artifact_properties: dict[str, Any] = {}
    artifact_required: list[str] = []
    for row in output_rows:
        name = str(row.get("name") or "").strip()
        if not name:
            continue
        field_schema = (
            dict(row["response_schema"])
            if isinstance(row.get("response_schema"), dict)
            else _contract_field_json_schema(
                str(row.get("type") or "text"),
                str(row.get("description") or ""),
                row.get("contract_schema")
                if isinstance(row.get("contract_schema"), dict)
                else None,
            )
        )
        if not bool(row.get("required", True)):
            field_schema = {"anyOf": [field_schema, {"type": "null"}]}
        artifact_properties[name] = field_schema
        # OpenAI strict structured outputs require every property key
        # to appear in `required`; optional outputs are represented as null.
        artifact_required.append(name)

    return {
        "title": "PhaseResultV2",
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "schema_version": {
                "type": "string",
                "enum": ["phase_result_v2"],
                "description": "Schema discriminator for strict phase-result payloads.",
            },
            "status": {
                "type": "string",
                "enum": ["pass", "fail", "blocked", "escalate"],
                "description": "Execution status for this phase step.",
            },
            "summary": {
                "type": "string",
                "description": "Concise human-readable summary of what was done.",
            },
            "artifacts": {
                "type": "object",
                "additionalProperties": False,
                "properties": artifact_properties,
                "required": artifact_required,
                "description": "Structured published block outputs (contract fields live here).",
            },
            "evidence": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Evidence pointers/snippets that justify the result.",
            },
            "errors": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Machine-actionable error list (empty on success).",
            },
            "files_to_touch": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Files modified or expected to be modified by this step.",
            },
            "next_action": {
                "type": "string",
                "enum": ["next_phase", "retry_phase", "escalate_to_human", "none"],
                "description": "Suggested next action for router logic.",
            },
            "confidence": {
                "type": "number",
                "minimum": 0.0,
                "maximum": 1.0,
                "description": "Confidence score in [0.0, 1.0].",
            },
        },
        "required": [
            "schema_version",
            "status",
            "summary",
            "artifacts",
            "evidence",
            "errors",
            "files_to_touch",
            "next_action",
            "confidence",
        ],
    }


# ---------------------------------------------------------------------------
# Agent Message Envelope (Hard cutover)
# ---------------------------------------------------------------------------


class AgentMessageTimestampsV1(BaseModel):
    """Lifecycle timestamps for a single agent message."""

    started_at: str | None = Field(
        default=None, description="ISO timestamp when agent execution started."
    )
    completed_at: str | None = Field(
        default=None, description="ISO timestamp when agent execution completed."
    )
    published_at: str = Field(
        description="ISO timestamp when this message was published to the runtime stream."
    )


class OperationRetrySignalV1(BaseModel):
    """Host-owned retry facts attached outside generic agent artifacts."""

    schema_version: Literal["operation_retry_signal_v1"] = "operation_retry_signal_v1"
    provenance: Literal[
        "deterministic_http_step",
        "deterministic_connector_step",
        "agent_managed_custom_http",
    ]
    signal: dict[str, Any]
    deadline_epoch_seconds: float | None = None
    step_id: str = ""
    operation_loop: dict[str, Any] | None = None
    operation_parallel: dict[str, Any] | None = None

    @field_validator("signal")
    @classmethod
    def _normalize_signal(cls, value: Any) -> dict[str, Any]:
        from core.application.orchestration.retry_policy import normalize_retry_signal

        normalized = normalize_retry_signal(value if isinstance(value, dict) else {})
        if not normalized:
            raise ValueError("host retry signal must contain canonical retry facts")
        return normalized

    @field_validator("deadline_epoch_seconds")
    @classmethod
    def _positive_deadline(cls, value: float | None) -> float | None:
        if value is None:
            return None
        parsed = float(value)
        if parsed <= 0:
            raise ValueError("deadline_epoch_seconds must be positive")
        return parsed


class AgentMessageV1(BaseModel):
    """Canonical inter-agent message envelope for runtime state transfer."""

    schema_version: Literal["agent_message_v1"] = Field(
        default="agent_message_v1", description="Schema discriminator for AgentMessageV1."
    )
    from_agent: str = Field(description="Producer agent_id.")
    to_agent: str = Field(description="Consumer agent_id (typically orchestrator).")
    step_id: str | None = Field(
        default=None,
        description="Workflow step that produced this message; absent for workflow/control messages.",
    )
    step_execution_id: str | None = Field(
        default=None,
        description="Logical step entry this message belongs to; retries keep it, rework changes it.",
    )
    attempt: int | None = Field(
        default=None, ge=1, description="Attempt of the step execution, starting at 1."
    )
    group_id: str | None = Field(
        default=None, description="Immediate Group of the step, when it is directly inside one."
    )
    for_each_id: str | None = Field(
        default=None, description="Immediate For each of the step, when it is inside one."
    )
    item_index: int | None = Field(
        default=None,
        ge=0,
        description="0-based position of the For each item this step ran for, inside a For each.",
    )
    skill: str = Field(description="Skill executed for this message.")
    status: Literal["pass", "fail", "escalate", "blocked"] = Field(
        description="Execution status for this message."
    )
    correlation_id: str = Field(description="Stable id used to correlate retries/attempts.")
    caused_by: list[dict[str, Any]] = Field(
        default_factory=list,
        description="Causal references (messages/events/feedback) that contributed to this message.",
    )
    artifacts: dict[str, Any] = Field(
        default_factory=dict, description="Skill artifacts passed across runtime boundaries."
    )
    output_contract: dict[str, Any] | None = Field(
        default=None, description="Internal host-owned output authority captured for this attempt."
    )

    @field_validator("output_contract", mode="before")
    @classmethod
    def _optional_output_contract(cls, value: Any) -> dict[str, Any] | None:
        # A malformed optional receipt must not erase explicit structured outputs.
        return value if isinstance(value, dict) else None

    operation_retry: OperationRetrySignalV1 | None = Field(
        default=None,
        description="Validated host-owned retry authority; never populated from LLM artifacts.",
    )
    summary: str = Field(default="", description="Human-readable summary.")
    errors: list[str] = Field(default_factory=list, description="Machine-actionable error list.")
    timestamps: AgentMessageTimestampsV1

    @field_validator("from_agent", "to_agent", "skill", "correlation_id")
    @classmethod
    def _required_non_empty(cls, value: str) -> str:
        if not value or not value.strip():
            raise ValueError("must be non-empty")
        return value.strip()

    @field_validator("step_id", "group_id", "for_each_id", mode="before")
    @classmethod
    def _optional_identity(cls, value: Any) -> str | None:
        text = str(value or "").strip()
        return text or None

    @field_validator("step_execution_id", mode="before")
    @classmethod
    def _execution_uuid(cls, value: Any) -> str | None:
        text = str(value or "").strip()
        if not text:
            return None
        try:
            return str(uuid.UUID(text))
        except ValueError as exc:
            raise ValueError(STEP_EXECUTION_ID_UUID_ERROR) from exc

    @model_validator(mode="after")
    def _step_scope_is_complete(self) -> "AgentMessageV1":
        # A step-scoped result must name the exact execution and attempt it
        # answers; otherwise it could complete a newer entry of the same step.
        scoped = (self.step_execution_id, self.attempt)
        if any(value is not None for value in scoped) and not (
            self.step_id and all(value is not None for value in scoped)
        ):
            raise ValueError("step_id, step_execution_id and attempt must be given together")
        return self

    @field_validator("status", mode="before")
    @classmethod
    def _normalize_status(cls, value: Any) -> str:
        normalized = normalize_phase_status(value, default="")
        if not normalized:
            raise ValueError("status must be one of pass, fail, blocked, escalate")
        return normalized


def validate_agent_message_payload(payload: dict | None) -> tuple[AgentMessageV1 | None, list[str]]:
    """Validate dict payload as AgentMessageV1 and return normalized model + errors."""
    if not isinstance(payload, dict):
        return None, ["agent_message payload must be an object"]
    try:
        model = AgentMessageV1.model_validate(payload)
        return model, []
    except ValidationError as exc:
        return None, [f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors()]


# ---------------------------------------------------------------------------
# External Agent Ingress v1
# ---------------------------------------------------------------------------


class ExternalAgentContractModel(BaseModel):
    """Shared base model for external agent ingress payloads."""

    model_config = ConfigDict(extra="forbid")

    def dump(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


class ExternalAgentRegisterRequestV1(ExternalAgentContractModel):
    agent_name: str
    worker_instance_id: str = ""
    project_id: str | None = None
    workflow_id: str | None = None
    skills: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("agent_name", "worker_instance_id", mode="before")
    @classmethod
    def _normalize_required_strings(cls, value: Any, info) -> str:
        text = str(value or "").strip()
        if info.field_name == "agent_name" and not text:
            raise ValueError("agent_name is required")
        return text

    @field_validator("workflow_id", mode="before")
    @classmethod
    def _normalize_optional_workflow_id(cls, value: Any) -> str | None:
        text = str(value or "").strip()
        return text or None

    @field_validator("project_id", mode="before")
    @classmethod
    def _normalize_optional_project_id(cls, value: Any) -> str | None:
        if value in (None, ""):
            return None
        if isinstance(value, (dict, list, tuple, set)):
            raise ValueError("project_id must be a scalar identifier")
        return str(value).strip()

    @field_validator("skills", mode="before")
    @classmethod
    def _normalize_skills(cls, value: Any) -> list[str]:
        if value in (None, ""):
            return []
        if not isinstance(value, list):
            raise ValueError("skills must be an array")
        normalized: list[str] = []
        for item in value:
            skill = str(item or "").strip()
            if skill and skill not in normalized:
                normalized.append(skill)
        return normalized


class ExternalAgentRegisterResponseV1(ExternalAgentContractModel):
    contract_version: Literal["external_agent_v1"] = "external_agent_v1"
    registration_id: str
    worker_instance_id: str
    agent_name: str
    account_id: str
    principal_type: str
    scopes: list[str] = Field(default_factory=list)
    allowed_project_ids: list[str] = Field(default_factory=list)
    allowed_workflow_ids: list[str] = Field(default_factory=list)
    project_id: str | None = None
    workflow_id: str | None = None
    server_time: str


class ExternalAgentHeartbeatRequestV1(ExternalAgentContractModel):
    worker_instance_id: str
    project_id: str
    lease_token: str = ""
    active_job_id: str | None = None
    agent_name: str = ""

    @field_validator("worker_instance_id", "lease_token", "agent_name", mode="before")
    @classmethod
    def _normalize_text(cls, value: Any) -> str:
        return str(value or "").strip()

    @field_validator("project_id", mode="before")
    @classmethod
    def _validate_project_id(cls, value: Any) -> str:
        return _coerce_required_entity_id(value, field_name="project_id")

    @field_validator("active_job_id", mode="before")
    @classmethod
    def _normalize_active_job_id(cls, value: Any) -> str | None:
        if value in (None, ""):
            return None
        return str(value).strip()


class ExternalAgentHeartbeatResponseV1(ExternalAgentContractModel):
    contract_version: Literal["external_agent_v1"] = "external_agent_v1"
    status: str = "ok"
    worker_instance_id: str
    project_id: str
    lease_token: str = ""
    active_job_id: str | None = None
    server_time: str


class ExternalAgentClaimRequestV1(ExternalAgentContractModel):
    worker_instance_id: str
    project_id: str
    workflow_id: str | None = None
    lease_token: str = ""
    agent_name: str = ""

    @field_validator("worker_instance_id", "lease_token", "agent_name", mode="before")
    @classmethod
    def _normalize_claim_text(cls, value: Any) -> str:
        return str(value or "").strip()

    @field_validator("workflow_id", mode="before")
    @classmethod
    def _normalize_claim_workflow_id(cls, value: Any) -> str | None:
        text = str(value or "").strip()
        return text or None

    @field_validator("project_id", mode="before")
    @classmethod
    def _validate_claim_project_id(cls, value: Any) -> str:
        return _coerce_required_entity_id(value, field_name="project_id")


class ExternalAgentClaimedJobV1(ExternalAgentContractModel):
    job_id: str
    claim_token: str
    thread_id: str | None = None
    task_id: str
    task_summary: str
    task_source: str
    project_id: str
    project_name: str
    workflow_id: str | None = None
    step_id: str | None = None
    step_execution_id: str | None = None
    attempt: int | None = Field(default=None, ge=1)
    skill: str | None = None
    input: dict[str, Any] = Field(default_factory=dict)
    job_meta: dict[str, Any] = Field(default_factory=dict)


class ExternalAgentClaimResponseV1(ExternalAgentContractModel):
    contract_version: Literal["external_agent_v1"] = "external_agent_v1"
    claimed: bool
    worker_instance_id: str
    project_id: str
    lease_token: str = ""
    job: ExternalAgentClaimedJobV1 | None = None
    server_time: str


class ExternalAgentReportRequestV1(ExternalAgentContractModel):
    worker_instance_id: str
    project_id: str
    job_id: str
    lease_token: str
    claim_token: str
    agent_name: str
    step_id: str
    step_execution_id: str | None = None
    attempt: int | None = Field(default=None, ge=1)
    skill: str
    status: Literal["pass", "fail", "blocked", "escalate"]
    summary: str = ""
    artifacts: dict[str, Any] = Field(default_factory=dict)
    errors: list[str] = Field(default_factory=list)
    correlation_id: str = ""
    completed: bool = True

    @field_validator(
        "worker_instance_id",
        "lease_token",
        "claim_token",
        "agent_name",
        "step_id",
        "skill",
        "summary",
        "correlation_id",
        mode="before",
    )
    @classmethod
    def _normalize_report_text(cls, value: Any, info) -> str:
        text = str(value or "").strip()
        if (
            info.field_name
            in {
                "worker_instance_id",
                "lease_token",
                "claim_token",
                "agent_name",
                "step_id",
                "skill",
            }
            and not text
        ):
            raise ValueError(f"{info.field_name} is required")
        return text

    @field_validator("project_id", "job_id", mode="before")
    @classmethod
    def _validate_positive_ids(cls, value: Any, info) -> str:
        return _coerce_required_entity_id(value, field_name=str(info.field_name or "id"))

    @field_validator("step_execution_id", mode="before")
    @classmethod
    def _report_execution_uuid(cls, value: Any) -> str | None:
        text = str(value or "").strip()
        if not text:
            return None
        try:
            return str(uuid.UUID(text))
        except ValueError as exc:
            raise ValueError(STEP_EXECUTION_ID_UUID_ERROR) from exc

    @model_validator(mode="after")
    def _report_execution_scope_is_complete(self) -> "ExternalAgentReportRequestV1":
        if (self.step_execution_id is None) != (self.attempt is None):
            raise ValueError("step_execution_id and attempt must be given together")
        return self

    @field_validator("errors", mode="before")
    @classmethod
    def _normalize_errors(cls, value: Any) -> list[str]:
        if value in (None, ""):
            return []
        if not isinstance(value, list):
            raise ValueError("errors must be an array")
        return [str(item or "").strip() for item in value if str(item or "").strip()]


class ExternalAgentReportResponseV1(ExternalAgentContractModel):
    contract_version: Literal["external_agent_v1"] = "external_agent_v1"
    job_id: str
    status: str
    job_status: str
    correlation_id: str
    message_published: bool
    lease_released: bool
    server_time: str


# ---------------------------------------------------------------------------
# Scoped Event Envelope v1
# ---------------------------------------------------------------------------


class EventActorV1(BaseModel):
    """Normalized actor metadata for event envelope payloads."""

    type: str = Field(default="system", description="Actor type that produced the event.")
    id: str = Field(default="", description="Stable actor identifier when available.")

    @field_validator("type", mode="before")
    @classmethod
    def _normalize_type(cls, value: Any) -> str:
        return str(value or "system").strip() or "system"

    @field_validator("id", mode="before")
    @classmethod
    def _normalize_id(cls, value: Any) -> str:
        return str(value or "").strip()


class EventEnvelopeV1(BaseModel):
    """Canonical scoped event envelope for new runtime event producers."""

    schema_version: Literal["event_envelope_v1"] = Field(
        default="event_envelope_v1",
        description="Schema discriminator for scoped runtime events.",
    )
    event_id: str = Field(description="Stable event id.")
    event_type: str = Field(description="Canonical event type identifier.")
    correlation_id: str = Field(description="Correlation id for retries, chains, and consumers.")
    occurred_at: str = Field(description="ISO timestamp when the event occurred.")
    account_id: str = Field(description="Authoritative account scope for the event.")
    severity: Literal["debug", "info", "warning", "error", "critical"] = Field(
        default="info",
        description="Normalized event severity.",
    )
    summary: str = Field(default="", description="Human-readable event summary.")
    payload: dict[str, Any] = Field(default_factory=dict, description="Event payload body.")
    project_id: str | None = Field(default=None, description="Scoped project id when applicable.")
    workflow_id: str | None = Field(default=None, description="Scoped workflow id when applicable.")
    job_id: str | None = Field(default=None, description="Scoped job id when applicable.")
    group_id: str | None = Field(
        default=None, description="Immediate Group of the scope (a Group event or a step in it)."
    )
    for_each_id: str | None = Field(
        default=None,
        description="Immediate For each of the scope (a For each event or a step in it).",
    )
    step_id: str | None = Field(default=None, description="Scoped step id when applicable.")
    step_execution_id: str | None = Field(
        default=None, description="Scoped logical step execution when applicable."
    )
    source: str | None = Field(default=None, description="Event producer source.")
    aggregate_type: str | None = Field(
        default=None, description="Aggregate type for event routing."
    )
    aggregate_id: str | None = Field(default=None, description="Aggregate id for event routing.")
    actor: EventActorV1 | None = Field(default=None, description="Optional actor metadata.")
    urls: dict[str, Any] = Field(default_factory=dict, description="Related URLs.")
    metadata: dict[str, Any] = Field(default_factory=dict, description="Producer metadata.")

    @field_validator(
        "event_id",
        "event_type",
        "correlation_id",
        "occurred_at",
        "account_id",
        mode="before",
    )
    @classmethod
    def _required_non_empty_event_fields(cls, value: Any) -> str:
        text = str(value or "").strip()
        if not text:
            raise ValueError("must be non-empty")
        return text

    @field_validator(
        "workflow_id",
        "group_id",
        "for_each_id",
        "step_id",
        "source",
        "aggregate_type",
        "aggregate_id",
        mode="before",
    )
    @classmethod
    def _normalize_optional_strings(cls, value: Any) -> str | None:
        text = str(value or "").strip()
        return text or None

    @field_validator("step_execution_id", mode="before")
    @classmethod
    def _event_execution_uuid(cls, value: Any) -> str | None:
        text = str(value or "").strip()
        if not text:
            return None
        try:
            return str(uuid.UUID(text))
        except ValueError as exc:
            raise ValueError(STEP_EXECUTION_ID_UUID_ERROR) from exc

    @field_validator("occurred_at", mode="after")
    @classmethod
    def _validate_iso_timestamp(cls, value: str) -> str:
        text = str(value or "").strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError as exc:
            raise ValueError("must be an ISO timestamp") from exc
        return parsed.isoformat()

    @field_validator("summary", mode="before")
    @classmethod
    def _normalize_summary(cls, value: Any) -> str:
        return str(value or "").strip()


def validate_event_envelope_payload(
    payload: dict | None,
) -> tuple[EventEnvelopeV1 | None, list[str]]:
    """Validate dict payload as EventEnvelopeV1 and return normalized model + errors."""
    if not isinstance(payload, dict):
        return None, ["event_envelope payload must be an object"]
    try:
        model = EventEnvelopeV1.model_validate(payload)
        return model, []
    except ValidationError as exc:
        return None, [f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors()]


def build_event_envelope_v1(
    *,
    event_type: str,
    account_id: str,
    correlation_id: str,
    summary: str = "",
    severity: str = "info",
    payload: dict | None = None,
    project_id: Any = None,
    workflow_id: str | None = None,
    job_id: Any = None,
    group_id: str | None = None,
    for_each_id: str | None = None,
    step_id: str | None = None,
    step_execution_id: str | None = None,
    source: str | None = None,
    aggregate_type: str | None = None,
    aggregate_id: str | None = None,
    actor: dict | None = None,
    urls: dict | None = None,
    metadata: dict | None = None,
    event_id: str | None = None,
    occurred_at: str | None = None,
) -> dict[str, Any]:
    """Build and validate the canonical scoped event envelope."""
    import uuid

    envelope = {
        "schema_version": "event_envelope_v1",
        "event_id": str(event_id or "").strip() or f"evt_{uuid.uuid4().hex}",
        "event_type": str(event_type or "").strip(),
        "correlation_id": str(correlation_id or "").strip() or f"corr_{uuid.uuid4().hex}",
        "occurred_at": str(occurred_at or "").strip() or datetime.now(UTC).isoformat(),
        "account_id": str(account_id or "").strip(),
        "severity": str(severity or "info").strip().lower() or "info",
        "summary": str(summary or event_type or "").strip(),
        "payload": payload if isinstance(payload, dict) else {},
        "project_id": str(project_id).strip() if project_id is not None else None,
        "workflow_id": str(workflow_id or "").strip() or None,
        "job_id": str(job_id).strip() if job_id is not None else None,
        "group_id": str(group_id or "").strip() or None,
        "for_each_id": str(for_each_id or "").strip() or None,
        "step_id": str(step_id or "").strip() or None,
        "step_execution_id": str(step_execution_id or "").strip() or None,
        "source": str(source or "").strip() or None,
        "aggregate_type": str(aggregate_type or "").strip() or None,
        "aggregate_id": str(aggregate_id or "").strip() or None,
        "actor": actor if isinstance(actor, dict) and actor else None,
        "urls": urls if isinstance(urls, dict) else {},
        "metadata": metadata if isinstance(metadata, dict) else {},
    }
    model, errors = validate_event_envelope_payload(envelope)
    if errors or model is None:
        raise ValueError("; ".join(errors or ["invalid event envelope"]))
    return model.model_dump()
