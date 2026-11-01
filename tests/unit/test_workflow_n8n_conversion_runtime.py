"""Exercise generated configurations with actual target renderers, without provider I/O."""

from __future__ import annotations

import json

import pytest
import yaml

from core.application.orchestration.template_step_runtime import execute_template_step
from tests.unit.test_workflow_n8n_conversion import convert, source


def if_source():
    return {
        "nodes": [
            {
                "name": "Start",
                "type": "n8n-nodes-base.manualTrigger",
                "typeVersion": 1,
                "parameters": {},
            },
            {
                "name": "Fields",
                "type": "n8n-nodes-base.set",
                "typeVersion": 3.4,
                "parameters": {
                    "assignments": {
                        "assignments": [{"name": "tier", "type": "string", "value": "gold"}]
                    },
                    "options": {},
                },
            },
            {
                "name": "Choose",
                "type": "n8n-nodes-base.if",
                "typeVersion": 2.2,
                "parameters": {
                    "conditions": {
                        "options": {"caseSensitive": True, "typeValidation": "strict"},
                        "conditions": [
                            {
                                "leftValue": "={{ $json.tier }}",
                                "rightValue": "gold",
                                "operator": {"type": "string", "operation": "equals"},
                            }
                        ],
                    }
                },
            },
            {"name": "Gold", "type": "n8n-nodes-base.noOp", "typeVersion": 1, "parameters": {}},
            {"name": "Other", "type": "n8n-nodes-base.noOp", "typeVersion": 1, "parameters": {}},
        ],
        "connections": {
            "Start": {"main": [[{"node": "Fields", "type": "main", "index": 0}]]},
            "Fields": {"main": [[{"node": "Choose", "type": "main", "index": 0}]]},
            "Choose": {
                "main": [
                    [{"node": "Gold", "type": "main", "index": 0}],
                    [{"node": "Other", "type": "main", "index": 0}],
                ]
            },
        },
    }


@pytest.mark.parametrize(
    "value,expected",
    [
        ({"results": []}, "object"),
        ([{"url": "example"}], "invalid"),
        (False, "invalid"),
        ("text", "invalid"),
    ],
)
def test_http_response_object_guard_uses_actual_template_runtime(value, expected):
    result = convert(source(), object_response_nodes=[1])
    assert result["status"] == "converted"
    workflow = yaml.safe_load(result["bundle_yaml"])["workflows"][0]
    config = next(
        s["override"]["template"] for s in workflow["steps"] if s["step_kind"] == "template"
    )
    rendered = execute_template_step(
        config, {"n8n": {"n0001": {"response": {"response_body": value}}}}
    )
    assert rendered["ok"] is True and rendered["text"] == expected


@pytest.mark.parametrize(
    "value,expected", [("gold", "string"), (3, "invalid"), (True, "invalid"), ({}, "invalid")]
)
def test_strict_if_input_type_is_guarded_before_comparison(value, expected):
    result = convert(if_source())
    assert result["status"] == "converted"
    workflow = yaml.safe_load(result["bundle_yaml"])["workflows"][0]
    configs = [s["override"]["template"] for s in workflow["steps"] if s["step_kind"] == "template"]
    assert len(configs) == 1
    rendered = execute_template_step(configs[0], {"n8n": {"n0001": {"item": {"tier": value}}}})
    assert rendered["ok"] is True and rendered["text"] == expected
    comparisons = [s for s in workflow["steps"] if s["step_kind"] == "if"]
    assert len(comparisons) == 2
    assert any(
        e["outcome"]["name"] == "false" and e["target"]["id"] == "failed"
        for e in workflow["execution_graph"]["edges"]
    )


@pytest.mark.parametrize("body", ['{"x":NaN}', '{"x":1e999}', '{"x":1,"x":2}'])
def test_ambiguous_or_nonfinite_json_body_is_blocked(body):
    data = source()
    data["nodes"][1]["parameters"]["jsonBody"] = body
    result = convert(data, object_response_nodes=[1])
    assert result["status"] == "blocked" and result["bundle_yaml"] is None


def test_deep_source_data_is_a_safe_blocker():
    data = source()
    value = 1
    for _ in range(300):
        value = [value]
    data["nodes"][1]["parameters"]["jsonBody"] = json.dumps(value)
    result = convert(data, object_response_nodes=[1])
    assert result["status"] == "blocked" and result["bundle_yaml"] is None


@pytest.mark.parametrize("operation", [[], {}, None, 3])
def test_malformed_if_operator_is_a_safe_blocker(operation):
    data = if_source()
    data["nodes"][2]["parameters"]["conditions"]["conditions"][0]["operator"]["operation"] = (
        operation
    )
    result = convert(data)
    assert result["status"] == "blocked" and result["bundle_yaml"] is None


@pytest.mark.parametrize(
    "selector", ["password", "headers.Authorization", "session.Cookie", "api_key"]
)
def test_credential_comparison_literal_is_not_exported(selector):
    data = if_source()
    condition = data["nodes"][2]["parameters"]["conditions"]["conditions"][0]
    condition["leftValue"] = "={{ $json." + selector + " }}"
    condition["rightValue"] = "synthetic-comparison-sentinel"
    result = convert(data)
    assert result["status"] == "blocked" and result["bundle_yaml"] is None
    assert "synthetic-comparison-sentinel" not in json.dumps(result)
