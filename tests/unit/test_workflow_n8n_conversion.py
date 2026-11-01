"""A migration preview must preserve behavior or refuse an importable bundle."""

from __future__ import annotations

import json
from copy import deepcopy

import pytest
import yaml

from core.application.workflows.workflow_definition_validation import validate_workflow_definition
from core.application.workflows.workflow_portability import WorkflowPortabilityService
from core.application.workflows.workflow_step_config_contracts import (
    validate_workflow_step_config_keys,
)
from tests.unit.workflow_migration_skill_helpers import convert_n8n


def source():
    return {
        "name": "Provider search",
        "active": True,
        "nodes": [
            {
                "name": "Start",
                "type": "n8n-nodes-base.manualTrigger",
                "typeVersion": 1,
                "parameters": {},
            },
            {
                "name": "Search",
                "type": "n8n-nodes-base.httpRequest",
                "typeVersion": 4.2,
                "parameters": {
                    "url": "https://example.com/search",
                    "method": "POST",
                    "sendBody": True,
                    "specifyBody": "json",
                    "jsonBody": '{"query":"B2B CRM"}',
                    "options": {},
                },
            },
        ],
        "connections": {"Start": {"main": [[{"node": "Search", "type": "main", "index": 0}]]}},
    }


def convert(data, **kwargs):
    return convert_n8n(json.dumps(data), workflow_id="provider_search", **kwargs)


def test_http_preview_is_disabled_importable_and_has_no_structural_issues():
    result = convert(source(), object_response_nodes=[1])
    assert result["status"] == "converted"
    bundle = WorkflowPortabilityService(object()).parse_bundle(result["bundle_yaml"])
    workflow = bundle["workflows"][0]
    assert workflow["enabled"] is False
    assert validate_workflow_step_config_keys(workflow) == []
    assert validate_workflow_definition(workflow) == []
    request = next(s for s in workflow["steps"] if s["step_kind"] == "http_request")
    config = request["override"]["http_request"]
    assert config["method"] == "POST"
    assert config["body_template"] == {"query": "B2B CRM"}
    assert config["response_target"].endswith("response")
    assert result["runtime_verified"] is False


@pytest.mark.parametrize(
    "header",
    ["Authorization", "X-Authorization", "x-authorization", "X-AUTHORIZATION", "x_authorization"],
)
def test_credential_literal_is_replaced_by_required_secret_input_without_echo(header):
    data = source()
    secret = "synthetic-provider-marker"  # pragma: allowlist secret
    data["nodes"][1]["parameters"].update(
        sendHeaders=True,
        headerParameters={"parameters": [{"name": header, "value": "Bearer " + secret}]},
    )
    original = deepcopy(data)
    result = convert(data, object_response_nodes=[1])
    assert result["status"] == "converted"
    assert secret not in json.dumps(result)
    bundle = yaml.safe_load(result["bundle_yaml"])
    fields = bundle["workflows"][0]["settings"]["callable"]["expected_inputs"]
    assert len(fields) == 1 and fields[0]["type"] == "secret" and fields[0]["required"] is True
    assert "default" not in fields[0]
    assert "Bearer {{ state.inputs." in result["bundle_yaml"]
    assert data == original


def test_unverified_http_item_cardinality_blocks_bundle():
    result = convert(source())
    assert result["status"] == "blocked" and result["bundle_yaml"] is None
    assert "n8n.http_object_contract_required" in {i["code"] for i in result["issues"]}


@pytest.mark.parametrize(
    "mutation,code",
    [
        (
            lambda d: d["nodes"][1].update(
                type="n8n-nodes-base.code",
                parameters={"jsCode": "throw new Error('never execute')"},
            ),
            "n8n.node_unsupported",
        ),
        (lambda d: d["nodes"][1].update(typeVersion=99), "n8n.node_version_unsupported"),
        (lambda d: d["nodes"][1].update(retryOnFail=True), "n8n.node_option_unsupported"),
        (
            lambda d: d["nodes"][1]["parameters"]["options"].update(pagination={"mode": "guess"}),
            "n8n.http_option_unsupported",
        ),
        (
            lambda d: d["nodes"][1]["parameters"].update(url="={{ $json.url.toLowerCase() }}"),
            "n8n.expression_unsupported",
        ),
        (
            lambda d: d["nodes"][1].update(
                credentials={"oAuth2Api": {"id": "local-record", "name": "private-name"}}
            ),
            "n8n.credentials_require_mapping",
        ),
        (
            lambda d: d["connections"]["Start"].update(
                ai_languageModel=[[{"node": "Search", "type": "ai_languageModel", "index": 0}]]
            ),
            "n8n.connection_kind_unsupported",
        ),
        (
            lambda d: d["connections"]["Start"]["main"][0].append(
                {"node": "Search", "type": "main", "index": 0}
            ),
            "n8n.fanout_unsupported",
        ),
        (
            lambda d: d["connections"].update(
                Search={"main": [[{"node": "Start", "type": "main", "index": 0}]]}
            ),
            "n8n.graph_unsupported",
        ),
    ],
)
def test_unsupported_semantics_never_produce_an_importable_bundle(mutation, code):
    data = source()
    mutation(data)
    result = convert(data, object_response_nodes=[1])
    assert result["status"] == "blocked" and result["bundle_yaml"] is None
    assert code in {i["code"] for i in result["issues"]}
    assert "private-name" not in json.dumps(result)
    assert "throw new Error" not in json.dumps(result)


def test_set_expression_uses_incoming_item_and_preserves_json_types():
    data = source()
    data["nodes"][1] = {
        "name": "Search",
        "type": "n8n-nodes-base.set",
        "typeVersion": 3.4,
        "parameters": {
            "assignments": {
                "assignments": [
                    {"name": "enabled", "value": True, "type": "boolean"},
                    {"name": "count", "value": 3, "type": "number"},
                ]
            },
            "options": {},
        },
    }
    result = convert(data)
    assert result["status"] == "converted"
    workflow = yaml.safe_load(result["bundle_yaml"])["workflows"][0]
    assignments = [
        s["override"]["set_variable"].get("assignments", {})
        for s in workflow["steps"]
        if s["step_kind"] == "set_variable"
    ]
    assert any(True is v for a in assignments for v in a.values())
    assert any(v == 3 and type(v) is int for a in assignments for v in a.values())
    assert validate_workflow_definition(workflow) == []


@pytest.mark.parametrize(
    "raw", ["[]", "null", "{}", '{"nodes":"wrong"}', "{bad json", '{"nodes":[],"nodes":[]}']
)
def test_invalid_or_ambiguous_exports_are_safe_blocked_results(raw):
    result = convert_n8n(raw, workflow_id="valid")
    assert result["status"] == "blocked" and result["bundle_yaml"] is None
    assert result["issues"]


@pytest.mark.parametrize(
    "mutation",
    [
        lambda d: d["connections"]["Start"]["main"][0][0].update(node=[]),
        lambda d: d["nodes"][1]["parameters"].update(method=[]),
        lambda d: d["nodes"][1]["parameters"].update(url="https://[invalid"),
    ],
)
def test_malformed_nested_source_values_are_blockers_not_exceptions(mutation):
    data = source()
    mutation(data)
    result = convert(data, object_response_nodes=[1])
    assert result["status"] == "blocked" and result["bundle_yaml"] is None


def test_set_credential_literal_is_not_exported_or_echoed():
    data = source()
    data["nodes"][1] = {
        "name": "Search",
        "type": "n8n-nodes-base.set",
        "typeVersion": 3.4,
        "parameters": {
            "assignments": {
                "assignments": [
                    {"name": "api_key", "value": "synthetic-secret-sentinel", "type": "string"}
                ]
            },
            "options": {},
        },
    }  # pragma: allowlist secret
    result = convert(data)
    assert result["status"] == "converted"
    assert "synthetic-secret-sentinel" not in json.dumps(result)
    assert len(result["credential_inputs"]) == 1


def test_sensitive_json_body_field_produces_exactly_one_credential_input():
    data = source()
    data["nodes"][1]["parameters"].update(
        specifyBody="keypair",
        bodyParameters={"parameters": [{"name": "api_key", "value": "synthetic-body-sentinel"}]},
    )
    result = convert(data, object_response_nodes=[1])
    assert result["status"] == "converted"
    assert "synthetic-body-sentinel" not in json.dumps(result)
    assert len(result["credential_inputs"]) == 1
