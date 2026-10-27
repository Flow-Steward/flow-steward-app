from __future__ import annotations

import pytest

from core.application.workflows.execution_graph import (
    ROOT_SCOPE,
    Edge,
    GroupExitRef,
    NodeRef,
    Outcome,
    edge_identity,
    group_scope,
    parse_execution_graph,
)


def _graph(*edges: dict, entry: dict | None = None) -> dict:
    return {
        "schema_version": 1,
        "entry": entry or {"kind": "step", "id": "a"},
        "edges": list(edges),
    }


def _edge(source: str, outcome: str, target: dict, **extra) -> dict:
    return {
        "source": {"kind": "step", "id": source},
        "outcome": {"name": outcome},
        "target": target,
        **extra,
    }


def _codes(issues: list[dict]) -> list[str]:
    return [issue["code"] for issue in issues]


def test_round_trip_is_lossless() -> None:
    raw = _graph(
        _edge("a", "success", {"kind": "group", "id": "g"}),
        {
            "source": {"kind": "step", "id": "s"},
            "outcome": {"name": "case", "case_id": "c1"},
            "target": {"kind": "step", "id": "a"},
            "rework": True,
            "rework_limit": 3,
        },
        _edge("b", "success", {"kind": "group_exit", "group_id": "g", "lane_id": None}),
    )
    graph, issues = parse_execution_graph(raw)
    assert issues == []
    assert graph is not None
    dumped = graph.dump()
    assert dumped["entry"] == raw["entry"]
    assert dumped["edges"][0]["rework"] is False
    assert dumped["edges"][1] == raw["edges"][1]
    assert dumped["edges"][2]["target"] == {"kind": "group_exit", "group_id": "g", "lane_id": None}
    assert parse_execution_graph(dumped)[0] == graph


def test_missing_graph_and_wrong_version_are_refused() -> None:
    assert _codes(parse_execution_graph(None)[1]) == ["workflow.graph.missing"]
    assert _codes(parse_execution_graph({"schema_version": 2, "entry": None})[1]) == [
        "workflow.graph.schema_version_unsupported"
    ]
    assert _codes(parse_execution_graph({"schema_version": True})[1]) == [
        "workflow.graph.schema_version_unsupported"
    ]


def test_missing_entry_is_an_issue() -> None:
    graph, issues = parse_execution_graph({"schema_version": 1, "edges": []})
    assert graph is None
    assert _codes(issues) == ["workflow.graph.entry_missing"]


@pytest.mark.parametrize(
    ("edge", "code"),
    [
        (_edge("a", "next", {"kind": "step", "id": "b"}), "workflow.graph.outcome_invalid"),
        (
            {**_edge("a", "case", {"kind": "step", "id": "b"})},
            "workflow.graph.case_id_required",
        ),
        (
            {
                "source": {"kind": "step", "id": "a"},
                "outcome": {"name": "true", "case_id": "x"},
                "target": {"kind": "step", "id": "b"},
            },
            "workflow.graph.case_id_unexpected",
        ),
        (
            _edge("a", "true", {"kind": "step", "id": "b"}, rework_limit=2),
            "workflow.graph.rework_limit_unexpected",
        ),
        (
            _edge("a", "true", {"kind": "step", "id": "b"}, rework=True, rework_limit=0),
            "workflow.graph.rework_limit_invalid",
        ),
        (
            _edge("a", "true", {"kind": "step", "id": "b"}, rework=True, rework_limit=51),
            "workflow.graph.rework_limit_invalid",
        ),
        (
            _edge("a", "true", {"kind": "step", "id": "b"}, rework=True, rework_limit=True),
            "workflow.graph.rework_limit_invalid",
        ),
        (
            _edge("a", "true", {"kind": "step", "id": "b"}, rework="yes"),
            "workflow.graph.rework_invalid",
        ),
        (
            _edge("a", "success", {"kind": "group_exit", "group_id": "g"}),
            "workflow.graph.group_exit_invalid",
        ),
        (
            _edge("a", "success", {"kind": "phase", "id": "p"}),
            "workflow.graph.node_ref_invalid",
        ),
        (
            _edge("a", "success", {"kind": "step", "id": "b"}, target_phase="x"),
            "workflow.graph.key_unknown",
        ),
    ],
)
def test_edge_shape_issues(edge: dict, code: str) -> None:
    graph, issues = parse_execution_graph(_graph(edge))
    assert graph is None
    assert code in _codes(issues)


def test_edge_for_finds_the_single_outcome_edge() -> None:
    graph, _ = parse_execution_graph(
        _graph(
            _edge("a", "true", {"kind": "step", "id": "b"}),
            _edge("a", "false", {"kind": "step", "id": "c"}),
        )
    )
    assert graph is not None
    source = NodeRef("step", "a")
    assert graph.edge_for(source, Outcome("false")).target == NodeRef("step", "c")
    assert graph.edge_for(source, Outcome("success")) is None
    assert len(graph.outgoing(source)) == 2


def test_edge_identity_ignores_target_and_list_order() -> None:
    first = Edge(NodeRef("step", "a"), Outcome("case", "c1"), NodeRef("step", "b"), True, 2)
    moved = Edge(NodeRef("step", "a"), Outcome("case", "c1"), NodeRef("step", "z"), True, 2)
    assert edge_identity(ROOT_SCOPE, first) == edge_identity(ROOT_SCOPE, moved)
    assert edge_identity(ROOT_SCOPE, first) != edge_identity(group_scope("g", None), first)
    assert edge_identity(ROOT_SCOPE, first) != edge_identity(
        ROOT_SCOPE, Edge(NodeRef("step", "a"), Outcome("case", "c2"), NodeRef("step", "b"))
    )


def test_group_exit_ref_dump_keeps_explicit_null_lane() -> None:
    assert GroupExitRef("g", None).dump() == {
        "kind": "group_exit",
        "group_id": "g",
        "lane_id": None,
    }
