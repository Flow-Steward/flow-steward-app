"""The step hub: routes one run cursor through the pinned workflow definition.

Every executor returns here. The hub accepts only the result of the exact step
execution and attempt it dispatched, then decides in a fixed order:

1. execution/attempt/cancel fencing and the run's safety guards;
2. contract health of a successful result;
3. the host-owned operation retry decision, when the result carries evidence;
4. the effective authored error policy (step, then its For each, then its
   Group, then ``fail``);
5. the execution graph's single edge for the resulting outcome.

Nothing here reads the live registry, sort order or step config targets; the
next step is always the graph's answer. A Stop step ends the run, a rework edge
spends the durable rework budget, and an exhausted budget pauses for a recovery
approval that can only add budget or abort.
"""

from __future__ import annotations

import time as time
import uuid
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any

from langgraph.types import Command

from core.application.jobs.workflow_definition_snapshot_repository import WorkflowDefinitionPinError
from core.application.orchestration import step_hub_groups as _step_hub_groups
from core.application.orchestration import step_hub_results as _step_hub_results
from core.application.orchestration.active_step import (
    ActiveStep,
    active_step,
    run_cursor,
)
from core.application.orchestration.boundary_contract_runtime import (
    check_entry,
    missing_context_inject,
)
from core.application.orchestration.models import normalize_phase_status
from core.application.orchestration.parallel_group_runtime import (
    LaneState,
)
from core.application.orchestration.run_cursor import (
    TRANSITION_BODY_DONE,
    TRANSITION_BREAK,
    TRANSITION_DISPATCH,
    TRANSITION_ENTER,
    TRANSITION_ESCALATE,
    TRANSITION_LANE_DONE,
    TRANSITION_LANES,
    TRANSITION_STOP,
    Frame,
    FrozenDefinition,
    RunCursor,
    RunCursorError,
    Transition,
    begin_step,
    resolve_after_step,
    resolve_entry,
)
from core.application.orchestration.run_definition import (
    IncompatibleRuntimeStateError,
    RunDefinitionIntegrityError,
    ensure_run_definition,
)
from core.application.orchestration.step_autonomy import autonomy_gate_reason
from core.application.orchestration.step_failures import (
    effective_step_policy,
    unresolved_step_failure_payload,
)
from core.application.orchestration.step_hub_support import (
    APPROVAL_NODE,
    HUB_NODE,
    INVOKE_NODE,
    MAX_TOTAL_ITERATIONS,
    STEP_SKIPPED_EVENT,
    STEP_SKIPPED_REASON,
    VALIDATION_NODE,
    WAIT_NODE,
    UnsupportedStepKindError,
    carry_forward_review_feedback,
    carry_forward_reviewer_comments,
    carry_forward_risk_notes,
    cursor_update,
    disabled_step_can_be_skipped,
    executor_node_for_step,
    graph_end_node,
    journal_entry,
    step_is_disabled,
)
from core.application.orchestration.step_hub_support import RECOVERY_AUTONOMY as RECOVERY_AUTONOMY
from core.application.orchestration.step_hub_support import RECOVERY_FAILURE as RECOVERY_FAILURE
from core.application.orchestration.step_hub_support import (
    RECOVERY_REWORK_BUDGET as RECOVERY_REWORK_BUDGET,
)
from core.application.orchestration.step_hub_support import (
    STOPPED_BY_WORKFLOW_PREFIX as STOPPED_BY_WORKFLOW_PREFIX,
)
from core.application.orchestration.step_hub_support import accepted_message as accepted_message
from core.application.orchestration.step_hub_support import result_outcome as result_outcome
from core.application.orchestration.step_operation_retry import (
    OperationRetryDecision,
    decide_operation_retry,
)
from core.application.orchestration.wait_step_runtime import (
    PENDING_WAIT_KEY,
    WAIT_DURATION_INVALID,
    approval_id_for_execution,
    approval_pending_wait,
    child_pending_wait,
    pending_wait_for,
    resolve_wait_duration,
    timer_pending_wait,
)
from core.application.workflows.execution_graph import (
    OUTCOME_SUCCESS,
    NodeRef,
    Outcome,
)
from core.application.workflows.workflow_definition_validation import (
    ERROR_POLICY_RETRY,
)
from core.application.workflows.workflow_lifecycle_event_sink import (
    publish_active_step_event,
)
from core.application.workflows.workflow_reaction_events import (
    EVENT_STEP_BLOCKED,
    EVENT_STEP_STARTED,
)
from core.application.workflows.workflow_step_kinds import (
    STEP_KIND_APPROVAL,
)

EmitEvent = Callable[..., Any]

_DEFINITION_ERRORS = (
    RunDefinitionIntegrityError,
    IncompatibleRuntimeStateError,
    WorkflowDefinitionPinError,
    RunCursorError,
)


class _StepHub:
    def __init__(
        self,
        state: Mapping[str, Any],
        definition: FrozenDefinition,
        iteration: int,
        base_update: Mapping[str, Any],
        emit_event: EmitEvent,
    ) -> None:
        self.state = state
        self.definition = definition
        self.iteration = iteration
        self.base_update = dict(base_update)
        self.emit = emit_event
        self._trace_facts: list[dict[str, Any] | None] = []
        self._container_patches: list[dict[str, Any] | None] = []

    def operation_retry_decision(
        self, current: ActiveStep, message: Mapping[str, Any]
    ) -> OperationRetryDecision | None:
        return decide_operation_retry(self.state, current, message)

    # --- small helpers ------------------------------------------------------

    def _event(
        self, message: str, detail: Mapping[str, Any] | None = None, *, severity: str = "info"
    ) -> None:
        self.emit(
            self.state,
            "orchestrator",
            "routing_decision",
            message,
            dict(detail or {}),
            severity=severity,
        )

    def capture_container(self, cursor: RunCursor, frame: Frame, **facts: Any) -> None:
        from core.application.orchestration.container_progress import container_patch

        self._container_patches.append(container_patch(cursor, frame, **facts))

    def _command(self, update: Mapping[str, Any], goto: str) -> Command:
        from core.application.orchestration.container_progress import append_container_progress
        from core.application.orchestration.run_trace import append_run_trace

        trace = append_run_trace({**self.state, **self.base_update}, self._trace_facts)
        # Only a route accepted into this Command's trace proves handling.
        for patch in self._container_patches:
            if patch is None or not patch.get("failure") or patch.get("outcome") != "error":
                continue
            scopes = {
                patch["execution_id"],
                *(row["execution_id"] for row in patch["parent_frames"]),
            }
            for fact in (trace or {}).get("transitions", []):
                if (
                    fact.get("container_execution_id") in scopes
                    and fact["outcome"]["name"] == "error"
                ):
                    patch["failure"]["handling"] = {
                        "kind": "route",
                        "execution_id": fact["container_execution_id"],
                        "transition_id": fact["id"],
                    }
        progress = append_container_progress(
            {**self.state, **self.base_update},
            self._container_patches,
            {**self.base_update, **update, "workflow_run_trace": trace},
        )
        return Command(
            update={
                **self.base_update,
                "iteration_count": self.iteration,
                **update,
                **({"workflow_run_trace": trace} if trace is not None else {}),
                **({"workflow_container_progress": progress} if progress is not None else {}),
            },
            goto=goto,
        )

    def _containers(self, current: ActiveStep) -> list[dict[str, Any]]:
        """The step's containers nearest first, for its effective error policy."""
        return self.definition.step_containers(current.step_id)

    def fail(
        self,
        message: str,
        *,
        event: str,
        failure_class: str = "execution",
        step_id: str = "",
        unresolved: Mapping[str, Any] | None = None,
        status: str = "failed",
        extra: Mapping[str, Any] | None = None,
        journal: list[dict[str, Any]] | None = None,
    ) -> Command:
        self.emit(self.state, "orchestrator", "error", message, {"event": event}, severity="error")
        update: dict[str, Any] = {
            **dict(extra or {}),
            "orchestrator_journal": [
                *(journal or []),
                journal_entry(
                    iteration=self.iteration,
                    event=event,
                    step_id=step_id,
                    action=status,
                    reason=message,
                    message=message,
                    detail=dict(unresolved or {}),
                    severity="error",
                ),
            ],
            "needs_user_approval": False,
            "approval_context": None,
            "user_approved": None,
            "status": status,
            "external_effect_resolution_ref": None,
            "error": message,
            "final_report": message,
            "last_failure_class": failure_class,
            "last_retry_reason": message,
            "last_retry_retryable": False,
            "retry_backoff_seconds": 0,
        }
        if unresolved is not None:
            update["unresolved_step_failure"] = dict(unresolved)
        return self._command(update, VALIDATION_NODE)

    # --- routing ------------------------------------------------------------

    def route(self) -> Command:
        pending = self.state.get("recovery_pending")
        if isinstance(pending, Mapping) and pending:
            return self.resume_recovery(dict(pending))
        cursor = run_cursor(self.state)
        if not cursor.step_id and cursor.frames:
            return self.lanes_returned(cursor)
        if not cursor.step_id:
            return self.apply(cursor, resolve_entry(self.definition), reason="entry")
        current = active_step(self.state, definition=self.definition)
        assert current is not None
        if not self._step_test_run_matches(current):
            return self.fail(
                "Step test run is missing its trusted isolated execution context; "
                "start a new step test run.",
                event="step_test_run_context_invalid",
                failure_class="execution_contract",
                step_id=current.step_id,
            )
        message = accepted_message(self.state, current)
        if (
            message is None
            and current.step_kind == STEP_KIND_APPROVAL
            and self.state.get("user_approved") is not None
        ):
            # A paused Approval resumed by state (test runs): the decision is in
            # user_approved and the gate turns it into this execution's result.
            return self._command({}, APPROVAL_NODE)
        if message is None:
            dispatched = self.state.get("dispatched_step_execution")
            if isinstance(dispatched, Mapping) and (
                dispatched.get("step_execution_id"),
                dispatched.get("attempt"),
            ) == (current.step_execution_id, current.attempt):
                if dispatched.get("skipped") == STEP_SKIPPED_REASON:
                    return self.continue_after_skip(cursor)
                payload = unresolved_step_failure_payload(
                    current,
                    status="fail",
                    message={"summary": "The step returned without a result."},
                )
                return self.fail(
                    str(payload["message"]),
                    event="step_result_missing",
                    failure_class="execution_contract",
                    step_id=current.step_id,
                    unresolved=payload,
                )
            # A seeded cursor (step test run) or a resume before dispatch.
            return self.dispatch(cursor, current, reason="resume")
        status = normalize_phase_status(message.get("status"))
        if str(self.state.get("task_source") or "").strip().lower() == "step_test_run":
            return self.finish_step_test_run(current, message, status)
        if status == "pass":
            return self.on_success(cursor, current, message)
        return self.on_failure(cursor, current, message, status)

    def _step_test_run_matches(self, current: ActiveStep) -> bool:
        if str(self.state.get("task_source") or "").strip().lower() != "step_test_run":
            return True
        context = self.state.get("step_test_run_context")
        if not isinstance(context, Mapping):
            return False
        execution_id = str(self.state.get("workflow_test_run_execution_id") or "").strip()
        return bool(
            execution_id
            and str(context.get("workflow_id") or "") == str(self.state.get("workflow_id") or "")
            and str(context.get("execution_id") or "") == execution_id
            and str(context.get("step_id") or "") == current.step_id
        )

    # --- dispatch -----------------------------------------------------------

    def dispatch(
        self,
        cursor: RunCursor,
        current: ActiveStep | None = None,
        *,
        reason: str,
        update: Mapping[str, Any] | None = None,
        skip_autonomy: bool = False,
    ) -> Command:
        current = current or active_step(
            {**self.state, **cursor_update(cursor)}, definition=self.definition
        )
        assert current is not None
        if (
            step_is_disabled(current.step)
            and str(self.state.get("task_source") or "").strip().lower() != "step_test_run"
        ):
            # A step test run is the author testing this one step, so it runs.
            return self.skip_disabled(cursor, current, reason=reason, update=update)
        try:
            node = executor_node_for_step(current.step)
        except UnsupportedStepKindError as exc:
            return self.fail(str(exc), event="step_kind_unsupported", step_id=current.step_id)
        from core.application.orchestration.external_effect_recovery import dispatch_resolution_ref

        try:
            resolution_ref = dispatch_resolution_ref(self.state, current, update)
        except ValueError as exc:
            return self.fail(
                str(exc), event="external_effect_resolution_invalid", step_id=current.step_id
            )
        base = {
            **cursor_update(cursor),
            **dict(update or {}),
            "external_effect_resolution_ref": resolution_ref,
            "dispatched_step_execution": {
                "step_id": current.step_id,
                "step_execution_id": current.step_execution_id,
                "attempt": current.attempt,
            },
            "user_approved": None,
            "review_feedback": carry_forward_review_feedback(self.state),
            "reviewer_comments": carry_forward_reviewer_comments(self.state),
            "risk_notes": carry_forward_risk_notes(self.state),
        }
        kept = self.kept_test_output(current, base)
        if kept is not None:
            return kept
        # Entry inputs are resolved before the executor, against the state this
        # dispatch produces (a loop item published by the same update included).
        entry_state = {**self.state, **dict(update or {})}
        entry = check_entry(current.step, entry_state, owner_path=f"steps.{current.step_id}")
        missing = [*entry.missing, *missing_context_inject(current.step, entry_state)]
        if missing:
            message = f"Step '{current.step_id}' is missing required inputs: {', '.join(missing)}"
            publish_active_step_event(self.state, EVENT_STEP_BLOCKED, current, error=message)
            return self.fail(
                message,
                event="step_input_blocked",
                failure_class="contract",
                step_id=current.step_id,
                unresolved={
                    "status": "blocked",
                    "step_id": current.step_id,
                    "step_execution_id": current.step_execution_id,
                    "attempt": current.attempt,
                    "error_code": entry.selector_failure.get("code") or "missing_required_inputs",
                    "message": message,
                    "missing": missing,
                    **{k: v for k, v in entry.selector_failure.items() if k != "code"},
                },
                extra={**cursor_update(cursor), **entry.updates},
            )
        base.update(entry.updates)
        pending_wait, wait_error = self._pending_wait(current, node, entry_state)
        if wait_error:
            return self.wait_input_failure(cursor, current, wait_error)
        base[PENDING_WAIT_KEY] = pending_wait
        # An authored Approval is itself the human decision and a Wait runs
        # nothing; no autonomy gate in front of either.
        if not skip_autonomy and node not in (APPROVAL_NODE, WAIT_NODE):
            gate_reason, gate_detail = autonomy_gate_reason(current, self.state, node)
            if gate_reason:
                return self.open_recovery(
                    current,
                    kind=RECOVERY_AUTONOMY,
                    reason=gate_reason,
                    detail={**gate_detail, "target_node": node},
                    allowed_actions=["approve", "reject"],
                    update={**base, "dispatched_step_execution": None},
                )
        publish_active_step_event(self.state, EVENT_STEP_STARTED, current)
        self._event(
            f"Dispatching step '{current.step_id}' to {node}",
            {
                **current.message_identity(),
                "step_kind": current.step_kind,
                "node": node,
                "reason": reason,
            },
        )
        journal = list(base.pop("orchestrator_journal", []) or [])
        journal.append(
            journal_entry(
                iteration=self.iteration,
                event="step_dispatch",
                step_id=current.step_id,
                action="dispatch",
                reason=reason,
                message=f"Step '{current.step_id}' attempt {current.attempt} dispatched to {node}",
                detail={**current.message_identity(), "node": node},
            )
        )
        return self._command({**base, "orchestrator_journal": journal}, node)

    def kept_test_output(self, current: ActiveStep, base: Mapping[str, Any]) -> Command | None:
        """In a workflow test run, a step with a kept output returns it instead of running.

        The result is recorded as this execution's passing message and the hub
        accepts it on its next turn, so the step's success edge, guardrails and
        contract checks apply exactly as to a result the step produced itself.
        """
        from core.application.orchestration.kept_test_output_runtime import (
            kept_output_for,
            kept_output_result,
        )

        outputs = kept_output_for(self.state, current)
        if outputs is None:
            return None
        updates, message = kept_output_result(
            {**self.state, **dict(base)}, current, outputs, to_agent=HUB_NODE
        )
        self._event(
            f"Step '{current.step_id}' returned its kept test output",
            {**current.message_identity(), "kept_test_output": True},
        )
        journal = list(dict(base).get("orchestrator_journal") or [])
        journal.append(
            journal_entry(
                iteration=self.iteration,
                event="kept_test_output",
                step_id=current.step_id,
                action="kept_test_output",
                reason="kept_test_output",
                message=f"Step '{current.step_id}' returned its kept test output",
                detail=current.message_identity(),
            )
        )
        return self._command(
            {
                **dict(base),
                **updates,
                PENDING_WAIT_KEY: None,
                "agent_messages": [*(self.state.get("agent_messages") or []), message],
                "orchestrator_journal": journal,
            },
            HUB_NODE,
        )

    def skip_disabled(
        self,
        cursor: RunCursor,
        current: ActiveStep,
        *,
        reason: str,
        update: Mapping[str, Any] | None = None,
    ) -> Command:
        """A disabled step does not run: record the skip and come back to route it.

        Nothing is executed, no step event is published and none of the step's
        outputs or state writes happen. The execution is recorded as dispatched
        to the skip itself and the hub runs again, so the next pass routes the
        step's Next (``success``) edge from committed state exactly as after a
        completed step: the edge appears in the run trace, and a step without
        one ends its branch as a completed step would.
        """
        if not disabled_step_can_be_skipped(current.step):
            return self.fail(
                f"Step '{current.step_id}' is disabled, but only a step with one Next "
                "outcome can be skipped",
                event="step_disabled_unsupported",
                failure_class="execution_contract",
                step_id=current.step_id,
            )
        self._event(
            f"Skipping disabled step '{current.step_id}'",
            {
                **current.message_identity(),
                "step_kind": current.step_kind,
                "reason": reason,
                "skipped": STEP_SKIPPED_REASON,
            },
        )
        base = {**cursor_update(cursor), **dict(update or {})}
        journal = list(base.pop("orchestrator_journal", []) or [])
        journal.append(
            journal_entry(
                iteration=self.iteration,
                event=STEP_SKIPPED_EVENT,
                step_id=current.step_id,
                action="skip",
                reason=STEP_SKIPPED_REASON,
                message=f"Step '{current.step_id}' is disabled; the run skipped it",
                detail={**current.message_identity(), "outcome": OUTCOME_SUCCESS},
            )
        )
        return self._command(
            {
                **base,
                "dispatched_step_execution": {
                    "step_id": current.step_id,
                    "step_execution_id": current.step_execution_id,
                    "attempt": current.attempt,
                    "step_kind": current.step_kind,
                    "skipped": STEP_SKIPPED_REASON,
                },
                "orchestrator_journal": journal,
            },
            HUB_NODE,
        )

    def continue_after_skip(self, cursor: RunCursor) -> Command:
        """Route a skipped step's Next edge, as after a completed step."""
        transition = resolve_after_step(self.definition, cursor, Outcome(OUTCOME_SUCCESS))
        return self.apply(
            cursor,
            transition,
            reason="disabled_step_skipped",
            update={"dispatched_step_execution": None},
        )

    def wait_input_failure(self, cursor: RunCursor, current: ActiveStep, error: str) -> Command:
        """A Wait whose mapped duration is not valid at run time is a failure of
        that step: it goes through the step's effective error policy. The error
        is deterministic, so a retry policy escalates instead of repeating it."""
        from core.application.orchestration.agent_messages import build_agent_message

        timestamp = datetime.now(UTC).isoformat()
        message = build_agent_message(
            from_agent=WAIT_NODE,
            to_agent=HUB_NODE,
            skill="wait",
            status="fail",
            correlation_id=uuid.uuid4().hex,
            caused_by=[],
            artifacts={
                "error_code": WAIT_DURATION_INVALID,
                "failure_class": "contract",
                "retryable": False,
            },
            summary=error,
            errors=[error],
            started_at=timestamp,
            completed_at=timestamp,
            published_at=timestamp,
            **current.message_identity(),
        )
        if str(self.state.get("task_source") or "").strip().lower() == "step_test_run":
            return self.finish_step_test_run(current, message, "fail")
        policy = effective_step_policy(current.step, self._containers(current))
        if policy.on_error == ERROR_POLICY_RETRY:
            return self.open_recovery(
                current,
                kind=RECOVERY_FAILURE,
                reason="The Wait duration is not valid; retrying cannot change it",
                detail={"on_error": policy.on_error, "error_code": WAIT_DURATION_INVALID},
                failure_class="contract",
                allowed_actions=["approve", "reject"],
            )
        return self.on_failure(cursor, current, message, "fail")

    def _pending_wait(
        self, current: ActiveStep, node: str, state: Mapping[str, Any]
    ) -> tuple[dict[str, Any] | None, str]:
        """The durable wait this dispatch records, fixed once per execution."""
        if node not in (APPROVAL_NODE, WAIT_NODE, INVOKE_NODE):
            return None, ""
        existing = pending_wait_for(self.state, current)
        if existing is not None:
            return existing, ""
        ref = self.state.get("workflow_definition_ref")
        revision = int(ref.get("revision") or 0) if isinstance(ref, Mapping) else 0
        if node == APPROVAL_NODE:
            approval_id = approval_id_for_execution(current.step_execution_id)
            return approval_pending_wait(current, approval_id=approval_id, revision=revision), ""
        if node == INVOKE_NODE:
            # A serial invocation waits for its child durably; the node adds the
            # child job and keeps the finite path when called from a lane.
            return child_pending_wait(current, revision=revision), ""
        duration_ms, error = resolve_wait_duration(current.step, state)
        if duration_ms is None:
            return None, f"Wait step '{current.step_id}': {error}"
        return timer_pending_wait(current, duration_ms=duration_ms, revision=revision), ""

    def apply(
        self,
        cursor: RunCursor,
        transition: Transition,
        *,
        reason: str,
        update: Mapping[str, Any] | None = None,
    ) -> Command:
        if transition.edge is not None:
            from core.application.orchestration.run_trace import edge_fact

            self._trace_facts.append(
                edge_fact(
                    self.state,
                    self.definition,
                    transition.edge,
                    cursor,
                    source_frame=transition.source_frame,
                )
            )
        if transition.kind != TRANSITION_ESCALATE:
            # Authored progress: a long linear graph, a long For each body or a
            # budgeted rework is not a runaway, so it leaves the guard alone.
            update = {
                **dict(update or {}),
                "routed_transitions": int(self.state.get("routed_transitions") or 0) + 1,
            }
        if transition.kind == TRANSITION_DISPATCH and transition.step_id:
            begin_step(cursor, transition.step_id)
            return self.dispatch(
                cursor,
                reason=f"{reason}:rework" if transition.rework else reason,
                update={**dict(update or {}), "feedback": None},
            )
        if transition.kind == TRANSITION_STOP:
            return self.stop(cursor, transition, update=update)
        if transition.kind == TRANSITION_ESCALATE:
            return self.escalate_rework_budget(cursor, transition, update=update)
        if transition.kind == TRANSITION_ENTER and transition.container is not None:
            return self.enter_container(cursor, transition.container, reason=reason, update=update)
        if transition.kind == TRANSITION_BODY_DONE and transition.container is not None:
            return self.body_done(cursor, transition.container, update=update)
        if transition.kind == TRANSITION_BREAK and transition.container is not None:
            return self.break_for_each(cursor, transition.container, update=update)
        if transition.kind in {TRANSITION_LANE_DONE, TRANSITION_LANES}:
            # Lanes start and finish inside the container lifecycle and the
            # parallel executor, never by a routed transition.
            raise RunCursorError(f"Transition '{transition.kind}' cannot reach the hub")
        raise RunCursorError(f"Unknown transition '{transition.kind}'")

    # --- containers -----------------------------------------------------------

    def enter_container(
        self,
        cursor: RunCursor,
        node: NodeRef,
        *,
        reason: str,
        update: Mapping[str, Any] | None,
    ) -> Command:
        return _step_hub_groups.enter_container(self, cursor, node, reason=reason, update=update)

    def start_lanes(
        self, cursor: RunCursor, frame: Frame, *, update: Mapping[str, Any] | None
    ) -> Command:
        return _step_hub_groups.start_lanes(self, cursor, frame, update=update)

    def lanes_returned(self, cursor: RunCursor) -> Command:
        return _step_hub_groups.lanes_returned(self, cursor)

    def lanes_completed(self, cursor: RunCursor, frame: Frame) -> Command:
        return _step_hub_groups.lanes_completed(self, cursor, frame)

    def retry_lane_operation(self, cursor: RunCursor, frame: Frame, lane: LaneState) -> Command:
        return _step_hub_groups.retry_lane_operation(self, cursor, frame, lane)

    def open_lane_recovery(self, cursor: RunCursor, frame: Frame, lane: LaneState) -> Command:
        return _step_hub_groups.open_lane_recovery(self, cursor, frame, lane)

    def resume_lane_recovery(self, pending: dict[str, Any]) -> Command:
        return _step_hub_groups.resume_lane_recovery(self, pending)

    def body_done(
        self, cursor: RunCursor, node: NodeRef, *, update: Mapping[str, Any] | None
    ) -> Command:
        return _step_hub_groups.body_done(self, cursor, node, update=update)

    def break_for_each(
        self, cursor: RunCursor, node: NodeRef, *, update: Mapping[str, Any] | None
    ) -> Command:
        return _step_hub_groups.break_for_each(self, cursor, node, update=update)

    def finish_container(
        self,
        cursor: RunCursor,
        node: NodeRef,
        *,
        update: Mapping[str, Any] | None,
        check_exit: bool = True,
    ) -> Command:
        return _step_hub_groups.finish_container(
            self, cursor, node, update=update, check_exit=check_exit
        )

    def container_contract_failure(
        self,
        cursor: RunCursor,
        node: NodeRef,
        missing: list[str],
        *,
        update: Mapping[str, Any] | None,
    ) -> Command:
        return _step_hub_groups.container_contract_failure(
            self, cursor, node, missing, update=update
        )

    def resume_container_contract(self, pending: dict[str, Any]) -> Command:
        return _step_hub_groups.resume_container_contract(self, pending)

    def fail_container(
        self,
        cursor: RunCursor,
        node: NodeRef,
        failure: Mapping[str, Any],
        *,
        journal: list[dict[str, Any]] | None = None,
        update: Mapping[str, Any] | None = None,
        catch: bool = True,
        boundary: str = "body",
    ) -> Command:
        return _step_hub_groups.fail_container(
            self,
            cursor,
            node,
            failure,
            journal=journal,
            update=update,
            catch=catch,
            boundary=boundary,
        )

    def _texts(self, texts: list[str], event: str) -> list[dict[str, Any]]:
        return _step_hub_results._texts(self, texts, event)

    # --- results ------------------------------------------------------------

    def on_success(
        self, cursor: RunCursor, current: ActiveStep, message: Mapping[str, Any]
    ) -> Command:
        return _step_hub_results.on_success(self, cursor, current, message)

    def _outcome_journal(
        self, current: ActiveStep, outcome: Outcome, **detail: Any
    ) -> dict[str, Any]:
        return _step_hub_results._outcome_journal(self, current, outcome, **detail)

    def contract_retry(self, cursor: RunCursor, current: ActiveStep, issues: list[str]) -> Command:
        return _step_hub_results.contract_retry(self, cursor, current, issues)

    def on_failure(
        self, cursor: RunCursor, current: ActiveStep, message: Mapping[str, Any], status: str
    ) -> Command:
        return _step_hub_results.on_failure(self, cursor, current, message, status)

    def retry_operation(self, cursor: RunCursor, current: ActiveStep, operation: Any) -> Command:
        return _step_hub_results.retry_operation(self, cursor, current, operation)

    def finish_step_test_run(
        self, current: ActiveStep, message: Mapping[str, Any], status: str
    ) -> Command:
        return _step_hub_results.finish_step_test_run(self, current, message, status)

    # --- terminal -----------------------------------------------------------

    def stop(
        self, cursor: RunCursor, transition: Transition, *, update: Mapping[str, Any] | None
    ) -> Command:
        return _step_hub_results.stop(self, cursor, transition, update=update)

    # --- recovery approvals -------------------------------------------------

    def unsupported_success_continuation(self, current: ActiveStep) -> Command | None:
        return _step_hub_results.unsupported_success_continuation(self, current)

    def open_recovery(
        self,
        current: ActiveStep,
        *,
        kind: str,
        reason: str,
        detail: Mapping[str, Any] | None = None,
        failure_class: str = "",
        allowed_actions: list[str],
        extra: Mapping[str, Any] | None = None,
        update: Mapping[str, Any] | None = None,
    ) -> Command:
        return _step_hub_results.open_recovery(
            self,
            current,
            kind=kind,
            reason=reason,
            detail=detail,
            failure_class=failure_class,
            allowed_actions=allowed_actions,
            extra=extra,
            update=update,
        )

    def escalate_rework_budget(
        self, cursor: RunCursor, transition: Transition, *, update: Mapping[str, Any] | None
    ) -> Command:
        return _step_hub_results.escalate_rework_budget(self, cursor, transition, update=update)

    def resume_recovery(self, pending: dict[str, Any]) -> Command:
        return _step_hub_results.resume_recovery(self, pending)


def _guard(state: Mapping[str, Any], iteration: int, emit_event: EmitEvent) -> Command | None:
    status = str(state.get("status") or "").strip().lower()
    if status == "cancelled":
        reason = state.get("review_feedback") or "Job cancelled by user."
        emit_event(
            state,
            "orchestrator",
            "routing_decision",
            "Job cancelled — skipping to final validation",
            {"reason": reason},
            severity="warn",
        )
        return Command(
            update={
                "iteration_count": iteration,
                "status": "cancelled",
                "final_report": f"Job cancelled by user: {reason}",
                "recovery_pending": None,
                "external_effect_resolution_ref": None,
                "orchestrator_journal": [
                    journal_entry(
                        iteration=iteration,
                        event="cancelled",
                        action="cancel",
                        reason=str(reason),
                        message=f"Job cancelled: {reason}",
                        severity="warn",
                    )
                ],
            },
            goto=VALIDATION_NODE,
        )
    if (
        status in {"failed", "stuck", "completed"}
        and state.get("workflow_validation_complete") is True
    ):
        report = str(
            state.get("final_report")
            or state.get("error")
            or f"Workflow already reached terminal status: {status}."
        )
        return Command(
            update={
                "iteration_count": iteration,
                "status": status,
                "final_report": report,
                "recovery_pending": None,
                "external_effect_resolution_ref": None,
            },
            goto=graph_end_node(),
        )
    bounded = max(0, int(state.get("bounded_loop_iterations") or 0)) + max(
        0, int(state.get("routed_transitions") or 0)
    )
    if iteration - bounded >= MAX_TOTAL_ITERATIONS:
        message = f"Max iterations ({MAX_TOTAL_ITERATIONS}) reached"
        emit_event(
            state,
            "orchestrator",
            "error",
            f"{message} — failing run",
            {"iteration_count": iteration},
            severity="error",
        )
        return Command(
            update={
                "iteration_count": iteration,
                "status": "stuck",
                "error": message,
                "final_report": message,
                "orchestrator_journal": [
                    journal_entry(
                        iteration=iteration,
                        event="max_iterations",
                        action="stuck",
                        reason=message,
                        message=message,
                        severity="error",
                    )
                ],
            },
            goto=VALIDATION_NODE,
        )
    return None


def orchestrator_hub_node(state: Mapping[str, Any]) -> Command:
    """Route the run cursor; every executor returns here."""
    from core.application.orchestration.agents.common import emit_event

    iteration = int(state.get("iteration_count") or 0) + 1
    guard = _guard(state, iteration, emit_event)
    if guard is not None:
        return guard
    try:
        definition, pinned = ensure_run_definition(state)
    except _DEFINITION_ERRORS as exc:
        hub = _StepHub(state, None, iteration, {}, emit_event)  # type: ignore[arg-type]
        return hub.fail(
            str(exc), event="workflow_definition_unavailable", failure_class="execution_contract"
        )
    hub = _StepHub({**state, **pinned}, definition, iteration, pinned, emit_event)
    try:
        return hub.route()
    except RunCursorError as exc:
        return hub.fail(str(exc), event="run_cursor_integrity", failure_class="execution_contract")


__all__ = [
    "RECOVERY_AUTONOMY",
    "RECOVERY_FAILURE",
    "RECOVERY_REWORK_BUDGET",
    "accepted_message",
    "orchestrator_hub_node",
    "result_outcome",
]
