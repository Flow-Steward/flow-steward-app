from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest

from tests._step_run_state import step_run_state

_HTTP_STEP = {
    "step_id": "fetch",
    "step_kind": "http_request",
    "override": {"http_request": {"method": "GET", "url": "https://x.test"}},
}


def _retried_connector_state(*, deadline_epoch_seconds: float) -> dict:
    """A connector step on its second attempt, with the hub's dispatch for that attempt."""
    state = step_run_state(
        {"step_id": "fetch", "step_kind": "connector", "connector_id": "x"}, attempt=2
    )
    state["operation_retry_dispatch"] = {
        "identity": "operation:stable",
        "deadline_epoch_seconds": deadline_epoch_seconds,
        "step_id": "fetch",
        "step_execution_id": state["run_cursor"]["step_execution_id"],
        "attempt": 2,
    }
    return state


def _failure_signal() -> dict[str, Any]:
    return {
        "failure_class": "provider",
        "retryable": True,
        "retry_after_seconds": 7.0,
        "request_semantics": "safe_read",
        "operation_kind": "source",
        "definitely_no_external_effect": True,
        "external_effect_status": "failed",
        "provider_error_code": "rate_limited",
        "http_status": 429,
    }


def test_connector_graph_node_exposes_retry_signal_as_top_level_artifacts() -> None:
    from core.application.orchestration.graph.nodes import execute_step_node

    result = {
        "handled": True,
        "ok": False,
        "runtime": "extension_action",
        "detail": "rate_limited",
        **_failure_signal(),
    }
    state = step_run_state({"step_id": "fetch", "step_kind": "connector", "connector_id": "x"})
    with patch(
        "core.application.orchestration.connector_step_runtime.execute_connector_step",
        return_value=result,
    ):
        update = execute_step_node(state)

    artifacts = update["agent_messages"][-1]["artifacts"]
    for key, value in _failure_signal().items():
        assert artifacts[key] == value
    retry = update["agent_messages"][-1]["operation_retry"]
    assert retry == {
        "schema_version": "operation_retry_signal_v1",
        "provenance": "deterministic_connector_step",
        "signal": _failure_signal(),
        "deadline_epoch_seconds": retry["deadline_epoch_seconds"],
        "step_id": "fetch",
        "operation_loop": None,
        "operation_parallel": None,
    }
    assert retry["deadline_epoch_seconds"] > 0


def test_http_graph_node_exposes_retry_signal_as_top_level_artifacts() -> None:
    from core.application.orchestration.graph.nodes import execute_step_node

    result = {
        "ok": False,
        "status_code": 429,
        "failure_reason": "unexpected_status",
        "response_mode": "inline",
        **_failure_signal(),
    }
    state = step_run_state(_HTTP_STEP)
    with patch(
        "core.application.orchestration.http_request_step_runtime.execute_http_request_step",
        return_value=result,
    ):
        update = execute_step_node(state)

    artifacts = update["agent_messages"][-1]["artifacts"]
    for key, value in _failure_signal().items():
        assert artifacts[key] == value
    retry = update["agent_messages"][-1]["operation_retry"]
    assert retry["provenance"] == "deterministic_http_step"
    assert retry["signal"] == _failure_signal()
    assert retry["step_id"] == "fetch"


def test_first_http_attempt_receives_and_publishes_absolute_operation_deadline() -> None:
    from core.application.orchestration.graph.nodes import execute_step_node

    captured: dict[str, object] = {}
    state = step_run_state(_HTTP_STEP, project_policy={"retry_operation_deadline_seconds": 30})

    def _execute(*, config: dict, state: dict) -> dict:
        _ = config
        captured.update(state)
        return {"ok": False, "status_code": 429, **_failure_signal()}

    with (
        patch("core.application.orchestration.graph.nodes.time.time", return_value=100.0),
        patch(
            "core.application.orchestration.http_request_step_runtime.execute_http_request_step",
            side_effect=_execute,
        ),
    ):
        update = execute_step_node(state)

    assert captured["_operation_deadline_epoch_seconds"] == 130.0
    assert update["agent_messages"][-1]["artifacts"]["operation_deadline_epoch_seconds"] == 130.0


def test_operation_retry_deadline_is_checked_immediately_before_dispatch() -> None:
    from core.application.orchestration.graph.nodes import execute_step_node

    state = _retried_connector_state(deadline_epoch_seconds=10.0)
    with (
        patch("core.application.orchestration.graph.nodes.time.time", return_value=11.0),
        patch(
            "core.application.orchestration.connector_step_runtime.execute_connector_step"
        ) as execute,
    ):
        update = execute_step_node(state)

    execute.assert_not_called()
    assert update["operation_retry_dispatch"] is None
    artifacts = update["agent_messages"][-1]["artifacts"]
    assert artifacts["error"] == "retry_deadline_exhausted"
    assert artifacts["retryable"] is False


def test_operation_retry_dispatch_is_consumed_by_the_attempt() -> None:
    from core.application.orchestration.graph.nodes import execute_step_node

    state = _retried_connector_state(deadline_epoch_seconds=20.0)
    with (
        patch("core.application.orchestration.graph.nodes.time.time", return_value=11.0),
        patch(
            "core.application.orchestration.connector_step_runtime.execute_connector_step",
            return_value={"handled": True, "ok": True, "runtime": "extension_action"},
        ),
    ):
        update = execute_step_node(state)

    assert update["operation_retry_dispatch"] is None
    assert update["agent_messages"][-1]["status"] == "pass"


_CONNECTOR_STEP = {"step_id": "fetch-records", "step_kind": "connector", "connector_id": "fixture"}

_SAFE_READ_SIGNAL: dict[str, Any] = {
    "operation_kind": "source",
    "request_semantics": "safe_read",
    "failure_class": "provider",
    "retryable": True,
    "definitely_no_external_effect": True,
    "external_effect_status": "failed",
}


@pytest.fixture
def _quiet_hub(monkeypatch):
    import core.application.orchestration.agents.common as agents_common
    from core.application.orchestration.run_definition import clear_run_definition_cache

    clear_run_definition_cache()
    monkeypatch.setattr(agents_common, "emit_event", lambda *args, **kwargs: None)
    yield
    clear_run_definition_cache()


def _dispatched(step: dict, *others: dict, **state) -> dict:
    """A run state whose hub has dispatched the first attempt of *step*."""
    run = step_run_state(step, *others, job_id="job-records", workflow_id="wf-records", **state)
    cursor = run["run_cursor"]
    run["dispatched_step_execution"] = {
        "step_id": cursor["step_id"],
        "step_execution_id": cursor["step_execution_id"],
        "attempt": cursor["attempt"],
    }
    return run


def _failed_result(
    state: dict,
    *,
    actor: str = "connector_step",
    retry: dict | None = None,
    status: str = "fail",
    correlation_id: str = "corr-first",
    **artifacts,
) -> dict:
    from core.application.orchestration.active_step import active_step, step_message_fields
    from core.application.orchestration.agent_messages import build_agent_message

    current = active_step(state)
    assert current is not None
    deterministic = current.step_kind in {"connector", "http_request"}
    envelope = None
    if retry is not None:
        envelope = {
            "schema_version": "operation_retry_signal_v1",
            **({"step_id": current.step_id} if deterministic else {}),
            **retry,
        }
    return build_agent_message(
        from_agent=actor,
        to_agent="orchestrator",
        skill=current.step_kind if deterministic else "step",
        status=status,
        correlation_id=correlation_id,
        artifacts=artifacts,
        operation_retry=envelope,
        summary="operation failed" if status == "fail" else "operation succeeded",
        published_at="2026-09-09T00:00:00Z",
        **step_message_fields(state),
    )


def _decide(state: dict, message: dict, **kwargs):
    from core.application.orchestration.active_step import active_step
    from core.application.orchestration.step_operation_retry import decide_operation_retry

    current = active_step(state)
    assert current is not None
    return decide_operation_retry(state, current, message, **kwargs)


@pytest.mark.parametrize(
    ("external_effect_status", "definitely_no_effect", "expected_action", "expected_reason"),
    [
        ("timeout_unknown", False, "escalate_to_human", "external_effect_ambiguous"),
        ("failed", True, "retry_operation", "retry_scheduled"),
    ],
)
def test_provider_neutral_retry_safety_is_applied_before_any_replay(
    external_effect_status: str,
    definitely_no_effect: bool,
    expected_action: str,
    expected_reason: str,
) -> None:
    signal: dict[str, Any] = {
        "failure_class": "provider",
        "retryable": True,
        "retry_after_seconds": 7,
        "request_semantics": "mutation",
        "operation_kind": "action",
        "definitely_no_external_effect": definitely_no_effect,
        "external_effect_status": external_effect_status,
        "provider_error_code": "rate_limited",
        "http_status": 429,
    }
    state = _dispatched(
        _CONNECTOR_STEP,
        project_policy={
            "retry_strategy": "bounded",
            "retry_max_attempts": 2,
            "retry_backoff_initial_seconds": 2,
            "retry_backoff_max_seconds": 120,
        },
    )
    message = _failed_result(
        state,
        retry={"provenance": "deterministic_connector_step", "signal": signal},
        **signal,
    )

    decision = _decide(state, message)

    assert decision is not None
    assert decision.action == expected_action
    assert decision.reason_code == expected_reason
    assert decision.signal["external_effect_status"] == external_effect_status
    assert decision.signal["provider_error_code"] == "rate_limited"
    if expected_action == "retry_operation":
        assert decision.delay_seconds == 7.0
        assert decision.identity
        assert decision.deadline_epoch_seconds
        assert decision.target_actor == "connector_step"


def test_incomplete_operation_signal_is_evaluated_fail_closed() -> None:
    signal: dict[str, Any] = {
        "operation_kind": "action",
        "external_effect_status": "timeout_unknown",
    }
    state = _dispatched(_CONNECTOR_STEP)
    message = _failed_result(
        state,
        retry={"provenance": "deterministic_connector_step", "signal": signal},
        **signal,
    )

    decision = _decide(state, message)

    assert decision is not None
    assert decision.action == "escalate_to_human"
    assert decision.reason_code == "not_retryable"


@pytest.mark.usefixtures("_quiet_hub")
def test_llm_authored_artifacts_cannot_authorize_operation_retry() -> None:
    """A generic step artifact must never become host retry authority."""
    from core.application.orchestration.step_hub import orchestrator_hub_node

    state = _dispatched({"step_id": "draft", "step_kind": "agent", "agent_id": "researcher"})
    message = _failed_result(state, actor="researcher", http_status=429, **_SAFE_READ_SIGNAL)

    assert _decide(state, message) is None
    command = orchestrator_hub_node({**state, "agent_messages": [message]})
    # The authored error policy (default ``fail``) decides, not the artifacts.
    assert command.goto == "orchestrator_validation"
    assert command.update["status"] == "failed"
    assert "operation_retry_dispatch" not in command.update


@pytest.mark.usefixtures("_quiet_hub")
def test_host_owned_retry_channel_authorizes_operation_retry() -> None:
    """Only the validated runtime envelope may schedule an operation retry."""
    from core.application.orchestration.step_hub import orchestrator_hub_node

    state = _dispatched(_CONNECTOR_STEP)
    message = _failed_result(
        state,
        retry={
            "provenance": "deterministic_connector_step",
            "signal": {**_SAFE_READ_SIGNAL, "http_status": 429},
            "step_id": "fetch-records",
        },
        error="connector_execution_failed",
    )

    command = orchestrator_hub_node({**state, "agent_messages": [message]})

    assert command.goto == "operation_retry_wait"
    dispatch = command.update["operation_retry_dispatch"]
    assert dispatch["step_id"] == "fetch-records"
    assert dispatch["step_execution_id"] == state["run_cursor"]["step_execution_id"]
    assert dispatch["attempt"] == 2
    assert dispatch["target"] == "execute_step"


def test_agent_managed_custom_http_retry_is_fail_closed_without_exact_tool_cursor() -> None:
    state = _dispatched({"step_id": "draft", "step_kind": "agent", "agent_id": "writer"})
    message = _failed_result(
        state,
        actor="writer",
        retry={
            "provenance": "agent_managed_custom_http",
            "signal": {**_SAFE_READ_SIGNAL, "http_status": 429},
        },
        error="custom_http_failed",
    )

    decision = _decide(state, message)

    assert decision is not None
    assert decision.action == "escalate_to_human"
    assert decision.reason_code == "agent_managed_tool_retry_unsupported"


@pytest.mark.usefixtures("_quiet_hub")
def test_successful_agent_mutation_is_not_replayed_after_custom_http_read_failure() -> None:
    from core.application.orchestration.step_hub import orchestrator_hub_node

    counts = {"agent": 0, "mutation": 0, "failed_read": 0}
    state = _dispatched(
        {"step_id": "draft", "step_kind": "agent", "agent_id": "writer"},
        {"step_id": "done", "step_kind": "stop_success"},
    )

    def _agent_attempt() -> dict:
        counts["agent"] += 1
        counts["mutation"] += 1
        counts["failed_read"] += 1
        return _failed_result(
            state,
            actor="writer",
            correlation_id=f"corr-agent-attempt-{counts['agent']}",
            retry={
                "provenance": "agent_managed_custom_http",
                "signal": {**_SAFE_READ_SIGNAL, "http_status": 429},
            },
            error="custom_http_failed",
        )

    command = orchestrator_hub_node({**state, "agent_messages": [_agent_attempt()]})
    if command.goto in {"operation_retry_wait", "writer"}:
        _agent_attempt()

    assert command.goto == "orchestrator_validation"
    assert command.update["status"] == "failed"
    assert not command.update.get("recovery_pending")
    assert not command.update.get("operation_retry_dispatch")
    assert counts == {"agent": 1, "mutation": 1, "failed_read": 1}


def test_operation_retry_identity_and_budget_survive_new_message_correlation() -> None:
    state = _dispatched(_CONNECTOR_STEP)
    retry = {
        "provenance": "deterministic_connector_step",
        "signal": dict(_SAFE_READ_SIGNAL),
        "step_id": "fetch-records",
    }
    first = _decide(state, _failed_result(state, retry=retry, **_SAFE_READ_SIGNAL))
    assert first is not None
    second = _decide(
        {
            **state,
            "operation_retry_state": {
                first.identity: {
                    "retry_count": 1,
                    "deadline_epoch_seconds": first.deadline_epoch_seconds,
                }
            },
        },
        _failed_result(state, retry=retry, correlation_id="corr-second", **_SAFE_READ_SIGNAL),
    )

    assert second is not None
    assert first.action == second.action == "retry_operation"
    assert second.identity == first.identity
    assert second.retry_number == 2
    assert second.deadline_epoch_seconds == first.deadline_epoch_seconds


def test_operation_retry_deadline_cannot_extend_checkpointed_deadline() -> None:
    state = _dispatched(_CONNECTOR_STEP)
    message = _failed_result(
        state,
        retry={
            "provenance": "deterministic_connector_step",
            "signal": dict(_SAFE_READ_SIGNAL),
            "deadline_epoch_seconds": 400.0,
            "step_id": "fetch-records",
        },
        error="connector_execution_failed",
    )

    first = _decide(state, message, now=100.0)
    assert first is not None
    second = _decide(
        {
            **state,
            "operation_retry_state": {
                first.identity: {"retry_count": 1, "deadline_epoch_seconds": 130.0}
            },
        },
        message,
        now=100.0,
    )

    assert first.deadline_epoch_seconds == 400.0
    assert second is not None
    assert second.deadline_epoch_seconds == 130.0


def test_parallel_operation_retry_identity_and_dispatch_coordinates_are_lane_scoped() -> None:
    state = _dispatched(_CONNECTOR_STEP)

    def _lane(lane_id: str, position: int):
        parallel = {"lane_id": lane_id, "position": position}
        message = _failed_result(
            state,
            correlation_id=f"corr-{lane_id}",
            retry={
                "provenance": "deterministic_connector_step",
                "signal": dict(_SAFE_READ_SIGNAL),
                "step_id": "fetch-records",
                "operation_parallel": parallel,
            },
            operation_parallel=parallel,
            **_SAFE_READ_SIGNAL,
        )
        return _decide(state, message)

    lane_a = _lane("lane_a", 0)
    lane_b = _lane("lane_b", 1)

    assert lane_a is not None
    assert lane_b is not None
    assert lane_a.action == lane_b.action == "retry_operation"
    assert lane_a.identity != lane_b.identity
    assert (lane_b.lane_id, lane_b.position) == ("lane_b", 1)


@pytest.mark.usefixtures("_quiet_hub")
def test_successful_operation_diagnostics_do_not_enter_retry_policy() -> None:
    from core.application.orchestration.step_hub import orchestrator_hub_node

    state = _dispatched(_CONNECTOR_STEP, {"step_id": "next", "step_kind": "transform"})
    message = _failed_result(
        state,
        status="pass",
        http_status=200,
        request_semantics="safe_read",
        external_effect_status="succeeded",
    )

    command = orchestrator_hub_node({**state, "agent_messages": [message]})

    assert command.goto == "execute_step"
    assert command.update["current_step_id"] == "next"
    assert "operation_retry_dispatch" not in command.update


_EXHAUSTED_HTTP_RETRY = {
    "provenance": "deterministic_http_step",
    # The deadline is already past, so the policy refuses another attempt
    # exactly as an exhausted budget does.
    "deadline_epoch_seconds": 1.0,
    "step_id": "slow",
    "signal": {**_SAFE_READ_SIGNAL, "http_status": 503},
}


@pytest.mark.usefixtures("_quiet_hub")
def test_exhausted_operation_retry_uses_default_failure_without_approval() -> None:
    from core.application.orchestration.step_hub import orchestrator_hub_node

    state = _dispatched(
        {**_HTTP_STEP, "step_id": "slow"}, {"step_id": "next", "step_kind": "transform"}
    )
    message = _failed_result(state, actor="http_request_step", retry=_EXHAUSTED_HTTP_RETRY)

    command = orchestrator_hub_node({**state, "agent_messages": [message]})

    assert command.goto == "orchestrator_validation"
    assert command.update["status"] == "failed"
    assert not command.update.get("recovery_pending")
    assert not command.update.get("operation_retry_dispatch")
    assert state["run_cursor"]["attempt"] == 1
    assert (
        command.update["unresolved_step_failure"]["step_execution_id"]
        == state["run_cursor"]["step_execution_id"]
    )


@pytest.mark.usefixtures("_quiet_hub")
def test_operator_approval_advances_past_an_exhausted_operation_retry() -> None:
    """AC17-R37: an approved escalation must leave awaiting_approval.

    The failed result still carries its spent retry signal; approving the
    recovery must move past the execution on its success edge instead of
    recomputing the same escalation forever.
    """
    from core.application.orchestration.step_hub import orchestrator_hub_node

    state = _dispatched(
        {**_HTTP_STEP, "step_id": "slow", "error_policy": {"on_error": "await_approval"}},
        {"step_id": "next", "step_kind": "transform"},
    )
    message = _failed_result(state, actor="http_request_step", retry=_EXHAUSTED_HTTP_RETRY)
    state = {**state, "agent_messages": [message]}
    escalated = orchestrator_hub_node(state)
    assert escalated.goto == "human_approval_gate"
    assert (
        escalated.update["recovery_pending"]["step_execution_id"]
        == state["run_cursor"]["step_execution_id"]
    )
    assert (
        escalated.update["recovery_pending"]["detail"]["retry_reason_code"]
        == "retry_delay_exceeds_deadline"
    )
    state = {**state, **escalated.update, "user_approved": True}

    approved = orchestrator_hub_node(state)

    assert approved.goto == "execute_step", (
        f"an approved escalation must advance; it went to {approved.goto!r}"
    )
    assert approved.update["current_step_id"] == "next"
    assert approved.update["recovery_pending"] is None
    assert approved.update["needs_user_approval"] is False
