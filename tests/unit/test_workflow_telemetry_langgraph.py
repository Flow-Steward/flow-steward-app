"""Telemetry scope through a real LangGraph run, not hand-called callbacks.

``bind_node`` relies on LangGraph copying the context after ``on_chain_start``,
and a pause on the root run ending with the state it had. Both are properties of
the pinned LangGraph, so they are checked against it.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from typing import Any, TypedDict

from langgraph.graph import END, StateGraph

from core.application.observability.workflow_telemetry import (
    current_workflow_span,
    lane_step_attempt,
)
from tests.unit.test_job_trace_context import _callback, _enqueue_meta
from tests.unit.test_workflow_telemetry import telemetry as telemetry

SEEN: dict[str, Any] = {}


class State(TypedDict, total=False):
    run_cursor: dict
    dispatched_step_execution: dict | None
    items_left: int
    done: bool


def hub(state: State) -> dict:
    left = state.get("items_left", 2)
    if left <= 0:
        return {"done": True, "dispatched_step_execution": None}
    execution_id = f"exec-{left}"  # a new execution per item, as begin_step does
    record = {"step_id": "body", "step_execution_id": execution_id, "attempt": 1}
    return {
        "run_cursor": record,
        "dispatched_step_execution": {**record, "step_kind": "invoke_workflow"},
        "items_left": left - 1,
    }


def step(state: State) -> dict:
    item = state["run_cursor"]["step_execution_id"]
    SEEN[f"{item}:span"] = current_workflow_span()
    SEEN[f"{item}:child_meta"] = _enqueue_meta({"parent_job_id": "job-1"})

    def lane() -> Any:
        with lane_step_attempt("http_request", 1):
            return current_workflow_span()

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(copy_context().run, lane) for _ in range(2)]
        SEEN[f"{item}:lanes"] = [future.result() for future in futures]
    return {}


def _graph():
    graph = StateGraph(State)
    graph.add_node("hub", hub)
    graph.add_node("execute_step", step)
    graph.set_entry_point("hub")
    graph.add_conditional_edges("hub", lambda s: END if s.get("done") else "execute_step")
    graph.add_edge("execute_step", "hub")
    return graph.compile()


def test_node_code_and_lane_threads_see_their_step_and_children_parent_to_it(
    telemetry,
) -> None:
    SEEN.clear()
    callback = _callback()
    for _ in _graph().stream({"items_left": 2}, {"callbacks": [callback]}, stream_mode="values"):
        pass
    finished = telemetry.finished()
    steps = [s for s in finished if s.attributes.get("fs.step.kind") == "invoke_workflow"]
    lanes = [s for s in finished if s.attributes.get("fs.step.kind") == "http_request"]
    segment = next(s for s in finished if s.name == "fs.workflow.segment")
    assert len(steps) == 2
    assert len(lanes) == 4
    for item, step_span in zip(
        ("exec-2", "exec-1"), sorted(steps, key=lambda s: s.start_time), strict=True
    ):
        assert SEEN[f"{item}:span"] is not None, f"{item}: node code does not see its step span"
        tp = SEEN[f"{item}:child_meta"]["trace_context"]["traceparent"]
        assert tp.split("-")[1] == format(segment.context.trace_id, "032x")
        assert tp.split("-")[2] == format(step_span.context.span_id, "016x"), item
    for lane in lanes:
        assert lane.parent is not None and lane.parent.span_id in {s.context.span_id for s in steps}


class _PauseState(TypedDict, total=False):
    status: str
    run_cursor: dict
    dispatched_step_execution: dict | None


def _dispatch_wait(state: _PauseState) -> dict:
    record = {"step_id": "w", "step_execution_id": "exec-w", "attempt": 1}
    return {
        "status": "in_progress",
        "run_cursor": record,
        "dispatched_step_execution": {**record, "step_kind": "wait"},
    }


def _wait(state: _PauseState) -> dict:
    from langgraph.types import interrupt

    interrupt({"reason": "wait"})
    return {}


def test_a_real_interrupt_pauses_the_step_and_its_segment(telemetry) -> None:
    from langgraph.checkpoint.memory import MemorySaver

    graph = StateGraph(_PauseState)
    graph.add_node("hub", _dispatch_wait)
    graph.add_node("execute_step", _wait)
    graph.set_entry_point("hub")
    graph.add_edge("hub", "execute_step")
    graph.add_edge("execute_step", END)
    config = {"callbacks": [_callback()], "configurable": {"thread_id": "t-1"}}
    for _ in graph.compile(checkpointer=MemorySaver()).stream({}, config, stream_mode="values"):
        pass

    outcomes = {span.name: span.attributes["fs.outcome"] for span in telemetry.finished()}
    assert outcomes == {"fs.workflow.step": "paused", "fs.workflow.segment": "paused"}
