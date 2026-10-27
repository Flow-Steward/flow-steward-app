from __future__ import annotations

import itertools

import pytest

from core.application.orchestration.run_cursor import (
    TRANSITION_BODY_DONE,
    TRANSITION_BREAK,
    TRANSITION_DISPATCH,
    TRANSITION_ENTER,
    TRANSITION_ESCALATE,
    TRANSITION_LANES,
    TRANSITION_STOP,
    ForEachFrame,
    FrozenDefinition,
    GroupFrame,
    RunCursor,
    RunCursorError,
    advance_item,
    begin_step,
    body_start,
    break_item,
    grant_rework_budget,
    push_frame,
    resolve_after_container,
    resolve_after_step,
    resolve_entry,
    retry_step,
)
from core.application.workflows.execution_graph import NodeRef, Outcome
from tests._workflow_definitions import edge, linear_graph, node


def ids():
    counter = itertools.count(1)
    return lambda: f"exec-{next(counter)}"


def definition(steps, edges, entry="a", groups=None, for_each=None) -> FrozenDefinition:
    return FrozenDefinition(
        {
            "steps": steps,
            "groups": groups or [],
            "for_each": for_each or [],
            "execution_graph": {"schema_version": 1, "entry": node(entry), "edges": edges},
        }
    )


def test_linear_run_dispatches_in_graph_order_and_stops() -> None:
    frozen = FrozenDefinition(
        {
            "steps": [
                {"step_id": "a", "step_kind": "http_request"},
                {"step_id": "b", "step_kind": "transform"},
                {"step_id": "done", "step_kind": "stop_success"},
            ],
            "groups": [],
            "execution_graph": linear_graph(["a", "b", "done"]),
        }
    )
    cursor = RunCursor()
    first = resolve_entry(frozen)
    assert (first.kind, first.step_id) == (TRANSITION_DISPATCH, "a")
    begin_step(cursor, "a", id_factory=ids())
    second = resolve_after_step(frozen, cursor, Outcome("success"))
    assert second.step_id == "b"
    begin_step(cursor, "b")
    third = resolve_after_step(frozen, cursor, Outcome("success"))
    begin_step(cursor, third.step_id)
    end = resolve_after_step(frozen, cursor, Outcome("success"))
    assert (end.kind, end.outcome) == (TRANSITION_STOP, "success")


def test_retry_keeps_the_execution_and_a_new_entry_gets_a_new_one() -> None:
    cursor = begin_step(RunCursor(), "a", id_factory=ids())
    execution = cursor.step_execution_id
    retry_step(cursor)
    assert (cursor.step_execution_id, cursor.attempt) == (execution, 2)
    begin_step(cursor, "a")
    assert cursor.step_execution_id != execution
    assert cursor.attempt == 1


def test_branch_outcome_follows_its_own_edge() -> None:
    frozen = definition(
        [
            {"step_id": "a", "step_kind": "if"},
            {"step_id": "yes", "step_kind": "stop_success"},
            {"step_id": "no", "step_kind": "stop_fail"},
        ],
        [edge("a", node("yes"), "true"), edge("a", node("no"), "false")],
    )
    cursor = begin_step(RunCursor(), "a")
    assert resolve_after_step(frozen, cursor, Outcome("false")).step_id == "no"


def test_missing_edge_is_an_integrity_error_not_a_success() -> None:
    frozen = definition([{"step_id": "a", "step_kind": "transform"}], [])
    cursor = begin_step(RunCursor(), "a")
    outcome = Outcome("success")
    with pytest.raises(RunCursorError):
        resolve_after_step(frozen, cursor, outcome)


def review_loop(limit: int | None = None) -> FrozenDefinition:
    rework_edge = edge("check", node("draft"), "false", rework=True)
    if limit is not None:
        rework_edge["rework_limit"] = limit
    return definition(
        [
            {"step_id": "draft", "step_kind": "agent"},
            {"step_id": "check", "step_kind": "if"},
            {"step_id": "done", "step_kind": "stop_success"},
        ],
        [edge("draft", node("check")), edge("check", node("done"), "true"), rework_edge],
        entry="draft",
    )


def test_fifty_rework_transitions_pass_and_the_fifty_first_escalates() -> None:
    frozen = review_loop()
    cursor = RunCursor()
    for _ in range(50):
        begin_step(cursor, "check")
        assert resolve_after_step(frozen, cursor, Outcome("false")).step_id == "draft"
    begin_step(cursor, "check")
    blocked = resolve_after_step(frozen, cursor, Outcome("false"))
    assert blocked.kind == TRANSITION_ESCALATE
    assert blocked.exhausted == ("run",)
    assert cursor.rework.used == 50


def test_edge_limit_blocks_first_and_approval_grants_one_more_block_once() -> None:
    frozen = review_loop(limit=2)
    cursor = RunCursor()
    for _ in range(2):
        begin_step(cursor, "check")
        resolve_after_step(frozen, cursor, Outcome("false"))
    begin_step(cursor, "check")
    blocked = resolve_after_step(frozen, cursor, Outcome("false"))
    assert blocked.exhausted == ("edge",)

    grant_rework_budget(cursor, blocked, edge_limit=2)
    taken = resolve_after_step(frozen, cursor, Outcome("false"))
    assert taken.step_id == "draft"
    counters = next(iter(cursor.rework.edges.values()))
    assert counters == {"used": 3, "allowance": 4}
    assert cursor.rework.used == 3


def test_rework_counters_survive_a_cursor_round_trip() -> None:
    frozen = review_loop(limit=5)
    cursor = begin_step(RunCursor(), "check")
    resolve_after_step(frozen, cursor, Outcome("false"))
    restored = RunCursor.load(cursor.dump())
    assert restored.rework.dump() == cursor.rework.dump()


FE = NodeRef("for_each", "fe")
GROUP = NodeRef("group", "g")


def for_each_definition() -> FrozenDefinition:
    """a → Group g → For each fe (l1 → brk | skip) → done, error → failed."""
    return definition(
        [
            {"step_id": "a", "step_kind": "transform"},
            {"step_id": "l1", "step_kind": "if", "for_each_id": "fe"},
            {"step_id": "brk", "step_kind": "break", "for_each_id": "fe"},
            {"step_id": "skip", "step_kind": "continue", "for_each_id": "fe"},
            {"step_id": "done", "step_kind": "stop_success"},
            {"step_id": "failed", "step_kind": "stop_fail"},
        ],
        [
            edge("a", node("g", "group")),
            edge("l1", node("brk"), "true"),
            edge("l1", node("skip"), "false"),
            edge(
                "fe",
                {"kind": "group_exit", "group_id": "g", "lane_id": None},
                source_kind="for_each",
            ),
            edge("g", node("done"), source_kind="group"),
            edge("g", node("failed"), "error", source_kind="group"),
        ],
        groups=[
            {
                "group_id": "g",
                "body": {"kind": "sequence", "entry": node("fe", "for_each")},
                "error_policy": {"on_error": "route_to_step"},
            }
        ],
        for_each=[
            {
                "for_each_id": "fe",
                "group_id": "g",
                "over": "state.items",
                "item_name": "item",
                "body": {"kind": "sequence", "entry": node("l1")},
            }
        ],
    )


def test_a_group_starts_its_for_each_and_the_item_body_starts_at_its_entry() -> None:
    frozen = for_each_definition()
    cursor = RunCursor()
    group = push_frame(frozen, cursor, GROUP, id_factory=ids())
    assert isinstance(group, GroupFrame)
    entered = body_start(frozen, group)
    assert (entered.kind, entered.container) == (TRANSITION_ENTER, FE)
    item = push_frame(frozen, cursor, FE, id_factory=ids())
    assert isinstance(item, ForEachFrame)
    assert item.selected_count == 0
    assert body_start(frozen, item).step_id == "l1"
    assert cursor.scope == ("for_each", "fe", None)


def test_items_advance_until_the_pass_is_over() -> None:
    frozen = for_each_definition()
    cursor = RunCursor()
    frame = push_frame(frozen, cursor, FE, id_factory=ids())
    assert isinstance(frame, ForEachFrame)
    frame.selected_count = 3
    assert advance_item(cursor, FE) is True
    assert advance_item(cursor, FE) is True
    assert frame.item_index == 2
    assert advance_item(cursor, FE) is False
    assert frame.processed_count == 3


def test_continue_ends_the_item_and_break_the_for_each() -> None:
    frozen = for_each_definition()
    cursor = RunCursor()
    push_frame(frozen, cursor, GROUP, id_factory=ids())
    frame = push_frame(frozen, cursor, FE, id_factory=ids())
    assert isinstance(frame, ForEachFrame)
    frame.selected_count = 2
    begin_step(cursor, "skip", id_factory=ids())
    cont = resolve_after_step(frozen, cursor, Outcome("success"))
    assert (cont.kind, cont.container) == (TRANSITION_BODY_DONE, FE)
    begin_step(cursor, "brk", id_factory=ids())
    brk = resolve_after_step(frozen, cursor, Outcome("success"))
    assert (brk.kind, brk.container) == (TRANSITION_BREAK, FE)
    assert break_item(cursor, FE).broke_at == 0


def test_a_container_leaves_by_its_edges_in_its_own_scope() -> None:
    frozen = for_each_definition()
    cursor = RunCursor()
    inner = resolve_after_container(frozen, cursor, FE)
    assert (inner.kind, inner.container) == (TRANSITION_BODY_DONE, GROUP)
    assert resolve_after_container(frozen, cursor, GROUP).step_id == "done"
    assert resolve_after_container(frozen, cursor, GROUP, failed=True).step_id == "failed"


def test_every_entry_is_a_new_container_execution() -> None:
    frozen = for_each_definition()
    cursor = RunCursor()
    factory = ids()
    first = push_frame(frozen, cursor, FE, id_factory=factory).execution_id
    cursor.frames.pop()
    second = push_frame(frozen, cursor, FE, id_factory=factory)
    assert second.execution_id != first


def test_frames_survive_a_checkpoint_round_trip_and_old_frames_are_refused() -> None:
    frozen = for_each_definition()
    cursor = RunCursor()
    push_frame(frozen, cursor, GROUP, id_factory=ids())
    frame = push_frame(frozen, cursor, FE, id_factory=ids())
    assert isinstance(frame, ForEachFrame)
    frame.selected_count, frame.item_index, frame.truncated = 2, 1, True
    restored = RunCursor.load(cursor.dump())
    assert restored.dump() == cursor.dump()
    with pytest.raises(RunCursorError):
        RunCursor.load({"frames": [{"group_id": "g", "kind": "loop", "item_index": 0}]})


def test_lanes_body_is_handed_to_the_parallel_executor() -> None:
    frozen = definition(
        [
            {"step_id": "a", "step_kind": "transform"},
            {"step_id": "x1", "step_kind": "http_request", "group_id": "p", "lane_id": "left"},
        ],
        [edge("a", node("p", "group"))],
        groups=[
            {
                "group_id": "p",
                "body": {"kind": "lanes", "lanes": [{"lane_id": "left", "entry": node("x1")}]},
            }
        ],
    )
    frame = push_frame(frozen, RunCursor(), NodeRef("group", "p"))
    assert body_start(frozen, frame).kind == TRANSITION_LANES
    assert frozen.lane_ids(NodeRef("group", "p")) == ["left"]
    assert frozen.body_entry(NodeRef("group", "p"), "left") == NodeRef("step", "x1")


def test_a_draft_without_a_graph_serves_its_steps_but_never_routes() -> None:
    frozen = FrozenDefinition({"steps": [{"step_id": "a", "step_kind": "transform"}], "groups": []})
    assert frozen.step("a")["step_kind"] == "transform"
    with pytest.raises(RunCursorError):
        resolve_entry(frozen)
