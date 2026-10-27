"""A converter must prove the boundary before proposing any database writes."""

import copy

import pytest

from core.application.workflows.workflow_group_extraction_bindings import analyze_group_bindings
from tests.unit.test_workflow_group_extraction_structure import source_workflow


def source():
    workflow = source_workflow()
    workflow["steps"][1]["override"] = {
        "http_request": {
            "url": "https://example.test/{{ state.record.id }}",
            "response_target": "response",
        }
    }
    return workflow


def analyze(workflow, *, outputs=None, types=None):
    return analyze_group_bindings(
        workflow,
        "process",
        output_rows=outputs
        or {"fetch": [{"name": "response", "type": "object", "source": "http_request"}]},
        available_types=types or {"fetch": {"state.items": "array"}},
    )


def test_read_only_external_input_moves_to_typed_interface_without_copying_history():
    workflow = source()
    original = copy.deepcopy(workflow)
    plan = analyze(workflow)
    assert workflow == original
    assert plan.issues == []
    assert plan.input_mapping == {"source_items": "state.items"}
    assert plan.output_mapping == {"response": "state.response"}
    assert plan.interface["input_mode"] == "json_schema"
    assert plan.interface["input_schema"]["properties"]["source_items"] == {
        "type": ["array", "null"]
    }
    assert plan.workflow["for_each"][0]["over"] == "state.inputs.source_items"
    assert plan.workflow["steps"][1]["override"] == original["steps"][1]["override"]


def test_rewrites_only_authored_read_fields_not_text_or_write_targets():
    workflow = source()
    workflow["steps"][1]["display_name"] = "state.items is documentation"
    workflow["steps"][1]["override"]["http_request"]["headers"] = {
        "X-Customer": "{{ state.customer.name }}",
    }
    plan = analyze(workflow, types={"fetch": {"state.items": "array", "state.customer": "object"}})
    assert not plan.issues
    step = plan.workflow["steps"][1]
    assert step["display_name"] == "state.items is documentation"
    assert step["override"]["http_request"]["response_target"] == "response"
    assert (
        step["override"]["http_request"]["headers"]["X-Customer"]
        == "{{ state.inputs.source_customer.name }}"
    )


def test_parent_workflow_input_and_state_root_with_same_name_do_not_collide():
    workflow = source()
    workflow["steps"][1]["override"]["http_request"]["headers"] = {
        "X-Input": "{{ state.inputs.items[0] }}"
    }
    plan = analyze(
        workflow, types={"fetch": {"state.items": "array", "state.inputs.items": "array"}}
    )
    assert not plan.issues
    assert plan.input_mapping == {
        "source_items": "state.items",
        "input_items": "state.inputs.items",
    }


@pytest.mark.parametrize(
    ("field", "value", "code"),
    [
        ("step_kind", "agent", "implicit_agent_context"),
        ("step_kind", "file_from_base64", "artifact_boundary"),
        ("context_inject", {"history": {"message_type": "answer"}}, "implicit_message_history"),
    ],
)
def test_implicit_or_untransferable_context_is_an_actionable_blocker(field, value, code):
    workflow = source()
    workflow["steps"][1][field] = value
    plan = analyze(workflow)
    assert any(issue["code"] == code and "fetch" in issue["path"] for issue in plan.issues)


def test_unknown_external_type_blocks_instead_of_passing_untyped_state():
    plan = analyze(source(), types={"fetch": {}})
    assert any(
        issue["code"] == "unknown_input_type" and issue["path"] == "for_each.records.over"
        for issue in plan.issues
    )
    # The refusal names the value it is about, so the dialog can say it in plain words.
    unknown = [issue for issue in plan.issues if issue["code"] == "unknown_input_type"]
    assert unknown
    assert all(issue["subject"].startswith("state.") for issue in unknown)


def test_loop_carried_accumulator_is_not_replaced_by_a_frozen_input():
    workflow = source()
    workflow["steps"][1].update(
        step_kind="set_variable",
        override={"set_variable": {"assignments": {"counter": {"source": "state.counter"}}}},
    )
    plan = analyze(
        workflow,
        outputs={"fetch": [{"name": "counter", "type": "number", "source": "set_variable"}]},
        types={"fetch": {"state.items": "array", "state.counter": "number"}},
    )
    assert any(issue["code"] == "mutable_external_state" for issue in plan.issues)


def test_partial_object_output_cannot_replace_unmoved_sibling_fields():
    plan = analyze(
        source(),
        outputs={
            "fetch": [
                {"name": "customer", "type": "object", "source": "http_request"},
                {"name": "customer.response", "type": "object", "source": "http_request"},
            ]
        },
    )
    assert any(issue["code"] == "partial_state_write" for issue in plan.issues)


def test_downstream_use_of_for_each_item_is_not_silently_lost():
    workflow = source()
    workflow["steps"][-1].update(
        step_kind="template",
        override={"template": {"inputs": [{"name": "last", "source": "state.record"}]}},
    )
    plan = analyze(workflow)
    assert any(issue["code"] == "for_each_local_escape" for issue in plan.issues)


def test_error_exit_blocks_partial_state_transfer():
    workflow = source()
    workflow["groups"][0]["error_policy"] = {"on_error": "route_to_step"}
    plan = analyze(workflow)
    assert any(issue["code"] == "failure_state_boundary" for issue in plan.issues)


def test_internal_pipeline_keeps_reads_of_definitely_written_state_inside_child():
    workflow = source()
    workflow["steps"][1].update(
        step_kind="template",
        error_policy={"on_error": "fail"},
        override={"template": {"template": "{}", "output_target": "response"}},
    )
    workflow["steps"].insert(
        2,
        {
            "step_id": "format",
            "step_kind": "template",
            "for_each_id": "records",
            "override": {
                "template": {"inputs": [{"name": "response", "source": "state.response"}]}
            },
        },
    )
    edges = workflow["settings"]["execution_graph"]["edges"]
    exit_edge = edges[1]
    exit_edge["source"]["id"] = "format"
    edges.append(
        {
            "source": {"kind": "step", "id": "fetch"},
            "outcome": {"name": "success"},
            "target": {"kind": "step", "id": "format"},
        }
    )
    plan = analyze(workflow)
    assert not plan.issues
    assert (
        plan.workflow["steps"][2]["override"]["template"]["inputs"][0]["source"] == "state.response"
    )
    assert "source_response" not in plan.input_mapping


def test_two_reads_of_one_external_root_in_one_template_are_rewritten_once():
    workflow = source()
    workflow["steps"][1]["override"]["http_request"]["headers"] = {
        "X-Input": "{{ state.customer.first }} {{ state.customer.last }}"
    }
    plan = analyze(workflow, types={"fetch": {"state.items": "array", "state.customer": "object"}})
    assert not plan.issues
    assert (
        plan.workflow["steps"][1]["override"]["http_request"]["headers"]["X-Input"]
        == "{{ state.inputs.source_customer.first }} {{ state.inputs.source_customer.last }}"
    )


def test_generated_schema_is_also_visible_to_existing_io_resolver_and_forms():
    plan = analyze(source())
    assert plan.interface["expected_inputs"] == [
        {"name": "source_items", "type": "array", "required": False}
    ]


def test_remaining_parent_cannot_keep_a_reference_to_a_moved_message_producer():
    workflow = source()
    workflow["steps"][-1].update(
        step_kind="connector", input_mapping_jsonb={"body": "message.step.fetch.artifacts.body"}
    )
    plan = analyze(workflow)
    assert any(issue["code"] == "moved_message_reference" for issue in plan.issues)


def test_another_for_each_reading_the_moved_item_also_blocks_conversion():
    workflow = source()
    workflow["for_each"].append(
        {"for_each_id": "later", "group_id": None, "over": "state.record.items", "item_name": "x"}
    )
    plan = analyze(workflow)
    assert any(
        issue["code"] == "for_each_local_escape" and "later" in issue["path"]
        for issue in plan.issues
    )


def test_parent_agent_implicit_history_is_an_external_dependency():
    workflow = source()
    workflow["steps"][-1].update(step_kind="agent", agent_id="reviewer")
    plan = analyze(workflow)
    assert any(issue["code"] == "parent_agent_context" for issue in plan.issues)


def test_for_each_source_must_be_a_typed_array_to_move():
    plan = analyze(source(), types={"fetch": {"state.items": "object"}})
    assert any(issue["code"] == "for_each_source_boundary" for issue in plan.issues)


def test_optional_predecessor_does_not_prove_an_internal_value_exists():
    workflow = source()
    workflow["steps"][1]["optional"] = True
    workflow["steps"].insert(
        2,
        {
            "step_id": "format",
            "step_kind": "template",
            "for_each_id": "records",
            "override": {"template": {"inputs": [{"name": "body", "source": "state.response"}]}},
        },
    )
    edges = workflow["settings"]["execution_graph"]["edges"]
    edges[1]["source"]["id"] = "format"
    edges.append(
        {
            "source": {"kind": "step", "id": "fetch"},
            "outcome": {"name": "success"},
            "target": {"kind": "step", "id": "format"},
        }
    )
    assert any(issue["code"] == "mutable_external_state" for issue in analyze(workflow).issues)


def test_parent_agent_scoped_message_selectors_cannot_lose_moved_messages():
    workflow = source()
    workflow["steps"][-1].update(
        step_kind="invoke_workflow",
        override={
            "invoke_workflow": {
                "workflow_id": "later",
                "input_mapping": {"payload": "message.agent.control_flow_step.artifacts.body"},
            }
        },
    )
    assert any(issue["code"] == "moved_message_reference" for issue in analyze(workflow).issues)


def test_literal_text_and_template_defaults_are_not_selector_rewrite_targets():
    workflow = source()
    workflow["steps"][1]["override"]["http_request"]["headers"] = {
        "X-Input": "literal state.customer {{ state.customer.name | default('state.customer') }}"
    }
    plan = analyze(workflow, types={"fetch": {"state.items": "array", "state.customer": "object"}})
    assert not plan.issues
    assert (
        plan.workflow["steps"][1]["override"]["http_request"]["headers"]["X-Input"]
        == "literal state.customer {{ state.inputs.source_customer.name | default('state.customer') }}"
    )


def test_display_labels_are_not_runtime_message_dependencies():
    workflow = source()
    workflow["groups"][0]["display_name"] = "Process message.text"
    assert not analyze(workflow).issues


def test_parent_callable_message_only_output_cannot_be_silently_lost():
    workflow = source()
    workflow["settings"]["callable"] = {
        "enabled": True,
        "expected_outputs": [{"name": "result", "type": "object", "required": True}],
    }
    assert any(issue["code"] == "parent_output_boundary" for issue in analyze(workflow).issues)


@pytest.mark.parametrize("policy", ["continue", "await_approval", "retry"])
def test_recovery_can_take_success_without_writing_a_planned_output(policy):
    workflow = source()
    workflow["steps"][1]["error_policy"] = {"on_error": policy}
    workflow["steps"].insert(
        2,
        {
            "step_id": "format",
            "step_kind": "template",
            "for_each_id": "records",
            "override": {"template": {"inputs": [{"name": "body", "source": "state.response"}]}},
        },
    )
    edges = workflow["settings"]["execution_graph"]["edges"]
    edges[1]["source"]["id"] = "format"
    edges.append(
        {
            "source": {"kind": "step", "id": "fetch"},
            "outcome": {"name": "success"},
            "target": {"kind": "step", "id": "format"},
        }
    )
    assert any(issue["code"] == "mutable_external_state" for issue in analyze(workflow).issues)


def test_dynamic_context_is_refused_and_the_moved_for_each_index_stays_local():
    dynamic = source()
    dynamic["steps"][1]["override"]["http_request"]["headers"] = {
        "X-Step": "{{ state.current_step_id }}"
    }
    dynamic_plan = analyze(
        dynamic, types={"fetch": {"state.items": "array", "state.current_step_id": "text"}}
    )
    indexed = source()
    indexed["for_each"][0]["index_name"] = "position"
    indexed["steps"][1]["override"]["http_request"]["headers"] = {"X-Index": "{{ state.position }}"}
    indexed_plan = analyze(
        indexed, types={"fetch": {"state.items": "array", "state.position": "number"}}
    )
    observed = {
        "dynamic_refused": any(
            issue["code"] == "runtime_context_boundary" for issue in dynamic_plan.issues
        ),
        "index_refused": bool(indexed_plan.issues),
    }
    assert observed == {"dynamic_refused": True, "index_refused": False}


def test_exit_contracts_cannot_keep_hidden_parent_message_or_state_dependencies():
    moved = source()
    moved["steps"][1]["boundary_contract"] = {"exit": ["state.customer"]}
    moved_plan = analyze(moved)
    parent = source()
    parent["steps"][-1]["boundary_contract"] = {"exit": ["message.step.fetch.artifacts.body"]}
    parent_plan = analyze(parent)
    assert {
        "moved_exit_refused": any(i["code"] == "exit_contract_boundary" for i in moved_plan.issues),
        "parent_message_refused": any(
            i["code"] == "moved_message_reference" for i in parent_plan.issues
        ),
    } == {"moved_exit_refused": True, "parent_message_refused": True}


def test_host_http_retry_can_recover_without_writes_even_with_fail_policy():
    workflow = source()
    workflow["steps"][1]["error_policy"] = {"on_error": "fail"}
    workflow["steps"].insert(
        2,
        {
            "step_id": "format",
            "step_kind": "template",
            "for_each_id": "records",
            "override": {"template": {"inputs": [{"name": "body", "source": "state.response"}]}},
        },
    )
    edges = workflow["settings"]["execution_graph"]["edges"]
    edges[1]["source"]["id"] = "format"
    edges.append(
        {
            "source": {"kind": "step", "id": "fetch"},
            "outcome": {"name": "success"},
            "target": {"kind": "step", "id": "format"},
        }
    )
    assert any(issue["code"] == "mutable_external_state" for issue in analyze(workflow).issues)


@pytest.mark.parametrize("mode", ["artifact", " ARTIFACT "])
def test_http_artifact_response_requires_explicit_child_artifact_ownership_contract(mode):
    workflow = source()
    workflow["steps"][1]["override"]["http_request"]["response_mode"] = mode
    plan = analyze(workflow)
    assert any(
        issue["code"] == "artifact_boundary" and "fetch" in issue["path"] for issue in plan.issues
    )


@pytest.mark.parametrize("selector", ["customer", "state"])
@pytest.mark.parametrize("owner", ["step", "group"])
def test_boundary_entry_without_explicit_state_path_cannot_escape_input_mapping(selector, owner):
    workflow = source()
    target = workflow["steps"][1] if owner == "step" else workflow["groups"][0]
    target["boundary_contract"] = {
        "entry": [{"name": "customer", "source": selector, "type": "string"}]
    }
    plan = analyze(workflow)
    assert any(issue["code"] == "implicit_boundary_input" for issue in plan.issues)


def test_the_for_each_control_steps_move_with_it():
    # Break and Continue only steer their own For each, and Collect has its own
    # target-boundary rule: none of them blocks a conversion by its kind alone.
    workflow = source()
    workflow["steps"].extend(
        [
            {"step_id": "skip", "step_kind": "continue", "for_each_id": "records"},
            {"step_id": "enough", "step_kind": "break", "for_each_id": "records"},
            {
                "step_id": "keep",
                "step_kind": "collect",
                "for_each_id": "records",
                "override": {
                    "collect": {"target": "state.kept", "mode": "append", "source": "state.record"}
                },
            },
        ]
    )
    plan = analyze(workflow)
    assert not [
        issue
        for issue in plan.issues
        if issue["code"] == "unsupported_step_boundary"
        and issue["path"] in {"steps.skip", "steps.enough", "steps.keep"}
    ]
