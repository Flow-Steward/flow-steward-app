"""Retained outputs need definite typed state evidence, not moved history."""

import copy

import pytest

from core.application.workflows.workflow_group_extraction_bindings import analyze_group_bindings
from tests.unit.test_workflow_definition_service import edge, ref, step
from tests.unit.test_workflow_group_extraction_structure import source_workflow


def retained_source():
    workflow = source_workflow()
    workflow["steps"][0].update(
        step_kind="set_variable",
        override={"set_variable": {"assignments": {"items": [{"name": "one"}, {"name": "two"}]}}},
    )
    workflow["steps"][1].update(
        step_kind="transform",
        override={
            "transform": {"input": {"source": "state.record"}, "result_target": "normalized"}
        },
    )
    workflow["settings"]["callable"] = {
        "enabled": True,
        "output_mode": "variables",
        "expected_outputs": [{"name": "items", "type": "array", "required": True}],
    }
    rows = {
        "prepare": [{"name": "items", "type": "array", "source": "set_variable"}],
        "fetch": [{"name": "normalized", "type": "object", "source": "transform"}],
        "done": [],
    }
    return workflow, rows


def retained_plan(workflow, rows):
    return analyze_group_bindings(
        workflow,
        "process",
        output_rows=rows,
        retained_output_rows=rows,
        available_types={"fetch": {"state.items": "array"}},
    )


def test_retained_definite_parent_output_is_not_a_moved_history_dependency():
    workflow, rows = retained_source()
    original = copy.deepcopy(workflow)
    plan = retained_plan(workflow, rows)
    assert plan.issues == []
    assert workflow == original
    assert plan.input_mapping == {"source_items": "state.items"}
    assert plan.output_mapping == {"normalized": "state.normalized"}
    assert plan.interface["expected_outputs"] == [
        {"name": "normalized", "type": "object", "required": False}
    ]


@pytest.mark.parametrize(
    "variant",
    [
        "optional",
        "missing",
        "mismatch",
        "branch",
        "error",
        "recovery",
        "conflict",
        "nested",
        "other_group",
        "loop_local",
    ],
)
def test_unproven_retained_parent_output_still_refuses(variant):
    workflow, rows = retained_source()
    prepare = workflow["steps"][0]
    graph = workflow["settings"]["execution_graph"]
    if variant == "optional":
        prepare["optional"] = True
    elif variant == "missing":
        rows["prepare"] = []
    elif variant == "mismatch":
        rows["prepare"][0]["type"] = "string"
    elif variant == "branch":
        workflow["steps"].insert(0, step("choose", "if"))
        graph["entry"] = ref("choose")
        graph["edges"].extend(
            [
                edge("choose", ref("prepare"), "true"),
                edge("choose", ref("process", "group"), "false"),
            ]
        )
    elif variant == "error":
        prepare["error_policy"] = {"on_error": "route_to_step"}
        graph["edges"].append(edge("prepare", ref("process", "group"), "error"))
    elif variant == "recovery":
        prepare["error_policy"] = {"on_error": "continue"}
    elif variant == "conflict":
        rows["done"] = [{"name": "items", "type": "number"}]
    elif variant == "nested":
        rows["prepare"].append({"name": "items.name", "type": "string"})
    elif variant == "other_group":
        prepare["group_id"] = "earlier"
    elif variant == "loop_local":
        workflow["for_each"][0]["item_name"] = "items"
    plan = retained_plan(workflow, rows)
    assert any(issue["code"] == "parent_output_boundary" for issue in plan.issues)


def test_successful_path_bypassing_retained_producer_does_not_prove_output():
    workflow, rows = retained_source()
    workflow["steps"].insert(0, step("choose", "if"))
    graph = workflow["settings"]["execution_graph"]
    graph["entry"] = ref("choose")
    graph["edges"].extend(
        [edge("choose", ref("prepare"), "true"), edge("choose", ref("done"), "false")]
    )
    assert any(
        issue["code"] == "parent_output_boundary" for issue in retained_plan(workflow, rows).issues
    )


def test_moved_output_keeps_existing_transfer_mapping():
    workflow, rows = retained_source()
    workflow["settings"]["callable"]["expected_outputs"] = [
        {"name": "normalized", "type": "object", "required": True}
    ]
    plan = retained_plan(workflow, rows)
    assert plan.issues == []
    assert plan.output_mapping == {"normalized": "state.normalized"}


@pytest.mark.parametrize(
    "target",
    [
        "items[0]",
        "outputs.items",
        "workflow_data.items",
        "workflow_data.outputs.items",
        "outputs",
        "workflow_data",
        "workflow_data.outputs",
        "state.outputs.items",
    ],
)
def test_retained_output_alias_or_whole_object_write_remains_unproven(target):
    workflow, rows = retained_source()
    rows["done"] = [{"name": target, "type": "object"}]
    assert any(
        issue["code"] == "parent_output_boundary" for issue in retained_plan(workflow, rows).issues
    )


def test_unrelated_output_name_prefix_is_not_a_competing_write():
    workflow, rows = retained_source()
    rows["done"] = [{"name": "items_backup", "type": "array"}]
    assert retained_plan(workflow, rows).issues == []


def test_second_success_terminal_bypassing_retained_producer_refuses():
    workflow, rows = retained_source()
    workflow["steps"].extend([step("choose", "if"), step("early_done", "stop_success")])
    graph = workflow["settings"]["execution_graph"]
    graph["entry"] = ref("choose")
    graph["edges"].extend(
        [edge("choose", ref("prepare"), "true"), edge("choose", ref("early_done"), "false")]
    )
    assert any(
        issue["code"] == "parent_output_boundary" for issue in retained_plan(workflow, rows).issues
    )


def test_rework_cycle_cannot_invent_a_write_on_first_bypass_path():
    workflow, rows = retained_source()
    workflow["steps"].append(step("choose", "if"))
    graph = workflow["settings"]["execution_graph"]
    graph["entry"] = ref("choose")
    graph["edges"].extend(
        [
            edge("choose", ref("prepare"), "true"),
            edge("choose", ref("process", "group"), "false"),
            {
                "source": ref("process", "group"),
                "outcome": {"name": "again"},
                "target": ref("choose"),
                "rework": True,
            },
        ]
    )
    assert any(
        issue["code"] == "parent_output_boundary" for issue in retained_plan(workflow, rows).issues
    )


def test_disconnected_cycle_does_not_remove_reachable_retained_proof():
    workflow, rows = retained_source()
    workflow["steps"].extend([step("cycle_a", "if"), step("cycle_b", "if")])
    workflow["settings"]["execution_graph"]["edges"].extend(
        [edge("cycle_a", ref("cycle_b")), edge("cycle_b", ref("cycle_a"), rework=True)]
    )
    assert retained_plan(workflow, rows).issues == []


def test_stop_success_is_a_sink_even_with_malformed_outgoing_edge():
    workflow, rows = retained_source()
    workflow["steps"].append(step("unreached_done", "stop_success"))
    workflow["settings"]["execution_graph"]["edges"].append(edge("done", ref("unreached_done")))
    assert retained_plan(workflow, rows).issues == []


@pytest.mark.parametrize("before_producer", [True, False])
def test_success_stop_in_another_group_requires_retained_state_before_its_entry(before_producer):
    workflow, rows = retained_source()
    workflow["groups"].append(
        {
            "group_id": "early",
            "body": {"kind": "sequence", "entry": ref("early_stop")},
        }
    )
    workflow["steps"].append(step("early_stop", "stop_success", group_id="early"))
    graph = workflow["settings"]["execution_graph"]
    if before_producer:
        workflow["steps"].append(step("choose", "if"))
        graph["entry"] = ref("choose")
        graph["edges"].extend(
            [edge("choose", ref("prepare"), "true"), edge("choose", ref("early", "group"), "false")]
        )
    else:
        graph["edges"].append(edge("prepare", ref("early", "group"), "alternate"))
    issues = retained_plan(workflow, rows).issues
    assert any(i["code"] == "parent_output_boundary" for i in issues) is before_producer


def test_canonical_text_aliases_match_a_retained_string_contract():
    workflow, rows = retained_source()
    workflow["settings"]["callable"]["expected_outputs"] = [
        {"name": "label", "type": "paragraph", "required": True}
    ]
    workflow["steps"][0]["override"]["set_variable"]["assignments"]["label"] = "sample"
    rows["prepare"].append({"name": "label", "type": "text", "source": "set_variable"})
    assert retained_plan(workflow, rows).issues == []


def greeting_source(target="state.outputs.text"):
    """Reported workflow topology: typed Transform inside a sequence, Template after it."""
    workflow = source_workflow(parallel=True)
    workflow["groups"][0]["body"] = {"kind": "sequence", "entry": ref("fetch")}
    workflow["steps"][0].update(
        step_kind="set_variable",
        override={"set_variable": {"assignments": {"person": {"name": "Ada"}}}},
    )
    workflow["steps"][1].update(
        step_kind="transform",
        lane_id=None,
        override={
            "transform": {
                "input": {"source": "state.person"},
                "mode": "object",
                "fields": [{"from": "name", "to": "name", "type": "text"}],
                "result_target": "state.normalized",
            }
        },
    )
    workflow["steps"].append(
        step(
            "greet",
            "template",
            override={
                "template": {
                    "text": "Hello, {{ name }}!",
                    "inputs": [
                        {
                            "name": "name",
                            "source": "state.normalized.name",
                            "type": "text",
                            "required": True,
                        }
                    ],
                    "result_target": target,
                }
            },
        )
    )
    graph = workflow["settings"]["execution_graph"]
    graph["edges"][1]["target"]["lane_id"] = None
    graph["edges"][2]["target"] = ref("greet")
    graph["edges"].append(edge("greet", ref("done")))
    workflow["settings"].pop("canvas_annotations", None)
    workflow["settings"]["callable"] = {
        "enabled": True,
        "trigger_channels": ["parent_workflow"],
        "output_mode": "variables",
        "expected_outputs": [{"name": "text", "type": "string", "required": True}],
    }
    return workflow


def real_resolver_plan(workflow):
    from types import SimpleNamespace

    from core.application.registry.io_resolver import IOResolver

    resolver = IOResolver(SimpleNamespace(get_workflow=lambda *_args, **_kwargs: workflow))
    return analyze_group_bindings(
        workflow,
        "process",
        output_rows={
            s["step_id"]: resolver.get_step_state_outputs("wf", s["step_id"])
            for s in workflow["steps"]
        },
        retained_output_rows={
            s["step_id"]: resolver.get_step_state_writes(s) for s in workflow["steps"]
        },
        available_types={"fetch": resolver.get_available_type_map_for_step("wf", "fetch")},
    )


@pytest.mark.parametrize(
    "target",
    [
        "state.outputs.text",
        "outputs.text",
        "state.workflow_data.outputs.text",
        "state.workflow_data.text",
        "text",
    ],
)
def test_actual_post_group_template_proves_each_supported_public_result_alias(target):
    from core.application.orchestration.graph.nodes import execute_step_node
    from core.application.registry.workflow_contract import interface_from_definition
    from core.application.registry.workflow_contract_service import collect_runtime_outputs
    from tests._step_run_state import step_run_state

    workflow = greeting_source(target)
    assert real_resolver_plan(workflow).issues == []
    state = step_run_state(workflow["steps"][-1], normalized={"name": "Ada"})
    update = execute_step_node(state)
    assert update["agent_messages"][-1]["status"] == "pass"
    assert collect_runtime_outputs(
        {**state, **update}, interface=interface_from_definition(workflow)
    ) == ({"text": "Hello, Ada!"}, {})


@pytest.mark.parametrize(
    "variant",
    [
        "optional",
        "disabled",
        "wrong_type",
        "missing",
        "synthetic_only",
        "branch",
        "second_stop",
        "error",
        "continue",
        "rework",
        "nested",
        "loop",
        "unreachable_group",
    ],
)
def test_real_retained_template_requires_every_successful_path(variant):
    workflow = greeting_source()
    greet = workflow["steps"][-1]
    graph = workflow["settings"]["execution_graph"]
    if variant in {"optional", "disabled"}:
        greet[variant] = True
    elif variant == "wrong_type":
        greet.update(
            step_kind="transform", override={"transform": {"result_target": "state.outputs.text"}}
        )
    elif variant == "missing":
        greet["override"]["template"].pop("result_target")
    elif variant == "synthetic_only":
        # A descendant gives the picker an outputs object, never the required text.
        greet["override"]["template"]["result_target"] = "state.outputs.text.child"
    elif variant in {"branch", "second_stop", "rework"}:
        workflow["steps"].append(step("choose", "if"))
        graph["edges"][2]["target"] = ref("choose")
        bypass = "done"
        if variant == "second_stop":
            bypass = "early_done"
            workflow["steps"].append(step(bypass, "stop_success"))
        graph["edges"].extend(
            [edge("choose", ref("greet"), "true"), edge("choose", ref(bypass), "false")]
        )
        if variant == "rework":
            graph["edges"].append(edge("greet", ref("choose"), "again", rework=True))
    elif variant in {"error", "continue"}:
        greet["error_policy"] = {"on_error": "route_to_step" if variant == "error" else "continue"}
        if variant == "error":
            graph["edges"].append(edge("greet", ref("done"), "error"))
    elif variant == "nested":
        greet["group_id"] = "other"
    elif variant == "loop":
        greet["for_each_id"] = "other_loop"
    elif variant == "unreachable_group":
        graph["edges"][0]["target"] = ref("greet")
    issues = real_resolver_plan(workflow).issues
    assert any(issue["code"] == "parent_output_boundary" for issue in issues), issues


@pytest.mark.parametrize(
    "target",
    [
        "text",
        "outputs.text",
        "state.outputs.text",
        "workflow_data.text",
        "workflow_data.outputs.text",
        "outputs",
        "workflow_data",
        "workflow_data.outputs",
        "outputs.text.child",
        "outputs.text[0]",
    ],
)
@pytest.mark.parametrize("same_step", [False, True])
def test_exact_competing_assignment_refuses_even_after_name_dedup(target, same_step):
    workflow = greeting_source()
    if same_step:
        # The generic output mapping and builtin target are separate actual writes.
        workflow["steps"][-1]["output_mapping_jsonb"] = {"text": target}
    else:
        workflow["steps"].append(
            step(
                "other",
                "set_variable",
                override={"set_variable": {"assignments": {target: "other"}}},
            )
        )
    issues = real_resolver_plan(workflow).issues
    assert any(issue["code"] == "parent_output_boundary" for issue in issues), issues


def test_sibling_leaf_assignments_do_not_conflict_through_synthetic_parent():
    workflow = greeting_source()
    workflow["steps"].append(
        step(
            "other",
            "template",
            override={
                "template": {"result_target": "state.outputs.other", "text": "other", "inputs": []}
            },
        )
    )
    assert real_resolver_plan(workflow).issues == []


def test_retained_success_does_not_relax_moved_nested_write_or_history_boundary():
    workflow = greeting_source()
    workflow["steps"][1]["override"]["transform"]["result_target"] = "state.outputs.normalized"
    assert any(i["code"] == "partial_state_write" for i in real_resolver_plan(workflow).issues)
    workflow = greeting_source()
    workflow["steps"][-1]["override"]["template"]["inputs"][0]["source"] = (
        "message.step.fetch.artifacts.normalized"
    )
    assert any(i["code"] == "moved_message_reference" for i in real_resolver_plan(workflow).issues)


def test_retained_output_requires_separate_exact_assignment_evidence():
    workflow, suggestions = retained_source()
    plan = analyze_group_bindings(
        workflow,
        "process",
        output_rows=suggestions,
        available_types={"fetch": {"state.items": "array"}},
    )
    assert any(issue["code"] == "parent_output_boundary" for issue in plan.issues)
