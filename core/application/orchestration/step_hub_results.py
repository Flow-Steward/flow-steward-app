"""Step results, retry decisions, and operator recovery routing."""

from __future__ import annotations

import time
import uuid
from collections.abc import Mapping
from dataclasses import replace
from typing import TYPE_CHECKING, Any

from langgraph.types import Command

from core.application.orchestration.active_step import (
    ActiveStep,
    active_step,
    run_cursor,
)
from core.application.orchestration.boundary_contract_runtime import (
    exit_applies,
    missing_exit_outputs,
)
from core.application.orchestration.external_effect_recovery import (
    CARRIER,
    RECOVERY_KIND,
    approval_return_ref,
    direct_error_association,
)
from core.application.orchestration.run_cursor import (
    TRANSITION_ESCALATE,
    RunCursor,
    RunCursorError,
    Transition,
    grant_rework_budget,
    resolve_after_container,
    resolve_after_step,
    retry_step,
)
from core.application.orchestration.step_failures import (
    FAILED_STATUSES,
    effective_step_policy,
    infer_failure_class,
    normalize_failure_class,
    unresolved_step_failure_payload,
)
from core.application.orchestration.step_guardrails import (
    GUARDRAIL_ESCALATE,
    GUARDRAIL_RETRY,
    check_step_guardrails,
)
from core.application.orchestration.step_hub_support import (
    APPROVAL_NODE,
    DEFAULT_CONTRACT_RETRY_BUDGET,
    OPERATION_RETRY_NODE,
    RECOVERY_AUTONOMY,
    RECOVERY_FAILURE,
    RECOVERY_GROUP_CONTRACT,
    RECOVERY_LANE,
    RECOVERY_REWORK_BUDGET,
    STOPPED_BY_WORKFLOW_PREFIX,
    VALIDATION_NODE,
    _contract_issues,
    _policy_int,
    _retry_backoff_seconds,
    _structured_contract_issues,
    accepted_message,
    cursor_update,
    executor_node_for_step,
    journal_entry,
    result_outcome,
    retry_continuation_updates,
)
from core.application.orchestration.step_operation_retry import (
    ACTION_RETRY_OPERATION,
    _host_executor_message,
    has_unknown_external_effect,
    trusted_operation_retry_signal,
)
from core.application.workflows.execution_graph import (
    OUTCOME_ERROR,
    OUTCOME_SUCCESS,
    NodeRef,
    Outcome,
)
from core.application.workflows.workflow_definition_validation import (
    ERROR_POLICY_AWAIT_APPROVAL,
    ERROR_POLICY_CONTINUE,
    ERROR_POLICY_RETRY,
    ERROR_POLICY_ROUTE_TO_STEP,
)
from core.application.workflows.workflow_lifecycle_event_sink import (
    publish_active_step_event,
    publish_step_event,
)
from core.application.workflows.workflow_reaction_events import (
    EVENT_STEP_COMPLETED,
    EVENT_STEP_FAILED,
    EVENT_STEP_STARTED,
)
from core.application.workflows.workflow_step_kinds import (
    STEP_KIND_STOP_SUCCESS,
)

if TYPE_CHECKING:
    from core.application.orchestration.step_hub import _StepHub


def _texts(hub: _StepHub, texts: list[str], event: str) -> list[dict[str, Any]]:
    return [
        journal_entry(iteration=hub.iteration, event=event, message=text)
        for text in texts
        if str(text or "").strip()
    ]


def on_success(
    hub: _StepHub, cursor: RunCursor, current: ActiveStep, message: Mapping[str, Any]
) -> Command:
    if has_unknown_external_effect(current, message):
        return on_failure(hub, cursor, current, message, "fail")
    guardrails = check_step_guardrails(hub.state, current, message, emit_event=hub.emit)
    passed_update: dict[str, Any] = {}
    if guardrails is not None and guardrails.action == GUARDRAIL_RETRY:
        retry_step(cursor)
        return hub.dispatch(
            cursor,
            reason="guardrail_retry",
            skip_autonomy=True,
            update={
                **retry_continuation_updates(),
                **guardrails.update,
                "feedback": guardrails.feedback,
                "orchestrator_journal": [
                    journal_entry(
                        iteration=hub.iteration,
                        event="guardrail_retry",
                        step_id=current.step_id,
                        action="retry",
                        reason=guardrails.reason,
                        message=f"Guardrail violations for step '{current.step_id}' — regenerating",
                        detail=guardrails.detail,
                        severity="warn",
                    )
                ],
            },
        )
    if guardrails is not None and guardrails.action == GUARDRAIL_ESCALATE:
        return hub.open_recovery(
            current,
            kind=RECOVERY_FAILURE,
            reason=f"Guardrail violations:\n{guardrails.feedback}",
            detail=guardrails.detail,
            failure_class="validation",
            allowed_actions=["approve", "reject"],
            update=guardrails.update,
        )
    if guardrails is not None:
        passed_update = dict(guardrails.update)
    # Approval never bypasses contract health; only a regenerated result does.
    issues = _contract_issues(message)
    if issues:
        return hub.contract_retry(cursor, current, issues)
    try:
        outcome = result_outcome(message)
    except RunCursorError as exc:
        return hub.fail(str(exc), event="step_transition_invalid", step_id=current.step_id)
    if exit_applies(current.step, outcome):
        missing_outputs = missing_exit_outputs(current.step, hub.state, [message])
        if missing_outputs:
            return hub.contract_retry(
                cursor,
                current,
                [f"missing required output '{name}'" for name in missing_outputs],
            )
    try:
        transition = resolve_after_step(hub.definition, cursor, outcome)
    except RunCursorError as exc:
        return hub.fail(str(exc), event="step_transition_invalid", step_id=current.step_id)
    try:
        resolution_ref = approval_return_ref(
            hub.state, current, transition, approved=outcome.name == "approved"
        )
    except ValueError as exc:
        return hub.fail(
            str(exc), event="external_effect_resolution_invalid", step_id=current.step_id
        )
    publish_active_step_event(hub.state, EVENT_STEP_COMPLETED, current, outcome=outcome.name)
    return hub.apply(
        cursor,
        transition,
        reason=f"{current.step_id}:{outcome.name}",
        update={
            **passed_update,
            CARRIER: resolution_ref,
            "orchestrator_journal": [hub._outcome_journal(current, outcome)],
        },
    )


def _outcome_journal(
    hub: _StepHub, current: ActiveStep, outcome: Outcome, **detail: Any
) -> dict[str, Any]:
    return journal_entry(
        iteration=hub.iteration,
        event="step_outcome",
        step_id=current.step_id,
        action=outcome.name,
        message=f"Step '{current.step_id}' finished with outcome '{outcome.name}'",
        detail={**current.message_identity(), "outcome": outcome.dump(), **detail},
    )


def contract_retry(
    hub: _StepHub, cursor: RunCursor, current: ActiveStep, issues: list[str]
) -> Command:
    budget = _policy_int(hub.state, "retry_max_attempts", DEFAULT_CONTRACT_RETRY_BUDGET)
    details = "; ".join(issues[:6])
    if cursor.attempt > budget:
        payload = unresolved_step_failure_payload(
            current, status="fail", message={"summary": f"Contract errors: {details}"}
        )
        publish_active_step_event(
            hub.state, EVENT_STEP_FAILED, current, error=f"Contract errors: {details}"
        )
        return hub.fail(
            f"Contract-health gate failed for step '{current.step_id}' after "
            f"{budget} retries: {details}",
            event="step_contract_health_failed",
            failure_class="contract",
            step_id=current.step_id,
            unresolved=payload,
        )
    retry_step(cursor)
    return hub.dispatch(
        cursor,
        reason="contract_health_retry",
        skip_autonomy=True,
        update={
            **retry_continuation_updates(),
            "feedback": (
                "Contract-health gate blocked progression for this step.\n"
                "The previous output has schema/contract issues and must be regenerated.\n"
                f"Issues: {details}"
            ),
            "last_failure_class": "contract",
            "last_retry_reason": "contract_health_gate",
            "last_retry_retryable": False,
        },
    )


def on_failure(
    hub: _StepHub,
    cursor: RunCursor,
    current: ActiveStep,
    message: Mapping[str, Any],
    status: str,
    *,
    terminal_reason: str = "",
) -> Command:
    if status not in FAILED_STATUSES:
        status = "fail"
    if (
        current.step_kind == "connector"
        and hub.state.get(CARRIER) is not None
        and _host_executor_message(current, message)
        and message.get("skill") == "connector"
        and isinstance(message.get("artifacts"), Mapping)
        and message["artifacts"].get("step_kind") == "connector"
    ):
        summary = str(
            message.get("summary") or "Bound external operation retry refused before claim"
        )
        publish_active_step_event(hub.state, EVENT_STEP_FAILED, current, error=summary)
        return hub.fail(
            summary,
            event="external_effect_retry_refused",
            failure_class="contract",
            step_id=current.step_id,
            unresolved=unresolved_step_failure_payload(current, status=status, message=message),
        )
    unknown = has_unknown_external_effect(current, message)
    signal = trusted_operation_retry_signal(current, message)
    transport = unknown or bool(signal)
    # Host transport failure is not a response schema that regeneration can fix.
    # Other failures retain their existing contract/retry/error-policy ordering.
    if not transport:
        contract_issues = _structured_contract_issues(message)
        if contract_issues:
            return hub.contract_retry(cursor, current, contract_issues)
    operation = None if unknown else hub.operation_retry_decision(current, message)
    if operation is not None and operation.action == ACTION_RETRY_OPERATION and not terminal_reason:
        return hub.retry_operation(cursor, current, operation)
    if operation is not None and not transport:
        return hub.open_recovery(
            current,
            kind=RECOVERY_FAILURE,
            reason=operation.reason,
            detail={"retry_reason_code": operation.reason_code, **operation.telemetry},
            failure_class=normalize_failure_class(operation.failure_class),
            allowed_actions=["approve", "reject"],
        )
    failure_class, retryable = infer_failure_class(message)
    if transport:
        failure_class = normalize_failure_class(
            operation.failure_class if operation is not None else signal.get("failure_class")
        )
    policy = effective_step_policy(current.step, hub._containers(current))
    if transport and policy.on_error == ERROR_POLICY_RETRY:
        if unknown:
            reason = "external_effect_ambiguous"
        elif terminal_reason:
            reason = terminal_reason
        elif operation is not None:
            reason = operation.reason
        else:
            reason = "host_operation_not_retryable"
        message = {**message, "summary": reason}
    detail = {
        "on_error": policy.on_error,
        "policy_source": policy.source,
        "original_status": status,
    }
    if transport and operation is not None:
        detail.update(
            {"retry_reason_code": terminal_reason or operation.reason_code, **operation.telemetry}
        )
    if policy.on_error == ERROR_POLICY_CONTINUE:
        unsupported = hub.unsupported_success_continuation(current)
        if unsupported is not None:
            return unsupported
        outcome = Outcome(OUTCOME_SUCCESS)
        try:
            transition = resolve_after_step(hub.definition, cursor, outcome)
        except RunCursorError as exc:
            return hub.fail(str(exc), event="step_transition_invalid", step_id=current.step_id)
        publish_active_step_event(
            hub.state,
            EVENT_STEP_FAILED,
            current,
            error=str(message.get("summary") or ""),
            outcome="continued",
        )
        return hub.apply(
            cursor,
            transition,
            reason=f"{current.step_id}:continued_error",
            update={
                "orchestrator_journal": [
                    hub._outcome_journal(
                        current,
                        outcome,
                        _step_error_policy={
                            "on_error": "continue",
                            "applied": True,
                            "original_status": status,
                        },
                    )
                ]
            },
        )
    if policy.on_error == ERROR_POLICY_RETRY and not transport:
        if not policy.retry_matches(failure_class):
            return hub.open_recovery(
                current,
                kind=RECOVERY_FAILURE,
                reason=f"step error_policy.retry_on does not match failure class ('{failure_class}')",
                detail=detail,
                failure_class=failure_class,
                allowed_actions=["approve", "reject"],
            )
        if policy.max_retries > 0 and cursor.attempt > policy.max_retries:
            return hub.open_recovery(
                current,
                kind=RECOVERY_FAILURE,
                reason=f"step error_policy.max_retries exhausted ({policy.max_retries})",
                detail=detail,
                failure_class=failure_class,
                allowed_actions=["approve", "reject"],
            )
        backoff = max(
            _retry_backoff_seconds(
                hub.state,
                failure_class=failure_class,
                retryable=retryable,
                attempt=cursor.attempt,
            ),
            policy.backoff_seconds,
        )
        if backoff > 0:
            time.sleep(backoff)
        retry_step(cursor)
        return hub.dispatch(
            cursor,
            reason="error_policy_retry",
            skip_autonomy=True,
            update={
                **retry_continuation_updates(),
                "feedback": str(message.get("summary") or "").strip() or None,
                "last_failure_class": failure_class,
                "last_retry_reason": "step error_policy.on_error=retry",
                "last_retry_retryable": True,
                "retry_backoff_seconds": backoff,
            },
        )
    if policy.on_error == ERROR_POLICY_ROUTE_TO_STEP:
        if policy.source in {"group", "for_each"} and policy.source_id:
            # Inherited from a container: the step has no error edge of its own;
            # the container that declared it fails as a whole and leaves by its
            # own error edge (frames inside it are removed, those around stay).
            payload = unresolved_step_failure_payload(current, status=status, message=message)
            publish_active_step_event(
                hub.state, EVENT_STEP_FAILED, current, error=str(payload.get("message") or "")
            )
            return hub.fail_container(
                cursor,
                NodeRef(policy.source, policy.source_id),
                {
                    "code": payload.get("error_code"),
                    "message": payload.get("message"),
                    "step_id": current.step_id,
                    "step_execution_id": current.step_execution_id,
                    "attempt": current.attempt,
                    "item_index": current.item_index,
                    "policy_source": {"kind": policy.source, "id": policy.source_id},
                },
            )
        try:
            transition = resolve_after_step(hub.definition, cursor, Outcome(OUTCOME_ERROR))
        except RunCursorError as exc:
            return hub.fail(str(exc), event="step_transition_invalid", step_id=current.step_id)
        association = (
            direct_error_association(hub.state, current, message, transition, hub.definition)
            if unknown
            else None
        )
        publish_active_step_event(
            hub.state, EVENT_STEP_FAILED, current, error=str(message.get("summary") or "")
        )
        return hub.apply(
            cursor,
            transition,
            reason=f"{current.step_id}:error",
            update={
                CARRIER: association,
                "orchestrator_journal": [
                    hub._outcome_journal(current, Outcome(OUTCOME_ERROR), **detail)
                ],
            },
        )
    if policy.on_error == ERROR_POLICY_AWAIT_APPROVAL:
        return hub.open_recovery(
            current,
            kind=RECOVERY_FAILURE,
            reason="step error_policy.on_error=await_approval",
            detail=detail,
            failure_class=failure_class,
            allowed_actions=["approve", "reject"],
        )
    payload = unresolved_step_failure_payload(current, status=status, message=message)
    publish_active_step_event(hub.state, EVENT_STEP_FAILED, current, error=str(payload["message"]))
    if cursor.frames:
        # An unhandled failure leaves its innermost container; an enclosing
        # container's own route_to_step may still catch it (ADR 0038).
        return hub.fail_container(
            cursor,
            cursor.frames[-1].node,
            {
                "code": payload.get("error_code") or "unhandled_step_failure",
                "message": payload.get("message"),
                "step_id": current.step_id,
                "step_execution_id": current.step_execution_id,
                "attempt": current.attempt,
                "item_index": current.item_index,
                "policy_source": {"kind": policy.source, "id": policy.source_id},
                "failure_class": failure_class,
            },
        )
    return hub.fail(
        str(payload["message"]),
        event="unhandled_step_failure",
        failure_class=failure_class,
        step_id=current.step_id,
        unresolved=payload,
    )


def retry_operation(
    hub: _StepHub, cursor: RunCursor, current: ActiveStep, operation: Any
) -> Command:
    remaining = operation.deadline_epoch_seconds - time.time()
    if (
        operation.deadline_epoch_seconds <= 0
        or remaining <= 0
        or operation.delay_seconds > remaining
    ):
        message = accepted_message(hub.state, current)
        if message is None or not trusted_operation_retry_signal(current, message):
            return hub.fail(
                "Operation retry result is unavailable", event="operation_retry_invalid"
            )
        return on_failure(
            hub, cursor, current, message, "fail", terminal_reason="retry_delay_exceeds_deadline"
        )
    rows = hub.state.get("operation_retry_state")
    rows = {key: dict(value) for key, value in rows.items()} if isinstance(rows, Mapping) else {}
    rows[operation.identity] = {
        "retry_count": operation.retry_number,
        "deadline_epoch_seconds": operation.deadline_epoch_seconds,
        "step_execution_id": current.step_execution_id,
    }
    node = executor_node_for_step(current.step)
    retry_step(cursor)
    publish_step_event(
        hub.state,
        EVENT_STEP_STARTED,
        step_id=current.step_id,
        step_execution_id=current.step_execution_id,
        attempt=cursor.attempt,
        group_id=current.group_id,
        for_each_id=current.for_each_id,
    )
    hub._event(
        f"Retrying operation '{operation.identity}' (retry {operation.retry_number})",
        {
            **current.message_identity(),
            "operation_retry_identity": operation.identity,
            "target": node,
        },
        severity="warn",
    )
    return hub._command(
        {
            **retry_continuation_updates(),
            **cursor_update(cursor),
            "dispatched_step_execution": {
                "step_id": current.step_id,
                "step_execution_id": current.step_execution_id,
                "attempt": cursor.attempt,
            },
            "operation_retry_state": rows,
            "operation_retry_dispatch": {
                "identity": operation.identity,
                "deadline_epoch_seconds": operation.deadline_epoch_seconds,
                "step_id": current.step_id,
                "step_execution_id": current.step_execution_id,
                "attempt": cursor.attempt,
                "target_actor": operation.target_actor,
                "target": node,
                "lane_id": operation.lane_id,
                "position": operation.position,
                "delay_seconds": operation.delay_seconds,
                "retry_signal": dict(operation.signal),
            },
            "last_failure_class": operation.failure_class,
            "last_retry_reason": operation.reason or "operation_retry",
            "last_retry_retryable": True,
            "retry_backoff_seconds": operation.delay_seconds,
            "retry_telemetry": dict(operation.telemetry) or None,
            "orchestrator_journal": [
                journal_entry(
                    iteration=hub.iteration,
                    event="retry_operation",
                    step_id=current.step_id,
                    action="retry_operation",
                    reason=operation.reason or "operation_retry",
                    message=f"Retrying operation '{operation.identity}' for step '{current.step_id}'",
                    detail={
                        "operation_retry_identity": operation.identity,
                        "retry_number": operation.retry_number,
                        "backoff_seconds": operation.delay_seconds,
                        **dict(operation.telemetry),
                    },
                    severity="warn",
                )
            ],
        },
        OPERATION_RETRY_NODE,
    )


def finish_step_test_run(
    hub: _StepHub, current: ActiveStep, message: Mapping[str, Any], status: str
) -> Command:
    """A step test run is an isolated boundary: it never follows the graph."""
    if status != "pass":
        payload = unresolved_step_failure_payload(current, status=status, message=message)
        return hub.fail(
            str(payload["message"]),
            event="step_test_run_failed",
            step_id=current.step_id,
            unresolved=payload,
        )
    summary = str(message.get("summary") or "").strip()
    return hub._command(
        {
            "status": "completed",
            "final_report": summary or "Step test run completed successfully.",
            "step_test_run_terminal": {
                "status": "pass",
                "step_id": current.step_id,
                "step_execution_id": current.step_execution_id,
                "step_kind": current.step_kind,
                "execution_id": str(hub.state.get("workflow_test_run_execution_id") or "").strip(),
            },
        },
        VALIDATION_NODE,
    )


def stop(
    hub: _StepHub, cursor: RunCursor, transition: Transition, *, update: Mapping[str, Any] | None
) -> Command:
    step = hub.definition.step(str(transition.step_id))
    succeeded = transition.outcome == "success"
    terminal = {
        "kind": str(step.get("step_kind") or ""),
        "status": "pass" if succeeded else "fail",
        "step_id": transition.step_id,
        "step_execution_id": cursor.step_execution_id,
    }
    journal = list(dict(update or {}).get("orchestrator_journal") or [])
    if succeeded and terminal["kind"] == STEP_KIND_STOP_SUCCESS:
        hub._event(f"Workflow reached Stop Success '{transition.step_id}'", terminal)
        return hub._command(
            {
                **cursor_update(cursor),
                "workflow_terminal": terminal,
                "orchestrator_journal": journal,
            },
            VALIDATION_NODE,
        )
    # The Stop step's own result carries the authored reason it resolved.
    current = active_step({**hub.state, **cursor_update(cursor)}, definition=hub.definition)
    result = accepted_message(hub.state, current) if current is not None else None
    artifacts = result.get("artifacts") if isinstance(result, Mapping) else None
    reason = (
        str(artifacts.get("reason") or "").strip() if isinstance(artifacts, Mapping) else ""
    ) or f"Stop Fail '{transition.step_id}'"
    return hub.fail(
        f"{STOPPED_BY_WORKFLOW_PREFIX}{reason}",
        event="workflow_stop_fail",
        step_id=str(transition.step_id),
        extra={**cursor_update(cursor), "workflow_terminal": terminal},
        journal=journal,
    )


def unsupported_success_continuation(hub: _StepHub, current: ActiveStep) -> Command | None:
    """A failed branching step cannot be approved into an invented success port."""
    if hub.definition.graph.edge_for(NodeRef("step", current.step_id), Outcome(OUTCOME_SUCCESS)):
        return None
    message = (
        f"Step '{current.step_id}' has no success continuation. "
        "Use an explicit error route or fail policy."
    )
    return hub.fail(
        message,
        event="step_recovery_unsupported",
        step_id=current.step_id,
        unresolved={
            **current.message_identity(),
            "status": "fail",
            "error_code": "recovery_outcome_unsupported",
            "message": message,
        },
    )


def open_recovery(
    hub: _StepHub,
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
    if kind == RECOVERY_FAILURE:
        unsupported = hub.unsupported_success_continuation(current)
        if unsupported is not None:
            return unsupported
    pending = {
        "request_id": str(uuid.uuid4()),
        "kind": kind,
        "step_id": current.step_id,
        "step_execution_id": current.step_execution_id,
        "attempt": current.attempt,
        "reason": reason,
        "allowed_actions": list(allowed_actions),
        "detail": dict(detail or {}),
        **dict(extra or {}),
    }
    hub._event(
        f"Recovery approval for step '{current.step_id}': {reason}",
        {**current.message_identity(), "kind": kind, "failure_class": failure_class},
        severity="warn",
    )
    return hub._command(
        {
            **dict(update or {}),
            "recovery_pending": pending,
            CARRIER: (update or {}).get(CARRIER) if kind == RECOVERY_AUTONOMY else None,
            "needs_user_approval": True,
            "approval_context": reason,
            "user_approved": None,
            "last_failure_class": failure_class or hub.state.get("last_failure_class"),
            "last_retry_reason": reason,
            "last_retry_retryable": False,
            "retry_backoff_seconds": 0,
            "orchestrator_journal": [
                *list(dict(update or {}).get("orchestrator_journal") or []),
                journal_entry(
                    iteration=hub.iteration,
                    event="recovery_requested",
                    step_id=current.step_id,
                    action="escalate_to_human",
                    reason=reason,
                    message=f"Recovery approval requested for step '{current.step_id}': {reason}",
                    detail={**pending, "failure_class": failure_class},
                    severity="warn",
                ),
            ],
        },
        APPROVAL_NODE,
    )


def escalate_rework_budget(
    hub: _StepHub, cursor: RunCursor, transition: Transition, *, update: Mapping[str, Any] | None
) -> Command:
    exhausted = ", ".join(transition.exhausted)
    outcome = {"name": transition.outcome}
    if transition.case_id is not None:
        outcome["case_id"] = transition.case_id
    detail = {
        "blocked_edge": [str(part) for part in transition.blocked_edge or ()],
        "exhausted": list(transition.exhausted),
        "rework": cursor.rework.dump(),
    }
    if transition.container is not None:
        source = transition.container
        reason = (
            f"Rework budget exhausted ({exhausted}) for the edge from {source.kind} '{source.id}'"
        )
        pending = {
            "request_id": str(uuid.uuid4()),
            "kind": RECOVERY_REWORK_BUDGET,
            "source": source.dump(),
            "group_id": transition.group_id,
            "source_frame": transition.source_frame.dump() if transition.source_frame else None,
            "outcome": outcome,
            "reason": reason,
            "allowed_actions": ["approve", "abort"],
            "detail": detail,
        }
        return hub._command(
            {
                **dict(update or {}),
                **cursor_update(cursor),
                "recovery_pending": pending,
                "needs_user_approval": True,
                "approval_context": reason,
                "user_approved": None,
            },
            APPROVAL_NODE,
        )
    current = active_step({**hub.state, **cursor_update(cursor)}, definition=hub.definition)
    assert current is not None
    return hub.open_recovery(
        current,
        kind=RECOVERY_REWORK_BUDGET,
        reason=f"Rework budget exhausted ({exhausted}) for the edge from '{current.step_id}'",
        detail=detail,
        allowed_actions=["approve", "abort"],
        extra={"source": {"kind": "step", "id": current.step_id}, "outcome": outcome},
        update={**dict(update or {}), **cursor_update(cursor)},
    )


def resume_recovery(hub: _StepHub, pending: dict[str, Any]) -> Command:
    if str(pending.get("kind") or "") == RECOVERY_LANE:
        return hub.resume_lane_recovery(pending)
    if str(pending.get("kind") or "") == RECOVERY_GROUP_CONTRACT:
        return hub.resume_container_contract(pending)
    cursor = run_cursor(hub.state)
    current = active_step(hub.state, definition=hub.definition)
    kind = str(pending.get("kind") or "")
    source = pending.get("source") or {}
    group_budget = kind == RECOVERY_REWORK_BUDGET and source.get("kind") in {"group", "for_each"}
    if kind == RECOVERY_KIND:
        return hub.fail(
            "This external operation recovery is no longer supported; use the authored Approval route.",
            event="external_effect_resolution_unsupported",
            extra={"recovery_pending": None, CARRIER: None},
        )
    if not group_budget and (
        current is None or pending.get("step_execution_id") != current.step_execution_id
    ):
        # A recovery request of another execution is stale; drop it.
        hub.base_update.update({"recovery_pending": None, CARRIER: None})
        hub.state = {**hub.state, "recovery_pending": None, CARRIER: None}
        return hub.route()
    approved = hub.state.get("user_approved")
    kind = str(pending.get("kind") or "")
    if approved is None or (kind == RECOVERY_REWORK_BUDGET and approved is not True):
        # No decision yet, or a reject of a budget pause (not an allowed action).
        return hub._command(
            {"needs_user_approval": True, "approval_context": pending.get("reason")},
            APPROVAL_NODE,
        )
    if group_budget:
        source_ref = NodeRef(str(source["kind"]), str(source["id"]))
        decision_step_id = ""
    else:
        assert current is not None
        source_ref = NodeRef("step", current.step_id)
        decision_step_id = current.step_id
    cleared = {
        "recovery_pending": None,
        "user_approved": None,
        CARRIER: None,
        **retry_continuation_updates(),
    }
    decision_journal = journal_entry(
        iteration=hub.iteration,
        event="recovery_decision",
        step_id=decision_step_id,
        action="approve" if approved else "reject",
        reason=str(pending.get("reason") or ""),
        message=f"Recovery request {pending.get('request_id')} {'approved' if approved else 'rejected'}",
        detail={"request_id": pending.get("request_id"), "kind": kind},
    )
    if kind == RECOVERY_REWORK_BUDGET:
        outcome_raw = pending.get("outcome") or {}
        outcome = Outcome(str(outcome_raw.get("name") or ""), outcome_raw.get("case_id"))

        def resolve():
            if group_budget:
                from core.application.orchestration.run_cursor import load_frame

                raw_frame = pending.get("source_frame")
                frame = load_frame(raw_frame) if isinstance(raw_frame, Mapping) else None
                return replace(
                    resolve_after_container(
                        hub.definition,
                        cursor,
                        source_ref,
                        failed=outcome.name == OUTCOME_ERROR,
                    ),
                    source_frame=frame,
                )
            return resolve_after_step(hub.definition, cursor, outcome)

        blocked = resolve()
        if blocked.kind != TRANSITION_ESCALATE:
            return hub.apply(
                cursor,
                blocked,
                reason="rework_budget_unchanged",
                update={**cleared, "orchestrator_journal": [decision_journal]},
            )
        edge = hub.definition.graph.edge_for(source_ref, outcome)
        grant_rework_budget(cursor, blocked, edge.rework_limit if edge is not None else None)
        transition = resolve()
        return hub.apply(
            cursor,
            transition,
            reason="rework_budget_granted",
            update={**cleared, "orchestrator_journal": [decision_journal]},
        )
    assert current is not None
    if kind == RECOVERY_AUTONOMY:
        from core.application.orchestration.external_effect_recovery import (
            checked_gate_resolution_ref,
        )

        try:
            cleared[CARRIER] = checked_gate_resolution_ref(hub.state, hub.state.get(CARRIER))
        except ValueError as exc:
            return hub.fail(str(exc), event="external_effect_resolution_invalid")
        approvals = hub.state.get("autonomy_gate_approvals")
        approvals = dict(approvals) if isinstance(approvals, Mapping) else {}
        approvals[current.step_execution_id] = True
        feedback = (
            None
            if approved
            else (
                str(hub.state.get("review_feedback") or "").strip()
                or "Operator requested revisions before step execution."
            )
        )
        return hub.dispatch(
            cursor,
            current,
            reason="autonomy_approved" if approved else "autonomy_rejected",
            skip_autonomy=True,
            update={
                **cleared,
                "autonomy_gate_approvals": approvals,
                "feedback": feedback,
                "orchestrator_journal": [decision_journal],
            },
        )
    if approved:
        unsupported = hub.unsupported_success_continuation(current)
        if unsupported is not None:
            return unsupported
        # Approving a failed execution moves past it on its success edge,
        # recorded as an operator-continued error.
        outcome = Outcome(OUTCOME_SUCCESS)
        transition = resolve_after_step(hub.definition, cursor, outcome)
        return hub.apply(
            cursor,
            transition,
            reason="recovery_approved",
            update={
                **cleared,
                "orchestrator_journal": [
                    decision_journal,
                    hub._outcome_journal(
                        current,
                        outcome,
                        _step_error_policy={
                            "on_error": "await_approval",
                            "applied": True,
                            "approved": True,
                        },
                    ),
                ],
            },
        )
    feedback = str(hub.state.get("review_feedback") or "").strip() or "Please revise and resubmit."
    retry_step(cursor)
    return hub.dispatch(
        cursor,
        reason="recovery_rejected",
        skip_autonomy=True,
        update={
            **cleared,
            "feedback": feedback,
            "orchestrator_journal": [decision_journal],
        },
    )
