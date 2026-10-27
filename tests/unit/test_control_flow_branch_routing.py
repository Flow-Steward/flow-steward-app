"""Regression coverage for AUDIT-067 — `if` routed a false condition to the true branch.

The boolean selection in `control_flow_step_node` was never wrong. The defect was
the phase runtime's jump: a `switch` pruned the phase stack so that the later
`if`'s false target was gone, and execution fell through to the next phase, a
`stop_fail`. The audit's fixture
(`docs/audits/full-product-audit-2026-07-28-evidence/workflow-control-flow-failure.yaml`)
is ``switch -> if -> success | failed``.

Branch steps no longer jump: they report an outcome and the hub follows the
execution graph's single edge for it. So both halves are pinned here: the node
reports exactly the outcome its condition selects, and the same workflow run
end to end through the hub reaches the stop its condition names.
"""

from __future__ import annotations

import pytest

import core.application.orchestration.agents.common as agents_common
from core.application.orchestration.control_flow_step_node import control_flow_step_node
from core.application.orchestration.run_definition import clear_run_definition_cache
from core.application.orchestration.step_hub import orchestrator_hub_node
from tests._step_run_state import prime, step_run_state
from tests._workflow_definitions import edge, node

IF_STEP = {
    "step_id": "choose_terminal",
    "step_kind": "if",
    "override": {"if": {"condition": {"operator": "truthy", "selector": "state.should_fail"}}},
}
SWITCH_STEP = {
    "step_id": "choose_loop",
    "step_kind": "switch",
    "override": {
        "switch": {
            "selector": "state.switch_value",
            "cases": [{"case_id": "route_loop", "when": "route_loop"}],
        }
    },
}
AUDIT_DEFINITION = {
    "workflow_id": "workflow_control_flow_failure",
    "steps": [
        {**SWITCH_STEP, "group_id": None, "lane_id": None},
        {**IF_STEP, "group_id": None, "lane_id": None},
        {"step_id": "success", "step_kind": "stop_success", "group_id": None, "lane_id": None},
        {
            "step_id": "failed",
            "step_kind": "stop_fail",
            "group_id": None,
            "lane_id": None,
            "override": {"stop_fail": {"message": "The condition chose failure"}},
        },
    ],
    "groups": [],
    "execution_graph": {
        "schema_version": 1,
        "entry": node("choose_loop"),
        "edges": [
            edge("choose_loop", node("choose_terminal"), "case", case_id="route_loop"),
            edge("choose_loop", node("success"), "default"),
            edge("choose_terminal", node("failed"), "true"),
            edge("choose_terminal", node("success"), "false"),
        ],
    },
    "settings": {},
}


@pytest.fixture(autouse=True)
def _isolated(monkeypatch):
    clear_run_definition_cache()
    monkeypatch.setattr(agents_common, "emit_event", lambda *args, **kwargs: None)
    monkeypatch.setattr(agents_common, "publish_agent_message", lambda *args, **kwargs: True)
    yield
    clear_run_definition_cache()


def _outcome(step: dict, **state) -> dict:
    update = control_flow_step_node(step_run_state(step, **state))
    assert set(update) == {"agent_messages"}, "a branch result never routes by itself"
    message = update["agent_messages"][-1]
    assert message["status"] == "pass"
    return message["artifacts"]["_outcome"]


class TestBranchStepsReportTheirOutcome:
    @pytest.mark.parametrize(("should_fail", "outcome"), [(False, "false"), (True, "true")])
    def test_if_reports_the_outcome_its_condition_selects(self, should_fail, outcome):
        assert _outcome(IF_STEP, should_fail=should_fail) == {"name": outcome}

    def test_switch_reports_the_matching_case(self):
        assert _outcome(SWITCH_STEP, switch_value="route_loop") == {
            "name": "case",
            "case_id": "route_loop",
        }

    def test_switch_without_a_matching_case_reports_default(self):
        assert _outcome(SWITCH_STEP, switch_value="other") == {"name": "default"}


def _apply(state: dict, update: dict) -> dict:
    merged = dict(state)
    for key, value in update.items():
        if key == "orchestrator_journal":
            merged[key] = [*(merged.get(key) or []), *value]
        else:
            merged[key] = value
    return merged


def _run_audit_workflow(**inputs) -> tuple[list[str], dict]:
    """Drive hub and control-flow node in turn until the hub leaves for validation."""
    state = {"workflow_id": "workflow_control_flow_failure", "agent_messages": []}
    state.update(prime(AUDIT_DEFINITION))
    state.update(inputs)
    visited: list[str] = []
    for _ in range(10):
        command = orchestrator_hub_node(state)
        state = _apply(state, command.update)
        if command.goto != "control_flow_step":
            assert command.goto == "orchestrator_validation"
            return visited, state
        visited.append(state["run_cursor"]["step_id"])
        state = _apply(state, control_flow_step_node(state))
    raise AssertionError(f"the run did not finish: {visited}")


class TestTheAuditWorkflowReachesTheStopItsConditionNames:
    def test_false_condition_reaches_the_success_stop(self):
        visited, state = _run_audit_workflow(switch_value="route_loop", should_fail=False)

        assert visited == ["choose_loop", "choose_terminal", "success"]
        assert state["workflow_terminal"]["kind"] == "stop_success"
        assert state["workflow_terminal"]["status"] == "pass"
        assert state.get("status") != "failed"

    def test_true_condition_reaches_the_failed_stop(self):
        visited, state = _run_audit_workflow(switch_value="route_loop", should_fail=True)

        assert visited == ["choose_loop", "choose_terminal", "failed"]
        assert state["workflow_terminal"]["kind"] == "stop_fail"
        assert state["status"] == "failed"

    def test_switch_default_skips_the_if(self):
        visited, state = _run_audit_workflow(switch_value="other", should_fail=True)

        assert visited == ["choose_loop", "success"]
        assert state["workflow_terminal"]["step_id"] == "success"

    def test_every_branch_result_carries_the_execution_it_answers(self):
        _visited, state = _run_audit_workflow(switch_value="route_loop", should_fail=False)

        executions = [
            message["step_execution_id"]
            for message in state["agent_messages"]
            if message["from_agent"] == "control_flow_step"
        ]
        assert len(executions) == 3
        assert len(set(executions)) == 3
