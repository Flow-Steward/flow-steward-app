"""Integration checks for mixed step-kind runtime progression through the step hub."""

from __future__ import annotations

import pytest

pytestmark = [pytest.mark.integration]


@pytest.fixture(autouse=True)
def _isolated(monkeypatch):
    import core.application.orchestration.agents.common as agents_common
    from core.application.orchestration.run_definition import clear_run_definition_cache

    clear_run_definition_cache()
    monkeypatch.setattr(agents_common, "emit_event", lambda *args, **kwargs: None)
    yield
    clear_run_definition_cache()


def _pass_message(state: dict, *, actor: str) -> dict:
    from core.application.orchestration.active_step import step_message_fields
    from core.application.orchestration.agent_messages import build_agent_message

    return build_agent_message(
        from_agent=actor,
        to_agent="orchestrator",
        skill="integration_test",
        status="pass",
        correlation_id=f"{actor}-corr",
        artifacts={},
        summary="ok",
        published_at="2026-04-08T00:00:00Z",
        **step_message_fields(state),
    )


def _apply(state: dict, command) -> dict:
    """Apply a hub command to the state the way LangGraph would."""
    merged = dict(state)
    for key, value in (command.update or {}).items():
        if key == "orchestrator_journal":
            merged[key] = [*(merged.get(key) or []), *value]
        else:
            merged[key] = value
    return merged


def test_mixed_step_kinds_route_and_progress_with_their_own_step_results():
    from core.application.orchestration.step_hub import orchestrator_hub_node
    from tests._step_run_state import pinned_definition, prime

    definition = pinned_definition(
        {"step_id": "research", "step_kind": "agent", "agent_id": "researcher"},
        {"step_id": "keywords", "step_kind": "connector", "connector_id": "connector.semrush"},
        {
            "step_id": "child",
            "step_kind": "invoke_workflow",
            "workflow_id": "child_workflow",
            "override": {"invoke_workflow": {"workflow_id": "child_workflow"}},
        },
        {
            "step_id": "remember",
            "step_kind": "set_variable",
            "override": {"set_variable": {"target": "foo", "value": "bar"}},
        },
        {"step_id": "done", "step_kind": "stop_success"},
    )
    state = {
        "workflow_id": "mixed_workflow",
        "workflow_selection_source": "explicit_request",
        "agent_messages": [],
        **prime(definition),
    }

    expected = [
        ("research", "researcher", "researcher"),
        ("keywords", "execute_step", "connector_step"),
        ("child", "invoke_workflow_step", "invoke_workflow"),
        ("remember", "control_flow_step", "control_flow_step"),
        ("done", "control_flow_step", "control_flow_step"),
    ]
    command = orchestrator_hub_node(state)
    for step_id, node, actor in expected:
        state = _apply(state, command)
        assert (command.goto, state["current_step_id"]) == (node, step_id)

        # An executor returning without this execution's result fails the run, never advances it.
        stale = orchestrator_hub_node(state)
        assert stale.goto == "orchestrator_validation"
        assert stale.update["unresolved_step_failure"]["step_id"] == step_id

        state = {
            **state,
            "agent_messages": [*state["agent_messages"], _pass_message(state, actor=actor)],
        }
        command = orchestrator_hub_node(state)

    assert command.goto == "orchestrator_validation"
    assert command.update["workflow_terminal"] == {
        "kind": "stop_success",
        "status": "pass",
        "step_id": "done",
        "step_execution_id": state["run_cursor"]["step_execution_id"],
    }


def test_a_previous_steps_result_never_completes_the_next_step():
    from core.application.orchestration.step_hub import orchestrator_hub_node
    from tests._step_run_state import pinned_definition, prime

    definition = pinned_definition(
        {"step_id": "keywords", "step_kind": "connector", "connector_id": "connector.semrush"},
        {"step_id": "research", "step_kind": "agent", "agent_id": "researcher"},
        {"step_id": "done", "step_kind": "stop_success"},
    )
    state = {"workflow_id": "wf", "agent_messages": [], **prime(definition)}
    state = _apply(state, orchestrator_hub_node(state))
    state["agent_messages"] = [_pass_message(state, actor="connector_step")]
    state = _apply(state, orchestrator_hub_node(state))
    assert state["current_step_id"] == "research"

    # The connector's accepted result is the only message; the agent step has not answered.
    command = orchestrator_hub_node(state)

    assert command.goto == "orchestrator_validation"
    assert command.update["unresolved_step_failure"]["step_id"] == "research"
