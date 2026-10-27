"""
Integration tests for step routing: the step hub plus the real human approval gate.

There are no phase gates. A successful step follows its success edge, an
authored Approval step reports an ``approved``/``rejected`` outcome that picks
its own edge, and a failed step is handled by its error policy (``fail`` by
default), with ``await_approval`` pausing at a recovery approval.

Run:
    docker compose exec fs-app python -m pytest tests/integration/test_workflow_routing.py -v
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import patch

import pytest
from langgraph.types import Command

from tests._workflow_definitions import edge, node

pytestmark = [pytest.mark.integration]


@pytest.fixture(autouse=True)
def _isolated(monkeypatch):
    import core.application.orchestration.agents.common as agents_common
    from core.application.orchestration.run_definition import clear_run_definition_cache

    clear_run_definition_cache()
    monkeypatch.setattr(agents_common, "emit_event", lambda *args, **kwargs: None)
    yield
    clear_run_definition_cache()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _definition(steps: list[dict], edges: list[dict]) -> dict:
    return {
        "steps": [{"group_id": None, "lane_id": None, **step} for step in steps],
        "groups": [],
        "execution_graph": {
            "schema_version": 1,
            "entry": node(steps[0]["step_id"]),
            "edges": edges,
        },
        "settings": {},
    }


def make_state(definition: dict, **overrides) -> dict:
    from tests._step_run_state import prime

    base = {
        "task_id": "wf-test-001",
        "task_summary": "Fix authentication bug",
        "project_name": "test-project",
        "workflow_id": "wf_routing",
        "workflow_selection_source": "explicit_request",
        "status": "in_progress",
        "agent_messages": [],
        "iteration_count": 0,
        "needs_user_approval": False,
        "user_approved": None,
        "review_feedback": None,
        "thread_id": "wf-thread-001",
        **prime(definition),
    }
    base.update(overrides)
    return base


def _apply(state: dict, command) -> dict:
    """Apply a node's update to the state the way LangGraph would."""
    merged = dict(state)
    for key, value in (command.update or {}).items():
        if key == "orchestrator_journal":
            merged[key] = [*(merged.get(key) or []), *value]
        else:
            merged[key] = value
    return merged


def _hub(state: dict) -> tuple[dict, Command]:
    from core.application.orchestration.step_hub import orchestrator_hub_node

    command = orchestrator_hub_node(state)
    return _apply(state, command), command


def _result(state: dict, status: str = "pass", *, actor: str = "writer", **artifacts) -> dict:
    from core.application.orchestration.active_step import step_message_fields
    from core.application.orchestration.agent_messages import build_agent_message

    message = build_agent_message(
        from_agent=actor,
        to_agent="orchestrator",
        skill="test_skill",
        status=status,
        correlation_id=f"{actor}-{state['run_cursor']['attempt']}",
        artifacts=artifacts,
        summary=f"{status} result",
        published_at=datetime.now(UTC).isoformat(),
        **step_message_fields(state),
    )
    return {**state, "agent_messages": [*state["agent_messages"], message]}


def _gate(state: dict, response) -> tuple[dict, dict]:
    from core.application.orchestration.graph.builder_approval_gate import (
        _registry_approval_gate_node,
    )

    with patch("langgraph.types.interrupt", return_value=response) as interrupt:
        command = _registry_approval_gate_node(state)
    assert interrupt.call_count == 1
    assert command.goto == "orchestrator"
    return _apply(state, command), interrupt.call_args.args[0]


# ---------------------------------------------------------------------------
# Successful steps and authored Approval steps
# ---------------------------------------------------------------------------

REVIEWED = _definition(
    [
        {"step_id": "draft", "step_kind": "agent", "agent_id": "writer"},
        {
            "step_id": "review",
            "step_kind": "approval",
            "display_name": "Summary Review",
            "override": {"approval": {"message": "Review the summary."}},
        },
        {"step_id": "done", "step_kind": "stop_success"},
    ],
    [
        edge("draft", node("review")),
        edge("review", node("done"), "approved"),
        edge("review", node("draft"), "rejected", rework=True),
    ],
)


def _at_review() -> tuple[dict, Command]:
    state, command = _hub(make_state(REVIEWED))
    assert (command.goto, state["current_step_id"]) == ("writer", "draft")
    state, command = _hub(_result(state))
    assert state["current_step_id"] == "review"
    return state, command


class TestStepRouting:
    def test_a_successful_step_advances_on_its_success_edge(self):
        state, command = _at_review()

        assert command.goto == "human_approval_gate"
        assert state["dispatched_step_execution"]["step_id"] == "review"

    def test_an_approval_step_pauses_for_a_human_decision(self):
        state, _ = _at_review()

        _, payload = _gate(state, {"approved": True})

        assert payload["gate_kind"] == "approval"
        assert payload["step_id"] == "review"
        assert payload["step_execution_id"] == state["run_cursor"]["step_execution_id"]
        assert payload["message"] == "Review the summary."

    def test_an_approved_decision_follows_the_approved_edge(self):
        state, _ = _at_review()
        state, _ = _gate(state, {"approved": True, "comment": "Looks good."})

        state, command = _hub(state)

        assert (command.goto, state["current_step_id"]) == ("control_flow_step", "done")
        assert state["review_feedback"] == "Looks good."

    def test_a_rejected_decision_follows_the_rejected_edge_with_its_feedback(self):
        state, _ = _at_review()
        review_execution = state["run_cursor"]["step_execution_id"]
        state, _ = _gate(state, {"approved": False, "comment": "Please add more detail"})

        state, command = _hub(state)

        assert (command.goto, state["current_step_id"]) == ("writer", "draft")
        assert state["run_cursor"]["step_execution_id"] != review_execution
        assert state["review_feedback"] == "Please add more detail"


# ---------------------------------------------------------------------------
# Failed steps and their error policy
# ---------------------------------------------------------------------------


def _failing(policy: dict | None) -> dict:
    step = {"step_id": "fetch", "step_kind": "agent", "agent_id": "writer"}
    if policy is not None:
        step["error_policy"] = policy
    return _definition(
        [
            step,
            {"step_id": "publish", "step_kind": "agent", "agent_id": "publisher"},
            {"step_id": "repair", "step_kind": "agent", "agent_id": "fixer"},
            {"step_id": "done", "step_kind": "stop_success"},
        ],
        [
            edge("fetch", node("publish")),
            edge("fetch", node("repair"), "error"),
            edge("publish", node("done")),
            edge("repair", node("done")),
        ],
    )


def _first_failure(policy: dict | None) -> tuple[dict, Command, dict]:
    started, _ = _hub(make_state(_failing(policy)))
    failed = _result(started, "fail")
    state, command = _hub(failed)
    return state, command, started


class TestFailedStepRouting:
    def test_a_failed_step_under_the_default_policy_ends_the_run(self):
        state, command, started = _first_failure(None)

        assert command.goto == "orchestrator_validation"
        assert state["status"] == "failed"
        failure = state["unresolved_step_failure"]
        assert failure["step_id"] == "fetch"
        assert failure["step_execution_id"] == started["run_cursor"]["step_execution_id"]

    def test_continue_moves_on_along_the_success_edge(self):
        state, command, _ = _first_failure({"on_error": "continue"})

        assert (command.goto, state["current_step_id"]) == ("publisher", "publish")

    def test_retry_dispatches_the_same_execution_again(self):
        state, command, started = _first_failure({"on_error": "retry", "max_retries": 2})

        assert (command.goto, state["current_step_id"]) == ("writer", "fetch")
        assert (
            state["run_cursor"]["step_execution_id"] == started["run_cursor"]["step_execution_id"]
        )
        assert state["run_cursor"]["attempt"] == 2

    def test_route_to_step_follows_the_error_edge(self):
        state, command, _ = _first_failure({"on_error": "route_to_step"})

        assert (command.goto, state["current_step_id"]) == ("fixer", "repair")

    def test_await_approval_approved_continues_on_the_success_edge(self):
        state, command, _ = _first_failure({"on_error": "await_approval"})
        assert command.goto == "human_approval_gate"
        assert state["recovery_pending"]["step_id"] == "fetch"

        state, payload = _gate(state, {"approved": True})
        assert payload["step_id"] == "fetch"
        state, command = _hub(state)

        assert (command.goto, state["current_step_id"]) == ("publisher", "publish")
        assert state["recovery_pending"] is None

    def test_await_approval_rejected_reruns_the_step_with_feedback(self):
        state, _, started = _first_failure({"on_error": "await_approval"})

        state, _ = _gate(state, {"approved": False, "comment": "Use the sandbox account"})
        state, command = _hub(state)

        assert (command.goto, state["current_step_id"]) == ("writer", "fetch")
        assert (
            state["run_cursor"]["step_execution_id"] == started["run_cursor"]["step_execution_id"]
        )
        assert state["run_cursor"]["attempt"] == 2
        assert state["feedback"] == "Use the sandbox account"


class TestRetryBudgets:
    def test_a_result_with_contract_errors_is_regenerated_once_with_feedback(self):
        started, _ = _hub(make_state(_failing(None)))
        invalid = _result(started, _schema_errors=["result is missing 'records'"])

        state, command = _hub(invalid)

        assert (command.goto, state["current_step_id"]) == ("writer", "fetch")
        assert state["run_cursor"]["attempt"] == 2
        assert "result is missing 'records'" in state["feedback"]
        assert state["last_failure_class"] == "contract"

    def test_contract_errors_after_the_retry_budget_end_the_run(self):
        started, _ = _hub(make_state(_failing(None)))
        state, _ = _hub(_result(started, _contract_errors=["missing output 'records'"]))

        state, command = _hub(_result(state, _contract_errors=["missing output 'records'"]))

        assert command.goto == "orchestrator_validation"
        assert state["status"] == "failed"
        assert state["last_failure_class"] == "contract"
        assert "after 1 retries" in state["error"]

    def test_project_retry_budget_sets_the_contract_retry_budget(self):
        state, _ = _hub(make_state(_failing(None), project_policy={"retry_max_attempts": 2}))
        for attempt in (2, 3):
            state, command = _hub(_result(state, _schema_errors=["bad shape"]))
            assert (command.goto, state["run_cursor"]["attempt"]) == ("writer", attempt)

        state, command = _hub(_result(state, _schema_errors=["bad shape"]))

        assert command.goto == "orchestrator_validation"
        assert "after 2 retries" in state["error"]

    def test_retry_on_retries_a_matching_failure_class(self):
        started, _ = _hub(
            make_state(_failing({"on_error": "retry", "retry_on": ["transient"], "max_retries": 2}))
        )
        failed = _result(started, "fail")
        failed["agent_messages"][-1]["summary"] = "Upstream timed out"

        with patch("core.application.orchestration.step_hub.time.sleep") as sleep:
            state, command = _hub(failed)

        assert (command.goto, state["run_cursor"]["attempt"]) == ("writer", 2)
        assert state["last_failure_class"] == "transient"
        sleep.assert_called_once_with(state["retry_backoff_seconds"])

    def test_retry_on_escalates_a_failure_class_it_does_not_name(self):
        state, command, _ = _first_failure({"on_error": "retry", "retry_on": ["transient"]})

        assert command.goto == "human_approval_gate"
        assert (
            "retry_on does not match failure class ('validation')"
            in (state["recovery_pending"]["reason"])
        )
