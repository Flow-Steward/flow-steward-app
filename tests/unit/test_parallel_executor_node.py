"""Lanes of a real group run concurrently, each with its own cursor, and merge in order.

Owning test of the parallel executor (steps-without-phases specification §11).
A lanes body is a parallel group whose lanes are step sequences closed by the
lane's ``group_exit``. These tests drive ``parallel_executor_node`` directly with
a primed pinned definition and fake executors.
"""

from __future__ import annotations

import threading
from datetime import UTC, datetime
from unittest.mock import patch

import pytest

from core.application.orchestration.active_step import step_message_fields
from core.application.orchestration.agent_messages import build_agent_message
from core.application.orchestration.parallel_group_runtime import initial_lanes
from core.application.orchestration.run_cursor import FrozenDefinition, GroupFrame, RunCursor
from core.application.orchestration.run_definition import clear_run_definition_cache
from core.application.workflows.execution_graph import NodeRef
from tests._step_run_state import prime
from tests._workflow_definitions import edge, node


@pytest.fixture(autouse=True)
def _isolated():
    clear_run_definition_cache()
    with patch("core.application.orchestration.agents.common.emit_event"):
        yield
    clear_run_definition_cache()


def _exit(lane_id: str) -> dict:
    return {"kind": "group_exit", "group_id": "p", "lane_id": lane_id}


def _definition(lanes: dict[str, list[dict]], *, concurrency_limit: int | None = None) -> dict:
    """A root step, then a parallel group of the given lanes, then Stop Success."""
    steps: list[dict] = [{"step_id": "start", "step_kind": "transform"}]
    edges = [edge("start", node("p", "group")), edge("p", node("done"), source_kind="group")]
    lane_rows = []
    for lane_id, lane_steps in lanes.items():
        for index, step in enumerate(lane_steps):
            steps.append({"group_id": "p", "lane_id": lane_id, **step})
            target = (
                node(lane_steps[index + 1]["step_id"])
                if index + 1 < len(lane_steps)
                else _exit(lane_id)
            )
            edges.append(edge(step["step_id"], target))
        lane_rows.append({"lane_id": lane_id, "entry": node(lane_steps[0]["step_id"])})
    body: dict = {"kind": "lanes", "lanes": lane_rows}
    if concurrency_limit is not None:
        body["concurrency_limit"] = concurrency_limit
    steps.append({"step_id": "done", "step_kind": "stop_success"})
    return {
        "steps": steps,
        "groups": [{"group_id": "p", "body": body}],
        "for_each": [],
        "execution_graph": {"schema_version": 1, "entry": node("start"), "edges": edges},
        "settings": {},
    }


def _agent(step_id: str, agent: str) -> dict:
    return {"step_id": step_id, "step_kind": "agent", "agent": agent}


def _state(definition: dict, **extra) -> dict:
    frame = GroupFrame(group_id="p", group_execution_id="group-exec-1")
    lanes = initial_lanes(FrozenDefinition(definition), NodeRef("group", "p"))
    return {
        "job_id": "11",
        "task_id": "TASK-11",
        "workflow_id": "wf",
        "agent_messages": [],
        **prime(definition),
        "run_cursor": RunCursor(frames=[frame]).dump(),
        "parallel_lanes": [lane.dump() for lane in lanes],
        **extra,
    }


def _result(local_state: dict, agent: str, status: str = "pass", **artifacts) -> dict:
    message = build_agent_message(
        from_agent=agent,
        to_agent="orchestrator",
        skill=f"{agent}_skill",
        status=status,
        correlation_id=f"corr-{agent}-{local_state['run_cursor']['attempt']}",
        artifacts=artifacts,
        summary=f"{agent} {status}",
        published_at=datetime.now(UTC).isoformat(),
        **step_message_fields(local_state),
    )
    return {"agent_messages": [*(local_state.get("agent_messages") or []), message]}


def _fake_agent(agent: str, calls: list[str] | None = None):
    def _node(local_state: dict) -> dict:
        if calls is not None:
            calls.append(agent)
        return {**_result(local_state, agent), f"{agent}_marker": True}

    return _node


def _run(state: dict, resolve) -> dict:
    from core.application.orchestration.parallel_executor_node import parallel_executor_node

    with patch(
        "core.application.orchestration.parallel_executor_node._resolve_agent_node",
        side_effect=resolve,
    ):
        return parallel_executor_node(state)


TWO_LANES = {"lane_a": [_agent("a1", "composer")], "lane_b": [_agent("b1", "proofreader")]}


def _statuses(update: dict) -> dict[str, str]:
    return {lane["lane_id"]: lane["status"] for lane in update["parallel_lanes"]}


def test_all_lanes_run_and_their_messages_and_state_merge() -> None:
    state = _state(_definition(TWO_LANES))

    update = _run(state, lambda agent: _fake_agent(str(agent)))

    assert _statuses(update) == {"lane_a": "completed", "lane_b": "completed"}
    assert update["parallel_outcome"]["status"] == "completed"
    assert update["parallel_outcome"]["execution_id"] == "group-exec-1"
    assert update["parallel_outcome"]["container"] == {"kind": "group", "id": "p"}
    assert {msg["from_agent"] for msg in update["agent_messages"]} == {"composer", "proofreader"}
    assert {msg["group_id"] for msg in update["agent_messages"]} == {"p"}
    assert update["composer_marker"] is True
    assert update["proofreader_marker"] is True
    assert not any("worktree" in key or "merge" in key for key in update)


def test_a_lane_walks_its_steps_and_later_steps_see_earlier_state() -> None:
    definition = _definition({"lane_a": [_agent("a1", "scout"), _agent("a2", "composer")]})
    seen: list[object] = []

    def resolve(agent):
        if agent == "composer":

            def _node(local_state: dict) -> dict:
                seen.append(local_state.get("scout_marker"))
                return _result(local_state, "composer")

            return _node
        return _fake_agent(str(agent))

    update = _run(_state(definition), resolve)

    assert seen == [True]
    executions = [msg["step_execution_id"] for msg in update["agent_messages"]]
    assert len(set(executions)) == 2
    assert _statuses(update) == {"lane_a": "completed"}


def test_a_disabled_lane_step_is_skipped_and_its_lane_continues_on_next() -> None:
    definition = _definition(
        {
            "lane_a": [{**_agent("a1", "scout"), "disabled": True}, _agent("a2", "composer")],
            "lane_b": [_agent("b1", "proofreader")],
        }
    )
    calls: list[str] = []

    update = _run(_state(definition), lambda agent: _fake_agent(str(agent), calls))

    assert sorted(calls) == ["composer", "proofreader"]
    assert _statuses(update) == {"lane_a": "completed", "lane_b": "completed"}
    assert "scout_marker" not in update
    assert {msg["step_id"] for msg in update["agent_messages"]} == {"a2", "b1"}
    lane_a = next(lane for lane in update["parallel_lanes"] if lane["lane_id"] == "lane_a")
    assert lane_a["completed_steps"] == 1
    routed = [
        (fact["source"]["id"], fact["target"].get("id") or fact["target"]["kind"])
        for fact in update["workflow_run_trace"]["transitions"]
    ]
    assert ("a1", "a2") in routed


def test_a_disabled_lane_step_publishes_no_step_events() -> None:
    from core.application.workflows.workflow_lifecycle_event_sink import (
        bind_workflow_event_sink,
    )

    definition = _definition(
        {
            "lane_a": [{**_agent("a1", "composer"), "disabled": True}],
            "lane_b": [_agent("b1", "proofreader")],
        }
    )
    events: list[dict] = []
    lock = threading.Lock()

    def sink(**event):
        with lock:
            events.append(event)

    with bind_workflow_event_sink(sink):
        update = _run(_state(definition), lambda agent: _fake_agent(str(agent)))

    assert sorted((event["event_type"], event["payload"]["step_id"]) for event in events) == [
        ("job.step_completed", "b1"),
        ("job.step_started", "b1"),
    ]
    assert _statuses(update) == {"lane_a": "completed", "lane_b": "completed"}


def test_messages_merge_by_step_position_then_lane_order() -> None:
    definition = _definition(
        {
            "lane_a": [_agent("a1", "scout"), _agent("a2", "composer")],
            "lane_b": [_agent("b1", "proofreader")],
        }
    )
    update = _run(_state(definition), lambda agent: _fake_agent(str(agent)))

    order = [msg["step_id"] for msg in update["agent_messages"]]
    assert order == ["a1", "b1", "a2"]
    positions = [msg["artifacts"]["operation_parallel"] for msg in update["agent_messages"]]
    assert positions[0] == {"lane_id": "lane_a", "position": 0}


def test_child_workflow_lanes_run_concurrently_and_merge_outputs() -> None:
    definition = _definition(
        {
            "amazon": [
                {
                    "step_id": "import_amazon",
                    "step_kind": "invoke_workflow",
                    "override": {"invoke_workflow": {"workflow_id": "import_amazon"}},
                }
            ],
            "storefront": [
                {
                    "step_id": "import_storefront",
                    "step_kind": "invoke_workflow",
                    "override": {"invoke_workflow": {"workflow_id": "import_storefront"}},
                }
            ],
        }
    )
    state = _state(definition, project_policy={"concurrency_cap": 2})
    rendezvous = threading.Barrier(2)
    started: list[str] = []
    lock = threading.Lock()

    def _invoke_child(local_state: dict) -> dict:
        child_id = local_state["run_cursor"]["step_id"]
        with lock:
            started.append(child_id)
        rendezvous.wait(timeout=2)
        return {
            f"{child_id}_report": {"imported": 1000, "failed": 0},
            **_result(local_state, "invoke_workflow"),
        }

    with patch(
        "core.application.orchestration.invoke_workflow_step_node.invoke_workflow_step_node",
        new=_invoke_child,
    ):
        update = _run(state, lambda agent: pytest.fail("a child workflow lane resolves no agent"))

    assert set(started) == {"import_amazon", "import_storefront"}
    assert update["import_amazon_report"] == {"imported": 1000, "failed": 0}
    assert update["import_storefront_report"] == {"imported": 1000, "failed": 0}
    assert _statuses(update) == {"amazon": "completed", "storefront": "completed"}


def test_a_parallel_child_workflow_cannot_invoke_an_ancestor() -> None:
    definition = _definition(
        {
            "lane_a": [
                {
                    "step_id": "recurse",
                    "step_kind": "invoke_workflow",
                    "override": {
                        "invoke_workflow": {
                            "workflow_id": "wf_parent",
                            "input_mapping": {},
                            "output_mapping": {},
                        }
                    },
                }
            ]
        }
    )
    state = _state(
        definition,
        workflow_id="wf_child",
        invoke_parent_workflow_id="wf_parent",
        invoke_workflow_ancestry=["wf_parent", "wf_child"],
    )

    with patch(
        "core.application.registry.registry_manager.RegistryManager.instance",
        side_effect=AssertionError("recursive invoke must fail before child lookup"),
    ):
        update = _run(state, lambda agent: pytest.fail("invoke must use its guarded node"))

    recursive = next(
        msg
        for msg in update["agent_messages"]
        if (msg.get("artifacts") or {}).get("_invoke_workflow_error")
        == "recursive_workflow_invocation"
    )
    assert recursive["status"] == "fail"
    assert (
        recursive["artifacts"]["_invoke_workflow_cycle_path"]
        == "wf_parent -> wf_child -> wf_parent"
    )
    assert _statuses(update) == {"lane_a": "failed"}
    assert update["parallel_outcome"]["status"] == "failed"


def test_a_failed_lane_does_not_fail_its_sibling() -> None:
    def resolve(agent):
        if agent == "composer":
            return lambda local_state: _result(local_state, "composer", "fail")
        return _fake_agent(str(agent))

    update = _run(_state(_definition(TWO_LANES)), resolve)

    assert _statuses(update) == {"lane_a": "failed", "lane_b": "completed"}
    assert update["parallel_outcome"]["status"] == "failed"
    statuses = {msg["from_agent"]: msg["status"] for msg in update["agent_messages"]}
    assert statuses == {"composer": "fail", "proofreader": "pass"}


_RETRYABLE_READ = {
    "operation_kind": "source",
    "request_semantics": "safe_read",
    "failure_class": "provider",
    "retryable": True,
    "definitely_no_external_effect": True,
    "external_effect_status": "failed",
}


def test_a_retryable_read_resumes_only_its_lane_and_never_replays_a_successful_sibling(
    monkeypatch,
) -> None:
    from core.application.orchestration.step_hub import orchestrator_hub_node

    calls = {"composer": 0, "proofreader": 0}

    def resolve(agent):
        def _node(local_state: dict) -> dict:
            calls[agent] += 1
            return _result(local_state, agent)

        return _node

    def read(*, config, state):
        calls["proofreader"] += 1
        return {
            "ok": calls["proofreader"] > 1,
            "status_code": 503 if calls["proofreader"] == 1 else 200,
            "response_mode": "inline",
            **(_RETRYABLE_READ if calls["proofreader"] == 1 else {}),
        }

    monkeypatch.setattr(
        "core.application.orchestration.http_request_step_runtime.execute_http_request_step", read
    )
    definition = _definition(
        {
            "lane_a": [_agent("a1", "composer")],
            "lane_b": [
                {
                    "step_id": "b1",
                    "step_kind": "http_request",
                    "override": {
                        "http_request": {"method": "GET", "url": "https://example.test/read"}
                    },
                }
            ],
        }
    )
    state = _state(definition)
    first = _run(state, resolve)

    assert first["parallel_outcome"]["status"] == "retry_operation"
    lane_b = next(lane for lane in first["parallel_lanes"] if lane["lane_id"] == "lane_b")
    assert lane_b["status"] == "retry_operation"
    failed = next(msg for msg in first["agent_messages"] if msg["status"] == "fail")
    assert failed["operation_retry"]["operation_parallel"] == {"lane_id": "lane_b", "position": 0}

    # Checkpoint the real host retry before resuming only that lane and execution.
    checkpointed = orchestrator_hub_node({**state, **first})
    assert checkpointed.goto == "operation_retry_wait"
    resumed_state = {**state, **first, **checkpointed.update}
    second = _run(resumed_state, resolve)

    assert calls == {"composer": 1, "proofreader": 2}
    assert second["operation_retry_dispatch"] is None
    assert _statuses(second) == {"lane_a": "completed", "lane_b": "completed"}
    retried = [msg for msg in second["agent_messages"] if msg["from_agent"] == "http_request_step"]
    assert [msg["attempt"] for msg in retried] == [1, 2]
    assert retried[0]["step_execution_id"] == retried[1]["step_execution_id"]


def test_an_operator_decision_resumes_only_the_escalated_lane() -> None:
    definition = _definition(
        {
            "lane_a": [_agent("a1", "composer")],
            "lane_b": [
                {**_agent("b1", "proofreader"), "error_policy": {"on_error": "await_approval"}},
                _agent("b2", "scout"),
            ],
        }
    )
    calls: list[str] = []

    def resolve(agent):
        def _node(local_state: dict) -> dict:
            calls.append(agent)
            status = "fail" if agent == "proofreader" and calls.count(agent) == 1 else "pass"
            return _result(local_state, agent, status)

        return _node

    state = _state(definition)
    first = _run(state, resolve)
    assert first["parallel_outcome"]["status"] == "escalated"
    assert _statuses(first) == {"lane_a": "completed", "lane_b": "escalated"}

    # The hub records the operator's reject: rerun b1 with feedback, lane_a untouched.
    lanes = [dict(lane) for lane in first["parallel_lanes"]]
    for lane in lanes:
        if lane["lane_id"] == "lane_b":
            lane.update(
                status="running",
                failure=None,
                decision={"action": "retry", "feedback": "Use the new spec"},
                attempt=lane["attempt"] + 1,
            )
    second = _run({**state, **first, "parallel_lanes": lanes, "parallel_outcome": None}, resolve)

    assert calls == ["composer", "proofreader", "proofreader", "scout"]
    assert _statuses(second) == {"lane_a": "completed", "lane_b": "completed"}


def test_an_approved_lane_continues_past_its_error_without_rerunning_the_step() -> None:
    definition = _definition(
        {"lane_a": [_agent("a1", "composer"), _agent("a2", "scout")]},
    )
    lanes = initial_lanes(FrozenDefinition(definition), NodeRef("group", "p"))
    lanes[0].status = "running"
    lanes[0].step_execution_id = "11111111-1111-1111-1111-111111111111"
    lanes[0].decision = {"action": "continue"}
    calls: list[str] = []

    update = _run(
        _state(definition, parallel_lanes=[lane.dump() for lane in lanes]),
        lambda agent: _fake_agent(str(agent), calls),
    )

    assert calls == ["scout"]
    assert _statuses(update) == {"lane_a": "completed"}


def test_the_body_concurrency_limit_caps_the_lanes() -> None:
    from core.application.orchestration import parallel_executor_node as module

    definition = _definition(TWO_LANES, concurrency_limit=1)
    frozen = FrozenDefinition(definition)

    frame = GroupFrame(group_id="p", group_execution_id="g-1")
    assert module._concurrency_cap(frozen, frame, {"project_policy": {"concurrency_cap": 4}}) == 1
    assert (
        module._concurrency_cap(
            FrozenDefinition(_definition(TWO_LANES)),
            frame,
            {"project_policy": {"concurrency_cap": 4}},
        )
        == 4
    )


def test_a_lane_retains_the_execution_scoped_mcp_client_until_teardown() -> None:
    from core.application.management.mcp_connector_client import (
        get_execution_mcp_client,
        mcp_execution_scope,
    )

    clients_seen: list[object] = []
    lock = threading.Lock()
    factory_calls = 0

    class _RetainedClient:
        def __init__(self) -> None:
            self.close_calls = 0

        def close(self) -> None:
            self.close_calls += 1

    retained = _RetainedClient()

    def _factory():
        nonlocal factory_calls
        factory_calls += 1
        return retained

    def resolve(agent):
        def _node(local_state: dict) -> dict:
            client = get_execution_mcp_client(
                account_id="account-11",
                job_id="11",
                connector_id="connector-11",
                client_factory=_factory,
            )
            with lock:
                clients_seen.append(client)
            return _result(local_state, agent)

        return _node

    with mcp_execution_scope(account_id="account-11", job_id="11"):
        update = _run(_state(_definition(TWO_LANES)), resolve)
        assert retained.close_calls == 0

    assert update["parallel_outcome"]["status"] == "completed"
    assert clients_seen == [retained, retained]
    assert factory_calls == 1
    assert retained.close_calls == 1


def test_lane_steps_publish_their_step_events_in_their_lane_threads() -> None:
    from core.application.workflows.workflow_lifecycle_event_sink import (
        bind_workflow_event_sink,
    )

    def resolve(agent):
        if agent == "composer":
            return lambda local_state: _result(local_state, "composer", "fail")
        return _fake_agent(str(agent))

    events: list[dict] = []
    lock = threading.Lock()

    def sink(**event):
        with lock:
            events.append(event)

    with bind_workflow_event_sink(sink):
        _run(_state(_definition(TWO_LANES)), resolve)

    seen = sorted((event["event_type"], event["payload"]["step_id"]) for event in events)
    assert seen == [
        ("job.step_completed", "b1"),
        ("job.step_failed", "a1"),
        ("job.step_started", "a1"),
        ("job.step_started", "b1"),
    ]
    assert {event["payload"]["group_id"] for event in events} == {"p"}
    assert all(event["payload"]["step_execution_id"] for event in events)


@pytest.mark.parametrize(
    "boundary",
    [
        {"entry": [{"name": "required", "required": True, "source": "state.missing"}]},
        {"exit": ["receipt"]},
    ],
)
def test_lane_boundary_contract_blocks_missing_inputs_or_outputs(boundary):
    calls = []
    definition = _definition(
        {"lane_a": [{**_agent("a1", "composer"), "boundary_contract": boundary}]}
    )
    update = _run(
        _state(definition, project_policy={"retry_max_attempts": 0}),
        lambda agent: _fake_agent(str(agent), calls),
    )
    assert _statuses(update) == {"lane_a": "failed"}
    assert update["parallel_lanes"][0]["failure"]["failure_class"] == "contract"
    assert calls == ([] if "entry" in boundary else ["composer"])


def test_lane_entry_materializes_declared_input_before_executor():
    definition = _definition(
        {
            "lane_a": [
                {
                    **_agent("a1", "composer"),
                    "boundary_contract": {
                        "entry": [
                            {
                                "name": "customer",
                                "required": True,
                                "source": "state.workflow_data.customer",
                            }
                        ],
                    },
                }
            ]
        }
    )
    seen = []

    def execute(state):
        seen.append(state.get("materialized_inputs", {}).get("customer"))
        return _result(state, "composer")

    update = _run(_state(definition, workflow_data={"customer": "Ada"}), lambda agent: execute)
    assert _statuses(update) == {"lane_a": "completed"}
    assert seen == ["Ada"]


def test_lane_manual_autonomy_pauses_before_effect_and_approval_executes_once():
    from core.application.orchestration.step_hub import orchestrator_hub_node

    calls = []
    definition = _definition(
        {"lane_a": [{**_agent("a1", "composer"), "autonomy": {"mode": "manual"}}]}
    )
    state = _state(definition)
    update = _run(state, lambda agent: _fake_agent(str(agent), calls))
    assert calls == []
    assert _statuses(update) == {"lane_a": "escalated"}
    state.update(update)
    pause = orchestrator_hub_node(state)
    assert pause.goto == "human_approval_gate"
    state.update(pause.update)
    state["user_approved"] = True
    resume = orchestrator_hub_node(state)
    state.update(resume.update)
    completed = _run(state, lambda agent: _fake_agent(str(agent), calls))
    assert _statuses(completed) == {"lane_a": "completed"}
    assert calls == ["composer"]


def test_lane_guardrails_regenerate_then_escalate_without_advancing(monkeypatch):
    from types import SimpleNamespace

    import core.application.orchestration.guardrails.checker as checker
    import core.application.registry.registry_manager as registry_manager

    monkeypatch.setattr(
        registry_manager.RegistryManager,
        "instance",
        classmethod(lambda cls: SimpleNamespace(_db_store=object())),
    )
    violation = SimpleNamespace(
        display_name="Answers task", message="Off topic", rule_type="output_addresses_input"
    )
    monkeypatch.setattr(
        checker,
        "check_guardrails",
        lambda *args: SimpleNamespace(violations=[violation], warnings=[]),
    )
    calls = []
    definition = _definition({"lane_a": [_agent("a1", "composer"), _agent("a2", "publisher")]})
    update = _run(_state(definition), lambda agent: _fake_agent(str(agent), calls))
    assert _statuses(update) == {"lane_a": "escalated"}
    assert calls == ["composer", "composer"]
    assert update["parallel_lanes"][0]["step_id"] == "a1"


def test_lane_guardrail_budgets_survive_other_lane_result_merging(monkeypatch):
    from types import SimpleNamespace

    import core.application.orchestration.guardrails.checker as checker
    import core.application.registry.registry_manager as registry_manager

    monkeypatch.setattr(
        registry_manager.RegistryManager,
        "instance",
        classmethod(lambda cls: SimpleNamespace(_db_store=object())),
    )
    violation = SimpleNamespace(
        display_name="Answers task", message="Off topic", rule_type="output_addresses_input"
    )
    monkeypatch.setattr(
        checker,
        "check_guardrails",
        lambda *args: SimpleNamespace(violations=[violation], warnings=[]),
    )
    state = _state(_definition(TWO_LANES))
    first = _run(state, lambda agent: _fake_agent(str(agent)))
    assert _statuses(first) == {"lane_a": "escalated", "lane_b": "escalated"}
    assert len(first["guardrail_retry_counts"]) == 2
    calls = []
    lanes = first["parallel_lanes"]
    for lane in lanes:
        lane.update(status="running", decision={"action": "retry"}, attempt=lane["attempt"] + 1)
    second = _run(
        {**state, **first, "parallel_lanes": lanes},
        lambda agent: _fake_agent(str(agent), calls),
    )
    assert sorted(calls) == ["composer", "proofreader"]
    assert _statuses(second) == {"lane_a": "escalated", "lane_b": "escalated"}


def test_trace_is_merged_from_successful_lane_routing_not_arbitrary_step_updates():
    state = _state(_definition(TWO_LANES))

    def resolve(agent):
        def execute(local):
            return {
                **_result(local, str(agent), "pass" if agent == "composer" else "fail"),
                "workflow_run_trace": {"transitions": [{"source": "forged"}]},
            }

        return execute

    update = _run(state, resolve)
    assert "workflow_run_trace" not in state
    facts = update["workflow_run_trace"]["transitions"]
    assert len(facts) == 1
    assert facts[0]["source"] == node("a1")
    assert facts[0]["target"] == _exit("lane_a")
    assert facts[0]["lane_id"] == "lane_a"
    assert facts[0]["frames"][-1]["execution_id"] == "group-exec-1"
    assert facts[0]["step_execution_id"]
    # Starting at an existing container cannot claim coverage of the run's beginning.
    assert update["workflow_run_trace"]["covered_from_start"] is False


def test_trace_parallel_order_and_resume_do_not_replay_finished_lane_edges():
    lanes = {
        "lane_a": [_agent("a1", "scout"), _agent("a2", "composer")],
        "lane_b": [_agent("b1", "critic"), _agent("b2", "proofreader")],
    }
    state = _state(_definition(lanes))
    update = _run(state, lambda agent: _fake_agent(str(agent)))
    trace = update["workflow_run_trace"]
    assert [fact["source"]["id"] for fact in trace["transitions"]] == ["a1", "b1", "a2", "b2"]
    resumed = _run(
        {**state, **update}, lambda agent: pytest.fail(f"Finished lane executed {agent}")
    )
    assert resumed["workflow_run_trace"] == trace


def test_unexpected_lane_future_failure_never_claims_a_complete_empty_trace(monkeypatch):
    import core.application.orchestration.parallel_executor_node as executor

    def fail_before_return(**kwargs):
        raise RuntimeError("Synthetic node update was never returned")

    monkeypatch.setattr(executor, "_run_lane", fail_before_return)
    state = _state(_definition(TWO_LANES))
    update = _run(state, lambda agent: _fake_agent(str(agent)))
    assert update["workflow_run_trace"]["transitions"] == []
    assert update["workflow_run_trace"]["truncated"] is True


def test_trace_keeps_long_canonical_group_lane_and_step_ids_in_the_real_parallel_update(
    monkeypatch,
):
    import core.application.orchestration.agents.common as common
    from core.application.workflows.workflow_definition_validation import (
        validate_workflow_definition,
    )

    group_id, lane_id = "group-" + "g" * 257, "lane-" + "l" * 300
    source_id, target_id = "source-" + "a" * 257, "target-" + "b" * 384
    variable = {
        "step_kind": "set_variable",
        "override": {"set_variable": {"target": "state.audit_value", "value": False}},
    }
    lane_exit = {"kind": "group_exit", "group_id": group_id, "lane_id": lane_id}
    definition = {
        "steps": [
            {"step_id": "start", **variable},
            {"step_id": source_id, "group_id": group_id, "lane_id": lane_id, **variable},
            {"step_id": target_id, "group_id": group_id, "lane_id": lane_id, **variable},
            {"step_id": "done", "step_kind": "stop_success"},
        ],
        "groups": [
            {
                "group_id": group_id,
                "body": {
                    "kind": "lanes",
                    "lanes": [{"lane_id": lane_id, "entry": node(source_id)}],
                },
            }
        ],
        "for_each": [],
        "settings": {},
        "execution_graph": {
            "schema_version": 1,
            "entry": node("start"),
            "edges": [
                edge("start", node(group_id, "group")),
                edge(source_id, node(target_id)),
                edge(target_id, lane_exit),
                edge(group_id, node("done"), source_kind="group"),
            ],
        },
    }
    assert validate_workflow_definition(definition) == []
    frozen = FrozenDefinition(definition)
    frame = GroupFrame(group_id=group_id, group_execution_id="long-id-group-execution")
    state = {
        "job_id": "long-id-parallel-job",
        "task_id": "long-id-task",
        "workflow_id": "wf",
        "agent_messages": [],
        **prime(definition),
        "run_cursor": RunCursor(frames=[frame]).dump(),
        "parallel_lanes": [
            lane.dump() for lane in initial_lanes(frozen, NodeRef("group", group_id))
        ],
    }
    monkeypatch.setattr(common, "publish_agent_message", lambda *args, **kwargs: True)
    update = _run(
        state, lambda _agent: pytest.fail("Set variable must use its real control-flow executor")
    )
    assert _statuses(update) == {lane_id: "completed"}
    assert update["parallel_outcome"]["status"] == "completed"
    trace = update["workflow_run_trace"]
    assert trace["truncated"] is False
    # The fixture starts inside a real container, so its old-cursor coverage stays partial.
    assert trace["covered_from_start"] is False
    assert len(trace["transitions"]) == 2
    assert trace["transitions"][0]["source"] == node(source_id)
    assert trace["transitions"][0]["target"] == node(target_id)
    assert trace["transitions"][1]["target"] == lane_exit
    assert all(fact["lane_id"] == lane_id for fact in trace["transitions"])
    assert all(fact["frames"][-1]["id"] == group_id for fact in trace["transitions"])
    assert all(
        fact["frames"][-1]["execution_id"] == frame.execution_id for fact in trace["transitions"]
    )


def test_container_progress_counts_committed_lanes_not_completed_steps_in_one_lane():
    from core.application.orchestration.step_hub import orchestrator_hub_node

    definition = _definition(
        {
            "lane_a": [_agent("a1", "composer"), _agent("a2", "scout")],
            "lane_b": [
                {**_agent("b1", "proofreader"), "error_policy": {"on_error": "await_approval"}}
            ],
        }
    )
    initial = {
        "job_id": "11",
        "project_id": "project-1",
        "workflow_id": "wf",
        "agent_messages": [],
        **prime(definition),
    }
    first = orchestrator_hub_node(initial)
    started = {**initial, **first.update}
    root_result = {**started, **_result(started, "transform_step")}
    lanes_command = orchestrator_hub_node(root_result)
    state = {**root_result, **lanes_command.update}
    frame = state["run_cursor"]["frames"][-1]

    def resolve(agent):
        def execute(local_state):
            return _result(local_state, agent, "fail" if agent == "proofreader" else "pass")

        return execute

    update = _run(state, resolve)
    assert _statuses(update) == {"lane_a": "completed", "lane_b": "escalated"}
    assert update["parallel_outcome"]["status"] == "escalated"
    assert (
        next(lane for lane in update["parallel_lanes"] if lane["lane_id"] == "lane_a")[
            "completed_steps"
        ]
        == 2
    )
    progress = update.get("workflow_container_progress")
    assert progress is not None, "A committed partial lane result needs its exact container pass"
    assert progress["covered_from_start"] is True
    assert len(progress["passes"]) == 1
    container = progress["passes"][0]
    assert container["container"] == {"kind": "group", "id": "p"}
    assert container["execution_id"] == frame["group_execution_id"]
    assert container["outcome"] is None
    assert container.get("finished_sequence") is None
    assert container["lanes"]["total_count"] == 2
    assert container["lanes"]["completed_count"] == 1
    assert {row["lane_id"]: row["status"] for row in container["lanes"]["rows"]} == {
        "lane_a": "completed",
        "lane_b": "escalated",
    }


def test_lane_agent_update_cannot_replace_runtime_container_progress():
    from core.application.orchestration.step_hub import orchestrator_hub_node

    definition = _definition(TWO_LANES)
    initial = {
        "job_id": "11",
        "project_id": "project-1",
        "workflow_id": "wf",
        "agent_messages": [],
        **prime(definition),
    }
    started = {**initial, **orchestrator_hub_node(initial).update}
    root_result = {**started, **_result(started, "transform_step")}
    state = {**root_result, **orchestrator_hub_node(root_result).update}
    frame = state["run_cursor"]["frames"][-1]

    def resolve(agent):
        def execute(local_state):
            return {
                **_result(local_state, agent),
                "workflow_container_progress": {"forged": True},
                "domain_zero": 0,
            }

        return execute

    update = _run(state, resolve)
    assert _statuses(update) == {"lane_a": "completed", "lane_b": "completed"}
    assert update["domain_zero"] == 0
    progress = update.get("workflow_container_progress")
    assert progress is not None
    assert "forged" not in progress
    assert progress["job_id"] == "11"
    assert progress["covered_from_start"] is True
    assert len(progress["passes"]) == 1
    container = progress["passes"][0]
    assert container["execution_id"] == frame["group_execution_id"]
    assert container["container"] == {"kind": "group", "id": "p"}
    assert container["lanes"]["completed_count"] == 2
    assert {row["lane_id"]: row["status"] for row in container["lanes"]["rows"]} == {
        "lane_a": "completed",
        "lane_b": "completed",
    }


def test_container_lane_future_exception_does_not_count_discarded_work_as_complete():
    from core.application.orchestration.step_hub import orchestrator_hub_node

    definition = _definition(TWO_LANES)
    initial = {
        "job_id": "11",
        "project_id": "project-1",
        "workflow_id": "wf",
        "agent_messages": [],
        **prime(definition),
    }
    started = {**initial, **orchestrator_hub_node(initial).update}
    completed = {**started, **_result(started, "transform_step")}
    state = {**completed, **orchestrator_hub_node(completed).update}

    def resolve(agent):
        if agent == "proofreader":
            raise RuntimeError("local future failure")
        return _fake_agent(str(agent))

    update = _run(state, resolve)
    assert _statuses(update) == {"lane_a": "completed", "lane_b": "failed"}
    progress = update["workflow_container_progress"]["passes"][0]
    assert progress["outcome"] is None
    assert progress["finished_sequence"] is None
    assert progress["lanes"]["completed_count"] == 1
    assert progress["lanes"]["total_count"] == 2
    assert progress["lanes"]["rows"] == [
        {"lane_id": "lane_a", "status": "completed"},
        {"lane_id": "lane_b", "status": "failed"},
    ]


def test_container_lanes_reset_for_next_item_without_reusing_previous_completion():
    from core.application.orchestration.step_hub import orchestrator_hub_node

    definition = _definition(TWO_LANES)
    body = definition["groups"].pop()["body"]
    definition["for_each"] = [
        {"for_each_id": "p", "over": "state.items", "item_name": "current_item", "body": body}
    ]
    for step in definition["steps"]:
        if step.pop("group_id", None) == "p":
            step["for_each_id"] = "p"
    for route in definition["execution_graph"]["edges"]:
        if route["source"]["kind"] == "group":
            route["source"]["kind"] = "for_each"
        target = route["target"]
        if target["kind"] == "group":
            target["kind"] = "for_each"
        elif target["kind"] == "group_exit":
            target["kind"] = "for_each_exit"
            target["for_each_id"] = target.pop("group_id")
    initial = {
        "job_id": "11",
        "project_id": "project-1",
        "workflow_id": "wf",
        "items": [1, 2],
        "agent_messages": [],
        **prime(definition),
    }
    started = {**initial, **orchestrator_hub_node(initial).update}
    completed = {**started, **_result(started, "transform_step")}
    state = {**completed, **orchestrator_hub_node(completed).update}
    lanes = _run(state, lambda agent: _fake_agent(str(agent)))
    first = lanes["workflow_container_progress"]["passes"][0]
    assert first["lanes"]["item_index"] == 0
    assert first["lanes"]["completed_count"] == 2
    next_item = orchestrator_hub_node({**state, **lanes})
    second = next_item.update["workflow_container_progress"]["passes"][0]
    assert next_item.goto == "parallel_executor"
    assert second["execution_id"] == first["execution_id"]
    assert second["outcome"] is None
    assert second["for_each"]["item_index"] == second["lanes"]["item_index"] == 1
    assert second["for_each"]["processed_count"] == 1
    assert second["lanes"]["completed_count"] == 0
    assert second["lanes"]["total_count"] == 2
    assert all(row["status"] == "pending" for row in second["lanes"]["rows"])
    assert first["lanes"]["completed_count"] == 2


def _container_recovery_return(
    kind, *, job_id="11", workflow_id="wf", retry_policy="await_approval"
):
    """Real hub start and real executor merge; only external agent results are substituted."""
    from core.application.orchestration.step_hub import orchestrator_hub_node

    right = _agent("b1", "proofreader")
    if kind == "approval":
        right["error_policy"] = {"on_error": "await_approval"}
    elif kind == "retry":
        right = {
            "step_id": "b1",
            "step_kind": "http_request",
            "error_policy": {"on_error": retry_policy},
            "override": {"http_request": {"method": "GET", "url": "https://example.test/read"}},
        }
    definition = _definition({"lane_a": [_agent("a1", "composer")], "lane_b": [right]})
    initial = {
        "job_id": job_id,
        "project_id": "project-1",
        "workflow_id": workflow_id,
        "status": "in_progress",
        "agent_messages": [],
        **prime(definition),
    }
    started = {**initial, **orchestrator_hub_node(initial).update}
    root_result = {**started, **_result(started, "transform_step")}
    entered = {**root_result, **orchestrator_hub_node(root_result).update}

    def resolve(agent):
        def execute(local_state):
            return _result(local_state, agent, "fail" if agent == "proofreader" else "pass")

        return execute

    if kind == "retry":
        with patch(
            "core.application.orchestration.http_request_step_runtime.execute_http_request_step",
            return_value={
                "ok": False,
                "status_code": 503,
                "failure_reason": "http_status",
                "response_mode": "inline",
                **_RETRYABLE_READ,
            },
        ):
            returned = {**entered, **_run(entered, resolve)}
    else:
        returned = {**entered, **_run(entered, resolve)}
    assert _statuses(returned) == {
        "lane_a": "completed",
        "lane_b": "escalated" if kind == "approval" else "retry_operation",
    }
    return definition, returned


@pytest.mark.parametrize("approved", [True, False])
def test_container_progress_captures_committed_lane_recovery_decision(approved):
    from core.application.orchestration.step_hub import orchestrator_hub_node

    _, state = _container_recovery_return("approval")
    before = state["workflow_container_progress"]["passes"][0]
    paused = orchestrator_hub_node(state)
    assert paused.goto == "human_approval_gate"
    resumed = orchestrator_hub_node(
        {
            **state,
            **paused.update,
            "user_approved": approved,
            "review_feedback": "Revise the local result",
        }
    )
    assert resumed.goto == "parallel_executor"
    lanes = {row["lane_id"]: row for row in resumed.update["parallel_lanes"]}
    record = resumed.update["workflow_container_progress"]["passes"][0]
    assert record["execution_id"] == before["execution_id"]
    assert record["parent_frames"] == before["parent_frames"]
    assert record["lanes"]["item_index"] == before["lanes"]["item_index"] is None
    assert record["lanes"]["rows"] == [
        {"lane_id": row["lane_id"], "status": row["status"]}
        for row in resumed.update["parallel_lanes"]
    ]
    assert {row["lane_id"]: row["status"] for row in record["lanes"]["rows"]} == {
        "lane_a": "completed",
        "lane_b": "running",
    }
    assert record["lanes"]["completed_count"] == 1
    assert record["lanes"]["total_count"] == 2
    assert record["outcome"] is None
    assert record["finished_sequence"] is None
    assert record["updated_sequence"] > before["updated_sequence"]
    assert lanes["lane_a"]["completed_steps"] == 1
    assert lanes["lane_b"]["decision"] == (
        {"action": "continue"}
        if approved
        else {"action": "retry", "feedback": "Revise the local result"}
    )
    assert lanes["lane_b"]["attempt"] == (1 if approved else 2)


def test_container_progress_captures_real_retry_deadline_escalation(monkeypatch):
    import core.application.orchestration.step_hub_groups as hub_groups
    from core.application.orchestration.step_hub import orchestrator_hub_node

    _, state = _container_recovery_return("retry")
    before = state["workflow_container_progress"]["passes"][0]
    retrying = next(row for row in state["parallel_lanes"] if row["lane_id"] == "lane_b")
    deadline = retrying["operation"]["deadline_epoch_seconds"]
    monkeypatch.setattr(hub_groups.time, "time", lambda: deadline + 1)
    paused = orchestrator_hub_node(state)
    assert paused.goto == "human_approval_gate"
    assert (
        paused.update["recovery_pending"]["reason"] == "step error_policy.on_error=await_approval"
    )
    assert (
        paused.update["recovery_pending"]["detail"]["retry_reason_code"]
        == "retry_delay_exceeds_deadline"
    )
    record = paused.update["workflow_container_progress"]["passes"][0]
    assert record["execution_id"] == before["execution_id"]
    assert record["lanes"]["rows"] == [
        {"lane_id": row["lane_id"], "status": row["status"]}
        for row in paused.update["parallel_lanes"]
    ]
    assert {row["lane_id"]: row["status"] for row in record["lanes"]["rows"]} == {
        "lane_a": "completed",
        "lane_b": "escalated",
    }
    assert record["lanes"]["completed_count"] == 1
    assert record["lanes"]["total_count"] == 2
    assert record["outcome"] is None
    assert record["finished_sequence"] is None
    assert record["updated_sequence"] > before["updated_sequence"]


@pytest.mark.parametrize("policy", [None, "continue", "route_to_step", "retry", "await_approval"])
@pytest.mark.parametrize("inherited", [False, True])
@pytest.mark.parametrize("transport", ["unknown", "forbidden"])
def test_real_http_lane_host_failure_obeys_authored_policy_before_regeneration(
    monkeypatch, policy, inherited, transport
):
    from core.application.orchestration.graph.nodes import execute_step_node
    from core.application.orchestration.parallel_executor_node import parallel_executor_node
    from core.application.orchestration.step_hub import orchestrator_hub_node

    authored = {"on_error": policy} if policy else {}
    definition = _definition(
        {
            "delivery": [
                {
                    "step_id": "send",
                    "step_kind": "http_request",
                    "error_policy": {} if inherited else authored,
                    "override": {
                        "http_request": {"method": "POST", "url": "https://example.test/write"}
                    },
                },
                {
                    "step_id": "lookup",
                    "step_kind": "http_request",
                    "override": {
                        "http_request": {"method": "GET", "url": "https://example.test/lookup"}
                    },
                },
            ]
        },
        concurrency_limit=1,
    )
    definition["groups"][0]["error_policy"] = authored if inherited else {}
    definition["steps"].append({"step_id": "outer_handler", "step_kind": "transform"})
    definition["execution_graph"]["edges"].extend(
        [
            edge("send", node("lookup"), "error"),
            edge("p", node("outer_handler"), "error", source_kind="group"),
            edge("outer_handler", node("done")),
        ]
    )
    calls = []

    def request(*, config, state):
        calls.append(config["url"])
        if config["method"] == "POST":
            return {
                "ok": False,
                "status_code": None,
                "failure_reason": "timeout",
                "response_mode": "inline",
                "operation_kind": "action",
                "request_semantics": "mutation",
                "failure_class": "transient",
                "external_effect_status": "timeout_unknown" if transport == "unknown" else "failed",
                "definitely_no_external_effect": transport != "unknown",
                "retryable": transport == "unknown",
            }
        return {
            "ok": True,
            "status_code": 200,
            "response_mode": "inline",
            "response_body": {"record_id": "actual-read-id"},
        }

    def execute(local_state):
        update = execute_step_node(local_state)
        message = update["agent_messages"][-1]
        if message["step_id"] == "send":
            message["artifacts"]["_schema_errors"] = [{"message": "write response field missing"}]
        return update

    monkeypatch.setattr(
        "core.application.orchestration.http_request_step_runtime.execute_http_request_step",
        request,
    )
    monkeypatch.setattr("core.application.orchestration.graph.nodes.execute_step_node", execute)
    state = _state(definition)
    update = parallel_executor_node(state)
    lane = update["parallel_lanes"][0]
    assert lane["attempt"] == 1
    assert not update.get("recovery_pending")
    assert not update.get("operation_retry_dispatch")
    if policy == "continue" or (policy == "route_to_step" and not inherited):
        assert lane["status"] == "completed"
        assert calls == ["https://example.test/write", "https://example.test/lookup"]
    else:
        opted_transport_gate = policy == "await_approval" and transport == "forbidden"
        assert lane["status"] == ("escalated" if opted_transport_gate else "failed")
        assert calls == ["https://example.test/write"]
        command = orchestrator_hub_node({**state, **update})
        if opted_transport_gate:
            assert command.goto == "human_approval_gate"
        else:
            assert command.goto != "human_approval_gate"
            assert not command.update.get("recovery_pending")
        if policy == "route_to_step" and inherited:
            assert command.update["current_step_id"] == "outer_handler"
        if policy == "await_approval" and transport == "unknown":
            assert "external_effect_resolution_unsupported" in lane["failure"]["reason"]


@pytest.mark.parametrize("policy", [None, "retry", "await_approval"])
@pytest.mark.parametrize("expiry", ["before_hub", "in_wait"])
def test_actual_lane_deadline_expiry_does_not_force_approval_or_repeat(monkeypatch, policy, expiry):
    from core.application.orchestration.parallel_executor_node import parallel_executor_node
    from core.application.orchestration.step_hub import orchestrator_hub_node
    from core.application.workflows.workflow_lifecycle_event_sink import bind_workflow_event_sink
    from core.application.workflows.workflow_reaction_events import EVENT_STEP_FAILED

    clock = [100.0]
    monkeypatch.setattr(
        "core.application.orchestration.step_operation_retry.time.time", lambda: clock[0]
    )
    calls = []

    def request(*, config, state):
        calls.append(config["url"])
        return {
            "ok": False,
            "status_code": 503,
            "failure_reason": "http_status",
            "response_mode": "inline",
            "operation_kind": "source",
            "request_semantics": "safe_read",
            "failure_class": "provider",
            "external_effect_status": "failed",
            "definitely_no_external_effect": True,
            "retryable": True,
        }

    monkeypatch.setattr(
        "core.application.orchestration.http_request_step_runtime.execute_http_request_step",
        request,
    )
    definition = _definition(
        {
            "delivery": [
                {
                    "step_id": "fetch",
                    "step_kind": "http_request",
                    "error_policy": {"on_error": policy} if policy else {},
                    "override": {
                        "http_request": {"method": "GET", "url": "https://example.test/read"}
                    },
                }
            ]
        },
        concurrency_limit=1,
    )
    state = _state(definition)
    state["project_policy"] = {
        "retry_max_attempts": 2,
        "retry_backoff_initial_seconds": 0,
        "retry_backoff_max_seconds": 0,
        "retry_operation_deadline_seconds": 100,
    }
    update = parallel_executor_node(state)
    assert update["parallel_outcome"]["status"] == "retry_operation"
    assert update["parallel_lanes"][0]["attempt"] == 1
    state.update(update)
    if expiry == "in_wait":
        from core.application.orchestration.operation_retry_wait_node import (
            operation_retry_wait_node,
        )

        queued = orchestrator_hub_node(state)
        assert queued.goto == "operation_retry_wait"
        state.update(queued.update)
        clock[0] = 201.0
        waited = operation_retry_wait_node(state)
        assert waited.goto == "orchestrator"
        state.update(waited.update)
    else:
        clock[0] = 201.0
    events = []
    with bind_workflow_event_sink(lambda **event: events.append(event)):
        command = orchestrator_hub_node(state)
    failed_step_events = [event for event in events if event["event_type"] == EVENT_STEP_FAILED]
    if policy == "await_approval":
        assert failed_step_events == []
    else:
        assert len(failed_step_events) == 1
        payload = failed_step_events[0]["payload"]
        assert payload["step_id"] == "fetch"
        assert payload["step_execution_id"] == state["parallel_lanes"][0]["step_execution_id"]
        assert payload["attempt"] == 1
    assert not command.update.get("operation_retry_dispatch")
    assert calls == ["https://example.test/read"]
    if policy == "await_approval":
        assert command.goto == "human_approval_gate"
    else:
        assert command.goto != "human_approval_gate"
        assert command.update["status"] == "failed"
        assert not command.update.get("recovery_pending")


@pytest.mark.parametrize("policy,expected_lane", [("continue", "completed"), ("fail", "failed")])
def test_expired_lane_retry_keeps_same_command_public_inventory_and_accepted_trace(
    monkeypatch, policy, expected_lane
):
    import core.application.orchestration.step_hub_groups as hub_groups
    from core.application.jobs.workflow_run_models import PublicWorkflowTraceV1
    from core.application.orchestration.container_progress import committed_container_progress
    from core.application.orchestration.step_hub import orchestrator_hub_node

    definition, state = _container_recovery_return("retry", retry_policy=policy)
    before = state["workflow_container_progress"]["passes"][0]
    retrying = next(row for row in state["parallel_lanes"] if row["lane_id"] == "lane_b")
    assert retrying["status"] == "retry_operation"
    deadline = retrying["operation"]["deadline_epoch_seconds"]
    monkeypatch.setattr(hub_groups.time, "time", lambda: deadline + 1)

    command = orchestrator_hub_node(state)
    values = {**state, **command.update}
    assert command.goto != "human_approval_gate"
    assert not command.update.get("operation_retry_dispatch")
    assert not command.update.get("recovery_pending")
    assert values["agent_messages"] == state["agent_messages"]
    trace = PublicWorkflowTraceV1.model_validate(values["workflow_run_trace"])
    public = committed_container_progress(
        values,
        FrozenDefinition(definition),
        identity={
            "job_id": state["job_id"],
            "project_id": state["project_id"],
            "workflow_id": state["workflow_id"],
            **state["workflow_definition_ref"],
        },
        trace=trace,
    )
    assert public is not None
    record = public.passes[0]
    assert record.execution_id == before["execution_id"]
    assert record.parent_frames == []
    assert record.updated_sequence > before["updated_sequence"]
    assert record.lanes is not None
    assert record.lanes.item_index is None
    assert [row.model_dump() for row in record.lanes.rows] == [
        {"lane_id": row["lane_id"], "status": row["status"]} for row in values["parallel_lanes"]
    ]
    assert {row.lane_id: row.status for row in record.lanes.rows} == {
        "lane_a": "completed",
        "lane_b": expected_lane,
    }
    assert record.lanes.total_count == 2
    facts = [fact for fact in trace.transitions if fact.source.id == "b1"]
    if policy == "continue":
        assert command.goto == "parallel_executor"
        assert record.lanes.completed_count == 2
        assert record.current
        assert record.outcome is None
        assert record.finished_sequence is None
        assert not public.checkpoint_terminal
        assert len(facts) == 1
        fact = facts[0]
        assert fact.step_execution_id == retrying["step_execution_id"]
        assert fact.attempt == retrying["attempt"] == 1
        assert fact.source.model_dump() == node("b1")
        assert fact.target.model_dump() == _exit("lane_b")
        assert fact.outcome == {"name": "success"}
        assert fact.lane_id == "lane_b"
        assert [frame.execution_id for frame in fact.frames] == [record.execution_id]
    else:
        assert values["status"] == "failed"
        assert public.checkpoint_terminal
        assert not record.current
        assert record.outcome == "error"
        assert record.finished_sequence is not None
        assert record.lanes.completed_count == 1
        assert facts == []
    # Constructing a Command does not mutate its input checkpoint evidence.
    assert state["workflow_container_progress"]["passes"][0] == before
    assert retrying["status"] == "retry_operation"


def test_lanes_writing_beside_each_other_under_one_parent_both_land() -> None:
    """Each lane's update carries its whole top-level key; only what it changed may land (0.7.24)."""
    from core.application.orchestration.parallel_executor_node import _merge
    from core.application.orchestration.step_templating import set_state_path

    state = {"result": {"seed": 1}, "workflow_data": {"result": {"seed": 1}, "kept": True}}

    def lane_update(target: str, value: str) -> dict:
        update: dict = {}
        set_state_path(update, state, target, value)
        return update

    rows = [
        {"lane_id": "a", "lane_order": 0, "position": 0, "state_update": lane_update("state.result.a", "from a")},
        {"lane_id": "b", "lane_order": 1, "position": 0, "state_update": lane_update("state.result.b", "from b")},
        {"lane_id": "c", "lane_order": 2, "position": 0, "state_update": lane_update("state.total", "from c")},
    ]
    _, merged = _merge(state, rows)

    assert merged["result"] == {"seed": 1, "a": "from a", "b": "from b"}
    assert merged["total"] == "from c"
    assert merged["workflow_data"] == {
        "result": {"seed": 1, "a": "from a", "b": "from b"},
        "kept": True,
        "total": "from c",
    }
    # The baseline the lanes started from is not changed in place.
    assert state["result"] == {"seed": 1}


def test_two_lanes_changing_the_same_field_keep_lane_order() -> None:
    from core.application.orchestration.parallel_executor_node import _merge

    state = {"result": {"status": "new"}}
    rows = [
        {"lane_id": "b", "lane_order": 1, "position": 0, "state_update": {"result": {"status": "from b"}}},
        {"lane_id": "a", "lane_order": 0, "position": 0, "state_update": {"result": {"status": "from a"}}},
    ]
    _, merged = _merge(state, rows)

    assert merged["result"] == {"status": "from b"}


def test_two_lanes_creating_the_same_new_map_both_land() -> None:
    from core.application.orchestration.parallel_executor_node import _merge

    rows = [
        {"lane_id": "a", "lane_order": 0, "position": 0, "state_update": {"totals": {"a": 1}}},
        {"lane_id": "b", "lane_order": 1, "position": 0, "state_update": {"totals": {"b": 2}}},
    ]
    _, merged = _merge({}, rows)

    assert merged["totals"] == {"a": 1, "b": 2}
