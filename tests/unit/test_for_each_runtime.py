"""The For each runtime on the hub: fixed items, Break/Continue, the one limit N (ADR 0038)."""

from __future__ import annotations

import pytest

from core.application.orchestration.run_cursor import RunCursor, RunCursorError
from core.application.orchestration.step_hub import orchestrator_hub_node
from tests._workflow_definitions import edge, node
from tests.unit.test_step_hub import _isolated as _isolated
from tests.unit.test_step_hub import _result, _run, _step

LIMIT_ENV = "FS_DATASET_QUERY_MAX_RUNTIME_ROWS"


def fe_exit(for_each_id: str = "fe") -> dict:
    return {"kind": "for_each_exit", "for_each_id": for_each_id, "lane_id": None}


def definition(
    body_steps: list[dict],
    body_edges: list[dict],
    *,
    entry: str,
    over: str = "state.items",
    root_steps: list[dict] | None = None,
    root_edges: list[dict] | None = None,
    graph_entry: dict | None = None,
    group: bool = False,
) -> dict:
    """[prepare →] For each 'fe' (optionally inside Group 'g') → done."""
    for_each = {
        "for_each_id": "fe",
        "group_id": "g" if group else None,
        "over": over,
        "item_name": "order",
        "index_name": "order_index",
        "body": {"kind": "sequence", "entry": node(entry)},
    }
    steps = [
        *({**step, "for_each_id": "fe"} for step in body_steps),
        *(root_steps or []),
        {"step_id": "done", "step_kind": "stop_success"},
    ]
    edges = [*body_edges, *(root_edges or [])]
    groups = []
    if group:
        groups = [{"group_id": "g", "body": {"kind": "sequence", "entry": node("fe", "for_each")}}]
        edges += [
            edge(
                "fe",
                {"kind": "group_exit", "group_id": "g", "lane_id": None},
                source_kind="for_each",
            ),
            edge("g", node("done"), source_kind="group"),
        ]
    elif not any(e["source"] == node("fe", "for_each") for e in edges):
        edges.append(edge("fe", node("done"), source_kind="for_each"))
    return {
        "steps": steps,
        "groups": groups,
        "for_each": [for_each],
        "execution_graph": {
            "schema_version": 1,
            "entry": graph_entry or node("g" if group else "fe", "group" if group else "for_each"),
            "edges": edges,
        },
        "settings": {},
    }


ONE_STEP = definition(
    [{"step_id": "work", "step_kind": "transform"}],
    [edge("work", fe_exit())],
    entry="work",
)


def run_items(state: dict, *, results: dict[str, dict] | None = None, limit: int = 50) -> tuple:
    """Drive the hub; answer each dispatched step from *results* (default: pass)."""
    seen: list[tuple[str, str, object, object]] = []
    command = orchestrator_hub_node(state)
    for _ in range(limit):
        state = _step(state, command)
        if command.goto in {"orchestrator_validation", "__end__"}:
            return state, command, seen
        step_id = state["current_step_id"]
        data = state.get("workflow_data") or {}
        seen.append(
            (
                step_id,
                state["run_cursor"]["step_execution_id"],
                data.get("order"),
                data.get("order_index"),
            )
        )
        answer = dict((results or {}).get(step_id, {}))
        actor = answer.pop("actor", "control_flow_step" if step_id != "work" else "transform_step")
        status = answer.pop("status", "pass")
        state = _result(state, status, actor=actor, **answer)
        command = orchestrator_hub_node(state)
    raise AssertionError("the run did not end")


def test_selected_source_is_fixed_across_resume() -> None:
    state = _run(ONE_STEP, items=[1, 2, 3], workflow_data={"items": [1, 2, 3]})
    first = orchestrator_hub_node(state)
    state = _step(state, first)
    frame = state["run_cursor"]["frames"][-1]
    assert frame["frame"] == "for_each"
    assert frame["selected_count"] == 3
    # The items live once in their own channel, never in the per-step cursor.
    assert state["for_each_items"] == {frame["for_each_execution_id"]: [1, 2, 3]}
    assert "items" not in frame
    # The source variable grows mid-pass: the pass keeps its fixed items.
    state = {
        **state,
        "items": [1, 2, 3, 4, 5],
        "workflow_data": {**state["workflow_data"], "items": [1, 2, 3, 4, 5]},
    }
    state = _result(state, actor="transform_step")
    _final, end, seen = run_items(state)
    executions = [
        state["run_cursor"]["step_execution_id"],
        *(row[1] for row in seen if row[0] == "work"),
    ]
    assert [row[2] for row in seen if row[0] == "work"] == [2, 3]
    assert len(set(executions)) == 3
    assert end.update["workflow_terminal"]["kind"] == "stop_success"


def test_item_and_index_are_published_for_the_body() -> None:
    state = _run(ONE_STEP, items=["a", "b"])
    _final, _end, seen = run_items(state)
    assert [(row[2], row[3]) for row in seen if row[0] == "work"] == [("a", 0), ("b", 1)]


@pytest.mark.parametrize(
    ("value", "items"),
    [(None, []), ("one", ["one"]), ({"id": 7}, [{"id": 7}]), ([], [])],
)
def test_null_scalar_and_object_sources(value, items) -> None:
    state = _run(ONE_STEP, items=value)
    _final, end, seen = run_items(state)
    assert [row[2] for row in seen if row[0] == "work"] == items
    assert end.update["workflow_terminal"]["kind"] == "stop_success"


def test_a_dataset_cursor_source_is_refused_before_any_item() -> None:
    cursor = {"kind": "dataset_cursor_v1", "dataset_handle": "h", "page_size": 10}
    state = _run(ONE_STEP, items=cursor)
    command = orchestrator_hub_node(state)
    assert command.goto == "orchestrator_validation"
    assert command.update["unresolved_step_failure"]["error_code"] == (
        "for_each.source_cursor_unsupported"
    )


@pytest.mark.parametrize(
    ("count", "limit"), [(999, "1000"), (1000, "1000"), (1001, "1000"), (3, "2")]
)
def test_the_operator_limit_takes_a_prefix_and_reports_truncation(
    monkeypatch, count, limit
) -> None:
    monkeypatch.setenv(LIMIT_ENV, limit)
    state = _run(ONE_STEP, items=list(range(count)))
    state = _step(state, orchestrator_hub_node(state))
    frame = state["run_cursor"]["frames"][-1]
    selected = min(count, int(limit))
    items = state["for_each_items"][frame["for_each_execution_id"]]
    assert frame["selected_count"] == len(items) == selected
    assert items[-1] == selected - 1
    assert (frame["source_count"], frame["applied_limit"]) == (count, int(limit))
    assert frame["truncated"] is (count > int(limit))


@pytest.mark.parametrize("raw", ["0", "-1", "true", "1.5", "lots"])
def test_an_invalid_limit_fails_instead_of_falling_back(monkeypatch, raw) -> None:
    monkeypatch.setenv(LIMIT_ENV, raw)
    command = orchestrator_hub_node(_run(ONE_STEP, items=[1]))
    assert command.goto == "orchestrator_validation"
    assert command.update["unresolved_step_failure"]["error_code"] == "for_each.limit_invalid"


CONTROLS = definition(
    [
        {"step_id": "check", "step_kind": "if", "override": {"if": {"condition": "x"}}},
        {"step_id": "work", "step_kind": "transform"},
        {"step_id": "skip", "step_kind": "continue"},
        {"step_id": "leave", "step_kind": "break"},
    ],
    [
        edge("check", node("work"), "true"),
        edge("check", node("skip"), "false"),
        edge("work", node("leave")),
    ],
    entry="check",
    root_steps=[{"step_id": "after", "step_kind": "transform"}],
    root_edges=[edge("fe", node("after"), source_kind="for_each"), edge("after", node("done"))],
)


def test_continue_skips_the_rest_of_the_item_and_break_leaves_the_for_each() -> None:
    state = _run(CONTROLS, items=[1, 2, 3])
    answers = iter(["false", "true"])

    def drive(state):
        command = orchestrator_hub_node(state)
        steps = []
        for _ in range(30):
            state = _step(state, command)
            if command.goto == "orchestrator_validation":
                return state, command, steps
            step_id = state["current_step_id"]
            steps.append((step_id, (state.get("workflow_data") or {}).get("order")))
            if step_id == "check":
                state = _result(state, actor="control_flow_step", _outcome={"name": next(answers)})
            else:
                actor = "transform_step" if step_id in {"work", "after"} else "control_flow_step"
                state = _result(state, actor=actor)
            command = orchestrator_hub_node(state)
        raise AssertionError("no end")

    _final, end, steps = drive(state)
    # item 1: check false → Continue; item 2: check true → work → Break; item 3 never runs.
    assert steps == [
        ("check", 1),
        ("skip", 1),
        ("check", 2),
        ("work", 2),
        ("leave", 2),
        ("after", 2),
        ("done", 2),
    ]
    assert end.update["workflow_terminal"]["kind"] == "stop_success"


def test_break_reports_the_item_it_left_at() -> None:
    state = _run(CONTROLS, items=[1, 2, 3])
    state = _step(state, orchestrator_hub_node(state))
    state = _result(state, actor="control_flow_step", _outcome={"name": "true"})
    state = _step(state, orchestrator_hub_node(state))
    state = _step(
        _result(state, actor="transform_step"),
        orchestrator_hub_node(_result(state, actor="transform_step")),
    )
    assert state["current_step_id"] == "leave"
    command = orchestrator_hub_node(_result(state, actor="control_flow_step"))
    finished = [
        entry
        for entry in command.update["orchestrator_journal"]
        if entry["event"] == "for_each_completed"
    ]
    assert finished
    assert finished[0]["detail"]["broke_at"] == 0
    assert finished[0]["detail"]["processed_count"] == 1
    assert command.update["current_step_id"] == "after"


def test_a_group_holding_a_for_each_runs_once_around_the_pass() -> None:
    grouped = definition(
        [{"step_id": "work", "step_kind": "transform"}],
        [edge("work", fe_exit())],
        entry="work",
        group=True,
    )
    state = _run(grouped, items=[1, 2])
    state = _step(state, orchestrator_hub_node(state))
    frames = state["run_cursor"]["frames"]
    assert [frame["frame"] for frame in frames] == ["group", "for_each"]
    final, end, seen = run_items(_result(state, actor="transform_step"))
    assert [row[2] for row in seen if row[0] == "work"] == [2]
    assert end.update["workflow_terminal"]["kind"] == "stop_success"
    assert final["run_cursor"]["frames"] == []


def test_a_long_pass_is_authored_progress_not_a_runaway() -> None:
    state = _run(ONE_STEP, items=list(range(200)))
    _final, end, seen = run_items(state, limit=500)
    assert len([row for row in seen if row[0] == "work"]) == 200
    assert end.update.get("status") != "stuck"
    assert end.update["workflow_terminal"]["kind"] == "stop_success"


def test_a_rework_re_entry_selects_the_items_again(monkeypatch) -> None:
    looped = definition(
        [{"step_id": "work", "step_kind": "transform"}],
        [edge("work", fe_exit())],
        entry="work",
        root_steps=[
            {"step_id": "check", "step_kind": "if", "override": {"if": {"condition": "x"}}}
        ],
        root_edges=[
            edge("fe", node("check"), source_kind="for_each"),
            edge("check", node("done"), "true"),
            {**edge("check", node("fe", "for_each"), "false"), "rework": True},
        ],
    )
    monkeypatch.setenv(LIMIT_ENV, "1")
    state = _run(looped, items=[1, 2])
    state = _step(state, orchestrator_hub_node(state))
    first_execution = state["run_cursor"]["frames"][-1]["for_each_execution_id"]
    state = _step(
        _result(state, actor="transform_step"),
        orchestrator_hub_node(_result(state, actor="transform_step")),
    )
    assert state["current_step_id"] == "check"
    monkeypatch.setenv(LIMIT_ENV, "5")
    state = {**state, "items": [7, 8, 9]}
    reentered = _result(state, actor="control_flow_step", _outcome={"name": "false"})
    state = _step(reentered, orchestrator_hub_node(reentered))
    frame = state["run_cursor"]["frames"][-1]
    assert state["for_each_items"] == {frame["for_each_execution_id"]: [7, 8, 9]}
    assert frame["applied_limit"] == 5
    assert frame["for_each_execution_id"] != first_execution


def test_a_pre_release_loop_frame_is_refused() -> None:
    with pytest.raises(RunCursorError, match="removed pre-release format"):
        RunCursor.load({"step_id": "a", "frames": [{"group_id": "g", "kind": "loop"}]})
