"""A Wait that ends on an extension's event: one key, one wake, a deadline (ADR 0071)."""

from __future__ import annotations

from unittest.mock import patch

import pytest

import core.application.orchestration.agents.common as agents_common
from core.application.orchestration.run_cursor import RUNTIME_SCHEMA_VERSION
from core.application.orchestration.run_definition import (
    clear_run_definition_cache,
    prime_run_definition,
)
from core.application.orchestration.step_hub import orchestrator_hub_node
from core.application.orchestration.wait_event_runtime import (
    WAIT_CORRELATION_KEY_INVALID,
    WAIT_EVENT_TIMED_OUT,
    event_wake_verdict,
)
from core.application.orchestration.wait_step_runtime import wait_step_node
from core.application.workflows.workflow_definition_snapshot import definition_digest
from tests._workflow_definitions import edge, node

EVENT_WAIT = {
    "event": {
        "source": "acme.crawler",
        "event_types": ["run_completed", "run_failed"],
        "correlation_key": "state.crawl.run_id",
    },
    "timeout_ms": 3_600_000,
    "output_mapping": {"payload": "state.crawl_result", "event_type": "state.crawl_outcome"},
}


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
    ref = {"snapshot_id": "snap", "revision": 5, "digest": definition_digest(definition)}
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


def _dispatch(state: dict) -> dict:
    command = orchestrator_hub_node(state)
    assert command.goto == "wait_step"
    return _apply(state, command.update)


def _event(pending: dict, **changes) -> dict:
    return {
        "reason": "event",
        "wait_id": pending["wait_id"],
        "event_id": "evt-1",
        "event_type": "run_completed",
        "correlation_key": "run-42",
        "payload": {"rows": 120, "file": "result.csv"},
        "received_at": "2026-11-14T12:00:00+00:00",
        **changes,
    }


def test_the_hub_records_the_event_wait_once_with_its_key_and_deadline() -> None:
    state = _dispatch(_run(EVENT_WAIT, crawl={"run_id": "run-42"}))
    pending = state["pending_wait"]

    assert pending["kind"] == "event"
    assert pending["source"] == "acme.crawler"
    assert pending["event_types"] == ["run_completed", "run_failed"]
    assert pending["correlation_key"] == "run-42"
    assert pending["timeout_ms"] == 3_600_000
    assert pending["due_epoch_ms"] > 3_600_000
    assert pending["workflow_revision"] == 5

    # State changes while the run waits: the key and the deadline stay.
    again = _dispatch({**state, "crawl": {"run_id": "other"}, "dispatched_step_execution": None})
    assert again["pending_wait"] == pending


def test_an_integer_key_is_compared_as_its_text() -> None:
    state = _dispatch(_run(EVENT_WAIT, crawl={"run_id": 4242}))
    assert state["pending_wait"]["correlation_key"] == "4242"


@pytest.mark.parametrize("crawl", [{}, {"run_id": ""}, {"run_id": True}, {"run_id": {"a": 1}}])
def test_a_key_that_does_not_resolve_is_a_typed_step_failure(crawl) -> None:
    command = orchestrator_hub_node(_run(EVENT_WAIT, crawl=crawl))

    failure = command.update["unresolved_step_failure"]
    assert failure["error_code"] == WAIT_CORRELATION_KEY_INVALID
    assert failure["step_id"] == "pause"
    assert not command.update.get("pending_wait")


def test_a_bad_key_follows_the_route_to_step_policy() -> None:
    command = orchestrator_hub_node(_run(EVENT_WAIT, {"on_error": "route_to_step"}, crawl={}))
    assert command.update["run_cursor"]["step_id"] == "handle"


def test_the_event_is_the_steps_result_and_its_mapping_writes_state() -> None:
    state = _dispatch(_run(EVENT_WAIT, crawl={"run_id": "run-42"}))
    pending = state["pending_wait"]

    with patch("langgraph.types.interrupt", return_value=_event(pending)) as interrupt:
        update = wait_step_node(state)

    paused_with = interrupt.call_args.args[0]
    assert paused_with["gate_kind"] == "wait"
    assert paused_with["kind"] == "event"
    assert paused_with["correlation_key"] == "run-42"
    assert update["pending_wait"] is None
    result = update["agent_messages"][-1]
    assert result["status"] == "pass"
    assert result["artifacts"]["payload"] == {"rows": 120, "file": "result.csv"}
    assert result["artifacts"]["event_type"] == "run_completed"
    assert result["artifacts"]["source"] == "acme.crawler"

    finished_state = _apply(state, update)
    assert finished_state["crawl_result"] == {"rows": 120, "file": "result.csv"}
    assert finished_state["crawl_outcome"] == "run_completed"
    finished = orchestrator_hub_node(finished_state)
    assert finished.update["run_cursor"]["step_id"] == "done"


def test_a_foreign_stale_or_unlisted_wake_pauses_again() -> None:
    state = _dispatch(_run(EVENT_WAIT, crawl={"run_id": "run-42"}))
    pending = state["pending_wait"]
    wakes = [
        _event(pending, wait_id="another-wait"),
        _event(pending, correlation_key="run-43"),
        _event(pending, correlation_key="RUN-42"),
        _event(pending, event_type="run_started"),
        _event(pending, payload=["not", "an", "object"]),
        {"reason": "timer", "wait_id": pending["wait_id"], "timer_elapsed": True},
        {"reason": "event", "wait_id": pending["wait_id"], "timed_out": True},  # early
        _event(pending, event_type="run_failed", payload={"error": "blocked"}),
    ]

    with (
        patch("langgraph.types.interrupt", side_effect=wakes) as interrupt,
        patch(
            "core.application.orchestration.wait_step_runtime._now_ms",
            return_value=pending["due_epoch_ms"] - 1,
        ),
    ):
        update = wait_step_node(state)

    assert interrupt.call_count == len(wakes)
    result = update["agent_messages"][-1]
    assert result["status"] == "pass"
    assert result["artifacts"]["event_type"] == "run_failed"


def test_the_deadline_fails_the_step_with_a_timeout_code() -> None:
    state = _dispatch(_run(EVENT_WAIT, crawl={"run_id": "run-42"}))
    pending = state["pending_wait"]
    timeout = {"reason": "event", "wait_id": pending["wait_id"], "timed_out": True}

    with (
        patch("langgraph.types.interrupt", return_value=timeout),
        patch(
            "core.application.orchestration.wait_step_runtime._now_ms",
            return_value=pending["due_epoch_ms"],
        ),
    ):
        update = wait_step_node(state)

    result = update["agent_messages"][-1]
    assert result["status"] == "fail"
    assert result["artifacts"]["error_code"] == WAIT_EVENT_TIMED_OUT
    assert "run-42" in result["summary"]
    assert "crawl_result" not in update


def test_a_timeout_takes_the_error_route_when_the_step_has_one() -> None:
    state = _dispatch(_run(EVENT_WAIT, {"on_error": "route_to_step"}, crawl={"run_id": "run-42"}))
    pending = state["pending_wait"]
    timeout = {"reason": "event", "wait_id": pending["wait_id"], "timed_out": True}
    with (
        patch("langgraph.types.interrupt", return_value=timeout),
        patch(
            "core.application.orchestration.wait_step_runtime._now_ms",
            return_value=pending["due_epoch_ms"],
        ),
    ):
        update = wait_step_node(state)

    routed = orchestrator_hub_node(_apply(state, update))
    assert routed.update["run_cursor"]["step_id"] == "handle"


def test_the_verdict_names_only_its_own_wait() -> None:
    pending = {
        "wait_id": "w",
        "event_types": ["done"],
        "correlation_key": "k",
        "due_epoch_ms": 10_000,
    }
    event = {"reason": "event", "wait_id": "w", "event_type": "done", "correlation_key": "k"}
    assert event_wake_verdict(pending, event, now_ms=0) == "event"
    assert event_wake_verdict(pending, {**event, "wait_id": "x"}, now_ms=0) is None
    timeout = {"reason": "event", "wait_id": "w", "timed_out": True}
    assert event_wake_verdict(pending, timeout, now_ms=9_999) is None
    assert event_wake_verdict(pending, timeout, now_ms=10_000) == "timed_out"
    assert event_wake_verdict(pending, None) is None
