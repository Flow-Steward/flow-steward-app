"""First-party spans and measurements of workflow execution and model calls.

``RunTelemetry`` follows one LangChain run tree: the root run is the active
workflow segment, a dispatched step executor is a step attempt, and chat model
and tool runs nest under the closest of those. Node names never decide what a
run is; the step hub's dispatch record does.

Every attribute value is normalized here, before recording: metric attributes
stay within fixed bounded sets, and model, tool and job identity appear only on
spans. Pauses (approvals, waits, child invocations) end their spans without an
error and expose nothing of the interrupt.
"""

from __future__ import annotations

import os
import re
import time
import uuid
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any

from core.application.observability.llm_usage import TokenUsage
from core.application.workflows.workflow_step_kinds import SUPPORTED_STEP_KINDS
from core.infrastructure.observability import otel_instruments as instruments

LLM_PURPOSES = frozenset(
    {"workflow", "classification", "summarization", "guardrail", "replay", "verification"}
)
OUTCOME_OK = "ok"
OUTCOME_ERROR = "error"
OUTCOME_PAUSED = "paused"
OUTCOME_CANCELLED = "cancelled"
OUTCOME_SKIPPED = "skipped"
OTHER = "other"
# The semantic conventions' fallback for an error with no bounded type.
ERROR_TYPE_OTHER = "_OTHER"

# Provider policies of model_provider.get_chat_model; extension and
# OpenAI-compatible routes are "other" by the export contract.
_PROVIDER_CATEGORIES = {
    "anthropic": "anthropic",
    "azure_openai": "azure.ai.openai",
    "google": "gcp.gemini",
    "openai": "openai",
}
_SEGMENT_OUTCOMES = {
    "completed": OUTCOME_OK,
    "failed": OUTCOME_ERROR,
    "stuck": OUTCOME_ERROR,
    "max_iterations": OUTCOME_ERROR,
    "cancelled": OUTCOME_CANCELLED,
}
_STEP_OUTCOMES = {
    "completed": OUTCOME_OK,
    "failed": OUTCOME_ERROR,
    "cancelled": OUTCOME_CANCELLED,
    "skipped": OUTCOME_SKIPPED,
}
_SAFE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:/@+-]{0,127}")
_MAX_PARENT_HOPS = 64

_purpose: ContextVar[str] = ContextVar("flow_steward_llm_purpose", default="workflow")
# The graph node run whose code is executing, and a step attempt opened inside
# it without a callback boundary of its own (a parallel lane step).
_node_scope: ContextVar[tuple[RunTelemetry, str] | None] = ContextVar(
    "flow_steward_telemetry_node", default=None
)
_step_scope: ContextVar[str | None] = ContextVar("flow_steward_telemetry_step", default=None)


@contextmanager
def llm_purpose(purpose: str) -> Iterator[None]:
    """Attribute the model calls made inside the block to *purpose*."""
    token = _purpose.set(purpose if purpose in LLM_PURPOSES else OTHER)
    try:
        yield
    finally:
        _purpose.reset(token)


def current_llm_purpose() -> str:
    return _purpose.get()


def provider_category(provider_type: Any) -> str:
    """The bounded ``gen_ai.provider.name`` of a provider policy."""
    return _PROVIDER_CATEGORIES.get(str(provider_type or "").strip().lower(), OTHER)


def step_kind(value: Any) -> str:
    kind = str(value or "").strip().lower()
    return kind if kind in SUPPORTED_STEP_KINDS else OTHER


def segment_outcome(status: Any) -> str:
    """The outcome of a segment that ended with graph state *status*."""
    text = str(status or "").strip().lower()
    if text.startswith(("awaiting", "waiting")):
        return OUTCOME_PAUSED
    return _SEGMENT_OUTCOMES.get(text, OUTCOME_OK if not text else OTHER)


def step_outcome(public_status: Any) -> str:
    text = str(public_status or "").strip().lower()
    if text.startswith(("awaiting", "waiting")):
        return OUTCOME_PAUSED
    return _STEP_OUTCOMES.get(text, OTHER)


def is_interrupt(error: Any) -> bool:
    """Whether *error* is LangGraph's pause signal rather than a failure."""
    if type(error).__name__ in {"GraphInterrupt", "NodeInterrupt"}:
        return True
    try:
        from langgraph.errors import GraphInterrupt
    except ImportError:  # pragma: no cover - langgraph ships with the runtime
        return False
    return isinstance(error, GraphInterrupt)


def error_type(error: Any) -> str:
    """A bounded ``error.type``; exception text and arbitrary class names never leave."""
    if isinstance(error, TimeoutError) or "timeout" in type(error).__name__.lower():
        return "timeout"
    if "cancel" in type(error).__name__.lower():
        return "cancelled"
    return ERROR_TYPE_OTHER


def _safe_name(value: Any) -> str | None:
    text = str(value or "").strip()
    return text if _SAFE_NAME.fullmatch(text) else None


def dispatched_step(payload: Mapping[str, Any] | None) -> tuple[str, int, str] | None:
    """(step kind, attempt, execution id) when *payload* carries the dispatched step.

    The hub records which execution and attempt it dispatched; the run cursor
    names the one in flight. Only their match is a step attempt. The record
    stays in state until the hub consumes the result, so the hub's own run
    sees it too: ``RunTelemetry.step_started`` counts the first node only.
    """
    if not isinstance(payload, Mapping):
        return None
    cursor = payload.get("run_cursor")
    dispatched = payload.get("dispatched_step_execution")
    if not isinstance(cursor, Mapping) or not isinstance(dispatched, Mapping):
        return None
    if dispatched.get("skipped"):
        return None
    execution_id = str(cursor.get("step_execution_id") or "").strip()
    try:
        attempt = max(1, int(cursor.get("attempt") or 1))
    except (TypeError, ValueError):
        return None
    if not execution_id or (
        str(dispatched.get("step_execution_id") or "").strip(),
        str(dispatched.get("attempt") or ""),
    ) != (execution_id, str(attempt)):
        return None
    return step_kind(dispatched.get("step_kind")), attempt, execution_id


@dataclass
class _OpenRun:
    kind: str
    started: float
    pid: int
    span: Any | None
    measurement: dict[str, str] = field(default_factory=dict)


class RunTelemetry:
    """Spans and measurements of one run tree; every method never raises."""

    def __init__(
        self,
        *,
        job_id: str | None = None,
        parent_carrier: Mapping[str, str] | None = None,
        link_carriers: tuple[Mapping[str, str], ...] = (),
        clock=time.monotonic,
    ) -> None:
        self._job_id = str(job_id or "") or None
        # The job's stored trace context: every segment of the job continues it.
        self._parent_carrier = parent_carrier
        self._link_carriers = link_carriers
        self._clock = clock
        self._parents: dict[str, str | None] = {}
        self._open: dict[str, _OpenRun] = {}
        self._segment_run: str | None = None
        # Step attempts already opened by an executor node of this segment.
        self._claimed_steps: set[tuple[str, int]] = set()

    # -- tree ----------------------------------------------------------------

    def run_started(self, run_id: Any, parent_run_id: Any) -> None:
        key = _key(run_id)
        if key:
            self._parents[key] = _key(parent_run_id)

    def bind_node(self, run_id: Any) -> None:
        """Mark *run_id* as the graph node whose code runs in this context.

        Called from ``on_chain_start``: LangGraph copies the context after the
        callback, so the node's code (and its lane threads) see the binding.
        """
        key = _key(run_id)
        if key:
            _node_scope.set((self, key))

    @property
    def segment_active(self) -> bool:
        return self._segment_run is not None

    def _parent_for_call(self, parent_run_id: Any) -> str | None:
        lane_step = _step_scope.get()
        if lane_step and lane_step in self._open:
            return lane_step
        return _key(parent_run_id)

    def run_forgotten(self, run_id: Any) -> None:
        key = _key(run_id)
        if key and key not in self._open:
            self._parents.pop(key, None)

    def _parent_span(self, parent_run_id: str | None) -> Any | None:
        current = parent_run_id
        for _ in range(_MAX_PARENT_HOPS):
            if not current:
                break
            entry = self._open.get(current)
            if entry is not None and entry.span is not None:
                return entry.span
            current = self._parents.get(current)
        segment = self._open.get(self._segment_run or "")
        return segment.span if segment is not None else None

    def _start(
        self,
        run_id: Any,
        parent_run_id: Any,
        *,
        kind: str,
        name: str,
        span_attributes: Mapping[str, Any],
        measurement: Mapping[str, str],
        client: bool = False,
    ) -> None:
        key = _key(run_id)
        if not key or key in self._open:
            return
        if not (instruments.traces_enabled() or instruments.metrics_enabled()):
            return
        parent = _key(parent_run_id)
        self._parents[key] = parent
        if kind == "segment":
            span = instruments.start_span(
                name,
                context=instruments.remote_parent(self._parent_carrier),
                links=self._link_carriers,
                attributes=span_attributes,
            )
        else:
            span = instruments.start_span(
                name,
                parent=self._parent_span(parent),
                attributes=span_attributes,
                client=client,
            )
        self._open[key] = _OpenRun(kind, self._clock(), os.getpid(), span, dict(measurement))

    def _finish(self, run_id: Any, kind: str) -> tuple[_OpenRun, float] | None:
        key = _key(run_id)
        entry = self._open.get(key or "")
        # A forked child holds copies of its parent's open runs; only the
        # process that opened a run may end it, or the span would export twice.
        if entry is None or entry.kind != kind or entry.pid != os.getpid():
            return None
        del self._open[key or ""]
        self._parents.pop(key or "", None)
        return entry, max(self._clock() - entry.started, 0.0)

    # -- segment and step ------------------------------------------------------

    def segment_started(self, run_id: Any) -> None:
        if self._segment_run is not None:
            return
        self._segment_run = _key(run_id)
        attributes = {"fs.job.id": self._job_id} if self._job_id else {}
        self._start(
            run_id,
            None,
            kind="segment",
            name="fs.workflow.segment",
            span_attributes=attributes,
            measurement={},
        )

    def segment_carrier(self) -> dict[str, str] | None:
        """The trace carrier of the open segment, for a job that had none stored."""
        segment = self._open.get(self._segment_run or "")
        if segment is None or segment.span is None:
            return None
        return instruments.trace_carrier(segment.span)

    def call_span(self, node_run: Any) -> Any | None:
        """The span a call made from *node_run*'s code nests under."""
        return self._parent_span(self._parent_for_call(node_run))

    def segment_finished(self, run_id: Any, *, outcome: str, error: Any = None) -> None:
        finished = self._finish(run_id, "segment")
        if finished is None:
            return
        entry, duration = finished
        self._segment_run = None
        self._end(entry, duration, instruments.SEGMENT_DURATION, outcome=outcome, error=error)

    def step_started(
        self,
        run_id: Any,
        parent_run_id: Any,
        *,
        kind: str,
        attempt: int,
        execution_id: str | None = None,
    ) -> None:
        if execution_id:
            # The executor runs first after a dispatch; later nodes reading the
            # same record (the hub collecting the result) are not the step.
            claim = (execution_id, attempt)
            if claim in self._claimed_steps:
                return
            self._claimed_steps.add(claim)
        self._start(
            run_id,
            parent_run_id,
            kind="step",
            name="fs.workflow.step",
            span_attributes={"fs.step.kind": kind, "fs.step.attempt": attempt},
            measurement={"fs.step.kind": kind},
        )

    def step_finished(self, run_id: Any, *, outcome: str, error: Any = None) -> None:
        finished = self._finish(run_id, "step")
        if finished is not None:
            entry, duration = finished
            self._end(entry, duration, instruments.STEP_DURATION, outcome=outcome, error=error)

    def _end(
        self,
        entry: _OpenRun,
        duration: float,
        instrument: str,
        *,
        outcome: str,
        error: Any,
    ) -> None:
        failed = outcome == OUTCOME_ERROR
        kind_of_error = (
            (error_type(error) if error is not None else ERROR_TYPE_OTHER) if failed else None
        )
        instruments.end_span(
            entry.span,
            error_type=kind_of_error,
            attributes={"fs.outcome": outcome},
        )
        measurement = {**entry.measurement, "fs.outcome": outcome}
        if kind_of_error:
            measurement["error.type"] = kind_of_error
        instruments.record(instrument, duration, measurement)

    # -- model and tool --------------------------------------------------------

    def model_started(
        self,
        run_id: Any,
        parent_run_id: Any,
        *,
        provider_type: Any,
        model: Any,
        purpose: str | None = None,
    ) -> None:
        provider = provider_category(provider_type)
        purpose_value = purpose if purpose in LLM_PURPOSES else current_llm_purpose()
        model_name = _safe_name(model)
        attributes: dict[str, Any] = {
            "gen_ai.operation.name": "chat",
            "gen_ai.provider.name": provider,
            "fs.llm.purpose": purpose_value,
        }
        if model_name:
            attributes["gen_ai.request.model"] = model_name
        self._start(
            run_id,
            self._parent_for_call(parent_run_id),
            kind="model",
            name=f"chat {model_name}" if model_name else "chat",
            span_attributes=attributes,
            measurement={
                "gen_ai.operation.name": "chat",
                "gen_ai.provider.name": provider,
                "fs.llm.purpose": purpose_value,
            },
            client=True,
        )

    def model_finished(
        self,
        run_id: Any,
        *,
        usage: TokenUsage | None = None,
        response_model: Any = None,
        error: Any = None,
    ) -> None:
        finished = self._finish(run_id, "model")
        if finished is None:
            return
        entry, duration = finished
        attributes: dict[str, Any] = {}
        if usage is not None:
            for name, value in (
                ("gen_ai.usage.input_tokens", usage.input_tokens),
                ("gen_ai.usage.output_tokens", usage.output_tokens),
                ("gen_ai.usage.cache_read.input_tokens", usage.cache_read_tokens),
                ("gen_ai.usage.cache_creation.input_tokens", usage.cache_write_tokens),
            ):
                if value is not None:
                    attributes[name] = value
        model_name = _safe_name(response_model)
        if model_name:
            attributes["gen_ai.response.model"] = model_name
        kind_of_error = error_type(error) if error is not None else None
        instruments.end_span(entry.span, error_type=kind_of_error, attributes=attributes)
        measurement = dict(entry.measurement)
        if kind_of_error:
            measurement["error.type"] = kind_of_error
        instruments.record(instruments.OPERATION_DURATION, duration, measurement)
        if usage is None:
            return
        for token_type, value in (("input", usage.input_tokens), ("output", usage.output_tokens)):
            if value is not None:
                instruments.record(
                    instruments.TOKEN_USAGE,
                    value,
                    {**entry.measurement, "gen_ai.token.type": token_type},
                )

    def tool_started(self, run_id: Any, parent_run_id: Any, *, tool_name: Any) -> None:
        name = _safe_name(tool_name)
        attributes: dict[str, Any] = {"gen_ai.operation.name": "execute_tool"}
        if name:
            attributes["gen_ai.tool.name"] = name
        self._start(
            run_id,
            self._parent_for_call(parent_run_id),
            kind="tool",
            name=f"execute_tool {name}" if name else "execute_tool",
            span_attributes=attributes,
            measurement={"gen_ai.operation.name": "execute_tool"},
        )

    def tool_finished(self, run_id: Any, *, error: Any = None) -> None:
        finished = self._finish(run_id, "tool")
        if finished is None:
            return
        entry, duration = finished
        paused = error is not None and is_interrupt(error)
        kind_of_error = error_type(error) if error is not None and not paused else None
        instruments.end_span(entry.span, error_type=kind_of_error)
        measurement = dict(entry.measurement)
        if kind_of_error:
            measurement["error.type"] = kind_of_error
        instruments.record(instruments.OPERATION_DURATION, duration, measurement)


class LaneStepAttempt:
    """The outcome a lane step reports before its attempt span ends."""

    def __init__(self) -> None:
        self.outcome = OUTCOME_OK
        self.error: Any = None

    def failed(self, error: Any = None) -> None:
        if error is not None and is_interrupt(error):
            self.outcome = OUTCOME_PAUSED
            return
        self.outcome = OUTCOME_ERROR
        self.error = error


def active_workflow_telemetry() -> tuple[RunTelemetry, str] | None:
    """The workflow run tree whose node code runs in this context, if its segment is open.

    The binding outlives its node in the context it was set in, so a binding
    whose segment has ended no longer counts.
    """
    scope = _node_scope.get()
    if scope is None or not scope[0].segment_active:
        return None
    return scope


def current_workflow_span() -> Any | None:
    """The innermost open workflow span (step or segment) of the running node code."""
    scope = active_workflow_telemetry()
    if scope is None:
        return None
    telemetry, node_run = scope
    return telemetry.call_span(node_run)


@contextmanager
def lane_step_attempt(kind: Any, attempt: Any) -> Iterator[LaneStepAttempt]:
    """A step attempt run directly by its container node, such as a parallel lane."""
    reported = LaneStepAttempt()
    scope = active_workflow_telemetry()
    if scope is None:
        yield reported
        return
    telemetry, node_run = scope
    run_id = f"lane:{uuid.uuid4().hex}"
    try:
        attempt_number = max(1, int(attempt or 1))
    except (TypeError, ValueError):
        attempt_number = 1
    telemetry.step_started(run_id, node_run, kind=step_kind(kind), attempt=attempt_number)
    token = _step_scope.set(run_id)
    try:
        yield reported
    except BaseException as exc:
        reported.failed(exc)
        raise
    finally:
        _step_scope.reset(token)
        telemetry.step_finished(run_id, outcome=reported.outcome, error=reported.error)


def _key(run_id: Any) -> str | None:
    return str(run_id) if run_id else None


__all__ = [
    "LLM_PURPOSES",
    "OUTCOME_CANCELLED",
    "OUTCOME_ERROR",
    "OUTCOME_OK",
    "OUTCOME_PAUSED",
    "OUTCOME_SKIPPED",
    "LaneStepAttempt",
    "RunTelemetry",
    "active_workflow_telemetry",
    "current_llm_purpose",
    "current_workflow_span",
    "dispatched_step",
    "error_type",
    "is_interrupt",
    "lane_step_attempt",
    "llm_purpose",
    "provider_category",
    "segment_outcome",
    "step_kind",
    "step_outcome",
]
