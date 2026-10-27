from __future__ import annotations

import copy
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

import core.application.orchestration.agents.common as agents_common
from core.application.orchestration.agent_messages import build_agent_message
from core.application.orchestration.run_cursor import RUNTIME_SCHEMA_VERSION
from core.application.orchestration.run_definition import (
    clear_run_definition_cache,
    prime_run_definition,
)
from core.application.orchestration.step_hub import orchestrator_hub_node
from core.application.workflows.workflow_definition_snapshot import definition_digest
from tests._workflow_definitions import edge, linear_graph, node


@pytest.fixture(autouse=True)
def _isolated(monkeypatch):
    clear_run_definition_cache()
    monkeypatch.setattr(agents_common, "emit_event", lambda *args, **kwargs: None)
    yield
    clear_run_definition_cache()


def _run(definition: dict, **state) -> dict:
    ref = {"snapshot_id": "snap", "revision": 1, "digest": definition_digest(definition)}
    prime_run_definition(ref, definition)
    return {
        "workflow_id": "wf",
        "runtime_schema_version": RUNTIME_SCHEMA_VERSION,
        "workflow_definition_ref": ref,
        "agent_messages": [],
        **state,
    }


def _definition(steps, edges, entry="a", groups=None, for_each=None) -> dict:
    return {
        "steps": steps,
        "groups": groups or [],
        "for_each": for_each or [],
        "execution_graph": {"schema_version": 1, "entry": node(entry), "edges": edges},
        "settings": {},
    }


LINEAR = _definition(
    [
        {"step_id": "a", "step_kind": "transform"},
        {"step_id": "b", "step_kind": "http_request"},
        {"step_id": "done", "step_kind": "stop_success"},
    ],
    linear_graph(["a", "b", "done"])["edges"],
)


def _step(state: dict, command) -> dict:
    """Apply a hub command to the state the way LangGraph would."""
    merged = dict(state)
    for key, value in (command.update or {}).items():
        if key == "orchestrator_journal":
            merged[key] = [*(merged.get(key) or []), *value]
        else:
            merged[key] = value
    return merged


def _result(
    state: dict, status: str = "pass", *, actor: str = "transform_step", **artifacts
) -> dict:
    cursor = state["run_cursor"]
    message = build_agent_message(
        from_agent=actor,
        to_agent="orchestrator",
        skill="step",
        status=status,
        correlation_id=f"{cursor['step_execution_id']}:{cursor['attempt']}",
        artifacts=artifacts,
        summary=f"{status} result",
        published_at=datetime.now(UTC).isoformat(),
        step_id=cursor["step_id"],
        step_execution_id=cursor["step_execution_id"],
        attempt=cursor["attempt"],
    )
    return {**state, "agent_messages": [*state["agent_messages"], message]}


def test_a_run_follows_the_graph_from_entry_to_stop_success() -> None:
    state = _run(LINEAR)
    first = orchestrator_hub_node(state)
    assert first.goto == "execute_step"
    state = _step(state, first)
    assert state["current_step_id"] == "a"
    entry_execution = state["run_cursor"]["step_execution_id"]
    assert state["dispatched_step_execution"]["step_execution_id"] == entry_execution

    second = orchestrator_hub_node(_result(state))
    state = _step(_result(state), second)
    assert (second.goto, state["current_step_id"]) == ("execute_step", "b")
    assert state["run_cursor"]["step_execution_id"] != entry_execution

    third = orchestrator_hub_node(_result(state, actor="http_request_step"))
    state = _step(_result(state, actor="http_request_step"), third)
    assert (third.goto, state["current_step_id"]) == ("control_flow_step", "done")

    end = orchestrator_hub_node(_result(state, actor="control_flow_step"))
    assert end.goto == "orchestrator_validation"
    assert end.update["workflow_terminal"]["kind"] == "stop_success"
    assert end.update["workflow_terminal"]["status"] == "pass"


def test_an_older_attempt_never_completes_the_current_one() -> None:
    state = _step(_run(LINEAR), orchestrator_hub_node(_run(LINEAR)))
    stale = _result(state)
    stale["run_cursor"] = {**state["run_cursor"], "attempt": 2}
    stale["dispatched_step_execution"] = {**state["dispatched_step_execution"], "attempt": 2}
    command = orchestrator_hub_node(stale)
    assert command.goto == "orchestrator_validation"
    assert command.update["unresolved_step_failure"]["error_code"]
    assert command.update["status"] == "failed"


def test_a_branch_outcome_picks_its_own_edge() -> None:
    definition = _definition(
        [
            {"step_id": "a", "step_kind": "if"},
            {"step_id": "yes", "step_kind": "stop_success"},
            {"step_id": "no", "step_kind": "stop_fail"},
        ],
        [edge("a", node("yes"), "true"), edge("a", node("no"), "false")],
    )
    state = _step(_run(definition), orchestrator_hub_node(_run(definition)))
    branched = _result(state, actor="control_flow_step", _outcome={"name": "false"})
    state = _step(branched, orchestrator_hub_node(branched))
    assert state["current_step_id"] == "no"
    end = orchestrator_hub_node(
        _result(state, actor="control_flow_step", terminal="fail", reason="Rejected by rule")
    )
    assert end.update["status"] == "failed"
    assert end.update["error"] == "Stopped by the workflow: Rejected by rule"
    assert end.update["workflow_terminal"]["status"] == "fail"


def test_exclusive_branches_meet_at_one_step_that_runs_once() -> None:
    definition = _definition(
        [
            {"step_id": "a", "step_kind": "if"},
            {"step_id": "left", "step_kind": "transform"},
            {"step_id": "right", "step_kind": "transform"},
            {"step_id": "c", "step_kind": "transform"},
            {"step_id": "done", "step_kind": "stop_success"},
        ],
        [
            edge("a", node("left"), "true"),
            edge("a", node("right"), "false"),
            edge("left", node("c")),
            edge("right", node("c")),
            edge("c", node("done")),
        ],
    )
    state = _step(_run(definition), orchestrator_hub_node(_run(definition)))
    branched = _result(state, actor="control_flow_step", _outcome={"name": "true"})
    state = _step(branched, orchestrator_hub_node(branched))
    visited = [state["current_step_id"]]
    while state["current_step_id"] != "done":
        finished = _result(state)
        state = _step(finished, orchestrator_hub_node(finished))
        visited.append(state["current_step_id"])
    assert visited == ["left", "c", "done"]


def test_a_stop_inside_a_loop_body_ends_the_whole_run() -> None:
    definition = _loop_definition()
    definition["steps"].insert(2, {"step_id": "halt", "step_kind": "stop_fail", "for_each_id": "g"})
    definition["steps"][1] = {"step_id": "l1", "step_kind": "if", "for_each_id": "g"}
    definition["execution_graph"]["edges"] = [
        edge("a", node("g", "for_each")),
        edge("l1", {"kind": "for_each_exit", "for_each_id": "g", "lane_id": None}, "true"),
        edge("l1", node("halt"), "false"),
        edge("g", node("done"), source_kind="for_each"),
    ]
    start = _run(definition, items=["x", "y", "z"])
    state = _step(start, orchestrator_hub_node(start))
    done_a = _result(state)
    state = _step(done_a, orchestrator_hub_node(done_a))
    halted = _result(state, actor="control_flow_step", _outcome={"name": "false"})
    state = _step(halted, orchestrator_hub_node(halted))
    assert (state["current_step_id"], state["workflow_data"]["current_item"]) == ("halt", "x")

    end = orchestrator_hub_node(
        _result(state, actor="control_flow_step", terminal="fail", reason="Bad item")
    )

    assert end.goto == "orchestrator_validation"
    assert end.update["workflow_terminal"]["kind"] == "stop_fail"
    assert end.update["status"] == "failed"


def _failing(policy: dict | None, extra_edges=()):
    step = {"step_id": "a", "step_kind": "transform"}
    if policy is not None:
        step["error_policy"] = policy
    return _definition(
        [
            step,
            {"step_id": "b", "step_kind": "transform"},
            {"step_id": "fix", "step_kind": "transform"},
            {"step_id": "done", "step_kind": "stop_success"},
        ],
        [edge("a", node("b")), edge("b", node("done")), edge("fix", node("done")), *extra_edges],
    )


def _first_failure(definition: dict) -> tuple[dict, object]:
    state = _step(_run(definition), orchestrator_hub_node(_run(definition)))
    failed = _result(state, "fail")
    return failed, orchestrator_hub_node(failed)


def test_an_unhandled_failure_is_terminal_with_its_execution_identity() -> None:
    state, command = _first_failure(_failing(None))
    failure = command.update["unresolved_step_failure"]
    assert command.goto == "orchestrator_validation"
    assert failure["step_id"] == "a"
    assert failure["step_execution_id"] == state["run_cursor"]["step_execution_id"]


def test_continue_takes_the_success_edge_and_records_the_original_status() -> None:
    state, command = _first_failure(_failing({"on_error": "continue"}))
    assert _step(state, command)["current_step_id"] == "b"
    marker = command.update["orchestrator_journal"][0]["detail"]["_step_error_policy"]
    assert marker == {"on_error": "continue", "applied": True, "original_status": "fail"}


def test_retry_keeps_the_execution_and_escalates_when_exhausted() -> None:
    definition = _failing({"on_error": "retry", "max_retries": 1})
    state, command = _first_failure(definition)
    state = _step(state, command)
    assert state["current_step_id"] == "a"
    assert state["run_cursor"]["attempt"] == 2
    again = _result(state, "fail")
    escalated = orchestrator_hub_node(again)
    assert escalated.goto == "human_approval_gate"
    assert (
        escalated.update["recovery_pending"]["step_execution_id"]
        == state["run_cursor"]["step_execution_id"]
    )


def test_route_to_step_follows_the_steps_error_edge() -> None:
    definition = _failing({"on_error": "route_to_step"}, [edge("a", node("fix"), "error")])
    state, command = _first_failure(definition)
    assert _step(state, command)["current_step_id"] == "fix"


def test_an_exhausted_rework_budget_pauses_and_approval_adds_budget_once() -> None:
    definition = _definition(
        [
            {"step_id": "a", "step_kind": "transform"},
            {"step_id": "check", "step_kind": "if"},
            {"step_id": "done", "step_kind": "stop_success"},
        ],
        [
            edge("a", node("check")),
            edge("check", node("done"), "true"),
            {**edge("check", node("a"), "false", rework=True), "rework_limit": 1},
        ],
    )
    state = _step(_run(definition), orchestrator_hub_node(_run(definition)))

    def through_check(current: dict) -> dict:
        done_a = _result(current)
        current = _step(done_a, orchestrator_hub_node(done_a))
        assert current["current_step_id"] == "check"
        return _result(current, actor="control_flow_step", _outcome={"name": "false"})

    rework = through_check(state)
    state = _step(rework, orchestrator_hub_node(rework))
    assert state["current_step_id"] == "a"

    blocked_state = through_check(state)
    blocked = orchestrator_hub_node(blocked_state)
    assert blocked.goto == "human_approval_gate"
    pending = blocked.update["recovery_pending"]
    assert pending["allowed_actions"] == ["approve", "abort"]
    state = _step(blocked_state, blocked)

    rejected = orchestrator_hub_node({**state, "user_approved": False})
    assert rejected.goto == "human_approval_gate"
    assert "recovery_pending" not in rejected.update

    approved = orchestrator_hub_node({**state, "user_approved": True})
    state = _step({**state, "user_approved": True}, approved)
    assert state["current_step_id"] == "a"
    assert state["recovery_pending"] is None
    counters = next(iter(state["run_cursor"]["rework"]["edges"].values()))
    assert counters == {"used": 2, "allowance": 2}


def test_two_hundred_linear_steps_run_to_their_stop_without_the_runaway_guard() -> None:
    ids = [f"s{index}" for index in range(200)]
    definition = _definition(
        [{"step_id": step_id, "step_kind": "transform"} for step_id in ids]
        + [{"step_id": "done", "step_kind": "stop_success"}],
        linear_graph([*ids, "done"])["edges"],
        entry="s0",
    )
    state = _step(_run(definition), orchestrator_hub_node(_run(definition)))
    for _ in ids:
        finished = _result(state)
        state = _step(finished, orchestrator_hub_node(finished))
    assert state["current_step_id"] == "done"
    end = orchestrator_hub_node(_result(state, actor="control_flow_step"))
    assert end.update["workflow_terminal"]["kind"] == "stop_success"


def test_a_long_loop_with_a_two_step_body_is_not_a_runaway() -> None:
    definition = _loop_definition()
    definition["steps"].insert(2, {"step_id": "l2", "step_kind": "transform", "for_each_id": "g"})
    definition["execution_graph"]["edges"] = [
        edge("a", node("g", "for_each")),
        edge("l1", node("l2")),
        edge("l2", {"kind": "for_each_exit", "for_each_id": "g", "lane_id": None}),
        edge("g", node("done"), source_kind="for_each"),
    ]
    items = [f"item-{index}" for index in range(60)]
    start = _run(definition, items=items)
    state = _step(start, orchestrator_hub_node(start))
    for _ in range(1 + 2 * len(items)):
        finished = _result(state)
        state = _step(finished, orchestrator_hub_node(finished))
    assert (state["current_step_id"], state["run_cursor"]["frames"]) == ("done", [])


def test_the_rework_budget_escalates_before_the_runaway_guard_could_stop_the_run() -> None:
    definition = _definition(
        [
            {"step_id": "a", "step_kind": "transform"},
            {"step_id": "check", "step_kind": "if"},
            {"step_id": "done", "step_kind": "stop_success"},
        ],
        [
            edge("a", node("check")),
            edge("check", node("done"), "true"),
            edge("check", node("a"), "false", rework=True),
        ],
    )
    state = _step(_run(definition), orchestrator_hub_node(_run(definition)))
    command = None
    for _ in range(51):
        done_a = _result(state)
        state = _step(done_a, orchestrator_hub_node(done_a))
        assert state["current_step_id"] == "check"
        rework = _result(state, actor="control_flow_step", _outcome={"name": "false"})
        command = orchestrator_hub_node(rework)
        state = _step(rework, command)
    assert command is not None
    assert command.goto == "human_approval_gate"
    assert command.update["recovery_pending"]["allowed_actions"] == ["approve", "abort"]
    assert state.get("status") != "stuck"


def test_a_phase_runtime_checkpoint_is_refused() -> None:
    command = orchestrator_hub_node({"job_id": "j", "phase_stack": [{"phase_id": "build"}]})
    assert command.goto == "orchestrator_validation"
    assert command.update["status"] == "failed"


@pytest.mark.parametrize("version", [None, 1, RUNTIME_SCHEMA_VERSION])
def test_partial_definition_ref_preserves_the_original_terminal_failure(monkeypatch, version):
    def never_pin(*_args):
        pytest.fail("Malformed checkpoint must not pin a new definition")

    monkeypatch.setattr("core.application.orchestration.run_definition._default_pin", never_pin)
    state = {
        "job_id": "job-upload",
        "workflow_id": "wf-upload",
        "runtime_schema_version": version,
        "workflow_definition_ref": {"revision": 1, "digest": "a" * 64},
    }

    command = orchestrator_hub_node(state)

    assert command.goto == "orchestrator_validation"
    assert command.update["status"] == "failed"
    assert command.update["last_failure_class"] == "execution_contract"
    assert command.update["orchestrator_journal"][-1]["event"] == "workflow_definition_unavailable"
    if version == RUNTIME_SCHEMA_VERSION:
        assert command.update["error"] == "The run state has no pinned workflow definition"
    else:
        assert command.update["error"] == (
            f"Run state version {version!r} cannot be continued by runtime version 2; start a new run."
        )
    assert "workflow_run_trace" not in command.update
    assert "workflow_definition_ref" not in command.update
    assert "runtime_schema_version" not in command.update


@pytest.mark.parametrize("version", [None, 1])
@pytest.mark.parametrize(
    "ref_patch", [{"revision": "not-a-number"}, {"revision": 0}, {"digest": ""}]
)
def test_invalid_trace_ref_cannot_mask_incompatible_runtime_failure(
    monkeypatch, version, ref_patch
):
    def never_pin(*_args):
        pytest.fail("Malformed checkpoint must not pin a new definition")

    monkeypatch.setattr("core.application.orchestration.run_definition._default_pin", never_pin)
    command = orchestrator_hub_node(
        {
            "job_id": "job-upload",
            "workflow_id": "wf-upload",
            "runtime_schema_version": version,
            "workflow_definition_ref": {
                "snapshot_id": "snapshot-upload",
                "revision": 4,
                "digest": "a" * 64,
                **ref_patch,
            },
        }
    )

    assert command.goto == "orchestrator_validation"
    assert command.update["status"] == "failed"
    assert command.update["last_failure_class"] == "execution_contract"
    assert command.update["error"] == (
        f"Run state version {version!r} cannot be continued by runtime version 2; start a new run."
    )
    assert "workflow_run_trace" not in command.update


def test_a_step_test_run_never_follows_the_graph() -> None:
    from core.application.orchestration.run_cursor import RunCursor, begin_step

    cursor = begin_step(RunCursor(), "b")
    state = _run(
        LINEAR,
        task_source="step_test_run",
        workflow_test_run_execution_id="exec-1",
        step_test_run_context={"workflow_id": "wf", "execution_id": "exec-1", "step_id": "b"},
        run_cursor=cursor.dump(),
    )
    dispatched = orchestrator_hub_node(state)
    assert dispatched.goto == "execute_step"
    state = _step(state, dispatched)
    finished = orchestrator_hub_node(_result(state, actor="http_request_step"))
    assert finished.goto == "orchestrator_validation"
    assert finished.update["step_test_run_terminal"]["step_id"] == "b"
    assert "current_step_id" not in finished.update


def test_a_state_resumed_approval_goes_back_to_its_gate_for_the_decision() -> None:
    definition = _definition(
        [
            {"step_id": "a", "step_kind": "approval"},
            {"step_id": "ok", "step_kind": "stop_success"},
            {"step_id": "no", "step_kind": "stop_fail"},
        ],
        [edge("a", node("ok"), "approved"), edge("a", node("no"), "rejected")],
    )
    state = _step(_run(definition), orchestrator_hub_node(_run(definition)))
    assert state["current_step_id"] == "a"
    resumed = orchestrator_hub_node({**state, "user_approved": True})
    assert resumed.goto == "human_approval_gate"
    assert "unresolved_step_failure" not in resumed.update


def test_a_failed_result_with_contract_errors_is_regenerated_before_its_error_policy() -> None:
    state = _step(_run(LINEAR), orchestrator_hub_node(_run(LINEAR)))
    failed = _result(state, "fail", _contract_errors=["output 'rows' is missing"])
    command = orchestrator_hub_node(failed)
    state = _step(failed, command)
    assert command.goto == "execute_step"
    assert (state["current_step_id"], state["run_cursor"]["attempt"]) == ("a", 2)
    assert "output 'rows' is missing" in state["feedback"]


def test_an_approval_does_not_bypass_contract_errors() -> None:
    state = _step(_run(LINEAR), orchestrator_hub_node(_run(LINEAR)))
    passed = {**_result(state, _contract_errors=["bad shape"]), "user_approved": True}
    command = orchestrator_hub_node(passed)
    assert _step(passed, command)["run_cursor"]["attempt"] == 2


def test_a_cancelled_run_goes_to_validation_without_routing() -> None:
    command = orchestrator_hub_node({**_run(LINEAR), "status": "cancelled"})
    assert command.goto == "orchestrator_validation"
    assert command.update["status"] == "cancelled"
    assert "run_cursor" not in command.update


def test_runaway_orchestration_is_stopped_by_the_iteration_guard() -> None:
    from core.application.orchestration.step_hub_support import MAX_TOTAL_ITERATIONS

    command = orchestrator_hub_node({**_run(LINEAR), "iteration_count": MAX_TOTAL_ITERATIONS})
    assert command.goto == "orchestrator_validation"
    assert command.update["status"] == "stuck"


@pytest.mark.parametrize(
    ("summary", "expected"),
    [
        ("bwrap: no permissions to create a new namespace", "platform_blocker"),
        ("Waiting for the customer to confirm the address", "human_blocked"),
    ],
)
def test_a_blocked_result_is_classified_as_platform_or_human(summary, expected) -> None:
    from core.application.orchestration.step_failures import infer_failure_class

    assert infer_failure_class({"status": "blocked", "summary": summary}) == (expected, False)


def _loop_definition(group_policy: dict | None = None) -> dict:
    """a → For each 'g' over state.items (body l1) → done; error → failed under a policy."""
    item = {
        "for_each_id": "g",
        "over": "state.items",
        "item_name": "current_item",
        "body": {"kind": "sequence", "entry": node("l1")},
    }
    edges = [
        edge("a", node("g", "for_each")),
        edge("l1", {"kind": "for_each_exit", "for_each_id": "g", "lane_id": None}),
        edge("g", node("done"), source_kind="for_each"),
    ]
    if group_policy is not None:
        item["error_policy"] = group_policy
        edges.append(edge("g", node("failed"), "error", source_kind="for_each"))
    return _definition(
        [
            {"step_id": "a", "step_kind": "transform"},
            {"step_id": "l1", "step_kind": "transform", "for_each_id": "g"},
            {"step_id": "done", "step_kind": "stop_success"},
            {"step_id": "failed", "step_kind": "stop_fail"},
        ],
        edges,
        for_each=[item],
    )


def _group_definition(group_policy: dict | None = None) -> dict:
    """a → run-once Group 'g' (body l1) → done; error → failed under a policy."""
    group = {"group_id": "g", "body": {"kind": "sequence", "entry": node("l1")}}
    edges = [
        edge("a", node("g", "group")),
        edge("l1", {"kind": "group_exit", "group_id": "g", "lane_id": None}),
        edge("g", node("done"), source_kind="group"),
    ]
    if group_policy is not None:
        group["error_policy"] = group_policy
        edges.append(edge("g", node("failed"), "error", source_kind="group"))
    return _definition(
        [
            {"step_id": "a", "step_kind": "transform"},
            {"step_id": "l1", "step_kind": "transform", "group_id": "g"},
            {"step_id": "done", "step_kind": "stop_success"},
            {"step_id": "failed", "step_kind": "stop_fail"},
        ],
        edges,
        groups=[group],
    )


def test_a_loop_runs_its_body_once_per_item_then_leaves_by_success() -> None:
    definition = _loop_definition()
    state = _step(
        _run(definition, items=["x", "y"]),
        orchestrator_hub_node(_run(definition, items=["x", "y"])),
    )
    done_a = _result(state)
    state = _step(done_a, orchestrator_hub_node(done_a))
    assert (state["current_step_id"], state["current_group_id"]) == ("l1", None)
    assert state["run_cursor"]["frames"][-1]["frame"] == "for_each"
    assert state["workflow_data"]["current_item"] == "x"
    first_item_execution = state["run_cursor"]["step_execution_id"]

    item_done = _result(state)
    state = _step(item_done, orchestrator_hub_node(item_done))
    assert state["current_step_id"] == "l1"
    assert state["workflow_data"]["current_item"] == "y"
    assert state["run_cursor"]["step_execution_id"] != first_item_execution
    assert state["bounded_loop_iterations"] == 1

    last = _result(state)
    state = _step(last, orchestrator_hub_node(last))
    assert (state["current_step_id"], state["current_group_id"]) == ("done", None)
    assert state["run_cursor"]["frames"] == []


def test_an_empty_loop_leaves_by_its_success_edge_without_running_the_body() -> None:
    definition = _loop_definition()
    start = _run(definition, items=[])
    state = _step(start, orchestrator_hub_node(start))
    done_a = _result(state)
    state = _step(done_a, orchestrator_hub_node(done_a))
    assert state["current_step_id"] == "done"


def test_a_failure_routed_by_the_group_leaves_by_the_groups_error_edge() -> None:
    definition = _loop_definition({"on_error": "route_to_step"})
    start = _run(definition, items=["x", "y"])
    state = _step(start, orchestrator_hub_node(start))
    done_a = _result(state)
    state = _step(done_a, orchestrator_hub_node(done_a))
    failed = _result(state, "fail")
    state = _step(failed, orchestrator_hub_node(failed))
    assert (state["current_step_id"], state["run_cursor"]["frames"]) == ("failed", [])


def test_an_unreadable_loop_source_ends_the_run_when_the_group_does_not_route() -> None:
    definition = _loop_definition()
    definition["for_each"][0]["over"] = "rows[abc]"
    start = _run(definition)
    state = _step(start, orchestrator_hub_node(start))
    done_a = _result(state)
    command = orchestrator_hub_node(done_a)
    assert command.goto == "orchestrator_validation"
    assert command.update["unresolved_step_failure"]["error_code"] == "invalid_input_selector"


def _parallel_definition(group_policy: dict | None = None) -> dict:
    group = {
        "group_id": "p",
        "body": {
            "kind": "lanes",
            "lanes": [
                {"lane_id": "left", "entry": node("x1")},
                {"lane_id": "right", "entry": node("y1")},
            ],
        },
    }
    edges = [
        edge("a", node("p", "group")),
        edge("x1", {"kind": "group_exit", "group_id": "p", "lane_id": "left"}),
        edge("y1", {"kind": "group_exit", "group_id": "p", "lane_id": "right"}),
        edge("p", node("done"), source_kind="group"),
    ]
    if group_policy is not None:
        group["error_policy"] = group_policy
        edges.append(edge("p", node("failed"), "error", source_kind="group"))
    return _definition(
        [
            {"step_id": "a", "step_kind": "transform"},
            {"step_id": "x1", "step_kind": "transform", "group_id": "p", "lane_id": "left"},
            {"step_id": "y1", "step_kind": "transform", "group_id": "p", "lane_id": "right"},
            {"step_id": "done", "step_kind": "stop_success"},
            {"step_id": "failed", "step_kind": "stop_fail"},
        ],
        edges,
        groups=[group],
    )


def _into_lanes(definition: dict) -> dict:
    state = _step(_run(definition), orchestrator_hub_node(_run(definition)))
    done_a = _result(state)
    command = orchestrator_hub_node(done_a)
    assert command.goto == "parallel_executor"
    return _step(done_a, command)


def _lanes_stopped(state: dict, status: str, **lane_changes) -> dict:
    lanes = [dict(lane) for lane in state["parallel_lanes"]]
    for lane in lanes:
        lane.update(lane_changes.get(lane["lane_id"], {"status": "completed", "step_id": None}))
    frame = state["run_cursor"]["frames"][-1]
    return {
        **state,
        "parallel_lanes": lanes,
        "parallel_outcome": {
            "container": node("p", "group"),
            "execution_id": frame["group_execution_id"],
            "status": status,
        },
    }


def test_entering_a_parallel_group_hands_every_lane_to_the_executor() -> None:
    state = _into_lanes(_parallel_definition())
    assert state["current_group_id"] == "p"
    assert state["run_cursor"]["step_id"] is None
    assert {lane["lane_id"]: lane["step_id"] for lane in state["parallel_lanes"]} == {
        "left": "x1",
        "right": "y1",
    }


def test_completed_lanes_leave_the_group_by_its_success_edge() -> None:
    state = _lanes_stopped(_into_lanes(_parallel_definition()), "completed")
    state = _step(state, orchestrator_hub_node(state))
    assert (state["current_step_id"], state["run_cursor"]["frames"]) == ("done", [])


def test_a_failed_lane_leaves_a_routing_group_by_its_error_edge() -> None:
    state = _lanes_stopped(
        _into_lanes(_parallel_definition({"on_error": "route_to_step"})),
        "failed",
        left={
            "status": "failed",
            "failure": {"reason": "x1 failed", "failure_class": "validation"},
        },
    )
    state = _step(state, orchestrator_hub_node(state))
    assert state["current_step_id"] == "failed"


def test_an_escalated_lane_asks_once_and_a_reject_resumes_only_that_lane() -> None:
    entered = _into_lanes(_parallel_definition())
    failure = {
        "step_id": "y1",
        "step_execution_id": "22222222-2222-2222-2222-222222222222",
        "attempt": 1,
        "reason": "y1 needs review",
        "failure_class": "validation",
    }
    state = _lanes_stopped(
        entered,
        "escalated",
        right={
            "status": "escalated",
            "failure": failure,
            "step_execution_id": failure["step_execution_id"],
        },
    )
    paused = orchestrator_hub_node(state)
    assert paused.goto == "human_approval_gate"
    assert paused.update["recovery_pending"]["kind"] == "lane_failure"
    state = _step(state, paused)

    resumed = orchestrator_hub_node({**state, "user_approved": False, "review_feedback": "Fix y1"})
    assert resumed.goto == "parallel_executor"
    lanes = {lane["lane_id"]: lane for lane in resumed.update["parallel_lanes"]}
    assert lanes["left"]["status"] == "completed"
    assert lanes["right"]["decision"] == {"action": "retry", "feedback": "Fix y1"}
    assert lanes["right"]["attempt"] == 2


def test_a_lane_operation_retry_waits_and_targets_the_parallel_executor() -> None:
    import time as _time

    entered = _into_lanes(_parallel_definition())
    operation = {
        "identity": "operation:abc",
        "retry_number": 1,
        "deadline_epoch_seconds": _time.time() + 300,
        "delay_seconds": 1.0,
        "signal": {},
        "telemetry": {},
        "position": 0,
    }
    state = _lanes_stopped(
        entered,
        "retry_operation",
        right={
            "status": "retry_operation",
            "operation": operation,
            "step_execution_id": "33333333-3333-3333-3333-333333333333",
            "attempt": 1,
        },
    )
    command = orchestrator_hub_node(state)
    assert command.goto == "operation_retry_wait"
    dispatch = command.update["operation_retry_dispatch"]
    assert (dispatch["target"], dispatch["lane_id"], dispatch["attempt"]) == (
        "parallel_executor",
        "right",
        2,
    )


def _bounded(contract: dict, **step_fields) -> dict:
    return _definition(
        [
            {
                "step_id": "a",
                "step_kind": "transform",
                "boundary_contract": contract,
                **step_fields,
            },
            {"step_id": "b", "step_kind": "transform"},
            {"step_id": "done", "step_kind": "stop_success"},
        ],
        linear_graph(["a", "b", "done"])["edges"],
    )


def test_a_missing_required_entry_input_blocks_the_step_before_it_runs() -> None:
    definition = _bounded(
        {"entry": [{"name": "topic", "type": "text", "source": "state.topic", "required": True}]}
    )
    command = orchestrator_hub_node(_run(definition))
    assert command.goto == "orchestrator_validation"
    assert command.update["unresolved_step_failure"]["missing"] == ["topic"]


def test_entry_inputs_are_materialized_for_the_step() -> None:
    definition = _bounded(
        {"entry": [{"name": "topic", "type": "text", "source": "state.topic", "required": True}]}
    )
    command = orchestrator_hub_node(_run(definition, topic="Quarterly report"))
    assert command.goto == "execute_step"
    assert command.update["materialized_inputs"] == {"topic": "Quarterly report"}


def test_a_success_without_its_exit_output_is_regenerated() -> None:
    definition = _bounded({"exit": ["summary"]})
    state = _step(_run(definition), orchestrator_hub_node(_run(definition)))
    command = orchestrator_hub_node(_result(state))
    state = _step(_result(state), command)
    assert (state["current_step_id"], state["run_cursor"]["attempt"]) == ("a", 2)
    assert "summary" in state["feedback"]


def test_a_success_with_its_exit_output_continues() -> None:
    definition = _bounded({"exit": ["summary"]})
    state = _step(_run(definition), orchestrator_hub_node(_run(definition)))
    done = _result(state, summary="ready")
    assert _step(done, orchestrator_hub_node(done))["current_step_id"] == "b"


def test_exit_on_checks_only_the_outcomes_that_leave_the_boundary() -> None:
    definition = _definition(
        [
            {
                "step_id": "a",
                "step_kind": "if",
                "boundary_contract": {"exit": ["summary"], "exit_on": [{"name": "true"}]},
            },
            {"step_id": "inside", "step_kind": "transform"},
            {"step_id": "done", "step_kind": "stop_success"},
        ],
        [
            edge("a", node("done"), "true"),
            edge("a", node("inside"), "false"),
            edge("inside", node("done")),
        ],
    )
    state = _step(_run(definition), orchestrator_hub_node(_run(definition)))
    inner = _result(state, actor="control_flow_step", _outcome={"name": "false"})
    assert _step(inner, orchestrator_hub_node(inner))["current_step_id"] == "inside"
    leaving = _result(state, actor="control_flow_step", _outcome={"name": "true"})
    assert _step(leaving, orchestrator_hub_node(leaving))["run_cursor"]["attempt"] == 2


def test_a_group_exit_contract_failure_ends_the_run_or_takes_the_routing_edge() -> None:
    plain = _loop_definition()
    plain["for_each"][0]["boundary_contract"] = {"exit": ["state.report"]}
    start = _run(plain, items=["x"])
    state = _step(start, orchestrator_hub_node(start))
    state = _step(_result(state), orchestrator_hub_node(_result(state)))
    last = _result(state)
    ended = orchestrator_hub_node(last)
    assert ended.goto == "orchestrator_validation"
    assert ended.update["unresolved_step_failure"]["error_code"] == (
        "container_exit_outputs_missing"
    )

    routed = _loop_definition({"on_error": "route_to_step"})
    routed["for_each"][0]["boundary_contract"] = {"exit": ["state.report"]}
    start = _run(routed, items=["x"])
    state = _step(start, orchestrator_hub_node(start))
    state = _step(_result(state), orchestrator_hub_node(_result(state)))
    last = _result(state)
    assert _step(last, orchestrator_hub_node(last))["current_step_id"] == "failed"


# ---------------------------------------------------------------------------
# Authored Approval in a serial loop and after a group (T15)
# ---------------------------------------------------------------------------


def _approval_loop_definition() -> dict:
    item = {
        "for_each_id": "g",
        "over": "items",
        "item_name": "current_item",
        "body": {"kind": "sequence", "entry": node("draft")},
    }
    return _definition(
        [
            {"step_id": "a", "step_kind": "transform"},
            {"step_id": "draft", "step_kind": "transform", "for_each_id": "g"},
            {"step_id": "check", "step_kind": "approval", "for_each_id": "g"},
            {"step_id": "final", "step_kind": "approval"},
            {"step_id": "done", "step_kind": "stop_success"},
            {"step_id": "failed", "step_kind": "stop_fail"},
        ],
        [
            edge("a", node("g", "for_each")),
            edge("draft", node("check")),
            edge(
                "check", {"kind": "for_each_exit", "for_each_id": "g", "lane_id": None}, "approved"
            ),
            edge("check", node("draft"), "rejected", rework=True),
            edge("g", node("final"), source_kind="for_each"),
            edge("final", node("done"), "approved"),
            edge("final", node("failed"), "rejected"),
        ],
        for_each=[item],
    )


def _decide(state: dict, response) -> tuple[dict, dict]:
    """Run the Approval gate for the dispatched execution; return state and its payload."""
    from unittest.mock import patch

    from core.application.orchestration.graph.builder_approval_gate import (
        _registry_approval_gate_node,
    )

    with patch("langgraph.types.interrupt", return_value=response) as interrupt:
        command = _registry_approval_gate_node(state)
    return {**state, **command.update}, interrupt.call_args.args[0]


def _advance(state: dict) -> dict:
    return _step(state, orchestrator_hub_node(state))


def test_an_approval_in_a_serial_loop_asks_once_per_item_with_a_new_approval_id() -> None:
    definition = _approval_loop_definition()
    state = _advance(_run(definition, items=["x", "y"]))
    state = _advance(_result(state))
    assert (state["current_step_id"], state["workflow_data"]["current_item"]) == ("draft", "x")

    state = _advance(_result(state))
    assert state["current_step_id"] == "check"
    state, first = _decide(state, {"approved": True})
    state = _advance(state)
    # Approved: the item is finished and the next one starts with the body entry.
    assert (state["current_step_id"], state["workflow_data"]["current_item"]) == ("draft", "y")

    state = _advance(_result(state))
    state, second = _decide(state, {"approved": True})
    assert second["approval_id"] != first["approval_id"]
    assert second["step_id"] == "check"

    state = _advance(state)
    assert (state["current_step_id"], state["run_cursor"]["frames"]) == ("final", [])


def test_a_rejected_item_is_reworked_inside_its_own_iteration_with_the_feedback() -> None:
    definition = _approval_loop_definition()
    state = _advance(_run(definition, items=["x", "y"]))
    state = _advance(_result(state))
    state = _advance(_result(state))
    state, first = _decide(state, {"approved": False})
    state = _advance(state)

    # Same item, the draft step again, with the default rework request.
    assert (state["current_step_id"], state["workflow_data"]["current_item"]) == ("draft", "x")
    assert state["review_feedback"]
    assert state["run_cursor"]["frames"][-1]["item_index"] == 0

    state = _advance(_result(state))
    state, again = _decide(state, {"approved": True})
    assert again["approval_id"] != first["approval_id"]
    state = _advance(state)
    assert state["workflow_data"]["current_item"] == "y"


def test_an_approval_after_a_loop_routes_by_its_decision() -> None:
    definition = _approval_loop_definition()
    state = _advance(_run(definition, items=["x"]))
    state = _advance(_result(state))
    state = _advance(_result(state))
    state, _payload = _decide(state, {"approved": True})
    state = _advance(state)
    assert state["current_step_id"] == "final"

    state, payload = _decide(state, {"approved": False, "comment": "Not this batch."})
    assert payload["step_id"] == "final"
    assert not payload.get("group_id")
    command = orchestrator_hub_node(state)
    assert command.update["run_cursor"]["step_id"] == "failed"


def test_an_approval_dispatch_records_its_pending_wait_and_the_decision_clears_it() -> None:
    definition = _approval_loop_definition()
    state = _advance(_run(definition, items=["x"]))
    state = _advance(_result(state))
    state = _advance(_result(state))
    assert state["current_step_id"] == "check"
    pending = state["pending_wait"]
    assert pending["kind"] == "approval"
    assert pending["step_execution_id"] == state["run_cursor"]["step_execution_id"]

    decided, payload = _decide(state, {"approved": True})

    assert payload["approval_id"] == pending["approval_id"]
    assert decided["pending_wait"] is None


def test_a_serial_invoke_dispatch_records_a_child_wait() -> None:
    definition = _definition(
        [
            {"step_id": "a", "step_kind": "invoke_workflow"},
            {"step_id": "done", "step_kind": "stop_success"},
        ],
        linear_graph(["a", "done"])["edges"],
    )
    state = _run(definition)
    command = orchestrator_hub_node(state)

    assert command.goto == "invoke_workflow_step"
    pending = command.update["pending_wait"]
    assert pending["kind"] == "child"
    assert pending["step_execution_id"] == command.update["run_cursor"]["step_execution_id"]
    assert "child_job_id" not in pending  # the node adds the child once it exists


def _recorded_events(run):
    from core.application.workflows.workflow_lifecycle_event_sink import (
        bind_workflow_event_sink,
    )

    events: list[dict] = []
    with bind_workflow_event_sink(lambda **event: events.append(event)):
        run()
    return [
        (event["event_type"], event["payload"].get("step_id") or event["payload"]["group_id"])
        for event in events
    ], events


def test_the_hub_publishes_step_events_with_their_execution_identity() -> None:
    def run() -> None:
        state = _run(LINEAR, job_id="77")
        state = _step(state, orchestrator_hub_node(state))
        done_a = _result(state)
        state = _step(done_a, orchestrator_hub_node(done_a))
        failed_b = _result(state, "fail", actor="http_request_step")
        orchestrator_hub_node(failed_b)

    names, events = _recorded_events(run)

    assert names == [
        ("job.step_started", "a"),
        ("job.step_completed", "a"),
        ("job.step_started", "b"),
        ("job.step_failed", "b"),
    ]
    started_a, completed_a = events[0], events[1]
    assert started_a["payload"]["step_execution_id"] == completed_a["payload"]["step_execution_id"]
    assert completed_a["payload"]["outcome"] == "success"
    assert len({event["event_id"] for event in events}) == 4


def test_the_hub_publishes_group_events_once_per_group_execution() -> None:
    definition = _group_definition({"on_error": "route_to_step"})

    def run() -> None:
        start = _run(definition, job_id="77")
        state = _step(start, orchestrator_hub_node(start))
        done_a = _result(state)
        state = _step(done_a, orchestrator_hub_node(done_a))
        item_done = _result(state)
        _step(item_done, orchestrator_hub_node(item_done))

    names, events = _recorded_events(run)

    assert ("job.group_started", "g") in names
    assert ("job.group_completed", "g") in names
    group_events = [event for event in events if event["event_type"].startswith("job.group_")]
    assert len({event["payload"]["group_execution_id"] for event in group_events}) == 1


def test_an_empty_for_each_is_not_reported_as_a_group() -> None:
    definition = _loop_definition()

    def run() -> None:
        start = _run(definition, job_id="77", items=[])
        state = _step(start, orchestrator_hub_node(start))
        done_a = _result(state)
        orchestrator_hub_node(done_a)

    names, events = _recorded_events(run)

    assert not [event for event in events if event["event_type"].startswith("job.group_")]
    assert names[-1] == ("job.step_started", "done")


def test_without_a_bound_sink_the_hub_publishes_nothing() -> None:
    state = _run(LINEAR, job_id="77")
    assert orchestrator_hub_node(state).goto == "execute_step"


def test_switch_case_rework_approval_preserves_case_identity() -> None:
    definition = _definition(
        [
            {"step_id": "a", "step_kind": "transform"},
            {"step_id": "check", "step_kind": "switch"},
            {"step_id": "done", "step_kind": "stop_success"},
        ],
        [
            edge("a", node("check")),
            edge("check", node("done"), "default"),
            {**edge("check", node("a"), "case", case_id="retry", rework=True), "rework_limit": 1},
        ],
    )
    state = _step(_run(definition), orchestrator_hub_node(_run(definition)))

    def through_check(current: dict) -> dict:
        done_a = _result(current)
        current = _step(done_a, orchestrator_hub_node(done_a))
        assert current["current_step_id"] == "check"
        return _result(
            current, actor="control_flow_step", _outcome={"name": "case", "case_id": "retry"}
        )

    rework = through_check(state)
    state = _step(rework, orchestrator_hub_node(rework))
    assert state["current_step_id"] == "a"

    blocked_state = through_check(state)
    blocked = orchestrator_hub_node(blocked_state)
    assert blocked.goto == "human_approval_gate"
    pending = blocked.update["recovery_pending"]
    assert pending["allowed_actions"] == ["approve", "abort"]
    state = _step(blocked_state, blocked)

    rejected = orchestrator_hub_node({**state, "user_approved": False})
    assert rejected.goto == "human_approval_gate"
    assert "recovery_pending" not in rejected.update

    approved = orchestrator_hub_node({**state, "user_approved": True})
    state = _step({**state, "user_approved": True}, approved)
    assert state["current_step_id"] == "a"
    assert state["recovery_pending"] is None
    counters = next(iter(state["run_cursor"]["rework"]["edges"].values()))
    assert counters == {"used": 2, "allowance": 2}


@pytest.mark.parametrize("group_kind", ["parallel", "for_each"])
def test_group_error_rework_budget_approval_resumes_the_group_edge(group_kind):
    definition = (_parallel_definition if group_kind == "parallel" else _loop_definition)(
        {"on_error": "route_to_step"}
    )
    group_id = "p" if group_kind == "parallel" else "g"
    source = node(group_id, "group" if group_kind == "parallel" else "for_each")
    for route in definition["execution_graph"]["edges"]:
        if route["source"] == source and route["outcome"]["name"] == "error":
            route.update(rework=True, rework_limit=1)
    if group_kind == "parallel":
        state = _lanes_stopped(
            _into_lanes(definition),
            "failed",
            left={"status": "failed", "failure": {"reason": "x failed"}},
        )
    else:
        initial = _run(definition, items=["x"])
        state = _step(initial, orchestrator_hub_node(initial))
        done_a = _result(state)
        state = _step(done_a, orchestrator_hub_node(done_a))
        state = _result(state, "fail")
    state["run_cursor"]["rework"]["used"] = state["run_cursor"]["rework"]["allowance"]
    paused = orchestrator_hub_node(state)
    assert paused.goto == "human_approval_gate"
    pending = paused.update["recovery_pending"]
    assert pending["group_id"] == (group_id if group_kind == "parallel" else None)
    assert pending["source"] == source
    state = _step(state, paused)
    continued = orchestrator_hub_node({**state, "user_approved": True})
    assert continued.update["current_step_id"] == "failed"
    assert continued.update["recovery_pending"] is None


def test_failed_if_retry_exhaustion_does_not_offer_an_undefined_success_outcome():
    definition = _definition(
        [
            {
                "step_id": "a",
                "step_kind": "if",
                "override": {"if": {"condition": "state.ok"}},
                "error_policy": {"on_error": "retry", "max_retries": 1},
            },
            {"step_id": "done", "step_kind": "stop_success"},
        ],
        [edge("a", node("done"), "true"), edge("a", node("done"), "false")],
    )
    failed, retry = _first_failure(definition)
    state = _step(failed, retry)
    final = orchestrator_hub_node(_result(state, "fail", actor="control_flow_step"))
    assert final.goto == "orchestrator_validation"
    assert not final.update.get("needs_user_approval")
    assert final.update["unresolved_step_failure"]["error_code"] == "recovery_outcome_unsupported"


def test_stale_recovery_approval_cannot_choose_success_for_if():
    definition = _definition(
        [
            {"step_id": "a", "step_kind": "if", "override": {"if": {"condition": "state.ok"}}},
            {"step_id": "done", "step_kind": "stop_success"},
        ],
        [edge("a", node("done"), "true"), edge("a", node("done"), "false")],
    )
    initial = _run(definition)
    state = _step(initial, orchestrator_hub_node(initial))
    state.update(
        user_approved=True,
        recovery_pending={
            "kind": "failure",
            "step_id": "a",
            "step_execution_id": state["run_cursor"]["step_execution_id"],
            "request_id": "old-recovery",
            "allowed_actions": ["approve", "reject"],
        },
    )
    result = orchestrator_hub_node(state)
    assert result.goto == "orchestrator_validation"
    assert result.update["unresolved_step_failure"]["error_code"] == "recovery_outcome_unsupported"


@pytest.mark.parametrize("outcome,target", [("approved", "yes"), ("rejected", "no")])
def test_trace_routes_the_accepted_approval_execution_in_the_same_command(outcome, target):
    definition = _definition(
        [
            {"step_id": "a", "step_kind": "approval"},
            {"step_id": "yes", "step_kind": "stop_success"},
            {"step_id": "no", "step_kind": "stop_fail"},
        ],
        [edge("a", node("yes"), "approved"), edge("a", node("no"), "rejected")],
    )
    start = _run(definition, job_id="job-trace")
    first = orchestrator_hub_node(start)
    state = _step(start, first)
    accepted = _result(
        state, actor="human_approval_gate", _outcome={"name": outcome}, approval_id="gate-1"
    )
    command = orchestrator_hub_node(accepted)
    # Constructing an output is not proof it was checkpointed: the input is unchanged.
    assert state["workflow_run_trace"]["transitions"] == []
    fact = command.update["workflow_run_trace"]["transitions"][-1]
    assert fact["source"] == node("a")
    assert fact["target"] == node(target)
    assert fact["outcome"] == {"name": outcome}
    assert fact["step_execution_id"] == state["run_cursor"]["step_execution_id"]
    assert fact["attempt"] == 1
    assert fact["approval_id"] == "gate-1"
    assert command.update["run_cursor"]["step_id"] == target
    ended = _step(accepted, command)
    end = orchestrator_hub_node(
        _result(ended, actor="control_flow_step", reason="Authored terminal")
    )
    assert (
        end.update["workflow_run_trace"]["transitions"]
        == command.update["workflow_run_trace"]["transitions"]
    )


def test_trace_captures_each_item_and_container_execution_before_the_frame_is_popped():
    start = _run(_loop_definition(), job_id="job-trace", items=["x", "y"])
    state = _step(start, orchestrator_hub_node(start))
    done_a = _result(state)
    state = _step(done_a, orchestrator_hub_node(done_a))
    execution = state["run_cursor"]["frames"][-1]["for_each_execution_id"]
    for _ in range(2):
        finished = _result(state)
        state = _step(finished, orchestrator_hub_node(finished))
    facts = state["workflow_run_trace"]["transitions"]
    item_facts = [fact for fact in facts if fact["source"] == node("l1")]
    assert [fact["frames"][-1]["item_index"] for fact in item_facts] == [0, 1]
    assert len({fact["step_execution_id"] for fact in item_facts}) == 2
    assert facts[-1]["source"] == node("g", "for_each")
    assert facts[-1]["container_execution_id"] == execution
    assert state["run_cursor"]["frames"] == []


def test_blocked_rework_has_no_taken_fact_until_the_real_budget_resume():
    definition = _definition(
        [
            {"step_id": "a", "step_kind": "switch"},
            {"step_id": "done", "step_kind": "stop_success"},
        ],
        [
            {**edge("a", node("a"), "case", case_id="retry", rework=True), "rework_limit": 1},
            edge("a", node("done"), "default"),
        ],
    )
    start = _run(definition, job_id="job-trace")
    state = _step(start, orchestrator_hub_node(start))
    for _ in range(2):
        accepted = _result(
            state, actor="control_flow_step", _outcome={"name": "case", "case_id": "retry"}
        )
        state = _step(accepted, orchestrator_hub_node(accepted))
    assert state["recovery_pending"]["kind"] == "rework_budget"
    assert len(state["workflow_run_trace"]["transitions"]) == 1
    resumed = orchestrator_hub_node({**state, "user_approved": True})
    facts = resumed.update["workflow_run_trace"]["transitions"]
    assert len(facts) == 2
    assert facts[-1]["outcome"] == {"name": "case", "case_id": "retry"}
    assert facts[-1]["id"] != facts[0]["id"]


@pytest.mark.parametrize("workflow_id", ["wf", "workflow-" + "w" * 300])
def test_trace_keeps_canonical_long_ids_without_changing_the_real_hub_route(
    monkeypatch, workflow_id
):
    from core.application.orchestration.control_flow_step_node import control_flow_step_node
    from core.application.workflows.workflow_definition_validation import (
        validate_workflow_definition,
    )

    source_id, target_id = "source-" + "a" * 257, "target-" + "b" * 384
    definition = _definition(
        [
            {
                "step_id": source_id,
                "step_kind": "set_variable",
                "override": {"set_variable": {"target": "state.audit_value", "value": 0}},
            },
            {"step_id": target_id, "step_kind": "stop_success"},
        ],
        [edge(source_id, node(target_id))],
        entry=source_id,
    )
    assert validate_workflow_definition(definition) == []
    monkeypatch.setattr(agents_common, "publish_agent_message", lambda *args, **kwargs: True)
    start = _run(definition, job_id="long-id-job", workflow_id=workflow_id)
    first = orchestrator_hub_node(start)
    assert first.goto == "control_flow_step"
    state = _step(start, first)
    executed = {**state, **control_flow_step_node(state)}
    command = orchestrator_hub_node(executed)
    assert command.goto == "control_flow_step"
    assert command.update["run_cursor"]["step_id"] == target_id
    trace = command.update["workflow_run_trace"]
    assert trace["workflow_id"] == workflow_id
    assert trace["covered_from_start"] is True
    assert trace["truncated"] is False
    assert len(trace["transitions"]) == 1
    fact = trace["transitions"][0]
    assert fact["source"] == node(source_id)
    assert fact["target"] == node(target_id)
    assert fact["step_execution_id"] == state["run_cursor"]["step_execution_id"]
    assert fact["outcome"] == {"name": "success"}


def test_container_progress_keeps_recursive_empty_passes_after_the_frames_leave():
    definition = _definition(
        [
            {"step_id": "a", "step_kind": "transform"},
            {"step_id": "member", "step_kind": "transform", "for_each_id": "items"},
            {"step_id": "done", "step_kind": "stop_success"},
        ],
        [
            edge("a", node("once", "group")),
            edge("member", {"kind": "for_each_exit", "for_each_id": "items", "lane_id": None}),
            edge(
                "items",
                {"kind": "group_exit", "group_id": "once", "lane_id": None},
                source_kind="for_each",
            ),
            edge("once", node("done"), source_kind="group"),
        ],
        groups=[
            {"group_id": "once", "body": {"kind": "sequence", "entry": node("items", "for_each")}}
        ],
        for_each=[
            {
                "for_each_id": "items",
                "group_id": "once",
                "over": "state.items",
                "item_name": "item",
                "body": {"kind": "sequence", "entry": node("member")},
            }
        ],
    )
    initial = _run(definition, job_id="job-1", project_id="project-1", items=[])
    entered = _step(initial, orchestrator_hub_node(initial))
    completed_root = _result(entered)
    command = orchestrator_hub_node(completed_root)
    finished = _step(completed_root, command)

    assert finished["current_step_id"] == "done"
    assert finished["run_cursor"]["frames"] == []
    assert "workflow_container_progress" not in initial
    progress = command.update.get("workflow_container_progress")
    assert progress is not None, (
        "The same committed Command must retain both ended container passes"
    )
    assert progress["covered_from_start"] is True
    assert progress["truncated"] is False
    assert progress["job_id"] == "job-1"
    assert progress["project_id"] == "project-1"
    assert progress["snapshot_id"] == initial["workflow_definition_ref"]["snapshot_id"]
    assert progress["digest"] == initial["workflow_definition_ref"]["digest"]
    assert len(progress["passes"]) == 2
    passes = {row["container"]["id"]: row for row in progress["passes"]}
    group, item = passes["once"], passes["items"]
    assert group["container"] == {"kind": "group", "id": "once"}
    assert item["container"] == {"kind": "for_each", "id": "items"}
    assert group["execution_id"]
    assert item["execution_id"]
    assert item["execution_id"] != group["execution_id"]
    assert group["parent_frames"] == []
    assert item["parent_frames"] == [
        {"kind": "group", "id": "once", "execution_id": group["execution_id"]}
    ]
    assert group["outcome"] == item["outcome"] == "success"
    assert group["started_sequence"] < item["started_sequence"]
    assert item["started_sequence"] < item["finished_sequence"] < group["finished_sequence"]
    assert item["for_each"]["selected_count"] == 0
    assert item["for_each"]["processed_count"] == 0
    assert item["for_each"].get("item_index") is None


@pytest.mark.parametrize(
    "target", ["state.workflow_container_progress", "state.workflow_container_progress.passes"]
)
def test_authored_set_variable_cannot_replace_container_progress(target, monkeypatch):
    from copy import deepcopy

    from core.application.orchestration.control_flow_step_node import control_flow_step_node
    from tests._step_run_state import step_run_state

    monkeypatch.setattr(agents_common, "publish_agent_message", lambda *args, **kwargs: True)
    state = step_run_state(
        {
            "step_kind": "set_variable",
            "override": {
                "set_variable": {
                    "assignments": {
                        target: {"forged": True},
                        "state.domain_zero": 0,
                    }
                }
            },
        },
        agent_messages=[],
    )
    before = deepcopy(state)
    update = control_flow_step_node(state)
    message = update["agent_messages"][-1]
    assert message["status"] == "fail"
    assert message["artifacts"]["error"] == "set_variable_reserved_target"
    assert message["artifacts"]["blocked_targets"] == [target]
    assert "workflow_container_progress" not in update
    assert "domain_zero" not in update.get("workflow_data", {})
    assert "domain_zero" not in update
    assert state == before


@pytest.mark.parametrize(
    "target", ["state.workflow_container_progress", "state.workflow_container_progress.passes"]
)
def test_extension_output_mapping_cannot_publish_container_progress(target):
    from copy import deepcopy

    from core.application.orchestration.connector_mapping import _apply_output_mapping

    state = {"workflow_container_progress": {"runtime_owned": True}}
    before = deepcopy(state)
    dispatch = {"ok": True, "handled": True, "result": {"data": {"forged": True}, "zero": 0}}
    _apply_output_mapping(
        step_payload={
            "step_id": "extension",
            "step_kind": "connector",
            "output_mapping_jsonb": {"data": target, "zero": "state.domain_zero"},
        },
        dispatch_result=dispatch,
        state=state,
    )
    assert dispatch["ok"] is False
    assert dispatch["errors"][0]["code"] == "output_mapping_reserved_target"
    assert dispatch["errors"][0]["blocked_targets"] == [target]
    assert "state_patch" not in dispatch
    assert state == before


def test_initial_extra_state_cannot_seed_container_progress():
    from core.application.orchestration.state import build_initial_orchestrator_state

    state = build_initial_orchestrator_state(
        task_id="task-1",
        task_summary="Local guard",
        task_source="manual",
        project_name="audit",
        extra_state={"workflow_container_progress": {"forged": True}, "domain_zero": 0},
    )
    assert state.get("workflow_container_progress") is None
    assert state["domain_zero"] == 0


def test_container_items_continue_then_break_keep_actual_processed_selection():
    definition = _loop_definition()
    definition["steps"][1]["step_kind"] = "if"
    definition["steps"].extend(
        [
            {"step_id": "skip", "step_kind": "continue", "for_each_id": "g"},
            {"step_id": "leave", "step_kind": "break", "for_each_id": "g"},
        ]
    )
    definition["execution_graph"]["edges"] = [
        edge("a", node("g", "for_each")),
        edge("l1", node("skip"), "true"),
        edge("l1", node("leave"), "false"),
        edge("g", node("done"), source_kind="for_each"),
    ]
    initial = _run(definition, job_id="job-1", project_id="project-1", items=list(range(5)))
    state = _step(initial, orchestrator_hub_node(initial))
    completed = _result(state)
    state = _step(completed, orchestrator_hub_node(completed))
    execution_id = state["run_cursor"]["frames"][-1]["for_each_execution_id"]
    for index in range(4):
        decision = _result(
            state, actor="control_flow_step", _outcome={"name": "false" if index == 3 else "true"}
        )
        state = _step(decision, orchestrator_hub_node(decision))
        assert state["current_step_id"] == ("leave" if index == 3 else "skip")
        traversed = _result(state, actor="control_flow_step")
        state = _step(traversed, orchestrator_hub_node(traversed))
    assert state["current_step_id"] == "done"
    record = state["workflow_container_progress"]["passes"][0]
    assert record["execution_id"] == execution_id
    assert record["outcome"] == "success"
    assert record["current"] is False
    assert record["for_each"]["selected_count"] == 5
    assert record["for_each"]["processed_count"] == 4
    assert record["for_each"]["broke_at"] == record["for_each"]["item_index"] == 3


def test_container_ancestor_route_has_exact_failed_pass_and_accepted_trace_proof():
    definition = _loop_definition()
    definition["for_each"][0]["group_id"] = "outer"
    definition["groups"] = [
        {
            "group_id": "outer",
            "error_policy": {"on_error": "route_to_step"},
            "body": {"kind": "sequence", "entry": node("g", "for_each")},
        }
    ]
    # The child explicitly fails; the ancestor then accepts its own error edge.
    definition["steps"][1]["error_policy"] = {"on_error": "fail"}
    definition["execution_graph"]["edges"] = [
        edge("a", node("outer", "group")),
        edge("l1", {"kind": "for_each_exit", "for_each_id": "g", "lane_id": None}),
        edge(
            "g",
            {"kind": "group_exit", "group_id": "outer", "lane_id": None},
            source_kind="for_each",
        ),
        edge("outer", node("done"), source_kind="group"),
        edge("outer", node("failed"), "error", source_kind="group"),
    ]
    initial = _run(definition, job_id="job-1", project_id="project-1", items=["one"])
    state = _step(initial, orchestrator_hub_node(initial))
    completed = _result(state)
    state = _step(completed, orchestrator_hub_node(completed))
    failed_id = state["run_cursor"]["step_execution_id"]
    failed = _result(state, "fail")
    command = orchestrator_hub_node(failed)
    assert command.update["current_step_id"] == "failed"
    records = command.update["workflow_container_progress"]["passes"]
    assert len(records) == 2
    assert all(row["outcome"] == "error" for row in records)
    route = next(
        fact
        for fact in command.update["workflow_run_trace"]["transitions"]
        if fact["source"] == {"kind": "group", "id": "outer"}
    )
    for record in records:
        assert record["failure"]["step_execution_id"] == failed_id
        assert record["failure"]["attempt"] == 1
        assert record["failure"]["handling"] == {
            "kind": "route",
            "execution_id": records[0]["execution_id"],
            "transition_id": route["id"],
            "request_id": None,
        }


def test_container_selection_failure_does_not_report_an_empty_selection():
    definition = _loop_definition()
    definition["for_each"][0]["over"] = "rows[abc]"
    initial = _run(definition, job_id="job-1", project_id="project-1")
    state = _step(initial, orchestrator_hub_node(initial))
    completed = _result(state)
    command = orchestrator_hub_node(completed)
    assert command.update["status"] == "failed"
    record = command.update["workflow_container_progress"]["passes"][0]
    assert record["outcome"] == "error"
    assert record["for_each"] is None
    assert record["failure"]["boundary"] == "selection"
    assert record["failure"]["handling"] is None


def test_container_blocked_rework_handling_waits_for_the_exact_accepted_pass_edge():
    definition = _loop_definition({"on_error": "route_to_step"})
    for route in definition["execution_graph"]["edges"]:
        if route["source"] == node("g", "for_each") and route["outcome"]["name"] == "error":
            route.update(rework=True, rework_limit=1)
    initial = _run(definition, job_id="job-1", project_id="project-1", items=["one"])
    state = _step(initial, orchestrator_hub_node(initial))
    completed = _result(state)
    state = _step(completed, orchestrator_hub_node(completed))
    failed = _result(state, "fail")
    failed["run_cursor"]["rework"]["used"] = failed["run_cursor"]["rework"]["allowance"]
    paused = orchestrator_hub_node(failed)
    assert paused.goto == "human_approval_gate"
    before = paused.update["workflow_container_progress"]["passes"][0]
    assert before["outcome"] == "error"
    assert before["failure"]["handling"] is None
    assert not any(
        fact["container_execution_id"] == before["execution_id"]
        for fact in paused.update["workflow_run_trace"]["transitions"]
    )
    accepted_state = {**_step(failed, paused), "user_approved": True}
    accepted = orchestrator_hub_node(accepted_state)
    assert accepted.update["current_step_id"] == "failed"
    after = accepted.update["workflow_container_progress"]["passes"][0]
    route = next(
        fact
        for fact in accepted.update["workflow_run_trace"]["transitions"]
        if fact["container_execution_id"] == before["execution_id"]
    )
    assert after["execution_id"] == before["execution_id"]
    assert after["finished_sequence"] == before["finished_sequence"]
    assert after["updated_sequence"] > before["updated_sequence"]
    assert after["failure"]["handling"]["transition_id"] == route["id"]
    assert after["failure"]["handling"]["execution_id"] == before["execution_id"]
    assert before["failure"]["handling"] is None


@pytest.mark.parametrize("blocking_check", ["contract", "guardrail"])
@pytest.mark.parametrize("spoof", ["artifacts_only", "wrong_executor_provenance"])
def test_model_unknown_artifacts_cannot_skip_success_checks_via_continue(
    monkeypatch, blocking_check, spoof
):
    definition = _failing({"on_error": "continue"})
    definition["steps"][0].update(step_kind="agent", agent="writer")
    state = _run(definition)
    state = _step(state, orchestrator_hub_node(state))
    cursor = state["run_cursor"]
    artifacts = {
        "external_effect_status": "timeout_unknown",
        "record_id": "made-up-original-id",
        "external_effect_observations": [
            {
                "effect_id": "11111111-1111-1111-1111-111111111111",
                "claim_version": 1,
                "step_execution_id": cursor["step_execution_id"],
                "attempt": cursor["attempt"],
            }
        ],
    }
    if blocking_check == "contract":
        artifacts["_schema_errors"] = [{"message": "required agent output missing"}]
    state = _result(state, actor="writer", **artifacts)
    if spoof == "wrong_executor_provenance":
        state["agent_messages"][-1]["operation_retry"] = {
            "schema_version": "operation_retry_signal_v1",
            "provenance": "deterministic_connector_step",
            "step_id": "a",
            "signal": {
                "external_effect_status": "timeout_unknown",
                "request_semantics": "mutation",
                "retryable": False,
            },
        }
    violation = SimpleNamespace(
        display_name="Answers the requested task",
        message="Unrelated answer",
        rule_type="output_addresses_input",
        details={},
    )
    monkeypatch.setattr(
        "core.application.registry.registry_manager.RegistryManager.instance",
        lambda: SimpleNamespace(_db_store=object()),
    )
    monkeypatch.setattr(
        "core.application.orchestration.guardrails.checker.check_guardrails",
        lambda *args: SimpleNamespace(
            violations=[violation] if blocking_check == "guardrail" else [], warnings=[]
        ),
    )
    command = orchestrator_hub_node(state)
    assert command.goto == "writer"
    assert command.update["current_step_id"] == "a"
    assert command.update["run_cursor"]["attempt"] == 2
    assert command.update["run_cursor"]["step_execution_id"] == cursor["step_execution_id"]
    assert not command.update.get("recovery_pending")
    assert not command.update.get("record_id")
    if blocking_check == "guardrail":
        assert "Guardrail" in command.update["feedback"]
    else:
        assert command.update["last_retry_reason"] == "contract_health_gate"


def test_nominal_pass_with_real_host_unknown_uses_authored_error_route_not_safe_artifacts():
    definition = _failing({"on_error": "route_to_step"}, [edge("a", node("fix"), "error")])
    definition["steps"][0].update(step_kind="agent", agent="writer")
    state = _run(definition)
    state = _step(state, orchestrator_hub_node(state))
    state = _result(
        state,
        actor="writer",
        external_effect_status="failed",
        definitely_no_external_effect=True,
        _schema_errors=[{"message": "lost original response"}],
    )
    state["agent_messages"][-1]["operation_retry"] = {
        "schema_version": "operation_retry_signal_v1",
        "provenance": "agent_managed_custom_http",
        "signal": {
            "external_effect_status": "timeout_unknown",
            "request_semantics": "mutation",
            "retryable": True,
        },
    }
    command = orchestrator_hub_node(state)
    assert command.update["current_step_id"] == "fix"
    assert not command.update.get("recovery_pending")
    assert not command.update.get("operation_retry_dispatch")
    assert state["run_cursor"]["attempt"] == 1


# --- disabled steps ---------------------------------------------------------------


def _disabled(definition: dict, *step_ids: str) -> dict:
    disabled = copy.deepcopy(definition)
    for item in disabled["steps"]:
        if item["step_id"] in step_ids:
            item["disabled"] = True
    return disabled


def _until_an_executor(state: dict) -> tuple[dict, object, int]:
    """Run the hub until it hands the run to a node other than itself."""
    passes = 0
    while True:
        command = orchestrator_hub_node(state)
        state = _step(state, command)
        passes += 1
        if command.goto != "orchestrator":
            return state, command, passes


def _skipped(state: dict) -> list[str]:
    return [
        entry["step_id"]
        for entry in state.get("orchestrator_journal") or []
        if entry["event"] == "step_skipped"
    ]


def test_a_disabled_step_is_recorded_as_skipped_and_never_dispatched_to_an_executor() -> None:
    start = _run(_disabled(LINEAR, "a"), job_id="job-skip")
    command = orchestrator_hub_node(start)
    state = _step(start, command)

    assert command.goto == "orchestrator"
    assert state["current_step_id"] == "a"
    assert state["dispatched_step_execution"] == {
        "step_id": "a",
        "step_execution_id": state["run_cursor"]["step_execution_id"],
        "attempt": 1,
        "step_kind": "transform",
        "skipped": "disabled",
    }
    (entry,) = [item for item in state["orchestrator_journal"] if item["event"] == "step_skipped"]
    assert (entry["step_id"], entry["action"], entry["reason"]) == ("a", "skip", "disabled")


def test_the_run_continues_on_the_skipped_steps_next_edge() -> None:
    start = _run(_disabled(LINEAR, "a"), job_id="job-skip")
    skipped = _step(start, orchestrator_hub_node(start))
    skipped_execution = skipped["run_cursor"]["step_execution_id"]

    command = orchestrator_hub_node(skipped)
    state = _step(skipped, command)

    assert command.goto == "execute_step"
    assert state["current_step_id"] == "b"
    assert state["dispatched_step_execution"]["step_id"] == "b"
    assert "skipped" not in state["dispatched_step_execution"]
    (fact,) = state["workflow_run_trace"]["transitions"]
    assert (fact["source"], fact["outcome"], fact["target"]) == (
        node("a"),
        {"name": "success"},
        node("b"),
    )
    assert fact["step_execution_id"] == skipped_execution


def test_consecutive_disabled_steps_are_each_skipped() -> None:
    state, command, passes = _until_an_executor(_run(_disabled(LINEAR, "a", "b")))

    assert (command.goto, state["current_step_id"]) == ("control_flow_step", "done")
    assert _skipped(state) == ["a", "b"]
    assert passes == 3
    assert state["routed_transitions"] == 3


def test_a_skipped_step_publishes_no_step_events() -> None:
    def run() -> None:
        _until_an_executor(_run(_disabled(LINEAR, "a"), job_id="77"))

    names, _events = _recorded_events(run)

    assert names == [("job.step_started", "b")]


def test_a_disabled_body_entry_is_skipped_for_every_item() -> None:
    definition = _disabled(_loop_definition(), "l1")
    state, _command, _passes = _until_an_executor(_run(definition, items=["x", "y"]))
    done_a = _result(state)
    state, command, _passes = _until_an_executor(done_a)

    assert (command.goto, state["current_step_id"]) == ("control_flow_step", "done")
    assert state["run_cursor"]["frames"] == []
    assert _skipped(state) == ["l1", "l1"]


def test_a_step_test_run_of_a_disabled_step_still_runs_it() -> None:
    from core.application.orchestration.run_cursor import RunCursor, begin_step

    state = _run(
        _disabled(LINEAR, "b"),
        task_source="step_test_run",
        workflow_test_run_execution_id="exec-1",
        step_test_run_context={"workflow_id": "wf", "execution_id": "exec-1", "step_id": "b"},
        run_cursor=begin_step(RunCursor(), "b").dump(),
    )

    assert orchestrator_hub_node(state).goto == "execute_step"


def test_a_disabled_branching_step_fails_instead_of_guessing_a_branch() -> None:
    definition = _disabled(
        _definition(
            [
                {"step_id": "a", "step_kind": "if"},
                {"step_id": "yes", "step_kind": "stop_success"},
                {"step_id": "no", "step_kind": "stop_fail"},
            ],
            [edge("a", node("yes"), "true"), edge("a", node("no"), "false")],
        ),
        "a",
    )
    command = orchestrator_hub_node(_run(definition))

    assert command.goto == "orchestrator_validation"
    assert command.update["status"] == "failed"
    assert command.update["orchestrator_journal"][-1]["event"] == "step_disabled_unsupported"
