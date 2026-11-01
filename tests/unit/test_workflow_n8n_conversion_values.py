"""Source expressions and credential handling must not change JSON meaning."""

from __future__ import annotations

import json

import pytest
import yaml

from tests.unit.test_workflow_n8n_conversion import convert, source


def test_static_url_expression_still_extracts_query_credentials():
    data = source()
    data["nodes"][1]["parameters"]["url"] = (
        '={{ "https://example.test/?api_key=synthetic-url-sentinel" }}'
    )
    result = convert(data, object_response_nodes=[1])
    assert result["status"] == "converted"
    assert "synthetic-url-sentinel" not in json.dumps(result)
    assert len(result["credential_inputs"]) == 1


def test_static_url_expression_does_not_bypass_userinfo_refusal():
    data = source()
    data["nodes"][1]["parameters"]["url"] = (
        '={{ "https://example:synthetic-userinfo@example.test/" }}'  # pragma: allowlist secret -- synthetic URL rejection fixture
    )
    result = convert(data, object_response_nodes=[1])
    assert result["status"] == "blocked" and result["bundle_yaml"] is None
    assert "synthetic-userinfo" not in json.dumps(result)


@pytest.mark.parametrize("value", [3, False, {"company": "B2B"}, [1, 2]])
def test_body_whole_source_reference_preserves_json_type_in_target_renderer(value):
    from core.application.orchestration.http_secret_boundary import render_http_value

    data = source()
    data["nodes"].insert(
        1,
        {
            "name": "Fields",
            "type": "n8n-nodes-base.set",
            "typeVersion": 3.4,
            "parameters": {
                "assignments": {"assignments": [{"name": "count", "type": "number", "value": 3}]},
                "options": {},
            },
        },
    )
    data["connections"] = {
        "Start": {"main": [[{"node": "Fields", "type": "main", "index": 0}]]},
        "Fields": {"main": [[{"node": "Search", "type": "main", "index": 0}]]},
    }
    data["nodes"][2]["parameters"].update(
        specifyBody="keypair",
        bodyParameters={"parameters": [{"name": "value", "value": "={{ $json.count }}"}]},
    )
    result = convert(data, object_response_nodes=[2])
    assert result["status"] == "converted"
    workflow = yaml.safe_load(result["bundle_yaml"])["workflows"][0]
    config = next(
        s["override"]["http_request"] for s in workflow["steps"] if s["step_kind"] == "http_request"
    )
    rendered = render_http_value(
        config["body_template"], {"n8n": {"n0001": {"item": {"count": value}}}}
    )
    assert rendered["value"] == value and type(rendered["value"]) is type(value)


def test_whitespace_nested_set_expression_is_not_mistaken_for_literal_data():
    data = source()
    data["nodes"][1] = {
        "name": "Search",
        "type": "n8n-nodes-base.set",
        "typeVersion": 3.4,
        "parameters": {
            "assignments": {
                "assignments": [
                    {"name": "payload", "type": "object", "value": {"nested": "= {{ $json.foo }}"}}
                ]
            },
            "options": {},
        },
    }
    result = convert(data)
    assert result["status"] == "blocked" and result["bundle_yaml"] is None


@pytest.mark.parametrize(
    "type_name,value",
    [("unknown", 3), ([], 3), ("string", 3), ("number", True), ("string", "={{ $json.name }}")],
)
def test_unqualified_set_type_or_coercion_semantics_block(type_name, value):
    data = source()
    data["nodes"][1] = {
        "name": "Search",
        "type": "n8n-nodes-base.set",
        "typeVersion": 3.4,
        "parameters": {
            "assignments": {"assignments": [{"name": "value", "type": type_name, "value": value}]},
            "options": {},
        },
    }
    result = convert(data)
    assert result["status"] == "blocked" and result["bundle_yaml"] is None


def test_literal_json_body_does_not_interpret_nested_n8n_expressions():
    data = source()
    data["nodes"][1]["parameters"]["jsonBody"] = json.dumps({"value": "={{ $json.foo }}"})
    result = convert(data, object_response_nodes=[1])
    assert result["status"] == "blocked" and result["bundle_yaml"] is None


def test_plain_equals_prefixed_string_in_literal_json_body_is_preserved():
    data = source()
    data["nodes"][1]["parameters"]["jsonBody"] = json.dumps({"value": "=literal"})
    result = convert(data, object_response_nodes=[1])
    assert result["status"] == "converted"
    workflow = yaml.safe_load(result["bundle_yaml"])["workflows"][0]
    config = next(
        s["override"]["http_request"] for s in workflow["steps"] if s["step_kind"] == "http_request"
    )
    assert config["body_template"] == {"value": "=literal"}


def test_official_n8n_http_node_branches_produce_structurally_valid_bundles():
    from core.application.workflows.workflow_definition_validation import (
        validate_workflow_definition,
    )
    from core.application.workflows.workflow_portability import WorkflowPortabilityService
    from tests.unit.workflow_migration_skill_helpers import SKILL_ROOT

    fixture = SKILL_ROOT / "assets/source_examples/n8n_http_branches.json"
    cases = json.loads(fixture.read_text())
    assert len(cases) == 6
    for case in cases:
        # DummyJSON's documented todo endpoints return objects. No calls are made.
        result = convert(case["workflow"], object_response_nodes=[1])
        assert result["status"] == "converted", result["issues"]
        workflow = WorkflowPortabilityService(object()).parse_bundle(result["bundle_yaml"])[
            "workflows"
        ][0]
        assert validate_workflow_definition(workflow) == []
        assert "public-fixture-placeholder" not in json.dumps(result)


@pytest.mark.parametrize("body", ['"literal"', "true", "null", "3"])
def test_json_body_scalars_are_not_reencoded_as_raw_text(body):
    data = source()
    data["nodes"][1]["parameters"]["jsonBody"] = body
    result = convert(data, object_response_nodes=[1])
    assert result["status"] == "blocked" and result["bundle_yaml"] is None


@pytest.mark.parametrize(
    "header",
    [
        False,
        "Authorization",
        "x-api-key",
        "X-Authorization",
        "x-authorization",
        "X-AUTHORIZATION",
        "x_authorization",
    ],
)
def test_credential_selector_from_set_keeps_one_input_and_original_wiring(header):
    data = source()
    data["nodes"].insert(
        1,
        {
            "name": "Fields",
            "type": "n8n-nodes-base.set",
            "typeVersion": 3.4,
            "parameters": {
                "assignments": {
                    "assignments": [
                        {"name": "api_key", "type": "string", "value": "synthetic-wiring-sentinel"}
                    ]
                },
                "options": {},
            },
        },
    )  # pragma: allowlist secret
    data["connections"] = {
        "Start": {"main": [[{"node": "Fields", "type": "main", "index": 0}]]},
        "Fields": {"main": [[{"node": "Search", "type": "main", "index": 0}]]},
    }
    params = data["nodes"][2]["parameters"]
    if header:
        params.update(
            sendHeaders=True,
            headerParameters={"parameters": [{"name": header, "value": "={{ $json.api_key }}"}]},
        )
    else:
        params.update(
            specifyBody="keypair",
            bodyParameters={"parameters": [{"name": "api_key", "value": "={{ $json.api_key }}"}]},
        )
    result = convert(data, object_response_nodes=[2])
    assert result["status"] == "converted", result["issues"]
    assert len(result["credential_inputs"]) == 1
    assert "synthetic-wiring-sentinel" not in json.dumps(result)
    assert "state.n8n.n0001.item.api_key" in result["bundle_yaml"]


@pytest.mark.parametrize(
    "header",
    [
        False,
        "Authorization",
        "x-api-key",
        "X-Authorization",
        "x-authorization",
        "X-AUTHORIZATION",
        "x_authorization",
    ],
)
def test_static_sensitive_expression_is_extracted_after_translation(header):
    data = source()
    params = data["nodes"][1]["parameters"]
    if header:
        params.update(
            sendHeaders=True,
            headerParameters={
                "parameters": [
                    {
                        "name": header,
                        "value": '={{ "Bearer synthetic-expression-sentinel" }}',
                    }
                ]
            },
        )
    else:
        params.update(
            sendQuery=True,
            queryParameters={
                "parameters": [
                    {"name": "api_key", "value": '={{ "synthetic-expression-sentinel" }}'}
                ]
            },
        )
    result = convert(data, object_response_nodes=[1])
    assert result["status"] == "converted", result["issues"]
    assert "synthetic-expression-sentinel" not in json.dumps(result)
    assert len(result["credential_inputs"]) == 1


@pytest.mark.parametrize(
    "name", ["headers.Authorization", "session.Cookie", "proxy.Proxy-Authorization"]
)
def test_dotted_authentication_fields_do_not_expose_the_literal(name):
    data = source()
    data["nodes"][1] = {
        "name": "Search",
        "type": "n8n-nodes-base.set",
        "typeVersion": 3.4,
        "parameters": {
            "assignments": {
                "assignments": [
                    {"name": name, "type": "string", "value": "synthetic-dotted-sentinel"}
                ]
            },
            "options": {},
        },
    }
    result = convert(data)
    # Unsupported field spellings can block; a converted artifact must extract the value.
    assert "synthetic-dotted-sentinel" not in json.dumps(result)
    if result["status"] == "converted":
        assert len(result["credential_inputs"]) == 1
    else:
        assert result["bundle_yaml"] is None
