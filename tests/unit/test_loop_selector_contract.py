"""Loop selectors defer unknown members and reject only proven impossible traversal."""

from __future__ import annotations

from copy import deepcopy

import pytest
import yaml

from core.application.registry.contract_validation import validate_workflow_readiness
from core.application.registry.io_resolver import IOResolver
from tests._workflow_definitions import edge, linear_workflow, node
from tests.unit.test_workflow_authoring_catalog import SCOPE
from tests.unit.test_workflow_authoring_drafts import DraftDB, bundle, service

ORDERS = [
    {"id": "A", "items": [1], "active": False, "count": 0},
    {"id": "B", "items": [2, 3], "active": True, "count": 2},
]


def variable(name, value, step_id="prepare"):
    return {
        "step_id": step_id,
        "step_kind": "set_variable",
        "override": {"set_variable": {"assignments": {name: value}}},
    }


def loop_workflow(body, *, batch=False):
    prepare = variable("orders", ORDERS)
    lead = [prepare]
    if batch:
        lead.append(
            {
                "step_id": "batch",
                "step_kind": "batch",
                "override": {
                    "batch": {
                        "source_path": "state.orders",
                        "result_target": "state.chunks",
                        "batch_size": 1,
                    }
                },
            }
        )
    body = {**body, "step_id": "consume", "for_each_id": "orders_loop"}
    wf = linear_workflow(*lead, body, workflow_id="selector_parent", account_id=SCOPE["account_id"])
    wf["for_each"] = [
        {
            "for_each_id": "orders_loop",
            "group_id": None,
            "over": "state.chunks" if batch else "state.orders",
            "item_name": "b" if batch else "o",
            "index_name": "i",
            "body": {"kind": "sequence", "entry": node("consume")},
        }
    ]
    wf["settings"]["execution_graph"] = {
        "schema_version": 1,
        "entry": node("prepare"),
        "edges": [
            *([edge("prepare", node("batch"))] if batch else []),
            edge("batch" if batch else "prepare", node("orders_loop", "for_each")),
            edge(
                "consume", {"kind": "for_each_exit", "for_each_id": "orders_loop", "lane_id": None}
            ),
            edge("orders_loop", node("done"), source_kind="for_each"),
        ],
    }
    return wf


def child_workflow(inputs=None):
    return linear_workflow(
        {
            "step_id": "summary",
            "step_kind": "template",
            "override": {
                "template": {
                    "text": "Shipment assembled",
                    "inputs": [],
                    "result_target": "state.outputs.summary",
                }
            },
        },
        workflow_id="selector_child",
        account_id=SCOPE["account_id"],
        settings={
            "callable": {
                "enabled": True,
                "trigger_channels": ["parent_workflow"],
                "expected_inputs": inputs
                or [
                    {"name": "order_id", "type": "text", "required": True},
                    {"name": "items", "type": "array", "required": True},
                ],
                "expected_outputs": [{"name": "summary", "type": "text", "required": True}],
            }
        },
    )


def invoke_workflow(mapping=None):
    return loop_workflow(
        {
            "step_kind": "invoke_workflow",
            "override": {
                "invoke_workflow": {
                    "workflow_id": "selector_child",
                    "input_mapping": mapping
                    or {"order_id": "state.o.id", "items": "state.o.items"},
                    "output_mapping": {"summary": "state.summary"},
                }
            },
        }
    )


def template_workflow(source="state.b.size", required=False, text="{{ size }}"):
    row = {"name": "size", "type": "json", "source": source}
    if required is not None:
        row["required"] = required
    return loop_workflow(
        {
            "step_kind": "template",
            "override": {
                "template": {"text": text, "inputs": [row], "result_target": "state.rendered"}
            },
        },
        batch=True,
    )


def database(workflow, child=None):
    db = DraftDB()
    db.workflows[workflow["workflow_id"]] = deepcopy(workflow)
    if child:
        db.workflows[child["workflow_id"]] = deepcopy(child)
    return db


def readiness(workflow, child=None):
    return validate_workflow_readiness(database(workflow, child), workflow["workflow_id"])


@pytest.mark.parametrize("input_name", ["order_id", "items"])
def test_in_scope_loop_member_is_deferred_independently(input_name):
    wf = invoke_workflow()
    result = readiness(wf, child_workflow())
    field = f"workflow.steps.consume.override.invoke_workflow.input_mapping.{input_name}"
    errors = [e for e in result["errors"] if e["path"] == field]
    assert errors == [], errors
    assert result["valid"], result["errors"]
    warning = next(w for w in result["warnings"] if w["path"] == field)
    assert warning["code"] == "invoke.input_mapping_state_member_unchecked"
    assert "runtime" in warning["message"].lower()
    assert "unknown" in warning["message"].lower()


@pytest.mark.parametrize("required", [False, True, None])
def test_bounded_batch_chunk_property_is_rejected_at_its_source(required):
    result = readiness(template_workflow(required=required))
    issues = [e for e in result["errors"] if e["code"] == "workflow.state_selector_shape_invalid"]
    assert len(issues) == 1, result
    assert issues[0]["path"] == "workflow.steps.consume.override.template.inputs[0].source"
    assert issues[0]["selector"] == "state.b.size"
    assert "list" in issues[0]["message"]
    assert "[0]" in issues[0]["message"]


def test_loop_index_retains_number_type():
    wf = invoke_workflow()
    assert (
        IOResolver(database(wf)).get_available_type_map_for_step(wf["workflow_id"], "consume")[
            "state.i"
        ]
        == "number"
    )


@pytest.mark.parametrize("source", ["state.b[0]", "state.b.0", "state.b"])
def test_bounded_batch_legal_sources_remain_valid(source):
    result = readiness(template_workflow(source))
    assert result["valid"], result["errors"]


@pytest.mark.parametrize(
    "source", ["state.other.id", "state.o[*]", "state.o[", "o.id", "state.i.size"]
)
def test_invoke_denies_wrong_root_syntax_bare_dotted_and_index_member(source):
    wf = invoke_workflow({"order_id": source, "items": "literal:[1]"})
    result = readiness(wf, child_workflow())
    assert not result["valid"]
    assert any(e["path"].endswith("input_mapping.order_id") for e in result["errors"]), result


def test_loop_member_after_exit_remains_unavailable():
    wf = invoke_workflow()
    wf["steps"][1]["for_each_id"] = None
    wf["for_each"] = []
    from tests._workflow_definitions import linear_graph

    wf["settings"]["execution_graph"] = linear_graph([s["step_id"] for s in wf["steps"]])
    result = readiness(wf, child_workflow())
    assert (
        sum(e["code"] == "invoke.input_mapping_source_unavailable" for e in result["errors"]) == 2
    )


def as_bundle(workflow):
    parsed = yaml.safe_load(bundle())
    candidate = deepcopy(workflow)
    candidate["execution_graph"] = candidate["settings"].pop("execution_graph")
    parsed["workflows"] = [candidate]
    return yaml.safe_dump(parsed)


def test_same_loop_definition_draft_import_save_and_stored_readiness():
    wf = invoke_workflow()
    db = DraftDB()
    db.workflows["selector_child"] = child_workflow()
    text = as_bundle(wf)
    drafts = service(db)
    checked = drafts.validate(scope=SCOPE, project_id="project", bundle_yaml=text)
    assert checked["can_save"], checked
    assert checked["readiness"]["valid"], checked
    saved = drafts.save(scope=SCOPE, project_id="project", bundle_yaml=text)
    stored = db.get_workflow(saved["workflow_id"])
    assert stored["for_each"] == wf["for_each"]
    assert stored["steps"][1]["override"] == wf["steps"][1]["override"]
    assert validate_workflow_readiness(db, saved["workflow_id"])["valid"]


@pytest.mark.parametrize(
    "mode", ["unknown", "dataset", "ordinary_array", "overwrite", "body_write", "branch"]
)
def test_batch_item_shape_is_not_invented(mode):
    wf = template_workflow()
    if mode in {"unknown", "dataset"}:
        wf["steps"][0] = variable(
            "orders",
            {"source": "state.external"}
            if mode == "unknown"
            else {"dataset_handle": "dst_rows", "row_count": 5},
        )
    elif mode == "ordinary_array":
        wf["for_each"][0]["over"] = "state.orders"
    elif mode == "body_write":
        write = variable("b", {"source": "state.external"}, "replace")
        write["for_each_id"] = "orders_loop"
        wf["steps"].insert(2, write)
        wf["for_each"][0]["body"]["entry"] = node("replace")
        wf["settings"]["execution_graph"]["edges"].append(edge("replace", node("consume")))
    elif mode == "overwrite":
        wf["steps"].insert(2, variable("chunks", [{"size": 7}], "replace"))
        edges = wf["settings"]["execution_graph"]["edges"]
        edges[1] = edge("batch", node("replace"))
        edges.append(edge("replace", node("orders_loop", "for_each")))
    else:
        wf["steps"].insert(
            1,
            {
                "step_id": "choose",
                "step_kind": "if",
                "override": {
                    "if": {"condition": {"selector": "state.task_summary", "operator": "truthy"}}
                },
            },
        )
        wf["steps"].insert(3, variable("chunks", [{"size": 7}], "alternate"))
        edges = wf["settings"]["execution_graph"]["edges"]
        edges[0] = edge("prepare", node("choose"))
        edges.extend(
            [
                edge("choose", node("batch"), "true"),
                edge("choose", node("alternate"), "false"),
                edge("alternate", node("orders_loop", "for_each")),
            ]
        )
    result = readiness(wf)
    assert not [
        e for e in result["errors"] if e["code"] == "workflow.state_selector_shape_invalid"
    ], result
    assert result["valid"], result["errors"]


@pytest.mark.parametrize(
    "value,source,impossible",
    [
        ({"0": "zero"}, "state.value.0", False),
        ({"0": "zero"}, "state.value[0]", True),
        ([1], "state.value.size", True),
        ([1], "state.value.0", False),
        (7, "state.value.size", True),
        ({"id": 7}, "state.value.body", False),
        ({"body": {"id": 7}}, "state.value.body", False),
    ],
)
def test_real_producer_shape_distinguishes_objects_lists_and_unknown_members(
    value, source, impossible
):
    wf = linear_workflow(
        variable("value", value),
        {
            "step_id": "consume",
            "step_kind": "template",
            "override": {
                "template": {
                    "text": "{{ payload | default('missing') }}",
                    "inputs": [
                        {"name": "payload", "type": "json", "required": False, "source": source}
                    ],
                    "result_target": "state.rendered",
                }
            },
        },
    )
    result = readiness(wf)
    issues = [e for e in result["errors"] if e["code"] == "workflow.state_selector_shape_invalid"]
    assert bool(issues) is impossible, result
    assert result["valid"] is not impossible, result


def test_exact_loop_leaf_keeps_type_mismatch_over_unknown_item():
    wf = invoke_workflow({"order_id": "state.o.id", "items": "literal:[1]"})
    wf["steps"].insert(1, {**variable("o.id", 7, "set_id"), "for_each_id": "orders_loop"})
    wf["for_each"][0]["body"]["entry"] = node("set_id")
    wf["settings"]["execution_graph"]["edges"].append(edge("set_id", node("consume")))
    result = readiness(wf, child_workflow())
    assert any(
        e["code"] == "invoke.input_mapping_type_mismatch" and e["path"].endswith(".order_id")
        for e in result["errors"]
    ), result
    assert not any(w["path"].endswith(".order_id") for w in result["warnings"])


def test_unknown_global_container_does_not_authorize_invoke_descendants():
    result = readiness(
        invoke_workflow({"order_id": "state.workflow_data.invented", "items": "literal:[1]"}),
        child_workflow(),
    )
    assert any(e["code"] == "invoke.input_mapping_source_unavailable" for e in result["errors"])


def test_whole_item_and_set_variable_workaround_stay_valid():
    wf = invoke_workflow({"payload": "state.o"})
    child = child_workflow([{"name": "payload", "type": "object", "required": True}])
    assert readiness(wf, child)["valid"]
    wf = invoke_workflow({"order_id": "state.order_id", "items": "state.items"})
    write = {
        "step_id": "copy",
        "step_kind": "set_variable",
        "for_each_id": "orders_loop",
        "override": {
            "set_variable": {
                "assignments": {
                    "order_id": {"source": "state.o.id"},
                    "items": {"source": "state.o.items"},
                }
            }
        },
    }
    wf["steps"].insert(1, write)
    wf["for_each"][0]["body"]["entry"] = node("copy")
    wf["settings"]["execution_graph"]["edges"].append(edge("copy", node("consume")))
    result = readiness(wf, child_workflow())
    assert result["valid"], result


def test_nested_sequence_scope_retains_batch_proof():
    wf = template_workflow()
    wf["groups"] = [{"group_id": "sequence", "body": {"kind": "sequence", "entry": node("batch")}}]
    wf["steps"][1]["group_id"] = "sequence"
    wf["for_each"][0]["group_id"] = "sequence"
    graph = wf["settings"]["execution_graph"]
    graph["edges"][0] = edge("prepare", node("sequence", "group"))
    graph["edges"][-1] = edge(
        "orders_loop",
        {"kind": "group_exit", "group_id": "sequence", "lane_id": None},
        source_kind="for_each",
    )
    graph["edges"].append(edge("sequence", node("done"), source_kind="group"))
    result = readiness(wf)
    assert any(e["code"] == "workflow.state_selector_shape_invalid" for e in result["errors"]), (
        result
    )


def test_bare_dotted_invoke_selector_is_denied_even_with_exact_leaf():
    wf = invoke_workflow({"order_id": "o.id", "items": "literal:[1]"})
    wf["steps"].insert(1, {**variable("o.id", "A", "set_id"), "for_each_id": "orders_loop"})
    wf["for_each"][0]["body"]["entry"] = node("set_id")
    wf["settings"]["execution_graph"]["edges"].append(edge("set_id", node("consume")))
    result = readiness(wf, child_workflow())
    assert any(
        e["code"] == "invoke.input_mapping_source_unavailable" and e["path"].endswith(".order_id")
        for e in result["errors"]
    ), result


@pytest.mark.parametrize("source,invalid", [("state.b.size", True), ("state.b", False)])
def test_transform_uses_the_same_bounded_batch_shape(source, invalid):
    wf = template_workflow()
    wf["steps"][2].update(
        step_kind="transform",
        override={
            "transform": {
                "input": {"source": source},
                "mode": "list",
                "result_target": "state.result",
                "fields": [{"from": "id", "to": "id", "type": "number"}],
            }
        },
    )
    result = readiness(wf)
    assert (
        bool([e for e in result["errors"] if e["code"] == "workflow.state_selector_shape_invalid"])
        is invalid
    ), result
    assert result["valid"] is not invalid, result


def test_http_member_shape_is_deferred_by_real_producer_contract():
    wf = linear_workflow(
        {
            "step_id": "request",
            "step_kind": "http_request",
            "override": {
                "http_request": {
                    "method": "GET",
                    "url": "https://example.com",
                    "response_target": "state.user",
                    "timeout_seconds": 5,
                    "expected_statuses": [200],
                }
            },
        },
        {
            "step_id": "consume",
            "step_kind": "transform",
            "override": {
                "transform": {
                    "input": {"source": "state.user.body"},
                    "mode": "object",
                    "result_target": "state.result",
                    "fields": [{"from": "id", "to": "id", "type": "number"}],
                }
            },
        },
    )
    result = readiness(wf)
    assert result["valid"], result


def test_impossible_batch_source_blocks_same_definition_draft_and_save():
    from core.application.workflows.workflow_portability import WorkflowPortabilityError

    wf = template_workflow()
    db = DraftDB()
    text = as_bundle(wf)
    checked = service(db).validate(scope=SCOPE, project_id="project", bundle_yaml=text)
    assert not checked["can_save"], checked
    assert any(e["code"] == "workflow.state_selector_shape_invalid" for e in checked["issues"])
    with pytest.raises(WorkflowPortabilityError):
        service(db).save(scope=SCOPE, project_id="project", bundle_yaml=text)
    assert db.workflows == {} and db.history == []


def test_child_declared_array_to_parent_loop_keeps_members_unknown():
    wf = invoke_workflow()
    producer = child_workflow()
    producer["workflow_id"] = "orders_child"
    producer["settings"]["callable"]["expected_inputs"] = []
    producer["settings"]["callable"]["allow_empty_inputs"] = True
    producer["settings"]["callable"]["expected_outputs"] = [
        {"name": "orders", "type": "array", "required": True}
    ]
    producer["steps"][0] = variable("state.outputs.orders", ORDERS, "summary")
    wf["steps"][0] = {
        "step_id": "prepare",
        "step_kind": "invoke_workflow",
        "override": {
            "invoke_workflow": {
                "workflow_id": "orders_child",
                "input_mapping": {},
                "output_mapping": {"orders": "state.orders"},
            }
        },
    }
    db = database(wf, child_workflow())
    db.workflows["orders_child"] = producer
    result = validate_workflow_readiness(db, wf["workflow_id"])
    assert result["valid"], result
    warnings = [
        w for w in result["warnings"] if w["code"] == "invoke.input_mapping_state_member_unchecked"
    ]
    assert len(warnings) == 2
    types = IOResolver(db).get_available_type_map_for_step(wf["workflow_id"], "consume")
    assert types["state.orders"] == "array" and types["state.o"] == "json"
    assert "state.o.id" not in types and "state.o.items" not in types


def whole_chunk_invoke():
    body = invoke_workflow({"payload": "state.b"})["steps"][1]
    return loop_workflow(body, batch=True)


def test_proven_whole_batch_item_cannot_feed_child_scalar():
    result = readiness(
        whole_chunk_invoke(),
        child_workflow([{"name": "payload", "type": "number", "required": True}]),
    )
    assert any(
        e["code"] == "invoke.input_mapping_type_mismatch" and e["path"].endswith(".payload")
        for e in result["errors"]
    ), result


@pytest.mark.parametrize("mode", ["fields", "json_schema"])
def test_proven_whole_batch_item_can_feed_array_or_json_schema(mode):
    child = child_workflow([{"name": "payload", "type": "array", "required": True}])
    if mode == "json_schema":
        child["settings"]["callable"].update(
            input_mode=mode,
            input_schema={
                "type": "object",
                "properties": {"payload": {"type": "array"}},
                "required": ["payload"],
            },
        )
    result = readiness(whole_chunk_invoke(), child)
    assert result["valid"], result


@pytest.mark.parametrize("mode", ["unknown", "dataset", "overwrite", "ordinary_array", "branch"])
def test_uncertain_batch_item_does_not_gain_list_type(mode):
    wf = whole_chunk_invoke()
    if mode in {"unknown", "dataset"}:
        wf["steps"][0] = variable(
            "orders",
            {"source": "state.external"}
            if mode == "unknown"
            else {"dataset_handle": "dst_rows", "row_count": 5},
        )
    elif mode == "ordinary_array":
        wf["for_each"][0]["over"] = "state.orders"
    elif mode == "overwrite":
        wf["steps"].insert(2, variable("chunks", [1], "replace"))
        wf["settings"]["execution_graph"]["edges"][1] = edge("batch", node("replace"))
        wf["settings"]["execution_graph"]["edges"].append(
            edge("replace", node("orders_loop", "for_each"))
        )
    else:
        wf["steps"].insert(
            1,
            {
                "step_id": "choose",
                "step_kind": "if",
                "override": {
                    "if": {"condition": {"selector": "state.task_summary", "operator": "truthy"}}
                },
            },
        )
        wf["steps"].insert(3, variable("chunks", [1], "alternate"))
        edges = wf["settings"]["execution_graph"]["edges"]
        edges[0] = edge("prepare", node("choose"))
        edges.extend(
            [
                edge("choose", node("batch"), "true"),
                edge("choose", node("alternate"), "false"),
                edge("alternate", node("orders_loop", "for_each")),
            ]
        )
    result = readiness(
        wf, child_workflow([{"name": "payload", "type": "number", "required": True}])
    )
    assert result["valid"], result


def test_loop_publication_shadows_a_preloop_writer_of_the_item_name():
    wf = loop_workflow(
        {
            "step_kind": "template",
            "override": {
                "template": {
                    "text": "{{ id }}",
                    "inputs": [{"name": "id", "type": "text", "source": "state.o.id"}],
                    "result_target": "state.text",
                }
            },
        }
    )
    wf["steps"][0]["override"]["set_variable"]["assignments"]["o"] = [1]
    result = readiness(wf)
    assert result["valid"], result


def test_loop_publication_discards_preloop_exact_leaf_type():
    wf = invoke_workflow({"payload": "state.o.id"})
    wf["steps"][0]["override"]["set_variable"]["assignments"]["o.id"] = "stale"
    wf["steps"][0]["override"]["set_variable"]["assignments"]["orders"] = [{"id": 7}, {"id": 8}]
    result = readiness(
        wf, child_workflow([{"name": "payload", "type": "number", "required": True}])
    )
    assert result["valid"], result
    assert any(
        w["code"] == "invoke.input_mapping_state_member_unchecked" for w in result["warnings"]
    ), result


def test_whole_chunk_does_not_relax_json_field_alias_to_object():
    result = readiness(
        whole_chunk_invoke(),
        child_workflow([{"name": "payload", "type": "json", "required": True}]),
    )
    assert any(e["code"] == "invoke.input_mapping_type_mismatch" for e in result["errors"]), result


def test_preloop_leaf_remains_available_through_explicit_message_binding():
    wf = invoke_workflow({"payload": "message.step.prepare.artifacts.o.id"})
    wf["steps"][0]["override"]["set_variable"]["assignments"]["o.id"] = "previous"
    assert readiness(wf, child_workflow([{"name": "payload", "type": "text", "required": True}]))[
        "valid"
    ]


def test_current_body_exact_leaf_overrides_shadowed_preloop_leaf():
    wf = invoke_workflow({"payload": "state.o.id"})
    wf["steps"][0]["override"]["set_variable"]["assignments"]["o.id"] = "stale"
    wf["steps"].insert(1, {**variable("o.id", 7, "set_id"), "for_each_id": "orders_loop"})
    wf["for_each"][0]["body"]["entry"] = node("set_id")
    wf["settings"]["execution_graph"]["edges"].append(edge("set_id", node("consume")))
    result = readiness(
        wf, child_workflow([{"name": "payload", "type": "number", "required": True}])
    )
    assert result["valid"], result
    assert not any(
        w["code"] == "invoke.input_mapping_state_member_unchecked" for w in result["warnings"]
    ), result


def body_write_before(wf, name, value, step_id="replace"):
    wf["steps"].insert(-1, {**variable(name, value, step_id), "for_each_id": "orders_loop"})
    previous = wf["for_each"][0]["body"]["entry"]
    wf["for_each"][0]["body"]["entry"] = node(step_id)
    wf["settings"]["execution_graph"]["edges"].append(edge(step_id, previous))
    return wf


@pytest.mark.parametrize("value,kind", [("A", "text"), ({"id": 7}, "object")])
@pytest.mark.parametrize("compatible", [True, False])
def test_current_body_index_type_controls_invoke(value, kind, compatible):
    wf = body_write_before(invoke_workflow({"payload": "state.i"}), "i", value)
    child = child_workflow(
        [{"name": "payload", "type": kind if compatible else "number", "required": True}]
    )
    result = readiness(wf, child)
    assert (
        bool([e for e in result["errors"] if e["code"] == "invoke.input_mapping_type_mismatch"])
        is not compatible
    ), result
    assert result["valid"] is compatible, result


def test_competing_body_index_writes_do_not_restore_numeric_publication():
    wf = body_write_before(invoke_workflow({"payload": "state.i"}), "i", "A")
    body_write_before(wf, "i", {"id": 7}, "earlier")
    types = IOResolver(database(wf)).get_available_type_map_for_step(wf["workflow_id"], "consume")
    assert types["i"] == types["state.i"] == "json", types
    assert readiness(wf, child_workflow([{"name": "payload", "type": "text", "required": True}]))[
        "valid"
    ]


def callable_collision_workflow():
    wf = invoke_workflow({"payload": "state.inputs.id"})
    wf["for_each"][0]["item_name"] = "inputs"
    wf["steps"][0]["override"]["set_variable"]["assignments"]["orders"] = [{"id": 7}, {"id": 8}]
    wf["settings"]["callable"] = {
        "enabled": True,
        "trigger_channels": ["parent_workflow"],
        "expected_inputs": [{"name": "id", "type": "text", "required": True}],
    }
    return wf


def test_callable_input_descendant_is_shadowed_by_loop_publication():
    wf = callable_collision_workflow()
    child = child_workflow([{"name": "payload", "type": "number", "required": True}])
    result = readiness(wf, child)
    assert result["valid"], result
    assert any(
        w["code"] == "invoke.input_mapping_state_member_unchecked" for w in result["warnings"]
    ), result
    db = DraftDB()
    db.workflows[child["workflow_id"]] = child
    checked = service(db).validate(scope=SCOPE, project_id="project", bundle_yaml=as_bundle(wf))
    assert checked["can_save"], checked
    saved = service(db).save(scope=SCOPE, project_id="project", bundle_yaml=as_bundle(wf))
    assert validate_workflow_readiness(db, saved["workflow_id"])["valid"]


@pytest.mark.parametrize("selector", ["b", "state.b"])
@pytest.mark.parametrize("kind,valid", [("number", False), ("array", True)])
def test_bare_whole_chunk_has_same_compatibility_as_state(selector, kind, valid):
    wf = whole_chunk_invoke()
    wf["steps"][2]["override"]["invoke_workflow"]["input_mapping"]["payload"] = selector
    result = readiness(wf, child_workflow([{"name": "payload", "type": kind, "required": True}]))
    assert result["valid"] is valid, result
    assert (
        bool([e for e in result["errors"] if e["code"] == "invoke.input_mapping_type_mismatch"])
        is not valid
    ), result


def entry_rework_workflow():
    template = {
        "step_id": "render",
        "step_kind": "template",
        "group_id": "retry",
        "error_policy": {"on_error": "route_to_step"},
        "override": {
            "template": {
                "text": "{{ size }}",
                "inputs": [{"name": "size", "type": "number", "source": "state.value.size"}],
                "result_target": "state.rendered",
            }
        },
    }
    wf = linear_workflow(
        variable("value", []),
        template,
        {**variable("value", {"size": 7}, "repair"), "group_id": "retry"},
        {
            "step_id": "check",
            "step_kind": "if",
            "group_id": "retry",
            "override": {"if": {"condition": {"selector": "state.rendered", "operator": "truthy"}}},
        },
        workflow_id="rework_shape",
        account_id=SCOPE["account_id"],
    )
    wf["groups"] = [{"group_id": "retry", "body": {"kind": "sequence", "entry": node("render")}}]
    leave = {"kind": "group_exit", "group_id": "retry", "lane_id": None}
    wf["settings"]["execution_graph"] = {
        "schema_version": 1,
        "entry": node("prepare"),
        "edges": [
            edge("prepare", node("retry", "group")),
            edge("render", leave),
            edge("render", node("repair"), "error"),
            edge("repair", node("check")),
            edge("check", leave, "true"),
            edge("check", node("render"), "false", rework=True),
            edge("retry", node("done"), source_kind="group"),
        ],
    }
    return wf


def test_entry_targeted_rework_defers_outside_producer_shape():
    from core.application.workflows.workflow_definition_service import definition_from_workflow
    from core.application.workflows.workflow_definition_validation import (
        validate_workflow_definition,
    )

    wf = entry_rework_workflow()
    assert validate_workflow_definition(definition_from_workflow(wf)) == []
    result = readiness(wf)
    assert not [
        e for e in result["errors"] if e["code"] != "workflow.state_selector_shape_invalid"
    ], result
    assert result["valid"], result


@pytest.mark.parametrize(
    "mapping,expected",
    [("state.inputs.id", "number"), ("message.step.replace.artifacts.inputs.id", "number")],
)
def test_current_body_callable_collision_leaf_and_message_remain_precise(mapping, expected):
    wf = body_write_before(callable_collision_workflow(), "inputs.id", 9)
    wf["steps"][1]["override"]["invoke_workflow"]["input_mapping"]["payload"] = mapping
    result = readiness(
        wf, child_workflow([{"name": "payload", "type": expected, "required": True}])
    )
    assert result["valid"], result
    assert not any(
        w["code"] == "invoke.input_mapping_state_member_unchecked" for w in result["warnings"]
    ), result


@pytest.mark.parametrize("selector,valid", [("o", True), ("state.o", True), ("o.id", False)])
def test_unknown_ordinary_whole_item_does_not_gain_type_from_bare_name(selector, valid):
    result = readiness(
        invoke_workflow({"payload": selector}),
        child_workflow([{"name": "payload", "type": "number", "required": True}]),
    )
    assert result["valid"] is valid, result


def test_unknown_body_index_write_defers_shape_and_compatibility():
    wf = body_write_before(invoke_workflow({"payload": "state.i"}), "i", {"source": "state.o.id"})
    types = IOResolver(database(wf)).get_available_type_map_for_step(wf["workflow_id"], "consume")
    assert types["state.i"] == "json"
    assert readiness(wf, child_workflow([{"name": "payload", "type": "text", "required": True}]))[
        "valid"
    ]
