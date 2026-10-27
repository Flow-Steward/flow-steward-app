"""The final definition model: run-once Groups and a standalone For each (ADR 0038)."""

from __future__ import annotations

from typing import Any

import pytest

from core.application.workflows.workflow_definition_validation import (
    effective_error_policy_source,
    validate_workflow_definition,
)


def step(step_id: str, kind: str = "http_request", **extra: Any) -> dict[str, Any]:
    return {
        "step_id": step_id,
        "step_kind": kind,
        "group_id": None,
        "for_each_id": None,
        "lane_id": None,
        **extra,
    }


def ref(node_id: str, kind: str = "step") -> dict[str, str]:
    return {"kind": kind, "id": node_id}


def group_exit(group_id: str, lane_id: str | None = None) -> dict[str, Any]:
    return {"kind": "group_exit", "group_id": group_id, "lane_id": lane_id}


def for_each_exit(for_each_id: str, lane_id: str | None = None) -> dict[str, Any]:
    return {"kind": "for_each_exit", "for_each_id": for_each_id, "lane_id": lane_id}


def edge(source: str, target: dict, outcome: str = "success", source_kind: str = "step") -> dict:
    return {
        "source": {"kind": source_kind, "id": source},
        "outcome": {"name": outcome},
        "target": target,
    }


def definition(
    steps: list,
    edges: list,
    *,
    entry: dict,
    groups: list | None = None,
    for_each: list | None = None,
) -> dict:
    return {
        "steps": steps,
        "groups": groups or [],
        "for_each": for_each or [],
        "execution_graph": {"schema_version": 1, "entry": entry, "edges": edges},
    }


def sequence_group(group_id: str, entry: dict) -> dict:
    return {"group_id": group_id, "body": {"kind": "sequence", "entry": entry}}


def lanes_body(lanes: dict[str, str]) -> dict:
    return {
        "kind": "lanes",
        "lanes": [{"lane_id": lane, "entry": ref(entry)} for lane, entry in lanes.items()],
    }


def for_each(for_each_id: str, entry: str, **extra: Any) -> dict:
    return {
        "for_each_id": for_each_id,
        "group_id": None,
        "over": "state.orders",
        "item_name": "order",
        "index_name": "order_index",
        "body": {"kind": "sequence", "entry": ref(entry)},
        **extra,
    }


def codes(issues: list[dict]) -> list[str]:
    return [issue["code"] for issue in issues]


def in_for_each(payload: dict, for_each_id: str, lane_id: str | None = None) -> dict:
    return {**payload, "for_each_id": for_each_id, "lane_id": lane_id}


def collect_step(step_id: str) -> dict:
    return step(
        step_id,
        "collect",
        override={
            "collect": {"target": "state.emails", "source": "state.order.email", "mode": "append"}
        },
    )


def group_with_for_each_and_collect() -> dict:
    """Group → For each → If → Collect | Continue, the composition of §3.2."""
    return definition(
        [
            step("prepare", "set_variable", group_id="g"),
            in_for_each(
                step(
                    "check",
                    "if",
                    override={"if": {"condition": "state.order.status == 'completed'"}},
                ),
                "fe",
            ),
            in_for_each(collect_step("keep"), "fe"),
            in_for_each(step("skip", "continue"), "fe"),
            step("done", "stop_success"),
        ],
        [
            edge("g", ref("done"), source_kind="group"),
            edge("prepare", ref("fe", "for_each")),
            edge("fe", group_exit("g"), source_kind="for_each"),
            edge("check", ref("keep"), "true"),
            edge("check", ref("skip"), "false"),
            edge("keep", for_each_exit("fe")),
        ],
        entry=ref("g", "group"),
        groups=[sequence_group("g", ref("prepare"))],
        for_each=[for_each("fe", "check", group_id="g")],
    )


def test_group_for_each_collect_composition_is_valid() -> None:
    assert validate_workflow_definition(group_with_for_each_and_collect()) == []


def test_root_for_each_is_a_valid_entry() -> None:
    payload = definition(
        [in_for_each(step("work"), "fe"), step("done", "stop_success")],
        [edge("work", for_each_exit("fe")), edge("fe", ref("done"), source_kind="for_each")],
        entry=ref("fe", "for_each"),
        for_each=[for_each("fe", "work")],
    )
    assert validate_workflow_definition(payload) == []


def test_group_has_no_kind_or_loop() -> None:
    payload = group_with_for_each_and_collect()
    payload["groups"][0]["kind"] = "loop"
    payload["groups"][0]["loop"] = {"over": "state.items"}
    found = codes(validate_workflow_definition(payload))
    assert found.count("workflow.group.loop_removed") == 2


def test_inline_for_each_step_is_refused() -> None:
    payload = definition(
        [step("a", "for_each", override={"for_each": {"source_path": "state.x", "steps": []}})],
        [],
        entry=ref("a"),
    )
    assert "workflow.step.kind_unsupported" in codes(validate_workflow_definition(payload))


def test_step_cannot_have_two_owners() -> None:
    payload = group_with_for_each_and_collect()
    payload["steps"][1]["group_id"] = "g"
    assert "workflow.step.owner_conflict" in codes(validate_workflow_definition(payload))


def test_lane_without_owner_is_refused() -> None:
    payload = group_with_for_each_and_collect()
    payload["steps"][4]["lane_id"] = "left"
    assert "workflow.step.lane_without_group" in codes(validate_workflow_definition(payload))


def test_for_each_cannot_live_in_a_lanes_group() -> None:
    payload = definition(
        [step("x", group_id="p", lane_id="left"), in_for_each(step("w"), "fe")],
        [edge("x", group_exit("p", "left")), edge("w", for_each_exit("fe"))],
        entry=ref("p", "group"),
        groups=[{"group_id": "p", "body": lanes_body({"left": "x"})}],
        for_each=[for_each("fe", "w", group_id="p")],
    )
    assert "workflow.for_each.parent_group_invalid" in codes(validate_workflow_definition(payload))


def test_for_each_cannot_hold_containers() -> None:
    payload = group_with_for_each_and_collect()
    payload["for_each"].append(for_each("inner", "keep"))
    payload["for_each"][1]["group_id"] = None
    payload["execution_graph"]["edges"].append(edge("check", ref("inner", "for_each"), "true"))
    found = codes(validate_workflow_definition(payload))
    assert "workflow.for_each.nested_unsupported" in found


@pytest.mark.parametrize("kind", ["collect", "break", "continue"])
def test_loop_controls_only_in_a_sequence_for_each(kind: str) -> None:
    payload = definition(
        [
            step(
                "a",
                kind,
                override={"collect": {"target": "state.x", "source": "state.y"}}
                if kind == "collect"
                else {},
            )
        ],
        [],
        entry=ref("a"),
    )
    assert "workflow.placement.for_each_only" in codes(validate_workflow_definition(payload))


@pytest.mark.parametrize("kind", ["collect", "break", "continue"])
def test_loop_controls_are_refused_in_a_for_each_lane(kind: str) -> None:
    payload = definition(
        [in_for_each(step("a", kind), "fe", "left"), step("done", "stop_success")],
        [edge("fe", ref("done"), source_kind="for_each")],
        entry=ref("fe", "for_each"),
        for_each=[{**for_each("fe", "a"), "body": lanes_body({"left": "a"})}],
    )
    found = codes(validate_workflow_definition(payload))
    assert "workflow.placement.lane_kind_unsupported" in found


def test_collect_directly_in_a_group_is_refused() -> None:
    payload = group_with_for_each_and_collect()
    payload["steps"][0] = collect_step("prepare") | {"group_id": "g"}
    assert "workflow.placement.for_each_only" in codes(validate_workflow_definition(payload))


def test_break_and_continue_have_no_outgoing_edges() -> None:
    payload = group_with_for_each_and_collect()
    payload["execution_graph"]["edges"].append(edge("skip", for_each_exit("fe")))
    assert "workflow.graph.outcome_unsupported" in codes(validate_workflow_definition(payload))


def test_for_each_exit_only_from_its_own_scope() -> None:
    payload = group_with_for_each_and_collect()
    payload["execution_graph"]["edges"][1] = edge("prepare", for_each_exit("fe"))
    found = codes(validate_workflow_definition(payload))
    assert "workflow.graph.for_each_exit_foreign" in found


def test_for_each_needs_source_and_item_name() -> None:
    payload = group_with_for_each_and_collect()
    payload["for_each"][0]["over"] = ""
    payload["for_each"][0]["item_name"] = "bad name"
    found = codes(validate_workflow_definition(payload))
    assert "workflow.for_each.over_missing" in found
    assert "workflow.for_each.item_name_invalid" in found


def test_business_ids_are_unique_across_containers() -> None:
    payload = group_with_for_each_and_collect()
    payload["for_each"][0]["for_each_id"] = "g"
    assert "workflow.node.id_duplicate" in codes(validate_workflow_definition(payload))


def test_stop_inside_a_for_each_body_ends_the_workflow() -> None:
    payload = group_with_for_each_and_collect()
    payload["steps"].append(in_for_each(step("halt", "stop_fail"), "fe"))
    payload["execution_graph"]["edges"][5] = edge("keep", ref("halt"))
    assert validate_workflow_definition(payload) == []
    payload["execution_graph"]["edges"].append(edge("halt", for_each_exit("fe")))
    assert "workflow.graph.outcome_unsupported" in codes(validate_workflow_definition(payload))


def test_policy_source_is_step_then_for_each_then_group() -> None:
    assert effective_error_policy_source(
        step_policy=None, containers=[("for_each", "fe", None), ("group", "g", "route_to_step")]
    ) == ("route_to_step", ("group", "g"))
    assert effective_error_policy_source(
        step_policy=None, containers=[("for_each", "fe", "fail"), ("group", "g", "route_to_step")]
    ) == ("fail", ("for_each", "fe"))
    assert effective_error_policy_source(step_policy="retry", containers=[]) == (
        "retry",
        ("step", None),
    )
    assert effective_error_policy_source(step_policy=None, containers=[]) == ("fail", None)
