from __future__ import annotations

from unittest.mock import patch

from tests._step_run_state import step_run_state


def test_wait_node_sleeps_after_checkpoint_and_routes_to_exact_target() -> None:
    from core.application.orchestration.operation_retry_wait_node import (
        operation_retry_wait_node,
    )

    state = {
        "operation_retry_dispatch": {
            "identity": "operation:stable",
            "deadline_epoch_seconds": 100.0,
            "delay_seconds": 7.0,
            "step_id": "fetch",
            "target_actor": "connector_step",
            "target": "execute_step",
        }
    }
    with (
        patch(
            "core.application.orchestration.operation_retry_wait_node.time.time",
            side_effect=[90.0, 91.0],
        ),
        patch("core.application.orchestration.operation_retry_wait_node.time.sleep") as sleep_mock,
    ):
        command = operation_retry_wait_node(state)

    assert command.goto == "execute_step"
    sleep_mock.assert_called_once_with(7.0)


def test_wait_node_keeps_runtime_heartbeat_fresh_during_long_backoff() -> None:
    from core.application.orchestration.operation_retry_wait_node import (
        operation_retry_wait_node,
    )

    state = {
        "job_id": "job-retry-wait",
        "operation_retry_dispatch": {
            "identity": "operation:stable",
            "deadline_epoch_seconds": 200.0,
            "delay_seconds": 5.0,
            "step_id": "fetch",
            "target_actor": "connector_step",
            "target": "execute_step",
        },
    }
    with (
        patch(
            "core.application.orchestration.operation_retry_wait_node.time.time",
            side_effect=[100.0, 106.0],
        ),
        patch(
            "core.application.orchestration.graph.nodes_agent_runtime._heartbeat_interval_seconds",
            return_value=2.0,
        ),
        patch(
            "core.application.orchestration.graph.nodes_agent_runtime._emit_agent_heartbeat"
        ) as heartbeat_mock,
        patch("core.application.orchestration.operation_retry_wait_node.time.sleep") as sleep_mock,
    ):
        command = operation_retry_wait_node(state)

    assert command.goto == "execute_step"
    assert [call.args[0] for call in sleep_mock.call_args_list] == [2.0, 2.0, 1.0]
    assert heartbeat_mock.call_count == 4
    heartbeat_mock.assert_any_call(state, agent_id="operation_retry_wait")


def test_wait_node_stops_when_deadline_expires_before_sleep() -> None:
    from core.application.orchestration.operation_retry_wait_node import (
        operation_retry_wait_node,
    )

    state = step_run_state(
        {"step_id": "fetch", "step_kind": "connector", "connector_id": "example"},
        attempt=2,
    )
    state = {
        **state,
        "operation_retry_dispatch": {
            "identity": "operation:stable",
            "deadline_epoch_seconds": 95.0,
            "delay_seconds": 7.0,
            "step_id": "fetch",
            "target_actor": "connector_step",
            "target": "execute_step",
            "retry_signal": {
                "operation_kind": "source",
                "request_semantics": "safe_read",
            },
        },
    }
    with (
        patch(
            "core.application.orchestration.operation_retry_wait_node.time.time",
            return_value=90.0,
        ),
        patch("core.application.orchestration.operation_retry_wait_node.time.sleep") as sleep_mock,
        patch(
            "core.application.orchestration.agents.common.publish_agent_message",
            return_value=True,
        ),
    ):
        command = operation_retry_wait_node(state)

    assert command.goto == "orchestrator"
    assert command.update["operation_retry_dispatch"] is None
    message = command.update["agent_messages"][-1]
    assert message["artifacts"]["retryable"] is False
    # The failure answers the retried attempt the cursor names, so the hub accepts it.
    assert message["step_id"] == "fetch"
    assert message["step_execution_id"] == state["run_cursor"]["step_execution_id"]
    assert message["attempt"] == 2
    sleep_mock.assert_not_called()


def test_checkpoint_restart_resumes_hub_retry_through_wait_to_exact_target() -> None:
    from langgraph.checkpoint.memory import InMemorySaver
    from langgraph.graph import END, START, StateGraph

    from core.application.orchestration.agent_messages import build_agent_message
    from core.application.orchestration.operation_retry_wait_node import (
        operation_retry_wait_node,
    )
    from core.application.orchestration.state import OrchestratorState
    from core.application.orchestration.step_hub import orchestrator_hub_node
    from core.application.orchestration.step_operation_retry import (
        ACTION_RETRY_OPERATION,
        OperationRetryDecision,
    )

    checkpointer = InMemorySaver()
    config = {"configurable": {"thread_id": "retry-restart"}}
    state = step_run_state(
        {"step_id": "fetch", "step_kind": "connector", "connector_id": "example"},
        iteration_count=0,
        orchestrator_journal=[],
        outputs={},
        operation_retry_state={},
    )
    cursor = state["run_cursor"]
    state["dispatched_step_execution"] = {
        "step_id": "fetch",
        "step_execution_id": cursor["step_execution_id"],
        "attempt": 1,
    }
    state["agent_messages"] = [
        build_agent_message(
            from_agent="connector_step",
            to_agent="orchestrator",
            skill="connector",
            status="fail",
            correlation_id="fetch-1",
            artifacts={"error": "rate_limited"},
            summary="rate limited",
            published_at="2026-10-12T00:00:00+00:00",
            step_id="fetch",
            step_execution_id=cursor["step_execution_id"],
            attempt=1,
        )
    ]
    decision = OperationRetryDecision(
        action=ACTION_RETRY_OPERATION,
        reason="rate limited",
        identity="operation:stable",
        retry_number=1,
        deadline_epoch_seconds=130.0,
        delay_seconds=7.0,
        failure_class="provider",
        target_actor="connector_step",
        signal={
            "failure_class": "provider",
            "retryable": True,
            "request_semantics": "safe_read",
            "operation_kind": "source",
            "definitely_no_external_effect": True,
            "external_effect_status": "failed",
        },
    )

    def _exact_target(state: OrchestratorState) -> dict:
        return {
            "outputs": {"retry_dispatch_seen": dict(state["operation_retry_dispatch"] or {})},
            "operation_retry_dispatch": None,
        }

    def _compile(*, interrupt_after: list[str] | None = None):
        builder = StateGraph(OrchestratorState)
        builder.add_node(
            "orchestrator", orchestrator_hub_node, destinations=("operation_retry_wait",)
        )
        builder.add_node(
            "operation_retry_wait",
            operation_retry_wait_node,
            destinations=("execute_step", "orchestrator"),
        )
        builder.add_node("execute_step", _exact_target)
        builder.add_edge(START, "orchestrator")
        builder.add_edge("execute_step", END)
        return builder.compile(checkpointer=checkpointer, interrupt_after=interrupt_after)

    with (
        patch("core.application.orchestration.step_hub.time.time", return_value=100.0),
        patch(
            "core.application.orchestration.step_hub.decide_operation_retry",
            return_value=decision,
        ),
    ):
        first_process = _compile(interrupt_after=["orchestrator"])
        first_process.invoke(state, config)

    checkpoint = first_process.get_state(config)
    persisted_dispatch = dict(checkpoint.values["operation_retry_dispatch"])
    assert checkpoint.next == ("operation_retry_wait",)
    assert persisted_dispatch["identity"] == "operation:stable"
    assert persisted_dispatch["deadline_epoch_seconds"] == 130.0
    assert persisted_dispatch["step_id"] == "fetch"
    assert persisted_dispatch["step_execution_id"] == cursor["step_execution_id"]
    assert persisted_dispatch["attempt"] == 2
    assert persisted_dispatch["target"] == "execute_step"
    assert checkpoint.values["run_cursor"]["attempt"] == 2

    restarted_process = _compile()
    with (
        patch(
            "core.application.orchestration.operation_retry_wait_node.time.time",
            return_value=101.0,
        ),
        patch("core.application.orchestration.operation_retry_wait_node.time.sleep") as sleep_mock,
    ):
        final_state = restarted_process.invoke(None, config)

    assert final_state["outputs"]["retry_dispatch_seen"] == persisted_dispatch
    assert final_state["operation_retry_dispatch"] is None
    sleep_mock.assert_called_once_with(7.0)
