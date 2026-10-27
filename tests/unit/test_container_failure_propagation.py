"""Failure precedence and propagation through Group → For each (ADR 0038, spec §5.1)."""

from __future__ import annotations

import pytest

from core.application.orchestration.step_hub import orchestrator_hub_node
from tests._workflow_definitions import edge, node
from tests.unit.test_step_hub import _isolated as _isolated
from tests.unit.test_step_hub import _result, _run, _step


def nested(
    *,
    step_policy: dict | None = None,
    for_each_policy: dict | None = None,
    group_policy: dict | None = None,
) -> dict:
    """a → Group g → [prep → For each fe (work → fe_exit)] → done.

    Each container with route_to_step has an error edge to its own handler:
    the For each's handler runs inside the Group, the Group's at the root.
    """
    work = {"step_id": "work", "step_kind": "http_request", "for_each_id": "fe"}
    if step_policy is not None:
        work["error_policy"] = step_policy
    steps = [
        {"step_id": "a", "step_kind": "transform"},
        {"step_id": "prep", "step_kind": "transform", "group_id": "g"},
        work,
        {"step_id": "fe_handler", "step_kind": "transform", "group_id": "g"},
        {"step_id": "g_handler", "step_kind": "stop_fail"},
        {"step_id": "work_handler", "step_kind": "transform", "for_each_id": "fe"},
        {"step_id": "done", "step_kind": "stop_success"},
    ]
    g_exit = {"kind": "group_exit", "group_id": "g", "lane_id": None}
    fe_exit = {"kind": "for_each_exit", "for_each_id": "fe", "lane_id": None}
    edges = [
        edge("a", node("g", "group")),
        edge("prep", node("fe", "for_each")),
        edge("work", fe_exit),
        edge("work_handler", fe_exit),
        edge("fe", g_exit, source_kind="for_each"),
        edge("fe_handler", g_exit),
        edge("g", node("done"), source_kind="group"),
    ]
    if (step_policy or {}).get("on_error") == "route_to_step":
        edges.append(edge("work", node("work_handler"), "error"))
    for_each = {
        "for_each_id": "fe",
        "group_id": "g",
        "over": "state.items",
        "item_name": "item",
        "body": {"kind": "sequence", "entry": node("work")},
    }
    if for_each_policy is not None:
        for_each["error_policy"] = for_each_policy
        if for_each_policy.get("on_error") == "route_to_step":
            edges.append(edge("fe", node("fe_handler"), "error", source_kind="for_each"))
    group = {"group_id": "g", "body": {"kind": "sequence", "entry": node("prep")}}
    if group_policy is not None:
        group["error_policy"] = group_policy
        if group_policy.get("on_error") == "route_to_step":
            edges.append(edge("g", node("g_handler"), "error", source_kind="group"))
    return {
        "steps": steps,
        "groups": [group],
        "for_each": [for_each],
        "execution_graph": {"schema_version": 1, "entry": node("a"), "edges": edges},
        "settings": {},
    }


ROUTE = {"on_error": "route_to_step"}
FAIL = {"on_error": "fail"}


def fail_first_item(definition: dict, items=(1, 2, 3)) -> tuple[dict, object]:
    """Run to the first item's work step, fail it, return the state and the hub's answer."""
    state = _run(definition, items=list(items))
    for _ in range(2):  # a, prep
        state = _step(state, orchestrator_hub_node(state))
        state = _result(state)
    state = _step(state, orchestrator_hub_node(state))
    assert state["current_step_id"] == "work"
    failed = _result(state, "fail", actor="http_request_step", failure_class="validation")
    return failed, orchestrator_hub_node(failed)


def frames(command) -> list[str]:
    return [frame["frame"] for frame in command.update["run_cursor"]["frames"]]


@pytest.mark.parametrize(
    ("policies", "next_step", "remaining"),
    [
        # The step's own error edge: both frames stay, the handler runs in the item.
        ({"step_policy": ROUTE}, "work_handler", ["group", "for_each"]),
        # The For each's route: only the For each ends; its handler runs in the Group.
        ({"for_each_policy": ROUTE}, "fe_handler", ["group"]),
        # The Group's route: both containers end; its handler runs at the root.
        ({"group_policy": ROUTE}, "g_handler", []),
        # Nearest declared policy wins: the For each catches before the Group.
        ({"for_each_policy": ROUTE, "group_policy": ROUTE}, "fe_handler", ["group"]),
    ],
)
def test_nested_failure_policy_and_frame_unwind(policies, next_step, remaining) -> None:
    _state, command = fail_first_item(nested(**policies))
    assert command.update["current_step_id"] == next_step
    assert frames(command) == remaining


def test_explicit_inner_fail_can_be_caught_by_outer_group() -> None:
    """step.fail → For each without a handler → Group.route_to_step."""
    _state, command = fail_first_item(
        nested(step_policy=FAIL, for_each_policy=FAIL, group_policy=ROUTE)
    )
    assert command.update["current_step_id"] == "g_handler"
    assert frames(command) == []
    failed = [
        entry["event"]
        for entry in command.update["orchestrator_journal"]
        if entry["event"].endswith("_failed")
    ]
    assert failed == ["for_each_failed", "group_failed"]


def test_an_unhandled_failure_at_every_level_fails_the_run() -> None:
    _state, command = fail_first_item(nested(step_policy=FAIL, for_each_policy=FAIL))
    assert command.goto == "orchestrator_validation"
    assert command.update["status"] == "failed"
    assert command.update["unresolved_step_failure"]["container"] == {"kind": "group", "id": "g"}


def test_container_failure_does_not_replay_items() -> None:
    """A For each that catches its failure leaves; no further item runs."""
    state, command = fail_first_item(nested(for_each_policy=ROUTE), items=[1, 2, 3])
    state = _step(state, command)
    assert state["current_step_id"] == "fe_handler"
    state = _step(_result(state), orchestrator_hub_node(_result(state)))
    assert state["current_step_id"] == "done"
    assert state["workflow_data"]["item"] == 1


def test_a_container_continue_policy_is_the_default_for_its_steps_only() -> None:
    """For each continue lets the failed step pass on; the next item still runs."""
    state, command = fail_first_item(nested(for_each_policy={"on_error": "continue"}), items=[1, 2])
    state = _step(state, command)
    assert state["current_step_id"] == "work"
    assert state["workflow_data"]["item"] == 2
    assert frames(command) == ["group", "for_each"]


def test_a_container_retry_policy_retries_only_the_failed_step() -> None:
    state, command = fail_first_item(
        nested(group_policy={"on_error": "retry", "max_retries": 2}), items=[1, 2]
    )
    state = _step(state, command)
    assert state["current_step_id"] == "work"
    assert state["run_cursor"]["attempt"] == 2
    assert state["workflow_data"]["item"] == 1


def test_a_stop_fail_inside_the_body_is_not_caught_by_containers() -> None:
    definition = nested(group_policy=ROUTE, for_each_policy=ROUTE)
    definition["steps"].append({"step_id": "halt", "step_kind": "stop_fail", "for_each_id": "fe"})
    for route in definition["execution_graph"]["edges"]:
        if route["source"] == node("work"):
            route["target"] = node("halt")
    state = _run(definition, items=[1])
    for _ in range(3):
        state = _step(state, orchestrator_hub_node(state))
        state = _result(state, actor="http_request_step")
    state = _step(state, orchestrator_hub_node(state))
    assert state["current_step_id"] == "halt"
    end = orchestrator_hub_node(
        _result(state, actor="control_flow_step", terminal="fail", reason="Stop")
    )
    assert end.update["workflow_terminal"]["kind"] == "stop_fail"
    assert end.update["status"] == "failed"


def test_a_failed_source_fails_the_for_each_and_its_group_catches_it() -> None:
    definition = nested(group_policy=ROUTE)
    definition["for_each"][0]["over"] = "rows[abc]"
    state = _run(definition)
    for _ in range(2):
        state = _step(state, orchestrator_hub_node(state))
        state = _result(state)
    command = orchestrator_hub_node(state)
    assert command.update["current_step_id"] == "g_handler"
    assert frames(command) == []


def test_exit_contract_reject_is_offered_to_the_enclosing_group() -> None:
    definition = nested(for_each_policy={"on_error": "await_approval"}, group_policy=ROUTE)
    definition["for_each"][0]["boundary_contract"] = {"exit": ["state.report"]}
    state = _run(definition, items=[1])
    for _ in range(3):
        state = _step(state, orchestrator_hub_node(state))
        state = _result(state, actor="http_request_step")
    paused = orchestrator_hub_node(state)
    assert paused.goto == "human_approval_gate"
    assert paused.update["recovery_pending"]["container"] == {"kind": "for_each", "id": "fe"}
    state = _step(state, paused)

    rejected = orchestrator_hub_node({**state, "user_approved": False})
    assert rejected.update["current_step_id"] == "g_handler"
    assert frames(rejected) == []

    # Approve lets this exit through unchecked: the For each and the Group finish.
    approved = orchestrator_hub_node({**state, "user_approved": True})
    assert approved.update["current_step_id"] == "done"
    assert frames(approved) == []


def lanes_for_each(group_policy: dict | None = None) -> dict:
    """a → Group g → For each fe with lanes (left: x) → done."""
    definition = nested(group_policy=group_policy)
    definition["steps"] = [
        step for step in definition["steps"] if step["step_id"] not in {"work", "work_handler"}
    ] + [{"step_id": "x", "step_kind": "transform", "for_each_id": "fe", "lane_id": "left"}]
    definition["for_each"][0]["body"] = {
        "kind": "lanes",
        "lanes": [{"lane_id": "left", "entry": node("x")}],
    }
    definition["execution_graph"]["edges"] = [
        route
        for route in definition["execution_graph"]["edges"]
        if route["source"] not in (node("work"), node("work_handler"))
    ] + [edge("x", {"kind": "for_each_exit", "for_each_id": "fe", "lane_id": "left"})]
    return definition


def test_a_failed_lane_of_a_for_each_item_is_caught_by_the_enclosing_group() -> None:
    state = _run(lanes_for_each(group_policy=ROUTE), items=[1, 2])
    for _ in range(2):
        state = _step(state, orchestrator_hub_node(state))
        state = _result(state)
    entered = orchestrator_hub_node(state)
    assert entered.goto == "parallel_executor"
    state = _step(state, entered)
    frame = state["run_cursor"]["frames"][-1]
    lanes = [
        {**lane, "status": "failed", "failure": {"reason": "x failed", "failure_class": "tool"}}
        for lane in state["parallel_lanes"]
    ]
    stopped = {
        **state,
        "parallel_lanes": lanes,
        "parallel_outcome": {
            "container": node("fe", "for_each"),
            "execution_id": frame["for_each_execution_id"],
            "status": "failed",
        },
    }
    command = orchestrator_hub_node(stopped)
    assert command.update["current_step_id"] == "g_handler"
    assert frames(command) == []


def test_completed_lanes_move_a_for_each_to_its_next_item() -> None:
    state = _run(lanes_for_each(), items=[1, 2])
    for _ in range(2):
        state = _step(state, orchestrator_hub_node(state))
        state = _result(state)
    state = _step(state, orchestrator_hub_node(state))
    frame = state["run_cursor"]["frames"][-1]
    stopped = {
        **state,
        "parallel_lanes": [
            {**lane, "status": "completed", "step_id": None} for lane in state["parallel_lanes"]
        ],
        "parallel_outcome": {
            "container": node("fe", "for_each"),
            "execution_id": frame["for_each_execution_id"],
            "status": "completed",
        },
    }
    command = orchestrator_hub_node(stopped)
    assert command.goto == "parallel_executor"
    assert command.update["run_cursor"]["frames"][-1]["item_index"] == 1
    assert command.update["workflow_data"]["item"] == 2


def test_a_serial_child_wait_belongs_to_the_item_execution() -> None:
    definition = nested()
    definition["steps"][2] = {
        "step_id": "work",
        "step_kind": "invoke_workflow",
        "for_each_id": "fe",
    }
    state = _run(definition, items=[1, 2])
    for _ in range(2):
        state = _step(state, orchestrator_hub_node(state))
        state = _result(state)
    first = orchestrator_hub_node(state)
    assert first.goto == "invoke_workflow_step"
    wait = first.update["pending_wait"]
    assert wait["kind"] == "child"
    assert wait["step_execution_id"] == first.update["run_cursor"]["step_execution_id"]
    state = _step(state, first)
    # A resume that redelivers the same result moves to the next item exactly once.
    done = _result(state, actor="invoke_workflow")
    second = orchestrator_hub_node(done)
    assert second.update["run_cursor"]["frames"][-1]["item_index"] == 1
    assert second.update["pending_wait"]["step_execution_id"] != wait["step_execution_id"]


@pytest.mark.parametrize(
    "policies",
    [
        {},
        {"step_policy": FAIL},
        {"for_each_policy": ROUTE},
        {"group_policy": {"on_error": "retry", "max_retries": 1}},
        {"for_each_policy": FAIL, "group_policy": ROUTE},
    ],
)
def test_the_validator_and_the_runtime_pick_the_same_policy_source(policies) -> None:
    """Imported or pinned definitions are judged by the policy the run will apply."""
    from core.application.orchestration.run_cursor import FrozenDefinition
    from core.application.orchestration.step_failures import effective_step_policy
    from core.application.workflows.workflow_definition_validation import (
        _Validator,
        effective_error_policy_source,
    )

    definition = nested(**policies)
    validator = _Validator(definition)
    frozen = FrozenDefinition(definition)
    step = validator.steps["work"]
    expected, source = effective_error_policy_source(
        step_policy=step.error_policy, containers=validator.step_containers(step)
    )
    runtime = effective_step_policy(frozen.step("work"), frozen.step_containers("work"))
    assert runtime.on_error == expected
    runtime_source = (runtime.source, runtime.source_id)
    expected_source = source if source is not None else ("default", None)
    assert runtime_source == expected_source


@pytest.mark.parametrize(
    "artifacts",
    [
        # The hook's result does not fit; a retry would see the same child result.
        {
            "_invoke_workflow_error": "workflow.hook.mapped_output_missing",
            "hook_error": {"code": "workflow.hook.mapped_output_missing", "message": "x"},
        },
        # The connected workflow no longer fits the binding.
        {"_invoke_workflow_error": "hook_binding_incompatible"},
    ],
)
def test_a_hook_error_in_an_item_is_caught_by_the_outer_group(artifacts) -> None:
    """A hook keeps Invoke's ordinary On error: its container's policy takes the error."""
    state = _run(nested(group_policy=ROUTE), items=[1, 2])
    for _ in range(2):  # a, prep
        state = _step(state, orchestrator_hub_node(state))
        state = _result(state)
    state = _step(state, orchestrator_hub_node(state))
    failed = _result(state, "fail", actor="invoke_workflow", **artifacts)

    command = orchestrator_hub_node(failed)

    assert command.update["current_step_id"] == "g_handler"
    assert frames(command) == []
