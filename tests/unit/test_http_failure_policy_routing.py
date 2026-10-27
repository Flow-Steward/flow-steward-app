"""A definitive HTTP rejection follows authored policy, not a hidden human pause."""

from __future__ import annotations

from copy import deepcopy
from unittest.mock import patch

import pytest

import core.application.orchestration.agents.common as agents_common
from core.application.orchestration.active_step import active_step
from core.application.orchestration.graph.nodes import execute_step_node
from core.application.orchestration.http_request_step_runtime import _http_retry_facts
from core.application.orchestration.parallel_group_runtime import decide_lane_result
from core.application.orchestration.run_cursor import (
    ForEachFrame,
    FrozenDefinition,
    GroupFrame,
    RunCursor,
    begin_step,
)
from core.application.orchestration.run_definition import clear_run_definition_cache
from core.application.orchestration.step_hub import orchestrator_hub_node
from core.application.orchestration.step_operation_retry import (
    decide_operation_retry,
    host_operation_retry_signal,
    operation_retry_identity,
)
from tests._step_run_state import prime
from tests._workflow_definitions import edge, node

POLICIES = (None, "fail", "continue", "retry", "route_to_step", "await_approval")


@pytest.fixture(autouse=True)
def _isolated(monkeypatch):
    clear_run_definition_cache()
    monkeypatch.setattr(agents_common, "emit_event", lambda *args, **kwargs: None)
    monkeypatch.setattr(agents_common, "publish_agent_message", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        "core.application.orchestration.step_hub_results.time.sleep", lambda _: None
    )
    yield
    clear_run_definition_cache()


def _case(
    policy=None,
    *,
    kind=None,
    inherited=False,
    task_source="workflow_test_run",
    authored_max_retries=1,
):
    """The HTTP step at the root, in a For each body, or in a parallel Group lane."""
    owner_key = "for_each_id" if kind == "for_each" else "group_id"
    scope = {owner_key: "g", "lane_id": "left" if kind == "parallel" else None} if kind else {}
    step = {
        "step_id": "fetch",
        "step_kind": "http_request",
        "override": {
            "http_request": {
                "method": "GET",
                "url": "https://example.test/orders",
                "expected_statuses": [201],
            }
        },
        **scope,
    }
    authored = (
        {"on_error": policy, **({"max_retries": authored_max_retries} if policy == "retry" else {})}
        if policy
        else {}
    )
    if not inherited:
        step["error_policy"] = authored
    groups: list[dict] = []
    for_each: list[dict] = []
    steps = [
        step,
        {"step_id": "next", "step_kind": "transform", **scope},
        {"step_id": "handled", "step_kind": "transform", **scope},
        {"step_id": "done", "step_kind": "stop_success"},
        {"step_id": "group_failed", "step_kind": "stop_fail"},
    ]
    edges = [edge("fetch", node("next")), edge("fetch", node("handled"), "error")]
    entry = node("fetch")
    frames: list = []
    if kind:
        container = {
            "error_policy": authored if inherited else {},
            "body": {"kind": "sequence", "entry": node("fetch")},
        }
        if kind == "parallel":
            container["body"] = {
                "kind": "lanes",
                "lanes": [{"lane_id": "left", "entry": node("fetch")}],
            }
            groups.append({"group_id": "g", **container})
            exit_ref = {"kind": "group_exit", "group_id": "g", "lane_id": "left"}
            node_kind = "group"
            frames = [GroupFrame("g", "group-execution")]
        elif kind == "group":
            groups.append({"group_id": "g", **container})
            exit_ref = {"kind": "group_exit", "group_id": "g", "lane_id": None}
            node_kind = "group"
            frames = [GroupFrame("g", "group-execution")]
        else:
            for_each.append(
                {"for_each_id": "g", "over": "state.items", "item_name": "item", **container}
            )
            exit_ref = {"kind": "for_each_exit", "for_each_id": "g", "lane_id": None}
            node_kind = "for_each"
            frames = [ForEachFrame("g", "for-each-execution", selected_count=1)]
        edges.extend(
            [
                edge("next", exit_ref),
                edge("handled", exit_ref),
                edge("g", node("done"), source_kind=node_kind),
                edge("g", node("group_failed"), "error", source_kind=node_kind),
            ]
        )
        entry = node("g", node_kind)
    else:
        edges.extend([edge("next", node("done")), edge("handled", node("done"))])
    definition = {
        "workflow_id": "wf",
        "steps": steps,
        "groups": groups,
        "for_each": for_each,
        "settings": {},
        "execution_graph": {"schema_version": 1, "entry": entry, "edges": edges},
    }
    cursor = begin_step(RunCursor(frames=frames), "fetch")
    state = {
        **prime(definition),
        "workflow_id": "wf",
        "job_id": "http-policy-job",
        "task_source": task_source,
        "agent_messages": [],
        "items": ["one"],
        "run_cursor": cursor.dump(),
        "current_step_id": "fetch",
        "dispatched_step_execution": {
            "step_id": "fetch",
            "step_execution_id": cursor.step_execution_id,
            "attempt": cursor.attempt,
        },
    }
    return state, FrozenDefinition(definition)


def _http_failure(state):
    # The real HTTP classifier and dispatch envelope; only the network adapter is replaced.
    result = {
        "ok": False,
        "status_code": 200,
        "failure_reason": "unexpected_status",
        "response_mode": "inline",
        **_http_retry_facts(
            method="GET", ok=False, status_code=200, failure_reason="unexpected_status"
        ),
    }
    with patch(
        "core.application.orchestration.http_request_step_runtime.execute_http_request_step",
        return_value=result,
    ):
        message = execute_step_node(state)["agent_messages"][-1]
    assert message["operation_retry"]["provenance"] == "deterministic_http_step"
    assert message["operation_retry"]["signal"]["retryable"] is False
    return message


@pytest.mark.parametrize("task_source", ["workflow_test_run", "api"])
@pytest.mark.parametrize("policy", POLICIES)
@pytest.mark.parametrize(
    ("kind", "inherited"), [(None, False), ("for_each", False), ("for_each", True)]
)
def test_definitive_http_failure_uses_effective_policy(policy, task_source, kind, inherited):
    state, _ = _case(policy, kind=kind, inherited=inherited, task_source=task_source)
    state["agent_messages"] = [_http_failure(state)]
    command = orchestrator_hub_node(state)
    if policy in (None, "fail"):
        assert command.goto == "orchestrator_validation"
        assert command.update["status"] == "failed"
        assert command.update["unresolved_step_failure"]["step_id"] == "fetch"
        assert (
            command.update["unresolved_step_failure"]["step_execution_id"]
            == state["run_cursor"]["step_execution_id"]
        )
    elif policy == "continue":
        assert command.update["current_step_id"] == "next"
        assert command.update["orchestrator_journal"][0]["detail"]["_step_error_policy"] == {
            "on_error": "continue",
            "applied": True,
            "original_status": "fail",
        }
    elif policy == "retry":
        assert command.goto == "orchestrator_validation"
        assert command.update["status"] == "failed"
        assert "not_retryable" in command.update["error"]
        assert not command.update.get("operation_retry_dispatch")
        assert state["run_cursor"]["attempt"] == 1
    elif policy == "route_to_step":
        assert command.update["current_step_id"] == ("group_failed" if inherited else "handled")
        if inherited:
            assert command.update["run_cursor"]["frames"] == []
    else:
        assert command.goto == "human_approval_gate"
        assert (
            command.update["recovery_pending"]["reason"]
            == "step error_policy.on_error=await_approval"
        )
    if policy != "await_approval":
        assert not command.update.get("recovery_pending")


@pytest.mark.parametrize("policy", POLICIES)
@pytest.mark.parametrize("inherited", [False, True])
def test_parallel_http_failure_uses_the_same_effective_policy(policy, inherited):
    state, definition = _case(policy, kind="parallel", inherited=inherited)
    current = active_step(state)
    assert current is not None
    decision = decide_lane_result(state, definition, current, _http_failure(state))
    expected = {
        None: "fail",
        "fail": "fail",
        "continue": "advance",
        "retry": "fail",
        "route_to_step": "fail" if inherited else "advance",
        "await_approval": "escalate",
    }
    assert decision.action == expected[policy]
    if policy in ("continue", "route_to_step") and decision.action == "advance":
        assert decision.outcome.name == ("error" if policy == "route_to_step" else "success")
    if policy == "await_approval":
        assert decision.reason == "step error_policy.on_error=await_approval"
    elif policy == "retry":
        assert "not_retryable" in decision.reason
    elif policy != "continue":
        source = "default" if policy is None else "group" if inherited else "step"
        assert decision.detail["policy_source"] == source


@pytest.mark.parametrize("policy", ["continue", "retry", "route_to_step", "await_approval"])
def test_isolated_step_test_failure_remains_terminal(policy):
    state, _ = _case(policy, task_source="step_test_run")
    state["step_test_run_context"] = {
        "workflow_id": "wf",
        "execution_id": "test",
        "step_id": "fetch",
    }
    state["agent_messages"] = [_http_failure(state)]
    command = orchestrator_hub_node(state)
    assert command.goto == "orchestrator_validation"
    assert command.update["status"] == "failed"
    assert not command.update.get("recovery_pending")
    assert "current_step_id" not in command.update


@pytest.mark.parametrize(
    "changes",
    [
        {"request_semantics": "mutation"},
        {"request_semantics": "reconciled"},
        {"request_semantics": ""},
        {"external_effect_status": "running"},
        {"external_effect_status": "succeeded"},
        {"external_effect_status": "suppressed"},
        {"external_effect_status": ""},
        {"definitely_no_external_effect": False},
        {"definitely_no_external_effect": None},
        {"definitely_no_external_effect": "true"},
        {"retryable": None},
        {"retryable": "false"},
    ],
)
@pytest.mark.parametrize("policy", ["continue", "route_to_step"])
def test_forbidden_host_transport_retry_uses_authored_route_without_implicit_approval(
    changes, policy
):
    state, _ = _case(policy)
    message = _http_failure(state)
    message["operation_retry"]["signal"].update(changes)
    state["agent_messages"] = [message]
    command = orchestrator_hub_node(state)
    assert command.update["current_step_id"] == ("next" if policy == "continue" else "handled")
    assert not command.update.get("recovery_pending")
    assert not command.update.get("operation_retry_dispatch")
    assert state["run_cursor"]["attempt"] == 1


@pytest.mark.parametrize("policy", POLICIES)
@pytest.mark.parametrize(
    ("kind", "inherited"),
    [(None, False), ("group", False), ("group", True), ("for_each", False), ("for_each", True)],
)
def test_unknown_http_uses_authored_error_policy_without_implicit_approval(policy, kind, inherited):
    state, _ = _case(policy, kind=kind, inherited=inherited)
    original_cursor = deepcopy(state["run_cursor"])
    message = _http_failure(state)
    message["operation_retry"]["signal"].update(
        external_effect_status="timeout_unknown", request_semantics="mutation", retryable=True
    )
    # Safe-looking model artifacts cannot erase the validated host's unknown result.
    message["artifacts"].update(
        external_effect_status="failed",
        definitely_no_external_effect=True,
        _schema_errors=[{"field": "record_id", "message": "producer response was lost"}],
    )
    state["agent_messages"] = [message]
    command = orchestrator_hub_node(state)
    if policy != "await_approval":
        assert not command.update.get("recovery_pending")
        assert not command.update.get("needs_user_approval")
    assert not command.update.get("operation_retry_dispatch")
    assert command.update.get("external_effect_resolution_ref") is None
    if policy == "await_approval":
        assert command.goto == "human_approval_gate"
        pending = command.update["recovery_pending"]
        assert pending["kind"] == "failure"
        assert pending["reason"] == "step error_policy.on_error=await_approval"
        assert pending["allowed_actions"] == ["approve", "reject"]
        assert "external_effect" not in pending["detail"]
        assert command.update["needs_user_approval"] is True
    elif policy == "continue":
        assert command.update["current_step_id"] == "next"
    elif policy == "route_to_step":
        assert command.update["current_step_id"] == ("group_failed" if inherited else "handled")
        if inherited:
            assert command.update["run_cursor"]["frames"] == []
    else:
        assert command.goto == "orchestrator_validation"
        assert command.update["status"] == "failed"
        if policy == "retry":
            assert "ambig" in command.update["error"].lower()
    assert state["run_cursor"] == original_cursor


@pytest.mark.parametrize("policy", POLICIES)
@pytest.mark.parametrize("inherited", [False, True])
def test_unknown_http_lane_uses_policy_without_contract_retry_or_generic_gate(policy, inherited):
    state, definition = _case(policy, kind="parallel", inherited=inherited)
    current = active_step(state)
    assert current is not None
    message = _http_failure(state)
    message["operation_retry"]["signal"]["external_effect_status"] = "timeout_unknown"
    message["artifacts"]["_schema_errors"] = [{"message": "response field was lost"}]
    decision = decide_lane_result(state, definition, current, message)
    expected = (
        "advance"
        if policy == "continue" or (policy == "route_to_step" and not inherited)
        else "fail"
    )
    assert decision.action == expected
    if expected == "advance":
        assert decision.outcome.name == ("error" if policy == "route_to_step" else "success")
    if policy == "await_approval":
        assert "external_effect_resolution_unsupported" in decision.reason
    elif policy == "retry":
        assert "ambig" in decision.reason.lower()
    assert state["run_cursor"]["attempt"] == 1


@pytest.mark.parametrize(
    ("retryable", "retry_count", "deadline", "expected"),
    [
        (True, 0, 200.0, "retry_operation"),
        (False, 0, 200.0, "escalate_to_human"),
        (True, 2, 200.0, "escalate_to_human"),
        (True, 0, 99.0, "escalate_to_human"),
        (False, 2, 200.0, "escalate_to_human"),
        (False, 0, 99.0, "escalate_to_human"),
    ],
)
def test_technical_budget_and_deadline_are_not_bypassed(retryable, retry_count, deadline, expected):
    state, _ = _case("continue")
    message = _http_failure(state)
    signal = message["operation_retry"]["signal"]
    signal["retryable"] = retryable
    message["operation_retry"]["deadline_epoch_seconds"] = deadline
    current = active_step(state)
    assert current is not None
    identity = operation_retry_identity(state, current, signal)
    state["operation_retry_state"] = {
        identity: {"retry_count": retry_count, "deadline_epoch_seconds": deadline}
    }
    decision = decide_operation_retry(state, current, message, now=100.0)
    assert decision is not None
    assert decision.action == expected


def test_model_artifacts_cannot_replace_host_effect_evidence():
    state, _ = _case("retry")
    message = _http_failure(state)
    safe_facts = deepcopy(message["operation_retry"]["signal"])
    message["operation_retry"]["signal"]["external_effect_status"] = "timeout_unknown"
    message["artifacts"].update(safe_facts)
    state["agent_messages"] = [message]
    original_cursor = deepcopy(state["run_cursor"])
    command = orchestrator_hub_node(state)
    assert command.goto == "orchestrator_validation"
    assert command.update["status"] == "failed"
    assert "ambig" in command.update["error"].lower()
    assert not command.update.get("recovery_pending")
    assert not command.update.get("operation_retry_dispatch")
    assert command.update.get("run_cursor", original_cursor) == original_cursor
    assert state["run_cursor"] == original_cursor


def _transport_failure(state, stop):
    """A real deterministic HTTP envelope plus its persisted logical retry history."""
    message = _http_failure(state)
    message["operation_retry"]["signal"].update(
        failure_class="provider", retryable=stop != "forbidden"
    )
    deadline = 99.0 if stop == "deadline" else 200.0
    message["operation_retry"]["deadline_epoch_seconds"] = deadline
    current = active_step(state)
    assert current is not None
    identity = operation_retry_identity(state, current, host_operation_retry_signal(message))
    rows = state.setdefault("operation_retry_state", {})
    rows.setdefault(identity, {"retry_count": 0, "deadline_epoch_seconds": deadline})
    if stop == "budget":
        rows[identity]["retry_count"] = 2
    state["project_policy"] = {
        "retry_max_attempts": 2,
        "retry_backoff_initial_seconds": 0,
        "retry_backoff_max_seconds": 0,
    }
    message["artifacts"]["_schema_errors"] = [{"message": "transport response field missing"}]
    return message, identity


@pytest.mark.parametrize("policy", POLICIES)
@pytest.mark.parametrize("stop", ["forbidden", "budget", "deadline"])
@pytest.mark.parametrize(
    ("kind", "inherited"), [(None, False), ("group", True), ("for_each", True)]
)
def test_stopped_host_transport_retry_applies_authored_policy_before_contract_regeneration(
    monkeypatch, policy, stop, kind, inherited
):
    monkeypatch.setattr(
        "core.application.orchestration.step_operation_retry.time.time", lambda: 100.0
    )
    state, _ = _case(policy, kind=kind, inherited=inherited)
    message, identity = _transport_failure(state, stop)
    state["agent_messages"] = [message]
    original_cursor = deepcopy(state["run_cursor"])
    original_budget = deepcopy(state["operation_retry_state"])
    command = orchestrator_hub_node(state)
    assert not command.update.get("operation_retry_dispatch")
    assert command.update.get("operation_retry_state", original_budget) == original_budget
    assert state["run_cursor"] == original_cursor
    assert state["operation_retry_state"][identity] == original_budget[identity]
    if policy == "continue":
        assert command.update["current_step_id"] == "next"
    elif policy == "route_to_step":
        assert command.update["current_step_id"] == ("group_failed" if inherited else "handled")
    elif policy == "await_approval":
        assert command.goto == "human_approval_gate"
        assert command.update["recovery_pending"]["kind"] == "failure"
    else:
        assert command.goto == "orchestrator_validation"
        assert command.update["status"] == "failed"
    if policy != "await_approval":
        assert not command.update.get("recovery_pending")
        assert not command.update.get("needs_user_approval")


@pytest.mark.parametrize("policy", POLICIES)
@pytest.mark.parametrize("stop", ["forbidden", "budget", "deadline"])
@pytest.mark.parametrize("inherited", [False, True])
def test_lane_stopped_host_transport_retry_uses_policy_without_regeneration_or_budget_reset(
    monkeypatch, policy, stop, inherited
):
    monkeypatch.setattr(
        "core.application.orchestration.step_operation_retry.time.time", lambda: 100.0
    )
    state, definition = _case(policy, kind="parallel", inherited=inherited)
    message, _ = _transport_failure(state, stop)
    current = active_step(state)
    assert current is not None
    decision = decide_lane_result(state, definition, current, message)
    expected = (
        "advance"
        if policy == "continue" or (policy == "route_to_step" and not inherited)
        else "escalate"
        if policy == "await_approval"
        else "fail"
    )
    assert decision.action == expected
    assert state["run_cursor"]["attempt"] == 1
    if expected == "advance":
        assert decision.outcome.name == ("error" if policy == "route_to_step" else "success")
    elif policy == "retry":
        assert decision.reason != "error_policy_retry"
    if policy == "await_approval":
        assert "await_approval" in decision.reason


@pytest.mark.parametrize("policy", [None, "continue", "route_to_step", "retry", "await_approval"])
def test_admissible_transport_retries_keep_one_logical_budget_before_authored_policy(
    monkeypatch, policy
):
    monkeypatch.setattr(
        "core.application.orchestration.step_operation_retry.time.time", lambda: 100.0
    )
    state, _ = _case(policy)
    execution_id = state["run_cursor"]["step_execution_id"]
    for retry_number in (1, 2):
        message, identity = _transport_failure(state, "admissible")
        state["agent_messages"] = [message]
        command = orchestrator_hub_node(state)
        assert command.goto == "operation_retry_wait"
        assert not command.update.get("recovery_pending")
        dispatch = command.update["operation_retry_dispatch"]
        assert dispatch["identity"] == identity
        assert dispatch["deadline_epoch_seconds"] == 200.0
        assert dispatch["step_execution_id"] == execution_id
        assert command.update["run_cursor"]["attempt"] == retry_number + 1
        assert command.update["operation_retry_state"][identity]["retry_count"] == retry_number
        state.update(command.update)
    message, identity = _transport_failure(state, "admissible")
    state["agent_messages"] = [message]
    command = orchestrator_hub_node(state)
    assert not command.update.get("operation_retry_dispatch")
    assert state["operation_retry_state"][identity]["retry_count"] == 2
    assert state["run_cursor"]["step_execution_id"] == execution_id
    assert state["run_cursor"]["attempt"] == 3
    if policy == "continue":
        assert command.update["current_step_id"] == "next"
    elif policy == "route_to_step":
        assert command.update["current_step_id"] == "handled"
    elif policy == "await_approval":
        assert command.goto == "human_approval_gate"
    else:
        assert command.update["status"] == "failed"
    if policy != "await_approval":
        assert not command.update.get("recovery_pending")


@pytest.mark.parametrize("policy", [None, "retry", "continue", "route_to_step", "await_approval"])
def test_real_retry_wait_deadline_failure_cannot_restart_transport_step_or_open_unrequested_gate(
    monkeypatch, policy
):
    from core.application.orchestration.operation_retry_wait_node import operation_retry_wait_node

    clock = [100.0]
    monkeypatch.setattr(
        "core.application.orchestration.step_operation_retry.time.time", lambda: clock[0]
    )
    state, _ = _case(policy, authored_max_retries=4)
    message, identity = _transport_failure(state, "admissible")
    message["artifacts"].pop("_schema_errors")
    state["agent_messages"] = [message]
    queued = orchestrator_hub_node(state)
    assert queued.goto == "operation_retry_wait"
    state.update(queued.update)
    clock[0] = 201.0
    waited = operation_retry_wait_node(state)
    assert waited.goto == "orchestrator"
    state.update(waited.update)
    failed_message = state["agent_messages"][-1]
    assert failed_message["skill"] == "operation_retry_wait"
    command = orchestrator_hub_node(state)
    with patch(
        "core.application.orchestration.http_request_step_runtime.execute_http_request_step",
        return_value={
            "ok": False,
            "response_mode": "inline",
            **_http_retry_facts(
                method="GET", ok=False, status_code=None, failure_reason="network_error"
            ),
        },
    ) as provider_after_deadline:
        if command.goto == "execute_step" and command.update.get("current_step_id") == "fetch":
            execute_step_node({**state, **command.update})
        provider_after_deadline.assert_not_called()
    assert not command.update.get("operation_retry_dispatch")
    assert state["operation_retry_state"][identity]["retry_count"] == 1
    assert state["run_cursor"]["attempt"] == 2
    if policy == "continue":
        assert command.update["current_step_id"] == "next"
    elif policy == "route_to_step":
        assert command.update["current_step_id"] == "handled"
    elif policy == "await_approval":
        assert command.goto == "human_approval_gate"
    else:
        assert command.goto == "orchestrator_validation"
        assert command.update["status"] == "failed"
    if policy != "await_approval":
        assert not command.update.get("recovery_pending")


@pytest.mark.parametrize("policy", [None, "retry", "continue", "route_to_step", "await_approval"])
def test_transport_deadline_expiring_after_selection_uses_policy_before_queueing(
    monkeypatch, policy
):
    from core.application.orchestration.step_hub import _StepHub

    clock = [100.0]
    monkeypatch.setattr(
        "core.application.orchestration.step_operation_retry.time.time", lambda: clock[0]
    )
    original = _StepHub.operation_retry_decision

    def select_then_expire(hub, current, message):
        decision = original(hub, current, message)
        clock[0] = 201.0
        return decision

    monkeypatch.setattr(_StepHub, "operation_retry_decision", select_then_expire)
    state, _ = _case(policy)
    message, _ = _transport_failure(state, "admissible")
    # This case exercises the queue's own deadline recheck, not producer contracts.
    message["artifacts"].pop("_schema_errors")
    state["agent_messages"] = [message]
    command = orchestrator_hub_node(state)
    assert not command.update.get("operation_retry_dispatch")
    assert state["run_cursor"]["attempt"] == 1
    if policy == "continue":
        assert command.update["current_step_id"] == "next"
    elif policy == "route_to_step":
        assert command.update["current_step_id"] == "handled"
    elif policy == "await_approval":
        assert command.goto == "human_approval_gate"
    else:
        assert command.update["status"] == "failed"
    if policy != "await_approval":
        assert not command.update.get("recovery_pending")
