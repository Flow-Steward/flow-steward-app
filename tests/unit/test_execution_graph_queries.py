from __future__ import annotations

from core.application.workflows.execution_graph_queries import DefinitionGraph


def ref(node_id: str, kind: str = "step") -> dict:
    return {"kind": kind, "id": node_id}


def edge(
    source: str, target: dict, outcome: str = "success", source_kind: str = "step", **extra
) -> dict:
    return {
        "source": ref(source, source_kind),
        "outcome": {"name": outcome},
        "target": target,
        **extra,
    }


def exit_ref(group_id: str, lane_id: str | None = None) -> dict:
    return {"kind": "group_exit", "group_id": group_id, "lane_id": lane_id}


def graph_of(*edges: dict) -> dict:
    return {"schema_version": 1, "entry": ref("a"), "edges": list(edges)}


def test_branches_see_only_their_own_path_and_the_join_sees_both() -> None:
    steps = [{"step_id": s} for s in ("a", "check", "b", "c", "d")]
    view = DefinitionGraph(
        steps,
        [],
        graph_of(
            edge("a", ref("check")),
            edge("check", ref("b"), "true"),
            edge("check", ref("c"), "false"),
            edge("b", ref("d")),
            edge("c", ref("d")),
        ),
    )
    assert view.upstream_step_ids("b") == ["a", "check"]
    assert view.upstream_step_ids("c") == ["a", "check"]
    assert view.upstream_step_ids("d") == ["a", "check", "b", "c"]


def test_rework_edge_does_not_make_a_later_step_upstream() -> None:
    steps = [{"step_id": s} for s in ("a", "draft", "check")]
    view = DefinitionGraph(
        steps,
        [],
        graph_of(
            edge("a", ref("draft")),
            edge("draft", ref("check")),
            edge("check", ref("draft"), "false", rework=True),
        ),
    )
    assert view.upstream_step_ids("draft") == ["a"]


def test_body_step_sees_its_body_predecessors_and_the_root_before_the_group() -> None:
    steps = [
        {"step_id": "a"},
        {"step_id": "l1", "group_id": "g"},
        {"step_id": "l2", "group_id": "g"},
        {"step_id": "after"},
    ]
    groups = [{"group_id": "g", "body": {"kind": "sequence", "entry": ref("l1")}}]
    view = DefinitionGraph(
        steps,
        groups,
        graph_of(
            edge("a", ref("g", "group")),
            edge("l1", ref("l2")),
            edge("l2", exit_ref("g")),
            edge("g", ref("after"), source_kind="group"),
        ),
    )
    assert view.upstream_step_ids("l2") == ["a", "l1"]
    assert view.upstream_step_ids("l1") == ["a"]
    assert view.upstream_step_ids("after") == ["a", "l1", "l2"]
    assert view.group_of("l1")["group_id"] == "g"


def test_parallel_sibling_lanes_are_not_upstream_of_each_other() -> None:
    steps = [
        {"step_id": "a"},
        {"step_id": "x1", "group_id": "p", "lane_id": "left"},
        {"step_id": "y1", "group_id": "p", "lane_id": "right"},
    ]
    view = DefinitionGraph(
        steps,
        [],
        graph_of(
            edge("a", ref("p", "group")),
            edge("x1", exit_ref("p", "left")),
            edge("y1", exit_ref("p", "right")),
        ),
    )
    assert view.upstream_step_ids("x1") == ["a"]
    assert view.upstream_step_ids("y1") == ["a"]


def test_unknown_step_has_no_upstream() -> None:
    assert DefinitionGraph([], [], None).upstream_step_ids("nope") == []
