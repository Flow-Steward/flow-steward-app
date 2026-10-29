"""Authoring-time coverage for the canonical workflow state-selector grammar."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

import pytest

from core.application.registry.contract_validation import validate_workflow_readiness
from tests._workflow_definitions import edge, linear_workflow, node


class _WorkflowOnlyDB:
    def __init__(self, workflow: dict[str, Any]) -> None:
        self.workflow = workflow

    def get_workflow(self, workflow_id: str) -> dict[str, Any] | None:
        if workflow_id == self.workflow["workflow_id"]:
            return self.workflow
        return None


def _base_workflow() -> dict[str, Any]:
    return linear_workflow(
        {
            "step_id": "shape",
            "step_kind": "set_variable",
            "override": {
                "set_variable": {
                    "target": "state.output",
                    "value": True,
                }
            },
        },
        workflow_id="selector-contract",
    )


def _step(workflow: dict[str, Any]) -> dict[str, Any]:
    return workflow["steps"][0]


def _case_if(workflow: dict[str, Any]) -> str:
    _step(workflow).update(
        step_kind="if",
        override={
            "if": {
                "condition": "state.rows[abc].sku",
            }
        },
    )
    return "workflow.steps.shape.override.if.condition"


def _case_switch(workflow: dict[str, Any]) -> str:
    _step(workflow).update(
        step_kind="switch",
        override={
            "switch": {
                "selector": "state.rows[abc].sku",
                "cases": [{"case_id": "ready", "when": "ready"}],
            }
        },
    )
    return "workflow.steps.shape.override.switch.selector"


def _case_set_variable(workflow: dict[str, Any]) -> str:
    _step(workflow)["override"]["set_variable"]["value"] = {"source": "state.rows[abc].sku"}
    return "workflow.steps.shape.override.set_variable.value.source"


def _case_collect_source(workflow: dict[str, Any]) -> str:
    _put_shape_in_for_each(workflow, over="state.rows")
    _step(workflow).update(
        step_kind="collect",
        override={"collect": {"target": "state.selected", "source": "state.rows[abc]"}},
    )
    return "workflow.steps.shape.override.collect.source"


def _case_collect_key_source(workflow: dict[str, Any]) -> str:
    _put_shape_in_for_each(workflow, over="state.rows")
    _step(workflow).update(
        step_kind="collect",
        override={
            "collect": {
                "target": "state.by_email",
                "source": "state.row",
                "mode": "set_key",
                "key": {"source": "state.rows[abc].email"},
            }
        },
    )
    return "workflow.steps.shape.override.collect.key.source"


def _case_batch(workflow: dict[str, Any]) -> str:
    _step(workflow).update(
        step_kind="batch",
        override={
            "batch": {
                "source_path": "state.rows[abc]",
                "result_target": "state.batches",
                "batch_size": 10,
            }
        },
    )
    return "workflow.steps.shape.override.batch.source_path"


def _case_http_template(workflow: dict[str, Any]) -> str:
    _step(workflow).update(
        step_kind="http_request",
        override={
            "http_request": {
                "method": "GET",
                "url": "https://example.test/items/{{ state.rows[abc].sku | default:'x' }}",
                "response_mode": "inline",
                "response_target": "state.response",
                "timeout_seconds": 5,
                "expected_statuses": [200],
            }
        },
    )
    return "workflow.steps.shape.override.http_request.url"


def _case_http_bare_template(workflow: dict[str, Any]) -> str:
    path = _case_http_template(workflow)
    _step(workflow)["override"]["http_request"]["url"] = (
        "https://example.test/items/{{ rows[abc].sku | default:'x' }}"
    )
    return path


def _case_if_in(workflow: dict[str, Any]) -> str:
    _step(workflow).update(
        step_kind="if",
        override={
            "if": {
                "condition": {
                    "in": {
                        "left": "state.rows[abc].sku",
                        "right": ["A1"],
                    }
                },
            }
        },
    )
    return "workflow.steps.shape.override.if.condition.in.left"


def _case_if_not_in(workflow: dict[str, Any]) -> str:
    _case_if_in(workflow)
    condition = _step(workflow)["override"]["if"]["condition"]
    condition["not_in"] = condition.pop("in")
    return "workflow.steps.shape.override.if.condition.not_in.left"


def _case_transform(workflow: dict[str, Any]) -> str:
    _step(workflow).update(
        step_kind="transform",
        override={
            "transform": {
                "input": {"source": "state.rows[abc]"},
                "mode": "list",
                "result_target": "state.output",
                "fields": [],
            }
        },
    )
    return "workflow.steps.shape.override.transform.input.source"


def _case_tabular(workflow: dict[str, Any]) -> str:
    _step(workflow).update(
        step_kind="tabular_feed",
        override={
            "tabular_feed": {
                "operation": "read_rows",
                "input": {"dataset_handle": "state.rows[abc].dataset_handle"},
                "result_target": "state.dataset",
            }
        },
    )
    return "workflow.steps.shape.override.tabular_feed.input.dataset_handle"


def _case_connector(workflow: dict[str, Any]) -> str:
    _step(workflow).update(
        step_kind="connector",
        connector_id="example.connector",
        override={
            "connector": {
                "function_name": "fetch",
                "input_mapping": {"sku": "state.rows[abc].sku"},
            }
        },
    )
    return "workflow.steps.shape.override.connector.input_mapping.sku"


def _case_invoke_workflow(workflow: dict[str, Any]) -> str:
    _step(workflow).update(
        step_kind="invoke_workflow",
        override={
            "invoke_workflow": {
                "workflow_id": "child-workflow",
                "input_mapping": {"sku": "state.rows[abc].sku"},
                "output_mapping": {"result": "state.rows[abc].result"},
                "artifact_mapping": {"report": "state.rows[abc].report"},
            }
        },
    )
    return "workflow.steps.shape.override.invoke_workflow.input_mapping.sku"


def _case_agent_unsupported_default_filter(workflow: dict[str, Any]) -> str:
    _step(workflow).update(
        step_kind="agent",
        input_mapping_jsonb={"sku": "state.rows[0].sku | default('x')"},
        override={},
    )
    return "workflow.steps.shape.input_mapping_jsonb.sku"


def _case_invoke_unsupported_default_filter(workflow: dict[str, Any]) -> str:
    _case_invoke_workflow(workflow)
    _step(workflow)["override"]["invoke_workflow"]["input_mapping"] = {
        "sku": "state.rows[0].sku | default('x')"
    }
    return "workflow.steps.shape.override.invoke_workflow.input_mapping.sku"


def _case_dataset_query(workflow: dict[str, Any]) -> str:
    _step(workflow).update(
        step_kind="dataset_query",
        override={
            "dataset_query": {
                "query_template_id": "saved-query",
                "params": {"sku": "state.rows[abc].sku"},
                "result_target": "state.rows[abc].result",
            }
        },
    )
    return "workflow.steps.shape.override.dataset_query.params.sku"


def _case_dataset_unsupported_default_filter(workflow: dict[str, Any]) -> str:
    _case_dataset_query(workflow)
    _step(workflow)["override"]["dataset_query"]["params"] = {
        "sku": "state.rows[0].sku | default('x')"
    }
    return "workflow.steps.shape.override.dataset_query.params.sku"


def _put_shape_in_for_each(workflow: dict[str, Any], *, over: str) -> dict[str, Any]:
    _step(workflow)["for_each_id"] = "per_row"
    item = {
        "for_each_id": "per_row",
        "over": over,
        "item_name": "row",
        "body": {"kind": "sequence", "entry": node("shape")},
    }
    workflow["for_each"] = [item]
    workflow["settings"]["execution_graph"] = {
        "schema_version": 1,
        "entry": node("per_row", "for_each"),
        "edges": [
            edge("shape", {"kind": "for_each_exit", "for_each_id": "per_row", "lane_id": None}),
            edge("per_row", node("done"), source_kind="for_each"),
        ],
    }
    return item


def _case_for_each_over(workflow: dict[str, Any]) -> str:
    _put_shape_in_for_each(workflow, over="state.rows[abc]")
    return "workflow.for_each.per_row.over"


def _case_for_each_boundary_entry(workflow: dict[str, Any]) -> str:
    item = _put_shape_in_for_each(workflow, over="state.rows")
    item["boundary_contract"] = {
        "entry": [
            {
                "name": "sku",
                "type": "text",
                "required": True,
                "source": "state.rows[abc].sku",
            }
        ]
    }
    return "workflow.for_each.per_row.boundary_contract.entry[0].source"


def _case_step_boundary_entry(workflow: dict[str, Any]) -> str:
    _step(workflow)["boundary_contract"] = {
        "entry": [
            {
                "name": "sku",
                "type": "text",
                "required": True,
                "source": "state.rows[abc].sku",
            }
        ]
    }
    return "workflow.steps.shape.boundary_contract.entry[0].source"


@pytest.mark.parametrize(
    "configure",
    [
        _case_if,
        _case_if_in,
        _case_if_not_in,
        _case_switch,
        _case_set_variable,
        _case_collect_source,
        _case_collect_key_source,
        _case_batch,
        _case_http_template,
        _case_http_bare_template,
        _case_transform,
        _case_tabular,
        _case_connector,
        _case_agent_unsupported_default_filter,
        _case_invoke_workflow,
        _case_invoke_unsupported_default_filter,
        _case_dataset_query,
        _case_dataset_unsupported_default_filter,
        _case_for_each_over,
        _case_for_each_boundary_entry,
        _case_step_boundary_entry,
    ],
)
def test_readiness_rejects_malformed_state_selectors_with_exact_paths(configure) -> None:
    workflow = deepcopy(_base_workflow())
    expected_path = configure(workflow)

    validation = validate_workflow_readiness(_WorkflowOnlyDB(workflow), workflow["workflow_id"])

    issues = [
        issue
        for issue in validation["errors"]
        if issue.get("code") == "workflow.state_selector_invalid"
    ]
    assert [issue.get("path") for issue in issues] == [expected_path]
    assert "rows[abc]" in issues[0]["message"] or "default('x')" in issues[0]["message"]
    assert "A1" not in issues[0]["message"]


@pytest.mark.parametrize(
    "selector",
    ["state.rows[0].sku", "state.rows[99].sku", "state.explicit_null"],
)
def test_readiness_accepts_valid_selector_syntax_independent_of_runtime_value(
    selector: str,
) -> None:
    workflow = _base_workflow()
    _step(workflow)["boundary_contract"] = {
        "entry": [{"name": "sku", "type": "text", "required": False, "source": selector}]
    }

    validation = validate_workflow_readiness(_WorkflowOnlyDB(workflow), workflow["workflow_id"])

    assert not any(
        issue.get("code") == "workflow.state_selector_invalid" for issue in validation["errors"]
    )


def test_selector_preflight_rejects_object_member_on_known_batch_array_target() -> None:
    from core.application.orchestration.workflow_state_selector_contract import (
        IMPOSSIBLE_SELECTOR_CODE,
        validate_workflow_state_selectors,
    )

    workflow = linear_workflow(
        {
            "step_id": "set_rows",
            "step_kind": "set_variable",
            "override": {"set_variable": {"target": "state.rows", "value": [{"size": 1}]}},
        },
        {
            "step_id": "make_batches",
            "step_kind": "batch",
            "override": {
                "batch": {
                    "source_path": "state.rows",
                    "result_target": "state.b",
                    "batch_size": 10,
                }
            },
        },
        {
            "step_id": "render",
            "step_kind": "template",
            "override": {
                "template": {
                    "text": "{{ batches }}",
                    "result_target": "state.rendered",
                    "inputs": [{"name": "batches", "type": "array", "source": "state.b.size"}],
                }
            },
        },
        workflow_id="batch-selector-shape",
    )

    issues = [
        issue
        for issue in validate_workflow_state_selectors(workflow)
        if issue.get("code") == IMPOSSIBLE_SELECTOR_CODE
    ]

    assert [issue["path"] for issue in issues] == [
        "workflow.steps.render.override.template.inputs[0].source"
    ]
    assert "array" in issues[0]["message"]


def test_selector_preflight_keeps_batch_indices_and_unknown_item_shape_valid() -> None:
    from core.application.orchestration.workflow_state_selector_contract import (
        IMPOSSIBLE_SELECTOR_CODE,
        validate_workflow_state_selectors,
    )

    workflow = linear_workflow(
        {
            "step_id": "set_rows",
            "step_kind": "set_variable",
            "override": {"set_variable": {"target": "state.rows", "value": [{"size": 1}]}},
        },
        {
            "step_id": "make_batches",
            "step_kind": "batch",
            "override": {
                "batch": {
                    "source_path": "state.rows",
                    "result_target": "state.b",
                    "batch_size": 10,
                }
            },
        },
        {
            "step_id": "render",
            "step_kind": "template",
            "override": {
                "template": {
                    "text": "{{ item }}",
                    "result_target": "state.rendered",
                    "inputs": [{"name": "item", "type": "json", "source": "state.b[0][0].size"}],
                }
            },
        },
        workflow_id="batch-selector-shape",
    )

    issues = [
        issue
        for issue in validate_workflow_state_selectors(workflow)
        if issue.get("code") == IMPOSSIBLE_SELECTOR_CODE
    ]

    assert issues == []


def test_selector_preflight_keeps_batch_paths_unknown_when_source_shape_is_unknown() -> None:
    from core.application.orchestration.workflow_state_selector_contract import (
        IMPOSSIBLE_SELECTOR_CODE,
        validate_workflow_state_selectors,
    )

    workflow = linear_workflow(
        {
            "step_id": "make_batches",
            "step_kind": "batch",
            "override": {
                "batch": {
                    "source_path": "state.dataset_result",
                    "result_target": "state.b",
                    "batch_size": 10,
                }
            },
        },
        {
            "step_id": "render",
            "step_kind": "template",
            "override": {
                "template": {
                    "text": "{{ batches }}",
                    "result_target": "state.rendered",
                    "inputs": [{"name": "batches", "type": "json", "source": "state.b.size"}],
                }
            },
        },
        workflow_id="unknown-batch-selector-shape",
    )

    issues = [
        issue
        for issue in validate_workflow_state_selectors(workflow)
        if issue.get("code") == IMPOSSIBLE_SELECTOR_CODE
    ]

    assert issues == []


def test_selector_preflight_keeps_dataset_batch_descriptor_fields_valid() -> None:
    from core.application.orchestration.workflow_state_selector_contract import (
        IMPOSSIBLE_SELECTOR_CODE,
        validate_workflow_state_selectors,
    )

    workflow = linear_workflow(
        {
            "step_id": "set_dataset",
            "step_kind": "set_variable",
            "override": {
                "set_variable": {
                    # Runtime ignores this fallback when assignments is nonempty.
                    "target": "state.rows",
                    "value": [{"unexpected": "fallback"}],
                    "assignments": {
                        "state.rows": {
                            "dataset_handle": "dataset:dst_selector_contract",
                            "row_count": 12,
                        }
                    },
                }
            },
        },
        {
            "step_id": "make_batches",
            "step_kind": "batch",
            "override": {
                "batch": {
                    "source_path": "state.rows",
                    "result_target": "state.b",
                    "batch_size": 10,
                }
            },
        },
        {
            "step_id": "read_count",
            "step_kind": "template",
            "override": {
                "template": {
                    "text": "{{ count }}",
                    "result_target": "state.count_text",
                    "inputs": [{"name": "count", "type": "number", "source": "state.b.row_count"}],
                }
            },
        },
        workflow_id="dataset-batch-selector-shape",
    )

    issues = [
        issue
        for issue in validate_workflow_state_selectors(workflow)
        if issue.get("code") == IMPOSSIBLE_SELECTOR_CODE
    ]

    assert issues == []


def test_selector_preflight_rejects_member_on_http_artifact_handle_scalar() -> None:
    from core.application.orchestration.workflow_state_selector_contract import (
        IMPOSSIBLE_SELECTOR_CODE,
        validate_workflow_state_selectors,
    )

    workflow = linear_workflow(
        {
            "step_id": "fetch_user",
            "step_kind": "http_request",
            "override": {
                "http_request": {
                    "method": "GET",
                    "url": "https://example.test/user",
                    "response_mode": "artifact",
                    "response_target": "state.user",
                }
            },
        },
        {
            "step_id": "shape_user",
            "step_kind": "transform",
            "override": {
                "transform": {
                    "input": {"source": "state.user.body"},
                    "mode": "scalar",
                    "result_target": "state.shaped",
                }
            },
        },
        workflow_id="http-artifact-selector-shape",
    )

    issues = [
        issue
        for issue in validate_workflow_state_selectors(workflow)
        if issue.get("code") == IMPOSSIBLE_SELECTOR_CODE
    ]

    assert [issue["path"] for issue in issues] == [
        "workflow.steps.shape_user.override.transform.input.source"
    ]
    assert "scalar" in issues[0]["message"]


@pytest.mark.parametrize("selector", ["state.user.name", "state.user[0]"])
def test_selector_preflight_keeps_http_inline_json_descendants_valid(selector: str) -> None:
    from core.application.orchestration.workflow_state_selector_contract import (
        IMPOSSIBLE_SELECTOR_CODE,
        validate_workflow_state_selectors,
    )

    workflow = linear_workflow(
        {
            "step_id": "fetch_user",
            "step_kind": "http_request",
            "override": {
                "http_request": {
                    "method": "GET",
                    "url": "https://example.test/user",
                    "response_mode": "inline",
                    "response_target": "state.user",
                }
            },
        },
        {
            "step_id": "shape_user",
            "step_kind": "transform",
            "override": {
                "transform": {
                    "input": {"source": selector},
                    "mode": "scalar",
                    "result_target": "state.shaped",
                }
            },
        },
        workflow_id="http-inline-json-selector-shape",
    )

    issues = [
        issue
        for issue in validate_workflow_state_selectors(workflow)
        if issue.get("code") == IMPOSSIBLE_SELECTOR_CODE
    ]

    assert issues == []


def test_selector_preflight_keeps_http_default_mode_opaque() -> None:
    from core.application.orchestration.workflow_state_selector_contract import (
        IMPOSSIBLE_SELECTOR_CODE,
        validate_workflow_state_selectors,
    )

    workflow = linear_workflow(
        {
            "step_id": "fetch_user",
            "step_kind": "http_request",
            "override": {
                "http_request": {
                    "method": "GET",
                    "url": "https://example.test/user",
                    "response_target": "state.user",
                }
            },
        },
        {
            "step_id": "shape_user",
            "step_kind": "transform",
            "override": {
                "transform": {
                    "input": {"source": "state.user.body"},
                    "mode": "scalar",
                    "result_target": "state.shaped",
                }
            },
        },
        workflow_id="http-default-selector-shape",
    )

    issues = [
        issue
        for issue in validate_workflow_state_selectors(workflow)
        if issue.get("code") == IMPOSSIBLE_SELECTOR_CODE
    ]

    assert issues == []


def test_selector_preflight_counts_group_incoming_edges_before_using_step_shape() -> None:
    from core.application.orchestration.workflow_state_selector_contract import (
        IMPOSSIBLE_SELECTOR_CODE,
        validate_workflow_state_selectors,
    )

    workflow = linear_workflow(
        {
            "step_id": "fetch_user",
            "step_kind": "http_request",
            "override": {
                "http_request": {
                    "method": "GET",
                    "url": "https://example.test/user",
                    "response_mode": "artifact",
                    "response_target": "state.user",
                }
            },
        },
        {
            "step_id": "shape_user",
            "step_kind": "transform",
            "override": {
                "transform": {
                    "input": {"source": "state.user.body"},
                    "mode": "scalar",
                    "result_target": "state.shaped",
                }
            },
        },
        workflow_id="mixed-predecessor-selector-shape",
    )
    workflow["settings"]["execution_graph"]["edges"].append(
        edge("alternate_group", node("shape_user"), source_kind="group")
    )

    issues = [
        issue
        for issue in validate_workflow_state_selectors(workflow)
        if issue.get("code") == IMPOSSIBLE_SELECTOR_CODE
    ]

    assert issues == []


def test_phase_message_selector_gets_an_explicit_replacement_hint() -> None:
    from core.application.orchestration.workflow_state_selector_contract import (
        PHASE_MESSAGE_SELECTOR_CODE,
        validate_workflow_state_selectors,
    )

    workflow = {
        "steps": [
            {
                "step_id": "write",
                "step_kind": "template",
                "input_mapping_jsonb": {"brief": "message.phase.research.artifacts.brief"},
                "override": {"template": {"text": "{message.phase.research.summary}"}},
            },
            {
                "step_id": "read",
                "input_mapping_jsonb": {"brief": "message.step.write.artifacts.brief"},
            },
        ],
        "groups": [],
    }
    issues = [
        issue
        for issue in validate_workflow_state_selectors(workflow)
        if issue["code"] == PHASE_MESSAGE_SELECTOR_CODE
    ]
    assert {issue["path"] for issue in issues} == {
        "workflow.steps.write.input_mapping_jsonb.brief",
        "workflow.steps.write.override.template.text",
    }
    assert "message.group.<group_id>" in issues[0]["message"]
