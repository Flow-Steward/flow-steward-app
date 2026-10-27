from __future__ import annotations

import json

import pytest


def _run_state(steps, *, active=0, **state):
    """A run whose cursor is on ``steps[active]`` of a primed pinned definition."""
    from tests._step_run_state import step_run_state

    named = [{"step_id": f"step_{index + 1}", **step} for index, step in enumerate(steps)]
    others = [step for index, step in enumerate(named) if index != active]
    return step_run_state(named[active], *others, **state)


def test_malformed_input_selector_is_typed_before_transform_writes_state():
    from core.application.orchestration.transform_step_runtime import execute_transform_step

    result = execute_transform_step(
        config={
            "input": {"source": "state.rows[abc]"},
            "mode": "list",
            "result_target": "state.output",
            "fields": [{"from": "sku", "to": "sku", "type": "text"}],
        },
        state={"rows": [{"sku": "A1"}]},
    )

    assert result == {
        "ok": False,
        "errors": [
            {
                "reason": "invalid_input_selector",
                "selector": "state.rows[abc]",
                "path": "override.transform.input.source",
            }
        ],
        "state_patch": {},
        "detail": "invalid_input_selector",
    }


def test_transform_step_object_mode_maps_literals_wildcards_and_action_pipelines():
    from core.application.orchestration.transform_step_runtime import execute_transform_step

    result = execute_transform_step(
        config={
            "input": {"source": "state.raw_post"},
            "mode": "object",
            "result_target": "state.normalized_post",
            "fields": [
                {
                    "from": "comments[0].user.username",
                    "to": "first_comment_username",
                    "type": "text",
                    "actions": [
                        {"action": "text.replace", "params": {"find": "_", "replace_with": " "}},
                        {"action": "text.replace", "params": {"find": "-", "replace_with": " "}},
                        {"action": "text.title_case"},
                    ],
                },
                {
                    "from": "comments[*].user.username",
                    "to": "comment_usernames",
                    "type": "array",
                    "item_type": "text",
                    "actions_each": [
                        {"action": "text.replace", "params": {"find": "_", "replace_with": " "}},
                        {"action": "text.replace", "params": {"find": "-", "replace_with": " "}},
                        {"action": "text.title_case"},
                    ],
                },
                {"value": {"literal": "WH-001"}, "to": "warehouse_id", "type": "text"},
            ],
        },
        state={
            "raw_post": {
                "comments": [
                    {"user": {"username": "john_doe"}},
                    {"user": {"username": "anna-smith"}},
                ]
            }
        },
    )

    assert result["ok"] is True
    assert result["output"] == {
        "first_comment_username": "John Doe",
        "comment_usernames": ["John Doe", "Anna Smith"],
        "warehouse_id": "WH-001",
    }
    assert result["state_patch"]["normalized_post"]["warehouse_id"] == "WH-001"
    assert result["state_patch"]["workflow_data"]["normalized_post"]["comment_usernames"] == [
        "John Doe",
        "Anna Smith",
    ]


def test_transform_step_combines_sources_and_numeric_pipeline_without_formulas():
    from core.application.orchestration.transform_step_runtime import execute_transform_step

    result = execute_transform_step(
        config={
            "input": {"source": "state.supplier_row"},
            "result_target": "state.import_payload",
            "fields": [
                {
                    "from": "first_name",
                    "to": "full_name",
                    "type": "text",
                    "actions": [
                        {"action": "text.append", "params": {"append_value": {"literal": " "}}},
                        {
                            "action": "text.append",
                            "params": {"append_value": {"selector": "last_name", "default": ""}},
                        },
                        {"action": "text.trim"},
                    ],
                },
                {
                    "from": "supplier_stock",
                    "to": "import_stock",
                    "type": "number",
                    "actions": [
                        {"action": "number.multiply", "params": {"factor": 0.1}},
                        {"action": "number.floor"},
                    ],
                },
            ],
        },
        state={
            "supplier_row": {"first_name": "Ada", "last_name": "Lovelace", "supplier_stock": 137}
        },
    )

    assert result["ok"] is True
    assert result["output"] == {"full_name": "Ada Lovelace", "import_stock": 13}


def test_transform_step_list_mode_filters_and_merges_input_rows():
    from core.application.orchestration.transform_step_runtime import execute_transform_step

    result = execute_transform_step(
        config={
            "input": {"source": "state.rows"},
            "mode": "list",
            "result_target": "state.active_rows",
            "output_mode": "merge_with_input",
            "row_filter": {"selector": "status", "operator": "equals", "value": "active"},
            "fields": [
                {
                    "from": "product_name",
                    "to": "title",
                    "type": "text",
                    "actions": [
                        {"action": "text.replace", "params": {"find": "_", "replace_with": " "}},
                        {"action": "text.title_case"},
                    ],
                },
                {"from": "missing_optional", "to": "omitted", "type": "text"},
                {"from": "missing_default", "to": "explicit_null", "type": "json", "default": None},
            ],
        },
        state={
            "rows": [
                {"sku": "A-1", "product_name": "sony_headphones", "status": "active"},
                {"sku": "B-2", "product_name": "skip_me", "status": "inactive"},
            ]
        },
    )

    assert result["ok"] is True
    assert result["output"] == [
        {
            "sku": "A-1",
            "product_name": "sony_headphones",
            "status": "active",
            "title": "Sony Headphones",
            "explicit_null": None,
        }
    ]
    assert "omitted" not in result["output"][0]


def test_transform_step_standard_action_families_happy_path():
    from core.application.orchestration.transform_step_runtime import execute_transform_step

    result = execute_transform_step(
        config={
            "input": {"source": "state.payload"},
            "result_target": "state.mapped",
            "fields": [
                {
                    "from": "name",
                    "to": "name_length",
                    "type": "number",
                    "actions": [{"action": "text.trim"}, {"action": "text.length"}],
                },
                {
                    "from": "score",
                    "to": "score_adjusted",
                    "type": "number",
                    "actions": [
                        {"action": "number.coerce"},
                        {"action": "number.add", "params": {"operand": 1}},
                        {"action": "number.subtract", "params": {"operand": 0.5}},
                        {"action": "number.divide", "params": {"divisor": 2}},
                        {"action": "number.round", "params": {"decimals": 1}},
                    ],
                },
                {
                    "from": "flag",
                    "to": "flag",
                    "type": "boolean",
                    "actions": [{"action": "boolean.coerce"}],
                },
                {
                    "from": "tags",
                    "to": "tag_count",
                    "type": "number",
                    "actions": [{"action": "array.length"}],
                },
                {
                    "from": "tags",
                    "to": "first_tag",
                    "type": "json",
                    "actions": [{"action": "array.first", "params": {"default": "none"}}],
                },
                {
                    "from": "tags",
                    "to": "second_tag",
                    "type": "json",
                    "actions": [{"action": "array.at", "params": {"index": 1}}],
                },
                {
                    "from": "tags",
                    "to": "compact_tags",
                    "type": "array",
                    "actions": [{"action": "array.compact"}],
                },
                {
                    "from": "tags",
                    "to": "joined_tags",
                    "type": "text",
                    "actions": [{"action": "array.join_text", "params": {"separator": "|"}}],
                },
                {
                    "from": "meta",
                    "to": "picked",
                    "type": "json",
                    "actions": [{"action": "json.pick", "params": {"path": "external-id"}}],
                },
                {
                    "from": "meta",
                    "to": "without_secret",
                    "type": "object",
                    "actions": [{"action": "json.omit", "params": {"paths": ["secret"]}}],
                },
                {
                    "from": "meta",
                    "to": "external_id",
                    "type": "number",
                    "actions": [
                        {"action": "json.get", "params": {"path": "external-id"}},
                        {"action": "number.coerce"},
                    ],
                },
                {
                    "from": "meta",
                    "to": "with_status",
                    "type": "object",
                    "actions": [
                        {
                            "action": "json.set",
                            "params": {"path": "status", "new_value": {"literal": "active"}},
                        }
                    ],
                },
                {
                    "from": "meta",
                    "to": "meta_keys",
                    "type": "array",
                    "actions": [{"action": "json.keys"}],
                },
                {
                    "from": "meta",
                    "to": "meta_values",
                    "type": "array",
                    "actions": [{"action": "json.values"}],
                },
                {
                    "from": "meta",
                    "to": "meta_json",
                    "type": "text",
                    "actions": [{"action": "json.stringify"}],
                },
                {
                    "from": "status_code",
                    "to": "status_label",
                    "type": "json",
                    "actions": [
                        {
                            "action": "map.values",
                            "params": {"mapping": {"A": "Active"}, "keep_unmapped": False},
                        }
                    ],
                },
            ],
        },
        state={
            "payload": {
                "name": "  ada  ",
                "score": "4",
                "flag": "yes",
                "tags": ["one", None, "three"],
                "meta": {"external-id": "42", "secret": "hide"},
                "status_code": "A",
            }
        },
    )

    assert result["ok"] is True
    assert result["output"]["name_length"] == 3
    # 4 + 1 - 0.5 = 4.5, / 2 = 2.25, rounded to 1 decimal. The action declares
    # mode half_up, so the tie goes away from zero: 2.3. This read 2.2 while the
    # declared mode was ignored and Python's half-to-even round() was used.
    assert result["output"]["score_adjusted"] == 2.3
    assert result["output"]["flag"] is True
    assert result["output"]["tag_count"] == 3
    assert result["output"]["first_tag"] == "one"
    assert result["output"]["second_tag"] is None
    assert result["output"]["compact_tags"] == ["one", "three"]
    assert result["output"]["joined_tags"] == "one||three"
    assert result["output"]["picked"] == "42"
    assert result["output"]["without_secret"] == {"external-id": "42"}
    assert result["output"]["external_id"] == 42
    assert result["output"]["with_status"] == {
        "external-id": "42",
        "secret": "hide",
        "status": "active",
    }
    assert result["output"]["meta_keys"] == ["external-id", "secret"]
    assert result["output"]["meta_values"] == ["42", "hide"]
    assert result["output"]["meta_json"] == '{"external-id":"42","secret":"hide"}'
    assert result["output"]["status_label"] == "Active"


def test_transform_step_rejects_workflow_state_selector_params():
    from core.application.orchestration.transform_step_runtime import validate_transform_config

    errors = validate_transform_config(
        {
            "input": {"source": "state.payload"},
            "result_target": "state.mapped",
            "fields": [
                {
                    "from": "first_name",
                    "to": "full_name",
                    "type": "text",
                    "actions": [
                        {
                            "action": "text.append",
                            "params": {
                                "append_value": {"selector": "state.inputs.customer.last_name"}
                            },
                        }
                    ],
                }
            ],
        }
    )

    assert [error["code"] for error in errors] == [
        "workflow.step_transform_action_param_selector_scope_invalid"
    ]


def test_transform_step_rejects_malformed_result_target_path():
    from core.application.orchestration.transform_step_runtime import validate_transform_config

    errors = validate_transform_config(
        {
            "input": {"source": "state.payload"},
            "result_target": "state.outputs..normalized",
            "fields": [{"from": "name", "to": "name", "type": "text"}],
        }
    )

    assert [error["code"] for error in errors] == ["workflow.step_transform_result_target_invalid"]


def test_transform_step_row_filter_failure_is_structured_and_writes_no_state():
    from core.application.orchestration.transform_step_runtime import execute_transform_step

    result = execute_transform_step(
        config={
            "input": {"source": "state.rows"},
            "mode": "list",
            "result_target": "state.filtered",
            "row_filter": {"selector": "price", "operator": "gt", "value": 10},
            "fields": [{"from": "sku", "to": "sku", "type": "text"}],
        },
        state={"rows": [{"sku": "A1", "price": "not-a-number"}]},
    )

    assert result["ok"] is False
    assert result["state_patch"] == {}
    assert result["errors"][0]["reason"] == "row_filter_failed"
    assert result["errors"][0]["element_index"] == 0


def test_transform_step_guard_limits_reject_too_many_fields_and_actions(monkeypatch):
    from core.application.orchestration.transform_step_runtime import validate_transform_config

    monkeypatch.setenv("FS_TRANSFORM_MAX_FIELDS", "1")
    monkeypatch.setenv("FS_TRANSFORM_MAX_ACTIONS_PER_FIELD", "1")

    errors = validate_transform_config(
        {
            "input": {"source": "state.payload"},
            "result_target": "state.mapped",
            "fields": [
                {
                    "from": "name",
                    "to": "name",
                    "type": "text",
                    "actions": [{"action": "text.trim"}, {"action": "text.title_case"}],
                },
                {"from": "email", "to": "email", "type": "text"},
            ],
        }
    )

    codes = [error["code"] for error in errors]
    assert "workflow.step_transform_fields_too_many" in codes
    assert "workflow.step_transform_actions_too_many" in codes


def test_transform_step_action_param_resolution_failure_is_structured():
    from core.application.orchestration.transform_step_runtime import execute_transform_step

    result = execute_transform_step(
        config={
            "input": {"source": "state.payload"},
            "result_target": "state.mapped",
            "fields": [
                {
                    "from": "first_name",
                    "to": "full_name",
                    "type": "text",
                    "actions": [
                        {
                            "action": "text.append",
                            "params": {"append_value": {"selector": "missing.last_name"}},
                        }
                    ],
                }
            ],
        },
        state={"payload": {"first_name": "Ada"}},
    )

    assert result["ok"] is False
    assert result["state_patch"] == {}
    assert result["errors"][0]["reason"] == "action_param_resolution_failed"
    assert result["errors"][0]["action"] == "text.append"


def test_transform_step_action_execution_failure_is_structured():
    from core.application.orchestration.transform_step_runtime import execute_transform_step

    result = execute_transform_step(
        config={
            "input": {"source": "state.payload"},
            "result_target": "state.mapped",
            "fields": [
                {
                    "from": "price",
                    "to": "price",
                    "type": "number",
                    "actions": [
                        {"action": "number.coerce"},
                        {"action": "number.divide", "params": {"divisor": 0}},
                    ],
                }
            ],
        },
        state={"payload": {"price": "12"}},
    )

    assert result["ok"] is False
    assert result["state_patch"] == {}
    assert result["errors"][0]["reason"] == "action_execution_failed"
    assert result["errors"][0]["action"] == "number.divide"


@pytest.mark.parametrize("field_type", ["object", "array"])
def test_transform_step_rejects_generic_json_pipeline_for_declared_shape(field_type):
    from core.application.orchestration.transform_step_runtime import validate_transform_config

    errors = validate_transform_config(
        {
            "input": {"source": "state.payload"},
            "result_target": "state.mapped",
            "fields": [
                {
                    "from": "meta",
                    "to": "bad_shape",
                    "type": field_type,
                    "actions": [{"action": "json.get", "params": {"path": "external-id"}}],
                }
            ],
        }
    )

    assert [error["code"] for error in errors] == ["workflow.step_transform_final_type_mismatch"]


@pytest.mark.parametrize(
    ("field_type", "value", "path", "expected_detail"),
    [
        ("object", {"external-id": "42"}, "external-id", "str is not an object"),
        ("array", {"external-id": "42"}, "external-id", "str is not an array"),
        ("array", {"nested": {"external-id": "42"}}, "nested", "dict is not an array"),
    ],
)
def test_transform_step_action_pipeline_enforces_declared_shape_at_runtime(
    field_type, value, path, expected_detail
):
    from core.application.orchestration.transform_step_runtime import (
        TransformRuntimeFailure,
        _execute_action_pipeline,
    )

    with pytest.raises(TransformRuntimeFailure) as exc_info:
        _execute_action_pipeline(
            value,
            [{"action": "json.get", "params": {"path": path}}],
            field_index=0,
            field_to="bad_shape",
            selector="meta",
            field_type=field_type,
            selector_root=value,
            element_index=None,
        )

    assert exc_info.value.errors[0]["reason"] == "field_type_mismatch"
    assert exc_info.value.errors[0]["detail"] == expected_detail


def test_transform_step_list_mode_failure_writes_no_partial_state_patch():
    from core.application.orchestration.transform_step_runtime import execute_transform_step

    result = execute_transform_step(
        config={
            "input": {"source": "state.rows"},
            "mode": "list",
            "result_target": "state.normalized_rows",
            "fields": [
                {"from": "sku", "to": "sku", "type": "text", "required": True},
                {"from": "stock", "to": "stock", "type": "number", "required": True},
            ],
        },
        state={"rows": [{"sku": "A-1", "stock": 5}, {"sku": "B-2"}]},
    )

    assert result["ok"] is False
    assert result["state_patch"] == {}
    assert result["errors"][0]["field_to"] == "stock"
    assert result["errors"][0]["element_index"] == 1


@pytest.mark.parametrize(
    "selector",
    [
        "",
        "comments[-1]",
        "orders[*].items[*].sku",
        "attributes[?key=color].value",
        "name.upper()",
        "name.",
    ],
)
def test_transform_selector_rejects_out_of_scope_v1_syntax(selector):
    from core.application.orchestration.transform_step_runtime import parse_transform_selector

    with pytest.raises(ValueError):
        parse_transform_selector(selector)


def test_transform_selector_supports_quoted_keys_and_indexes():
    from core.application.orchestration.transform_step_runtime import execute_transform_step

    result = execute_transform_step(
        config={
            "input": {"source": "state.payload"},
            "result_target": "state.out",
            "fields": [
                {
                    "from": 'attributes["external-id"]',
                    "to": "external_id",
                    "type": "text",
                },
                {"from": "images[0].url", "to": "first_image_url", "type": "text"},
            ],
        },
        state={
            "payload": {
                "attributes": {"external-id": "sku-1"},
                "images": [{"url": "https://example.test/1.png"}],
            }
        },
    )

    assert result["ok"] is True
    assert result["output"] == {
        "external_id": "sku-1",
        "first_image_url": "https://example.test/1.png",
    }


def test_transform_step_validation_rejects_ambiguous_and_out_of_scope_config():
    from core.application.orchestration.transform_step_runtime import validate_transform_config

    errors = validate_transform_config(
        {
            "input": {"source": "state.rows"},
            "result_target": "state.out",
            "fields": [
                {
                    "from": "orders[*].items[*].sku",
                    "value": {"literal": "x"},
                    "to": "sku",
                    "type": "text",
                    "actions": [{"action": "text.trim"}],
                    "actions_each": [{"action": "text.trim"}],
                },
                {
                    "value": {"literal": "WH-001"},
                    "to": "warehouse",
                    "type": "text",
                    "default": "",
                },
                {
                    "from": "name",
                    "to": "name",
                    "type": "text",
                    "actions": [
                        {
                            "action": "text.replace",
                            "params": {"find": "_", "replace_with": " ", "regex": True},
                        }
                    ],
                },
            ],
        }
    )

    codes = {error["code"] for error in errors}
    assert "workflow.step_transform_field_source_invalid" in codes
    assert "workflow.step_transform_selector_invalid" in codes
    assert "workflow.step_transform_actions_conflict" in codes
    assert "workflow.step_transform_literal_default_required_invalid" in codes
    assert "workflow.step_transform_action_param_unknown" in codes


def test_transform_step_guard_failure_is_structured_and_writes_no_state(monkeypatch):
    from core.application.orchestration.transform_step_runtime import execute_transform_step

    monkeypatch.setenv("FS_TRANSFORM_MAX_INPUT_BYTES", "12")
    result = execute_transform_step(
        config={
            "input": {"source": "state.payload"},
            "result_target": "state.out",
            "fields": [{"from": "name", "to": "name", "type": "text"}],
        },
        state={"payload": {"name": "this payload is intentionally too large"}},
    )

    assert result["ok"] is False
    assert result["state_patch"] == {}
    assert result["errors"] == [{"reason": "input_too_large", "selector": "state.payload"}]


def test_transform_action_catalog_exposes_only_transform_safe_pure_actions():
    from core.application.orchestration.transform_step_runtime import (
        list_transform_action_contracts,
    )

    actions = {row["action_id"]: row for row in list_transform_action_contracts()}

    for action_id in (
        "text.title_case",
        "text.append",
        "number.multiply",
        "number.floor",
        "array.join_text",
        "json.set",
        "map.values",
    ):
        assert action_id in actions
        assert actions[action_id]["transform_safe"] is True
        assert actions[action_id]["pure"] is True
        assert actions[action_id]["deterministic"] is True
        assert actions[action_id]["io_allowed"] is False


def test_transform_money_and_text_pad_keep_shared_catalog_semantics():
    from core.application.orchestration.transform_step_runtime import execute_transform_step

    result = execute_transform_step(
        config={
            "input": {"source": "state.payload"},
            "result_target": "state.out",
            "fields": [
                {
                    "from": "amount",
                    "to": "amount",
                    "type": "money",
                    "actions": [
                        {"action": "money.coerce"},
                        {"action": "money.multiply", "params": {"factor": {"literal": "1.21"}}},
                        {"action": "money.round", "params": {"decimals": 2}},
                    ],
                },
                {
                    "from": "code",
                    "to": "code",
                    "type": "text",
                    "actions": [
                        {
                            "action": "text.pad",
                            "params": {"width": 4, "fill_char": "0", "side": "left"},
                        }
                    ],
                },
            ],
        },
        state={"payload": {"amount": "19.99", "code": "7"}},
    )

    assert result["ok"] is True
    assert result["output"] == {"amount": "24.19", "code": "0007"}


def test_execute_step_node_routes_transform_and_writes_result_target():
    from core.application.orchestration.graph.nodes import execute_step_node

    update = execute_step_node(
        _run_state(
            [
                {
                    "step_id": "normalize_name",
                    "step_kind": "transform",
                    "override": {
                        "transform": {
                            "input": {"source": "state.raw"},
                            "result_target": "state.outputs.normalized",
                            "fields": [
                                {
                                    "from": "first_name",
                                    "to": "full_name",
                                    "type": "text",
                                    "actions": [
                                        {
                                            "action": "text.append",
                                            "params": {"append_value": {"literal": " "}},
                                        },
                                        {
                                            "action": "text.append",
                                            "params": {"append_value": {"selector": "last_name"}},
                                        },
                                        {"action": "text.trim"},
                                    ],
                                }
                            ],
                        }
                    },
                }
            ],
            agent_messages=[],
            raw={"first_name": "Ada", "last_name": "Lovelace"},
        )
    )

    assert update["outputs"]["normalized"] == {"full_name": "Ada Lovelace"}
    message = update["agent_messages"][-1]
    assert message["from_agent"] == "transform_step"
    assert message["status"] == "pass"


def test_step_debugger_shows_transform_inputs_and_result():
    from core.application.orchestration.graph.nodes import execute_step_node
    from core.application.reports.ui_projection_helpers import _message_step_debug_fields

    update = execute_step_node(
        _run_state(
            [
                {
                    "step_id": "normalize_name",
                    "step_kind": "transform",
                    "override": {
                        "transform": {
                            "input": {"source": "state.raw"},
                            "result_target": "state.outputs.normalized",
                            "fields": [{"from": "first_name", "to": "full_name", "type": "text"}],
                        }
                    },
                }
            ],
            agent_messages=[],
            raw={"first_name": "Ada"},
        )
    )

    message = update["agent_messages"][-1]
    rules = {"fields": [{"from": "first_name", "to": "full_name", "type": "text"}]}
    assert message["artifacts"]["input_payload"] == {
        "source": "state.raw",
        "value": {"first_name": "Ada"},
        "rules": rules,
    }
    debug = _message_step_debug_fields(message)
    assert debug["inputs"] == {
        "source": "state.raw",
        "value": {"first_name": "Ada"},
        "rules": rules,
    }
    assert debug["outputs"]["value"] == {"full_name": "Ada"}


def test_execute_step_node_keeps_large_transform_result_out_of_agent_message():
    from core.application.orchestration.graph.nodes import execute_step_node

    rows = [{"sku": f"SKU-{index:04d}", "qty": str(index)} for index in range(1000)]
    expected = [{"sku": row["sku"], "qty": row["qty"]} for row in rows]
    update = execute_step_node(
        _run_state(
            [
                {
                    "step_id": "normalize_rows",
                    "step_kind": "transform",
                    "override": {
                        "transform": {
                            "input": {"source": "state.rows"},
                            "mode": "list",
                            "result_target": "state.normalized_rows",
                            "fields": [
                                {"from": "sku", "to": "sku", "type": "text"},
                                {"from": "qty", "to": "qty", "type": "text"},
                            ],
                        }
                    },
                }
            ],
            agent_messages=[],
            rows=rows,
            workflow_data={},
        )
    )

    assert update["normalized_rows"] == expected
    assert update["workflow_data"]["normalized_rows"] == expected
    message = update["agent_messages"][-1]
    assert len(json.dumps(message, separators=(",", ":"))) <= 4096
    assert message["artifacts"]["transform"]["result_target"] == "state.normalized_rows"
    assert "output" not in message["artifacts"]["transform"]
    assert "state_patch" not in message["artifacts"]["transform"]
    assert "_state_patch" not in message["artifacts"]
    # The debugger still explains the run: the first rows and the full count.
    input_payload = message["artifacts"]["input_payload"]
    assert input_payload["source"] == "state.rows"
    assert input_payload["value"][0] == {"sku": "SKU-0000", "qty": "0"}
    assert "(1000 in total)" in json.dumps(input_payload)
    output_payload = message["artifacts"]["output_payload"]
    assert output_payload["result_target"] == "state.normalized_rows"
    assert "(1000 in total)" in json.dumps(output_payload)


def test_execute_step_node_reports_transform_runtime_failure_without_raising():
    from core.application.orchestration.graph.nodes import execute_step_node

    update = execute_step_node(
        _run_state(
            [
                {
                    "step_id": "filter_rows",
                    "step_kind": "transform",
                    "override": {
                        "transform": {
                            "input": {"source": "state.rows"},
                            "mode": "list",
                            "result_target": "state.outputs.filtered",
                            "row_filter": {
                                "selector": "price",
                                "operator": "gt",
                                "value": 10,
                            },
                            "fields": [{"from": "sku", "to": "sku", "type": "text"}],
                        }
                    },
                }
            ],
            agent_messages=[],
            rows=[{"sku": "A1", "price": "not-a-number"}],
        )
    )

    message = update["agent_messages"][-1]
    assert message["from_agent"] == "transform_step"
    assert message["status"] == "fail"
    assert message["artifacts"]["transform"]["errors"][0]["reason"] == "row_filter_failed"
    assert "_state_patch" not in message["artifacts"]


def test_transform_object_mode_failure_identifies_safe_input_selector():
    from core.application.orchestration.transform_step_runtime import execute_transform_step

    result = execute_transform_step(
        config={
            "input": {"source": "state.raw_post"},
            "mode": "object",
            "result_target": "state.normalized_post",
            "fields": [{"from": "name", "to": "name", "type": "text"}],
        },
        state={"raw_post": "not-an-object"},
    )

    assert result == {
        "ok": False,
        "errors": [
            {
                "reason": "input_item_not_object",
                "selector": "state.raw_post",
            }
        ],
        "state_patch": {},
        "detail": "runtime_failed",
    }
