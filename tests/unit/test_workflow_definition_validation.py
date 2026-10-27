from __future__ import annotations

from typing import Any

import pytest

from core.application.workflows.workflow_definition_validation import (
    DISABLED_STEP_NEEDS_NEXT_CODE,
    DISABLED_STEP_NEEDS_NEXT_MESSAGE,
    ChildDefinition,
    definition_validation_result,
    effective_error_policy_source,
    scan_child_chain,
    step_can_be_disabled,
    validate_workflow_definition,
    wait_duration_issues,
)


def step(step_id: str, kind: str = "http_request", **extra: Any) -> dict[str, Any]:
    return {"step_id": step_id, "step_kind": kind, "group_id": None, "lane_id": None, **extra}


def ref(node_id: str, kind: str = "step") -> dict[str, str]:
    return {"kind": kind, "id": node_id}


def exit_ref(group_id: str, lane_id: str | None = None) -> dict[str, Any]:
    return {"kind": "group_exit", "group_id": group_id, "lane_id": lane_id}


def edge(source: str, target: dict, outcome: str = "success", **extra: Any) -> dict[str, Any]:
    source_kind = extra.pop("source_kind", "step")
    payload: dict[str, Any] = {
        "source": {"kind": source_kind, "id": source},
        "outcome": {"name": outcome},
        "target": target,
    }
    if "case_id" in extra:
        payload["outcome"]["case_id"] = extra.pop("case_id")
    payload.update(extra)
    return payload


def definition(
    steps: list,
    edges: list,
    *,
    entry: str = "a",
    entry_kind: str = "step",
    groups: list | None = None,
) -> dict:
    return {
        "steps": steps,
        "groups": groups or [],
        "execution_graph": {"schema_version": 1, "entry": ref(entry, entry_kind), "edges": edges},
    }


def sequence_group(group_id: str = "g", entry: str = "l1", **extra: Any) -> dict[str, Any]:
    """A run-once Group with a sequence body (ADR 0038)."""
    return {"group_id": group_id, "body": {"kind": "sequence", "entry": ref(entry)}, **extra}


def lanes_body(lanes: dict[str, str]) -> dict[str, Any]:
    return {
        "kind": "lanes",
        "lanes": [{"lane_id": lane, "entry": ref(entry)} for lane, entry in lanes.items()],
        "concurrency_limit": 2,
    }


def lanes_group(group_id: str = "p", lanes: dict[str, str] | None = None, **extra: Any) -> dict:
    return {
        "group_id": group_id,
        "body": lanes_body(lanes or {"left": "x1", "right": "y1"}),
        **extra,
    }


def in_group(step_payload: dict, group_id: str, lane_id: str | None = None) -> dict:
    return {**step_payload, "group_id": group_id, "lane_id": lane_id}


def codes(issues: list[dict]) -> list[str]:
    return [issue["code"] for issue in issues]


def linear_root() -> dict:
    return definition(
        [step("a"), step("b", "transform"), step("end", "stop_success")],
        [edge("a", ref("b")), edge("b", ref("end"))],
    )


# --- T01 / T02: root graph, entry, ports ---------------------------------------


def test_root_sequence_without_groups_is_valid() -> None:
    issues = validate_workflow_definition(linear_root())
    assert issues == []
    assert definition_validation_result(issues) == {"valid": True, "errors": [], "warnings": []}


def test_empty_definition_reports_missing_graph() -> None:
    assert "workflow.graph.missing" in codes(
        validate_workflow_definition({"steps": [], "groups": []})
    )


def test_missing_continuation_is_an_issue_not_success() -> None:
    payload = definition([step("a"), step("b")], [edge("a", ref("b"))])
    issues = validate_workflow_definition(payload)
    assert codes(issues) == ["workflow.graph.outcome_missing"]
    assert issues[0]["path"] == "steps.b"


def test_disconnected_step_is_unreachable() -> None:
    payload = linear_root()
    payload["steps"].append(step("orphan", "stop_fail"))
    assert codes(validate_workflow_definition(payload)) == ["workflow.graph.unreachable"]


def test_unknown_target_and_source() -> None:
    payload = linear_root()
    payload["execution_graph"]["edges"][1] = edge("b", ref("nope"))
    payload["execution_graph"]["edges"].append(edge("ghost", ref("end")))
    found = codes(validate_workflow_definition(payload))
    assert "workflow.graph.target_unknown" in found
    assert "workflow.graph.source_unknown" in found


def test_entry_must_be_a_root_node() -> None:
    payload = definition(
        [step("a"), in_group(step("l1"), "g"), step("end", "stop_success")],
        [
            edge("a", ref("g", "group")),
            edge("l1", exit_ref("g")),
            edge("g", ref("end"), source_kind="group"),
        ],
        entry="l1",
        groups=[sequence_group()],
    )
    assert "workflow.graph.entry_invalid" in codes(validate_workflow_definition(payload))


def test_if_needs_both_ports_and_one_target_per_outcome() -> None:
    payload = definition(
        [
            step("a", "if", override={"if": {"condition": "state.ok == true"}}),
            step("b", "stop_success"),
        ],
        [edge("a", ref("b"), "true"), edge("a", ref("b"), "true")],
    )
    found = codes(validate_workflow_definition(payload))
    assert "workflow.graph.outcome_duplicate" in found
    assert found.count("workflow.graph.outcome_missing") == 1


def test_switch_routes_every_case_and_default_by_case_id() -> None:
    switch = step(
        "a",
        "switch",
        override={
            "switch": {
                "selector": "state.kind",
                "cases": [
                    {"case_id": "c_red", "when": "red"},
                    {"case_id": "c_blue", "when": "blue"},
                ],
            }
        },
    )
    stops = [step("r", "stop_success"), step("b", "stop_success"), step("d", "stop_fail")]
    edges = [
        edge("a", ref("r"), "case", case_id="c_red"),
        edge("a", ref("b"), "case", case_id="c_blue"),
        edge("a", ref("d"), "default"),
    ]
    assert validate_workflow_definition(definition([switch, *stops], edges)) == []

    missing = definition([switch, *stops[:1], stops[2]], [edges[0], edges[2]])
    assert codes(validate_workflow_definition(missing)) == ["workflow.graph.outcome_missing"]

    unknown = definition([switch, *stops], [*edges, edge("a", ref("b"), "case", case_id="c_green")])
    assert "workflow.graph.outcome_unsupported" in codes(validate_workflow_definition(unknown))


def test_switch_config_rejects_targets_and_needs_stable_case_ids() -> None:
    switch = step(
        "a",
        "switch",
        override={
            "switch": {
                "selector": "s",
                "cases": [{"when": "x", "target_phase": "p"}],
                "default": "p",
            }
        },
    )
    found = codes(validate_workflow_definition(definition([switch], [])))
    assert "workflow.switch.case_id_missing" in found
    assert "workflow.switch.case_key_unknown" in found
    assert "workflow.step_config_key_unknown" in found


def test_if_config_has_no_branch_targets() -> None:
    payload = definition(
        [
            step("a", "if", override={"if": {"condition": "c", "on_true": "p2"}}),
            step("b", "stop_success"),
        ],
        [edge("a", ref("b"), "true"), edge("a", ref("b"), "false")],
    )
    assert codes(validate_workflow_definition(payload)) == ["workflow.step_config_key_unknown"]


def test_stop_has_no_outgoing_edges() -> None:
    payload = linear_root()
    payload["execution_graph"]["edges"].append(edge("end", ref("a"), rework=True))
    assert "workflow.graph.outcome_unsupported" in codes(validate_workflow_definition(payload))


# --- disabled steps -------------------------------------------------------------


def test_a_disabled_step_with_one_next_outcome_is_valid() -> None:
    payload = linear_root()
    payload["steps"][1]["disabled"] = True
    assert validate_workflow_definition(payload) == []


def test_a_disabled_if_is_refused_with_a_clear_message() -> None:
    payload = definition(
        [
            step("a", "if", override={"if": {"condition": "state.ok == true"}}, disabled=True),
            step("b", "stop_success"),
        ],
        [edge("a", ref("b"), "true"), edge("a", ref("b"), "false")],
    )
    issues = validate_workflow_definition(payload)
    assert issues == [
        {
            "code": DISABLED_STEP_NEEDS_NEXT_CODE,
            "message": DISABLED_STEP_NEEDS_NEXT_MESSAGE,
            "path": "steps.a.disabled",
            "severity": "error",
        }
    ]
    assert DISABLED_STEP_NEEDS_NEXT_MESSAGE == "Only a step with one Next outcome can be disabled."


@pytest.mark.parametrize("kind", ["stop_success", "stop_fail"])
def test_a_disabled_stop_is_refused(kind: str) -> None:
    payload = linear_root()
    payload["steps"][2] = step("end", kind, disabled=True)
    assert codes(validate_workflow_definition(payload)) == [DISABLED_STEP_NEEDS_NEXT_CODE]


def test_a_disabled_flag_must_be_a_boolean() -> None:
    payload = linear_root()
    payload["steps"][1]["disabled"] = "yes"
    issues = validate_workflow_definition(payload)
    assert codes(issues) == ["workflow.step.disabled_invalid"]
    assert issues[0]["path"] == "steps.b.disabled"


def test_an_enabled_flag_is_not_an_issue_on_any_kind() -> None:
    payload = linear_root()
    payload["steps"][2]["disabled"] = False
    assert validate_workflow_definition(payload) == []


@pytest.mark.parametrize(
    ("kind", "config", "skippable"),
    [
        ("agent", {}, True),
        ("transform", {}, True),
        ("http_request", {}, True),
        ("set_variable", {}, True),
        ("wait", {}, True),
        ("invoke_workflow", {}, True),
        ("collect", {}, True),
        ("if", {}, False),
        ("switch", {"cases": [{"case_id": "c1", "when": "x"}]}, False),
        ("approval", {}, False),
        ("stop_success", {}, False),
        ("stop_fail", {}, False),
        ("break", {}, False),
        ("continue", {}, False),
    ],
)
def test_only_a_kind_with_one_next_outcome_can_be_disabled(
    kind: str, config: dict, skippable: bool
) -> None:
    assert step_can_be_disabled(kind, config) is skippable


# --- T03: exclusive branches converge without a join ---------------------------


def test_exclusive_branches_converge_on_an_ordinary_step() -> None:
    payload = definition(
        [
            step("a", "if", override={"if": {"condition": "c"}}),
            step("b"),
            step("c"),
            step("d"),
            step("end", "stop_success"),
        ],
        [
            edge("a", ref("b"), "true"),
            edge("a", ref("c"), "false"),
            edge("b", ref("d")),
            edge("c", ref("d")),
            edge("d", ref("end")),
        ],
    )
    assert validate_workflow_definition(payload) == []


# --- T04: groups, group_exit, Stop placement ------------------------------------


def serial_loop(*body: dict, body_edges: list, extra_root: list | None = None) -> dict:
    return definition(
        [step("a"), *body, step("end", "stop_success"), *(extra_root or [])],
        [edge("a", ref("g", "group")), *body_edges, edge("g", ref("end"), source_kind="group")],
        groups=[sequence_group()],
    )


def test_serial_loop_body_ends_at_group_exit() -> None:
    payload = serial_loop(
        in_group(step("l1"), "g"),
        in_group(step("l2", "transform"), "g"),
        body_edges=[edge("l1", ref("l2")), edge("l2", exit_ref("g"))],
    )
    assert validate_workflow_definition(payload) == []


def test_stop_inside_serial_loop_is_allowed() -> None:
    payload = serial_loop(
        in_group(step("l1", "if", override={"if": {"condition": "c"}}), "g"),
        in_group(step("halt", "stop_fail"), "g"),
        body_edges=[edge("l1", ref("halt"), "true"), edge("l1", exit_ref("g"), "false")],
    )
    assert validate_workflow_definition(payload) == []


def test_body_cannot_jump_to_root_and_root_cannot_enter_a_body_step() -> None:
    payload = serial_loop(
        in_group(step("l1"), "g"),
        body_edges=[edge("l1", ref("end"))],
    )
    assert "workflow.graph.cross_scope" in codes(validate_workflow_definition(payload))

    payload = serial_loop(
        in_group(step("l1"), "g"),
        body_edges=[edge("l1", exit_ref("g"))],
    )
    payload["execution_graph"]["edges"][0] = edge("a", ref("l1"))
    assert "workflow.graph.cross_scope" in codes(validate_workflow_definition(payload))


def test_group_exit_only_from_its_own_scope() -> None:
    payload = serial_loop(in_group(step("l1"), "g"), body_edges=[edge("l1", exit_ref("g"))])
    payload["execution_graph"]["edges"][0] = edge("a", exit_ref("g"))
    assert "workflow.graph.group_exit_foreign" in codes(validate_workflow_definition(payload))


def parallel(lane_steps: list[dict], lane_edges: list[dict], groups: list | None = None) -> dict:
    return definition(
        [step("a"), *lane_steps, step("end", "stop_success")],
        [edge("a", ref("p", "group")), *lane_edges, edge("p", ref("end"), source_kind="group")],
        groups=groups or [lanes_group()],
    )


def test_parallel_lanes_end_at_their_lane_exit() -> None:
    payload = parallel(
        [in_group(step("x1"), "p", "left"), in_group(step("y1", "agent"), "p", "right")],
        [edge("x1", exit_ref("p", "left")), edge("y1", exit_ref("p", "right"))],
    )
    assert validate_workflow_definition(payload) == []


def test_lane_cannot_exit_through_a_sibling_lane() -> None:
    payload = parallel(
        [in_group(step("x1"), "p", "left"), in_group(step("y1"), "p", "right")],
        [edge("x1", exit_ref("p", "right")), edge("y1", exit_ref("p", "right"))],
    )
    assert "workflow.graph.group_exit_foreign" in codes(validate_workflow_definition(payload))


@pytest.mark.parametrize("kind", ["stop_success", "stop_fail", "approval", "wait", "if", "switch"])
def test_lane_rejects_control_and_waiting_kinds(kind: str) -> None:
    payload = parallel(
        [in_group(step("x1", kind), "p", "left"), in_group(step("y1"), "p", "right")],
        [edge("y1", exit_ref("p", "right"))],
    )
    assert "workflow.placement.lane_kind_unsupported" in codes(
        validate_workflow_definition(payload)
    )


def test_for_each_with_lanes_body_runs_lanes_inside_each_item() -> None:
    item = {
        "for_each_id": "g",
        "over": "state.items",
        "item_name": "item",
        "body": lanes_body({"left": "x1", "right": "y1"}),
    }
    payload = definition(
        [
            step("a"),
            {**step("x1"), "for_each_id": "g", "lane_id": "left"},
            {**step("y1"), "for_each_id": "g", "lane_id": "right"},
            step("end", "stop_success"),
        ],
        [
            edge("a", ref("g", "for_each")),
            edge("x1", {"kind": "for_each_exit", "for_each_id": "g", "lane_id": "left"}),
            edge("y1", {"kind": "for_each_exit", "for_each_id": "g", "lane_id": "right"}),
            edge("g", ref("end"), source_kind="for_each"),
        ],
    )
    payload["for_each"] = [item]
    assert validate_workflow_definition(payload) == []
    payload["steps"][1] = {
        **step("x1", "wait", override={"wait": {"duration_ms": 1}}),
        "for_each_id": "g",
        "lane_id": "left",
    }
    assert "workflow.placement.lane_kind_unsupported" in codes(
        validate_workflow_definition(payload)
    )


def test_group_cannot_enter_another_group() -> None:
    payload = serial_loop(in_group(step("l1"), "g"), body_edges=[edge("l1", ref("g2", "group"))])
    payload["groups"].append(sequence_group("g2", "m1"))
    payload["steps"].append(in_group(step("m1"), "g2"))
    found = codes(validate_workflow_definition(payload))
    assert "workflow.group.nested_unsupported" in found


def test_empty_group_and_empty_lane_are_refused() -> None:
    group = {"group_id": "g", "body": {"kind": "sequence"}}
    payload = definition([step("a")], [edge("a", ref("g", "group"))], groups=[group])
    assert "workflow.group.body_entry_missing" in codes(validate_workflow_definition(payload))

    group = {"group_id": "p", "body": {"kind": "lanes", "lanes": []}}
    payload = definition([step("a")], [edge("a", ref("p", "group"))], groups=[group])
    assert "workflow.group.lanes_missing" in codes(validate_workflow_definition(payload))


# --- T29: membership, legacy fields, group error outcome ------------------------


def test_step_must_belong_to_a_declared_lane() -> None:
    payload = parallel(
        [in_group(step("x1"), "p", "middle"), in_group(step("y1"), "p", "right")],
        [edge("y1", exit_ref("p", "right"))],
    )
    found = codes(validate_workflow_definition(payload))
    assert "workflow.step.lane_unknown" in found
    assert "workflow.group.entry_foreign" in found


def test_lane_entry_must_be_in_that_lane() -> None:
    payload = parallel(
        [in_group(step("x1"), "p", "right"), in_group(step("y1"), "p", "right")],
        [edge("x1", ref("y1")), edge("y1", exit_ref("p", "right"))],
    )
    assert "workflow.group.entry_foreign" in codes(validate_workflow_definition(payload))


def test_sequence_body_steps_have_no_lane() -> None:
    payload = serial_loop(in_group(step("l1"), "g", "left"), body_edges=[edge("l1", exit_ref("g"))])
    assert "workflow.step.lane_unexpected" in codes(validate_workflow_definition(payload))


def test_phase_era_fields_are_refused() -> None:
    payload = linear_root()
    payload["steps"][0]["parallel_group"] = "lane-a"
    payload["steps"][1]["phase_id"] = "p1"
    payload["groups"] = [{**sequence_group(), "gate": {"type": "human"}, "phase_kind": "action"}]
    found = codes(validate_workflow_definition(payload))
    assert found.count("workflow.definition.legacy_key") == 4


def test_group_error_edge_only_with_route_to_step_and_then_required() -> None:
    payload = serial_loop(in_group(step("l1"), "g"), body_edges=[edge("l1", exit_ref("g"))])
    payload["execution_graph"]["edges"].append(edge("g", ref("end"), "error", source_kind="group"))
    assert "workflow.graph.outcome_unsupported" in codes(validate_workflow_definition(payload))

    payload["groups"][0]["error_policy"] = {"on_error": "route_to_step"}
    assert validate_workflow_definition(payload) == []

    payload["execution_graph"]["edges"].pop()
    assert codes(validate_workflow_definition(payload)) == ["workflow.graph.outcome_missing"]


def test_inherited_route_gives_the_step_no_local_error_edge() -> None:
    payload = serial_loop(in_group(step("l1"), "g"), body_edges=[edge("l1", exit_ref("g"))])
    payload["groups"][0]["error_policy"] = {"on_error": "route_to_step"}
    payload["execution_graph"]["edges"] += [
        edge("g", ref("end"), "error", source_kind="group"),
        edge("l1", exit_ref("g"), "error"),
    ]
    assert "workflow.graph.outcome_unsupported" in codes(validate_workflow_definition(payload))


def test_step_route_to_step_needs_its_own_error_edge() -> None:
    payload = linear_root()
    payload["steps"][0]["error_policy"] = {"on_error": "route_to_step"}
    assert codes(validate_workflow_definition(payload)) == ["workflow.graph.outcome_missing"]
    payload["steps"].append(step("fail", "stop_fail"))
    payload["execution_graph"]["edges"].append(edge("a", ref("fail"), "error"))
    assert validate_workflow_definition(payload) == []


def test_error_policy_route_to_phase_and_second_target_are_refused() -> None:
    payload = linear_root()
    payload["steps"][0]["error_policy"] = {"on_error": "route_to_phase", "target_phase": "p2"}
    found = codes(validate_workflow_definition(payload))
    assert "workflow.error_policy.on_error_invalid" in found
    assert "workflow.error_policy.key_unknown" in found


def test_effective_error_policy_prefers_step_then_group_then_fail() -> None:
    group = [("group", "g", "continue")]
    assert effective_error_policy_source(step_policy="retry", containers=group)[0] == "retry"
    assert effective_error_policy_source(
        step_policy=None, containers=[("group", "g", "route_to_step")]
    ) == ("route_to_step", ("group", "g"))
    assert effective_error_policy_source(step_policy=None, containers=[]) == ("fail", None)


# --- rework -----------------------------------------------------------------------


def review_loop(*, rework: bool, outcome: str = "false") -> dict:
    return definition(
        [
            step("draft", "agent"),
            step("check", "if", override={"if": {"condition": "c"}}),
            step("end", "stop_success"),
        ],
        [
            edge("draft", ref("check")),
            edge("check", ref("end"), "true" if outcome == "false" else "false"),
            edge("check", ref("draft"), outcome, rework=rework),
        ],
        entry="draft",
    )


def test_rework_back_edge_from_a_branch_is_allowed() -> None:
    assert validate_workflow_definition(review_loop(rework=True)) == []


def test_cycle_without_rework_is_refused() -> None:
    assert codes(validate_workflow_definition(review_loop(rework=False))) == [
        "workflow.graph.cycle_without_rework"
    ]


def test_success_rework_cycle_is_refused() -> None:
    payload = definition(
        [step("a"), step("b")],
        [edge("a", ref("b")), edge("b", ref("a"), rework=True)],
    )
    assert "workflow.graph.rework_source_invalid" in codes(validate_workflow_definition(payload))


def test_rework_from_rejected_approval_with_edge_limit() -> None:
    payload = definition(
        [
            step("draft", "agent"),
            step("review", "approval", override={"approval": {"message": "Check the draft"}}),
            step("end", "stop_success"),
        ],
        [
            edge("draft", ref("review")),
            edge("review", ref("end"), "approved"),
            edge("review", ref("draft"), "rejected", rework=True, rework_limit=3),
        ],
        entry="draft",
    )
    assert validate_workflow_definition(payload) == []


def test_root_rework_may_reenter_a_group_through_its_node() -> None:
    payload = serial_loop(
        in_group(step("l1"), "g"),
        body_edges=[edge("l1", exit_ref("g"))],
    )
    payload["steps"].append(step("check", "if", override={"if": {"condition": "c"}}))
    edges = payload["execution_graph"]["edges"]
    edges[-1] = edge("g", ref("check"), source_kind="group")
    edges += [
        edge("check", ref("end"), "true"),
        edge("check", ref("g", "group"), "false", rework=True),
    ]
    assert validate_workflow_definition(payload) == []


def test_lane_has_no_rework() -> None:
    payload = parallel(
        [
            in_group(step("x1", error_policy={"on_error": "route_to_step"}), "p", "left"),
            in_group(step("x2"), "p", "left"),
            in_group(step("y1"), "p", "right"),
        ],
        [
            edge("x1", ref("x2")),
            edge("x2", exit_ref("p", "left")),
            edge("x1", ref("x2"), "error", rework=True),
            edge("y1", exit_ref("p", "right")),
        ],
    )
    assert "workflow.graph.rework_in_lane" in codes(validate_workflow_definition(payload))


def test_two_hundred_linear_steps_validate() -> None:
    steps = [step(f"s{i}") for i in range(200)] + [step("end", "stop_success")]
    edges = [edge(f"s{i}", ref(f"s{i + 1}")) for i in range(199)] + [edge("s199", ref("end"))]
    assert validate_workflow_definition(definition(steps, edges, entry="s0")) == []


# --- T31: boundary contracts, Approval and Wait config ---------------------------


def test_boundary_exit_on_names_outcomes_leaving_the_section() -> None:
    payload = review_loop(rework=True)
    payload["steps"][1]["boundary_contract"] = {
        "entry": [{"key": "draft", "source": "state.draft"}],
        "exit": ["draft"],
        "exit_on": [{"name": "true"}],
    }
    assert validate_workflow_definition(payload) == []

    payload["steps"][1]["boundary_contract"]["exit_on"] = [{"name": "error"}, {"name": "approved"}]
    assert (
        codes(validate_workflow_definition(payload)).count("workflow.boundary.exit_on_unsupported")
        == 2
    )

    payload["steps"][1]["boundary_contract"] = {"exit_on": [{"name": "true"}]}
    assert "workflow.boundary.exit_on_without_exit" in codes(validate_workflow_definition(payload))


def approval_root(config: dict) -> dict:
    return definition(
        [
            step("a", "approval", override={"approval": config}),
            step("ok", "stop_success"),
            step("no", "stop_fail"),
        ],
        [edge("a", ref("ok"), "approved"), edge("a", ref("no"), "rejected")],
    )


def test_approval_needs_both_outcomes_and_only_known_config() -> None:
    assert validate_workflow_definition(approval_root({"show_outputs": ["state.summary"]})) == []
    found = codes(
        validate_workflow_definition(
            approval_root({"timeout_hours": 4, "on_timeout": "approve", "condition": "x"})
        )
    )
    assert found.count("workflow.step_config_key_unknown") == 3
    missing = approval_root({})
    missing["execution_graph"]["edges"].pop()
    missing["steps"].pop()
    assert codes(validate_workflow_definition(missing)) == ["workflow.graph.outcome_missing"]


def wait_root(config: dict) -> dict:
    return definition(
        [step("a", "wait", override={"wait": config}), step("end", "stop_success")],
        [edge("a", ref("end"))],
    )


@pytest.mark.parametrize("value", [0, 5000, 31_536_000_000])
def test_wait_literal_durations(value: int) -> None:
    assert validate_workflow_definition(wait_root({"duration_ms": value})) == []


@pytest.mark.parametrize("value", [-1, 31_536_000_001, 1.5, True, None, "5000"])
def test_wait_invalid_durations(value: Any) -> None:
    assert codes(wait_duration_issues(value, "d")) == ["workflow.wait.duration_invalid"]
    assert "workflow.wait.duration_invalid" in codes(
        validate_workflow_definition(wait_root({"duration_ms": value}))
    )


def test_wait_needs_exactly_one_duration_source() -> None:
    assert (
        validate_workflow_definition(wait_root({"input_mapping": {"duration_ms": "state.delay"}}))
        == []
    )
    assert codes(validate_workflow_definition(wait_root({}))) == ["workflow.wait.duration_source"]
    both = wait_root({"duration_ms": 1, "input_mapping": {"duration_ms": "state.delay"}})
    assert codes(validate_workflow_definition(both)) == ["workflow.wait.duration_source"]
    extra = wait_root({"input_mapping": {"duration_ms": "state.d", "note": "x"}})
    assert codes(validate_workflow_definition(extra)) == ["workflow.wait.input_unknown"]


def test_approval_and_wait_inside_serial_loop_body_are_allowed() -> None:
    payload = serial_loop(
        in_group(step("l1", "approval"), "g"),
        in_group(step("l2", "wait", override={"wait": {"duration_ms": 1000}}), "g"),
        body_edges=[
            edge("l1", ref("l2"), "approved"),
            edge("l1", exit_ref("g"), "rejected"),
            edge("l2", exit_ref("g")),
        ],
    )
    assert validate_workflow_definition(payload) == []


# --- T17: waits hidden in a child chain under a lane -----------------------------


def child_with(*kinds: str, invokes: str | None = None) -> dict:
    steps = [step(f"c{i}", kind) for i, kind in enumerate(kinds)]
    if invokes:
        steps.append(step("call", "invoke_workflow", workflow_id=invokes))
    return {"steps": steps}


def loader(children: dict[str, dict], revisions: dict[str, int] | None = None):
    def load(workflow_id: str) -> ChildDefinition | None:
        if workflow_id not in children:
            return None
        return ChildDefinition(
            workflow_id, (revisions or {}).get(workflow_id, 1), children[workflow_id]
        )

    return load


def lane_invoking(child_id: str) -> dict:
    return parallel(
        [
            in_group(step("x1", "invoke_workflow", workflow_id=child_id), "p", "left"),
            in_group(step("y1"), "p", "right"),
        ],
        [edge("x1", exit_ref("p", "left")), edge("y1", exit_ref("p", "right"))],
    )


def test_lane_child_chain_with_a_grandchild_wait_is_refused() -> None:
    load = loader(
        {"child": child_with("http_request", invokes="grand"), "grand": child_with("wait")}
    )
    issues = validate_workflow_definition(lane_invoking("child"), load_child=load)
    assert codes(issues) == ["workflow.placement.lane_child_waits"]
    assert "child → grand" in issues[0]["message"]


def test_lane_child_chain_without_waits_is_allowed() -> None:
    load = loader(
        {"child": child_with("http_request", invokes="grand"), "grand": child_with("transform")}
    )
    assert validate_workflow_definition(lane_invoking("child"), load_child=load) == []


def test_serial_child_may_wait() -> None:
    payload = definition(
        [step("a", "invoke_workflow", workflow_id="child"), step("end", "stop_success")],
        [edge("a", ref("end"))],
    )
    load = loader({"child": child_with("approval")})
    assert validate_workflow_definition(payload, load_child=load) == []


def test_child_chain_keeps_recursion_and_depth_guards() -> None:
    recursive = loader({"child": child_with(invokes="child")})
    assert codes(validate_workflow_definition(lane_invoking("child"), load_child=recursive)) == [
        "workflow.child.recursion"
    ]
    deep = loader({f"w{i}": child_with(invokes=f"w{i + 1}") for i in range(10)})
    found = codes(
        validate_workflow_definition(lane_invoking("w0"), load_child=deep, max_invoke_depth=3)
    )
    assert found == ["workflow.child.depth_exceeded"]
    missing = loader({})
    assert codes(validate_workflow_definition(lane_invoking("child"), load_child=missing)) == [
        "workflow.child.unavailable"
    ]


def test_child_scan_records_the_revision_it_judged() -> None:
    load = loader(
        {"child": child_with(invokes="grand"), "grand": child_with("wait")},
        {"child": 7, "grand": 3},
    )
    scan = scan_child_chain("child", load, max_depth=5)
    assert scan.revisions == {"child": 7, "grand": 3}
    assert scan.waits[0]["workflow_id"] == "grand"
    assert scan.waits[0]["revision"] == 3
    assert scan.waits[0]["chain"] == ["child", "grand"]


@pytest.mark.parametrize(
    "kind,config,outcomes",
    [
        ("if", {"condition": "state.ok == true"}, ["true", "false"]),
        (
            "switch",
            {"selector": "state.kind", "cases": [{"case_id": "one", "when": "x"}]},
            ["case", "default"],
        ),
        ("approval", {}, ["approved", "rejected"]),
    ],
)
@pytest.mark.parametrize("policy", ["continue", "await_approval"])
@pytest.mark.parametrize("inherited", [False, True])
def test_branch_step_rejects_policy_requiring_a_success_outcome(
    kind, config, outcomes, policy, inherited
):
    branch = step("a", kind, override={kind: config})
    groups = []
    edges = [
        edge("a", ref("end"), outcome, **({"case_id": "one"} if outcome == "case" else {}))
        for outcome in outcomes
    ]
    entry, entry_kind = "a", "step"
    if inherited:
        branch = in_group(branch, "g")
        groups = [sequence_group(entry="a", error_policy={"on_error": policy})]
        edges = [
            edge("a", exit_ref("g"), outcome, **({"case_id": "one"} if outcome == "case" else {}))
            for outcome in outcomes
        ]
        edges.append(edge("g", ref("end"), source_kind="group"))
        entry, entry_kind = "g", "group"
    else:
        branch["error_policy"] = {"on_error": policy}
    payload = definition(
        [branch, step("end", "stop_success")],
        edges,
        entry=entry,
        entry_kind=entry_kind,
        groups=groups,
    )
    issues = validate_workflow_definition(payload)
    assert "workflow.error_policy.success_outcome_required" in codes(issues)
    branch["error_policy"] = {"on_error": "fail"}
    assert validate_workflow_definition(payload) == []
