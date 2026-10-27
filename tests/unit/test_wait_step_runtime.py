"""Authored Wait: one deadline per entry, a durable pause, one valid wake (T18, T19)."""

from __future__ import annotations

from unittest.mock import patch

import pytest

import core.application.orchestration.agents.common as agents_common
from core.application.orchestration.run_cursor import RUNTIME_SCHEMA_VERSION, RunCursor, begin_step
from core.application.orchestration.run_definition import (
    clear_run_definition_cache,
    prime_run_definition,
)
from core.application.orchestration.step_hub import orchestrator_hub_node
from core.application.orchestration.wait_step_runtime import (
    WAIT_DURATION_INVALID,
    resolve_wait_duration,
    timer_wake_is_valid,
    wait_step_node,
)
from core.application.workflows.workflow_definition_snapshot import definition_digest
from core.application.workflows.workflow_definition_validation import WAIT_DURATION_MS_MAX
from tests._workflow_definitions import edge, node


@pytest.fixture(autouse=True)
def _isolated(monkeypatch):
    clear_run_definition_cache()
    monkeypatch.setattr(agents_common, "emit_event", lambda *args, **kwargs: None)
    yield
    clear_run_definition_cache()


def _definition(wait: dict, error_policy: dict | None = None) -> dict:
    pause = {"step_id": "pause", "step_kind": "wait", "override": {"wait": wait}}
    edges = [edge("pause", node("done"))]
    if error_policy is not None:
        pause["error_policy"] = error_policy
        edges.append(edge("pause", node("handle"), "error"))
    return {
        "steps": [
            pause,
            {"step_id": "handle", "step_kind": "stop_fail"},
            {"step_id": "done", "step_kind": "stop_success"},
        ],
        "groups": [],
        "execution_graph": {"schema_version": 1, "entry": node("pause"), "edges": edges},
        "settings": {},
    }


def _run(wait: dict, error_policy: dict | None = None, **state) -> dict:
    definition = _definition(wait, error_policy)
    ref = {"snapshot_id": "snap", "revision": 3, "digest": definition_digest(definition)}
    prime_run_definition(ref, definition)
    return {
        "workflow_id": "wf",
        "runtime_schema_version": RUNTIME_SCHEMA_VERSION,
        "workflow_definition_ref": ref,
        "agent_messages": [],
        **state,
    }


def _apply(state: dict, update: dict | None) -> dict:
    merged = dict(state)
    for key, value in (update or {}).items():
        if key == "orchestrator_journal":
            merged[key] = [*(merged.get(key) or []), *value]
        else:
            merged[key] = value
    return merged


def _dispatch(state: dict) -> tuple[dict, object]:
    command = orchestrator_hub_node(state)
    return _apply(state, command.update), command


def _wake(pending: dict, **changes) -> dict:
    return {
        "reason": "timer",
        "wait_id": pending["wait_id"],
        "timer_elapsed": True,
        **changes,
    }


def test_a_wait_records_one_deadline_before_it_runs() -> None:
    state, command = _dispatch(_run({"duration_ms": 5000}))

    assert command.goto == "wait_step"
    pending = state["pending_wait"]
    assert pending["kind"] == "timer"
    assert pending["duration_ms"] == 5000
    assert pending["due_epoch_ms"] - 5000 > 0
    assert pending["step_execution_id"] == state["run_cursor"]["step_execution_id"]
    assert pending["workflow_revision"] == 3

    # The hub runs again on the same checkpoint (a re-dispatch of the same
    # execution): the recorded wait is kept, the deadline does not move.
    again, _ = _dispatch({**state, "dispatched_step_execution": None})
    assert again["pending_wait"] == pending


def test_the_node_pauses_with_the_recorded_wait_and_finishes_on_a_valid_wake() -> None:
    state, _ = _dispatch(_run({"duration_ms": 5000}))
    pending = state["pending_wait"]

    with (
        patch("langgraph.types.interrupt", return_value=_wake(pending)) as interrupt,
        patch(
            "core.application.orchestration.wait_step_runtime._now_ms",
            return_value=pending["due_epoch_ms"],
        ),
    ):
        update = wait_step_node(state)

    assert interrupt.call_args.args[0]["wait_id"] == pending["wait_id"]
    assert update["pending_wait"] is None
    result = update["agent_messages"][-1]
    assert result["status"] == "pass"
    assert result["step_execution_id"] == pending["step_execution_id"]
    assert result["artifacts"]["due_at"] == pending["due_at"]

    finished = orchestrator_hub_node(_apply(state, update))
    assert finished.update["run_cursor"]["step_id"] == "done"


def test_an_early_or_stale_wake_pauses_again() -> None:
    state, _ = _dispatch(_run({"duration_ms": 5000}))
    pending = state["pending_wait"]
    wakes = [
        _wake(pending, wait_id="another-wait"),
        {"reason": "approval", "approved": True},
        _wake(pending, timer_elapsed=False),
        _wake(pending),
    ]

    with (
        patch("langgraph.types.interrupt", side_effect=wakes) as interrupt,
        patch(
            "core.application.orchestration.wait_step_runtime._now_ms",
            return_value=pending["due_epoch_ms"],
        ),
    ):
        update = wait_step_node(state)

    assert interrupt.call_count == 4
    assert update["agent_messages"][-1]["status"] == "pass"


def test_a_wake_before_the_deadline_is_not_valid() -> None:
    pending = {"wait_id": "w", "due_epoch_ms": 10_000}
    wake = {"reason": "timer", "wait_id": "w", "timer_elapsed": True}
    assert timer_wake_is_valid(pending, wake, now_ms=9_999) is False
    assert timer_wake_is_valid(pending, wake, now_ms=10_000) is True


def test_a_zero_wait_finishes_without_pausing() -> None:
    state, command = _dispatch(_run({"duration_ms": 0}))
    assert command.goto == "wait_step"

    with patch("langgraph.types.interrupt") as interrupt:
        update = wait_step_node(state)

    interrupt.assert_not_called()
    assert update["agent_messages"][-1]["status"] == "pass"


def test_the_maximum_duration_is_accepted() -> None:
    state, command = _dispatch(_run({"duration_ms": WAIT_DURATION_MS_MAX}))
    assert command.goto == "wait_step"
    assert state["pending_wait"]["duration_ms"] == WAIT_DURATION_MS_MAX


@pytest.mark.parametrize("value", [True, None, 1.5, -1, WAIT_DURATION_MS_MAX + 1, "5000"], ids=repr)
def test_an_invalid_duration_ends_the_run_with_a_typed_error(value) -> None:
    command = orchestrator_hub_node(_run({"duration_ms": value}))

    assert command.goto == "orchestrator_validation"
    failure = command.update["unresolved_step_failure"]
    assert failure["error_code"] == WAIT_DURATION_INVALID
    assert failure["step_id"] == "pause"
    assert not command.update.get("pending_wait")


def test_a_mapped_duration_is_read_once_at_entry() -> None:
    wait = {"input_mapping": {"duration_ms": "state.inputs.delay"}}
    state, _ = _dispatch(_run(wait, inputs={"delay": 5000}))
    pending = state["pending_wait"]
    assert pending["duration_ms"] == 5000

    # The input changes while the run waits: the deadline stays the original one.
    changed = {**state, "inputs": {"delay": 999_999}, "dispatched_step_execution": None}
    again, _ = _dispatch(changed)
    assert again["pending_wait"]["due_epoch_ms"] == pending["due_epoch_ms"]


def test_a_mapped_duration_that_does_not_resolve_is_a_typed_error() -> None:
    wait = {"input_mapping": {"duration_ms": "state.inputs.delay"}}
    command = orchestrator_hub_node(_run(wait, inputs={}))
    assert command.update["unresolved_step_failure"]["error_code"] == WAIT_DURATION_INVALID


def test_resolve_wait_duration_reports_why() -> None:
    step = {"override": {"wait": {"duration_ms": -5}}}
    duration, error = resolve_wait_duration(step, {})
    assert duration is None
    assert "-5" in error


MAPPED = {"input_mapping": {"duration_ms": "state.inputs.delay"}}


@pytest.mark.parametrize("inputs", [{}, {"delay": -1}], ids=["missing", "invalid"])
@pytest.mark.parametrize(
    "policy",
    [
        {"on_error": "route_to_step"},
        {"on_error": "continue"},
        {"on_error": "retry", "max_retries": 3},
        {"on_error": "await_approval"},
        {"on_error": "fail"},
    ],
    ids=["error-edge", "continue", "retry", "approval", "fail"],
)
def test_an_isolated_wait_duration_failure_never_leaves_the_selected_step(inputs, policy) -> None:
    cursor = begin_step(RunCursor(), "pause")
    state = _run(
        MAPPED,
        policy,
        inputs=inputs,
        task_source="step_test_run",
        workflow_test_run_execution_id="exec-wait",
        step_test_run_context={
            "workflow_id": "wf",
            "execution_id": "exec-wait",
            "step_id": "pause",
        },
        run_cursor=cursor.dump(),
    )

    command = orchestrator_hub_node(state)
    finished = _apply(state, command.update)

    assert command.goto == "orchestrator_validation"
    assert finished["status"] == "failed"
    failure = finished["unresolved_step_failure"]
    assert failure["error_code"] == WAIT_DURATION_INVALID
    assert failure["step_id"] == "pause"
    assert failure["step_execution_id"] == cursor.step_execution_id
    assert finished["run_cursor"] == cursor.dump()
    assert not finished.get("dispatched_step_execution")
    assert not finished.get("pending_wait")
    assert not finished.get("recovery_pending")
    assert not finished.get("needs_user_approval")
    assert finished["last_retry_retryable"] is False
    assert finished["retry_backoff_seconds"] == 0
    assert not finished.get("routed_transitions")


def test_an_invalid_mapped_duration_follows_the_steps_route_to_step_policy() -> None:
    command = orchestrator_hub_node(
        _run(MAPPED, {"on_error": "route_to_step"}, inputs={"delay": -1})
    )

    assert command.update["run_cursor"]["step_id"] == "handle"
    assert not command.update.get("pending_wait")


def test_an_invalid_mapped_duration_is_never_retried() -> None:
    command = orchestrator_hub_node(
        _run(MAPPED, {"on_error": "retry", "max_retries": 3}, inputs={"delay": -1})
    )

    # Deterministic: the operator decides, the step is not dispatched again.
    assert command.goto == "human_approval_gate"
    assert command.update["recovery_pending"]["kind"] == "failure"
    assert command.update["recovery_pending"]["step_id"] == "pause"
