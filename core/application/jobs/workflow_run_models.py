"""Stable, versioned public workflow-run models."""

from __future__ import annotations

from datetime import UTC, datetime
from itertools import pairwise
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_serializer,
    field_validator,
    model_validator,
)

PublicStepStatus = Literal[
    "in_progress",
    "awaiting_approval",
    "awaiting_timer",
    "awaiting_child",
    "external_wait",
    "completed",
    "failed",
    "cancelled",
    "skipped",
]


class _PublicModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _UtcTimestamps(_PublicModel):
    @field_validator("*", mode="after")
    @classmethod
    def _normalize_datetimes(cls, value: Any) -> Any:
        if not isinstance(value, datetime):
            return value
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)

    @field_serializer("*", when_used="json", check_fields=False)
    def _serialize_datetimes(self, value: Any) -> Any:
        if not isinstance(value, datetime):
            return value
        return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


class PublicWorkflowStepV1(_UtcTimestamps):
    step_id: str
    #: The logical step entry; None for graph nodes that are not authored steps.
    step_execution_id: str | None = None
    attempt: int | None = Field(default=None, ge=1)
    #: The immediate owner: at most one of the two is set, both None at the root.
    group_id: str | None = None
    for_each_id: str | None = None
    lane_id: str | None = None
    #: The For each item index of the execution, when it runs inside a For each.
    iteration: int | None = Field(default=None, ge=0)
    occurrence: int = Field(ge=1)
    display_name: str | None = None
    step_kind: str
    status: PublicStepStatus
    started_at: datetime | None = None
    finished_at: datetime | None = None
    child_job_id: str | None = None


class PublicApprovalGateV1(_UtcTimestamps):
    approval_id: str
    wait_id: str | None = None
    reason: Literal["authored", "recovery", "autonomy"] | None = None
    job_id: str
    thread_id: str
    step_id: str | None = None
    step_execution_id: str | None = None
    group_id: str | None = None
    for_each_id: str | None = None
    message: str = ""
    content: dict[str, Any] = Field(default_factory=dict)
    allowed_actions: list[str] = Field(default_factory=list)
    created_at: datetime
    expires_at: datetime | None = None


class PublicWorkflowResultV1(_PublicModel):
    outputs: dict[str, Any] = Field(default_factory=dict)
    artifacts: dict[str, Any] = Field(default_factory=dict)


class PublicWorkflowErrorV1(_PublicModel):
    code: str
    message: str
    step_id: str | None = None
    step_execution_id: str | None = None
    group_id: str | None = None
    occurrence: int | None = Field(default=None, ge=1)
    category: str
    retryable: bool
    details: dict[str, Any] = Field(default_factory=dict)


_TraceIdentifier = Annotated[str, Field(min_length=1)]


class PublicTraceNodeV1(_PublicModel):
    kind: Literal["step", "group", "for_each"]
    id: _TraceIdentifier


class PublicTraceGroupExitV1(_PublicModel):
    kind: Literal["group_exit"]
    group_id: _TraceIdentifier
    lane_id: _TraceIdentifier | None = None


class PublicTraceForEachExitV1(_PublicModel):
    kind: Literal["for_each_exit"]
    for_each_id: _TraceIdentifier
    lane_id: _TraceIdentifier | None = None


class PublicTraceFrameV1(_PublicModel):
    kind: Literal["group", "for_each"]
    id: _TraceIdentifier
    execution_id: _TraceIdentifier
    item_index: int | None = Field(default=None, ge=0)


class PublicWorkflowTransitionV1(_PublicModel):
    id: Annotated[str, Field(min_length=64, max_length=64)]
    sequence: int = Field(ge=1)
    source: PublicTraceNodeV1
    outcome: dict[str, str]
    target: PublicTraceNodeV1 | PublicTraceGroupExitV1 | PublicTraceForEachExitV1
    step_execution_id: _TraceIdentifier | None = None
    attempt: int | None = Field(default=None, ge=1)
    container_execution_id: _TraceIdentifier | None = None
    lane_id: _TraceIdentifier | None = None
    frames: list[PublicTraceFrameV1] = Field(default_factory=list, max_length=2)
    approval_id: _TraceIdentifier | None = None

    @field_validator("outcome")
    @classmethod
    def _authored_outcome(cls, value: dict[str, str]) -> dict[str, str]:
        from core.application.workflows.execution_graph import OUTCOME_NAMES

        if set(value) - {"name", "case_id"} or value.get("name") not in OUTCOME_NAMES:
            raise ValueError("Trace outcome must be an authored outcome")
        if (value["name"] == "case") != bool(value.get("case_id")):
            raise ValueError("Only a case outcome carries its case id")
        return value

    @model_validator(mode="after")
    def _source_execution(self) -> PublicWorkflowTransitionV1:
        if self.source.kind == "step":
            if not self.step_execution_id or self.attempt is None or self.container_execution_id:
                raise ValueError("Step trace needs its exact execution and attempt")
        elif not self.container_execution_id or self.step_execution_id or self.attempt:
            raise ValueError("Container trace needs its own execution")
        elif not self.frames or (
            self.frames[-1].kind,
            self.frames[-1].id,
            self.frames[-1].execution_id,
        ) != (self.source.kind, self.source.id, self.container_execution_id):
            raise ValueError("Container trace must retain its exact frame")
        return self


class PublicWorkflowTraceV1(_PublicModel):
    schema_version: Literal[1] = 1
    job_id: _TraceIdentifier
    workflow_id: _TraceIdentifier
    snapshot_id: _TraceIdentifier
    revision: int = Field(ge=1)
    digest: _TraceIdentifier
    covered_from_start: bool
    # Only a committed terminal checkpoint can establish final trace coverage.
    terminal: bool = False
    truncated: bool = False
    last_sequence: int = Field(default=0, ge=0)
    transitions: list[PublicWorkflowTransitionV1] = Field(default_factory=list, max_length=1000)

    @model_validator(mode="after")
    def _ordered_facts(self) -> PublicWorkflowTraceV1:
        sequences = [fact.sequence for fact in self.transitions]
        if sequences != sorted(set(sequences)) or len(
            {fact.id for fact in self.transitions}
        ) != len(sequences):
            raise ValueError("Trace identities and sequences must be unique and ordered")
        if self.last_sequence != (sequences[-1] if sequences else 0):
            raise ValueError("Trace sequence must describe the retained tail")
        if sequences and sequences[0] > 1 and not self.truncated:
            raise ValueError("An omitted trace prefix must be explicit")
        if any(right != left + 1 for left, right in pairwise(sequences)):
            raise ValueError("A trace tail cannot hide sequence gaps")
        return self


class PublicContainerNodeV1(_PublicModel):
    kind: Literal["group", "for_each"]
    id: _TraceIdentifier


class PublicContainerParentV1(_PublicModel):
    kind: Literal["group", "for_each"]
    id: _TraceIdentifier
    execution_id: _TraceIdentifier
    item_index: int | None = Field(default=None, ge=0)


class PublicContainerItemsV1(_PublicModel):
    item_index: int | None = Field(default=None, ge=0)
    source_count: int = Field(ge=0)
    selected_count: int = Field(ge=0)
    processed_count: int = Field(ge=0)
    applied_limit: int = Field(gt=0)
    truncated: bool
    broke_at: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def _selection(self) -> PublicContainerItemsV1:
        if self.selected_count != min(self.source_count, self.applied_limit):
            raise ValueError("Container selection must retain its actual limit")
        if self.truncated != (self.source_count > self.applied_limit):
            raise ValueError("Container selection truncation is explicit")
        if self.processed_count > self.selected_count:
            raise ValueError("Processed items cannot exceed selection")
        if (self.item_index is None) != (self.selected_count == 0):
            raise ValueError("Only a nonempty selection has an item")
        if self.item_index is not None and self.item_index >= self.selected_count:
            raise ValueError("Item must belong to the selected pass")
        if self.broke_at is not None and (
            self.broke_at != self.item_index or self.processed_count != self.broke_at + 1
        ):
            raise ValueError("Break must retain its exact traversed item")
        return self


class PublicContainerLaneV1(_PublicModel):
    lane_id: _TraceIdentifier
    status: Literal["pending", "running", "completed", "failed", "escalated", "retry_operation"]


class PublicContainerLanesV1(_PublicModel):
    item_index: int | None = Field(default=None, ge=0)
    rows: list[PublicContainerLaneV1]
    completed_count: int = Field(ge=0)
    total_count: int = Field(ge=0)

    @model_validator(mode="after")
    def _inventory(self) -> PublicContainerLanesV1:
        if len({row.lane_id for row in self.rows}) != len(self.rows):
            raise ValueError("Duplicate container lane")
        if self.total_count != len(self.rows) or self.completed_count != sum(
            row.status == "completed" for row in self.rows
        ):
            raise ValueError("Lane counts must describe the complete inventory")
        return self


class PublicContainerHandlingV1(_PublicModel):
    kind: Literal["route", "exit_approval"]
    execution_id: _TraceIdentifier
    transition_id: Annotated[str, Field(min_length=64, max_length=64)] | None = None
    request_id: _TraceIdentifier | None = None

    @model_validator(mode="after")
    def _proof(self) -> PublicContainerHandlingV1:
        if self.kind == "route" and (not self.transition_id or self.request_id):
            raise ValueError("Handling needs its accepted transition")
        if self.kind == "exit_approval" and (not self.request_id or self.transition_id):
            raise ValueError("Recovery needs its accepted request")
        return self


class PublicContainerFailureV1(_PublicModel):
    code: str = Field(max_length=500)
    message: str = Field(max_length=500)
    boundary: Literal["entry", "selection", "exit", "parallel", "body"]
    step_id: _TraceIdentifier | None = None
    step_execution_id: _TraceIdentifier | None = None
    attempt: int | None = Field(default=None, ge=1)
    lane_id: _TraceIdentifier | None = None
    item_index: int | None = Field(default=None, ge=0)
    handling: PublicContainerHandlingV1 | None = None


class PublicContainerPassV1(_PublicModel):
    container: PublicContainerNodeV1
    execution_id: _TraceIdentifier
    parent_frames: list[PublicContainerParentV1] = Field(default_factory=list)
    started_sequence: int = Field(ge=1)
    updated_sequence: int = Field(ge=1)
    finished_sequence: int | None = Field(default=None, ge=1)
    current: bool = False
    outcome: Literal["success", "error"] | None = None
    for_each: PublicContainerItemsV1 | None = None
    lanes: PublicContainerLanesV1 | None = None
    failure: PublicContainerFailureV1 | None = None

    @model_validator(mode="after")
    def _pass(self) -> PublicContainerPassV1:
        if self.updated_sequence < self.started_sequence:
            raise ValueError("Container updates cannot precede their start")
        if (self.outcome is None) != (self.finished_sequence is None):
            raise ValueError("Only an explicit finish has an outcome")
        if self.finished_sequence is not None and not (
            self.started_sequence <= self.finished_sequence <= self.updated_sequence
        ):
            raise ValueError("Container finish is out of order")
        if self.current and self.outcome is not None:
            raise ValueError("A finished pass is not current")
        if self.outcome == "error" and self.failure is None:
            raise ValueError("A failed pass needs its captured boundary failure")
        if (
            self.container.kind == "for_each"
            and self.outcome == "success"
            and self.for_each is None
        ):
            raise ValueError("Successful For each must retain its selected pass")
        if self.container.kind != "for_each" and self.for_each is not None:
            raise ValueError("Group has no item counts")
        if self.lanes is not None and self.lanes.item_index != (
            self.for_each.item_index if self.for_each else None
        ):
            raise ValueError("Lanes belong to one exact item")
        return self


class PublicContainerProgressV1(_PublicModel):
    schema_version: Literal[1] = 1
    job_id: _TraceIdentifier
    project_id: _TraceIdentifier
    workflow_id: _TraceIdentifier
    snapshot_id: _TraceIdentifier
    revision: int = Field(ge=1)
    digest: _TraceIdentifier
    covered_from_start: bool
    truncated: bool = False
    last_sequence: int = Field(default=0, ge=0)
    checkpoint_terminal: bool = False
    passes: list[PublicContainerPassV1] = Field(default_factory=list, max_length=1000)

    @model_validator(mode="after")
    def _passes(self) -> PublicContainerProgressV1:
        if len({row.execution_id for row in self.passes}) != len(self.passes):
            raise ValueError("Container passes must be unique")
        starts = [row.started_sequence for row in self.passes]
        if starts != sorted(set(starts)):
            raise ValueError("Container starts must remain unique and ordered")
        if (
            not self.passes
            and self.last_sequence
            and self.covered_from_start
            and not self.truncated
        ):
            raise ValueError("A complete empty run cannot hide container facts")
        if any(row.updated_sequence > self.last_sequence for row in self.passes):
            raise ValueError("Container pass exceeds its committed sequence")
        if self.checkpoint_terminal and any(row.current for row in self.passes):
            raise ValueError("A terminal checkpoint has no current container")
        return self


class WorkflowRunStateV1(_UtcTimestamps):
    schema_version: Literal["1"] = "1"
    job_id: str
    project_id: str
    workflow_id: str
    status: str
    terminal: bool
    #: The step the run waits on or runs serially; None in a parallel group.
    current_step_id: str | None = None
    current_group_id: str | None = None
    active_steps: list[PublicWorkflowStepV1] = Field(default_factory=list)
    steps: list[PublicWorkflowStepV1] = Field(default_factory=list)
    # Unknown for old runs; independent from retained step-row completeness.
    run_trace: PublicWorkflowTraceV1 | None = None
    container_progress: PublicContainerProgressV1 | None = None
    approval: PublicApprovalGateV1 | None = None
    result: PublicWorkflowResultV1 | None = None
    error: PublicWorkflowErrorV1 | None = None
    queued_at: datetime | None = None
    started_at: datetime | None = None
    updated_at: datetime
    finished_at: datetime | None = None

    @model_validator(mode="after")
    def _terminal_payload_semantics(self) -> WorkflowRunStateV1:
        if not self.terminal:
            if self.finished_at is not None or self.result is not None or self.error is not None:
                raise ValueError("non-terminal workflow runs cannot expose terminal fields")
        elif self.status == "completed":
            if self.result is None or self.error is not None:
                raise ValueError("completed workflow runs require only result")
        elif self.result is not None:
            raise ValueError("unsuccessful workflow runs cannot expose result")
        return self


class PublicSnapshotRevision(_PublicModel):
    snapshot: WorkflowRunStateV1
    revision: int = Field(ge=1)
    marker_id: str | None = None
    changed: bool


__all__ = [
    "PublicApprovalGateV1",
    "PublicSnapshotRevision",
    "PublicStepStatus",
    "PublicWorkflowErrorV1",
    "PublicWorkflowResultV1",
    "PublicWorkflowStepV1",
    "PublicWorkflowTraceV1",
    "PublicWorkflowTransitionV1",
    "WorkflowRunStateV1",
]
