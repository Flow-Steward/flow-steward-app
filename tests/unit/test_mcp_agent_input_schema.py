"""Discovered MCP input contracts must survive projection into agent tools."""

from __future__ import annotations

import json
from copy import deepcopy
from types import SimpleNamespace

import pytest

from core.application.management.mcp_connector_services import McpConnectorCommandService
from core.application.orchestration.extension_chat_model import ExtensionChatModel
from core.application.orchestration.tools import tool_resolver
from core.application.registry.tool_manifest import ToolManifest
from tests._step_run_state import step_run_state
from tests.fixtures.mcp_streamable_http import modern_mcp_server


@pytest.fixture
def input_schema() -> dict:
    return {
        "type": "object",
        "$defs": {
            "Filter": {
                "type": "object",
                "properties": {
                    "kind": {"type": "string", "enum": ["fact", "event"]},
                    "tags": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["kind"],
                "additionalProperties": False,
            }
        },
        "properties": {
            "filters": {"type": "array", "items": {"$ref": "#/$defs/Filter"}},
            "owner": {"anyOf": [{"type": "string"}, {"type": "null"}]},
            "limit": {"type": "integer", "minimum": 1, "default": 10},
            "metadata": {"type": "object", "additionalProperties": {"type": "string"}},
        },
        "required": ["filters"],
        "additionalProperties": {"type": "boolean"},
    }


def _project_manifest(schema: dict) -> ToolManifest:
    service = McpConnectorCommandService(get_logger=lambda: None)
    projected = service._build_projected_tool_manifest(
        {
            "connector_id": "memory-service",
            "account_id": "account-test",
            "project_id": "project-test",
            "tenant_id": "tenant-test",
            "display_name": "Memory service",
        },
        {
            "remote_tool_name": "search",
            "projected_tool_id": "memory.search",
            "description": "Search records using structured filters.",
            "input_schema": schema,
        },
        enabled=True,
    )
    return ToolManifest.from_db_row(projected)


def test_discovered_mcp_schema_reaches_agent_tool_without_loss(input_schema):
    manifest = _project_manifest(input_schema)
    [tool] = tool_resolver.build_manifest_dispatch_tools(manifest)

    assert tool.args_schema == input_schema
    assert tool.args_schema is not manifest.config["input_schema"]
    tool.args_schema["properties"]["limit"]["default"] = 99
    assert manifest.config["input_schema"]["properties"]["limit"]["default"] == 10


def test_mcp_nested_contract_reaches_extension_model(input_schema):
    [tool] = tool_resolver.build_manifest_dispatch_tools(_project_manifest(input_schema))
    model = ExtensionChatModel(
        account_id="account-test", provider_key="llmext_test", model_id="test-model"
    ).bind_tools([tool])

    parameters = model.bound_tools[0]["function"]["parameters"]
    assert parameters == input_schema


def test_mcp_workflow_operation_contract_survives_registry_round_trip(input_schema):
    manifest = _project_manifest(input_schema)
    restored = ToolManifest.from_db_row(manifest.to_registry_payload())

    assert restored.interface.operations[0]["input_schema"] == input_schema
    assert restored.catalog_entry()["interface"]["operations"][0]["input_schema"] == input_schema
    assert restored.input_schema == input_schema


def test_empty_mcp_schema_remains_empty_in_registry():
    manifest = _project_manifest({})

    assert manifest.input_schema == {}
    assert manifest.to_registry_payload()["interface"]["input_schema"] == {}


def test_mcp_recursive_reference_reaches_model_unchanged():
    schema = {
        "type": "object",
        "$defs": {
            "Node": {
                "type": "object",
                "properties": {"children": {"type": "array", "items": {"$ref": "#/$defs/Node"}}},
            }
        },
        "properties": {"root": {"$ref": "#/$defs/Node"}},
        "required": ["root"],
    }
    [tool] = tool_resolver.build_manifest_dispatch_tools(_project_manifest(schema))
    model = ExtensionChatModel(
        account_id="account-test", provider_key="llmext_test", model_id="test-model"
    ).bind_tools([tool])

    assert model.bound_tools[0]["function"]["parameters"] == schema


@pytest.mark.parametrize("fallback", [False, True], ids=["langchain", "fallback"])
@pytest.mark.parametrize("owner", ["customer-1", None])
def test_mcp_arguments_preserve_nullable_values_and_extra_properties(
    monkeypatch, input_schema, fallback, owner
):
    if fallback:
        monkeypatch.setattr(tool_resolver, "StructuredTool", None)
    monkeypatch.setattr(
        tool_resolver, "invoke_manifest_tool", lambda _manifest, **kwargs: kwargs["arguments"]
    )
    [tool] = tool_resolver.build_manifest_dispatch_tools(_project_manifest(input_schema))
    payload = {
        "filters": [{"kind": "fact", "tags": ["approved"]}],
        "owner": owner,
        "metadata": {"source": "editor"},
        "include_archived": False,
    }

    assert json.loads(tool.invoke(deepcopy(payload))) == payload


@pytest.mark.parametrize("fallback", [False, True], ids=["langchain", "fallback"])
def test_mcp_omitted_defaults_remain_server_owned(monkeypatch, input_schema, fallback):
    if fallback:
        monkeypatch.setattr(tool_resolver, "StructuredTool", None)
    monkeypatch.setattr(
        tool_resolver, "invoke_manifest_tool", lambda _manifest, **kwargs: kwargs["arguments"]
    )
    [tool] = tool_resolver.build_manifest_dispatch_tools(_project_manifest(input_schema))

    assert json.loads(tool.invoke({"filters": []})) == {"filters": []}


@pytest.mark.parametrize("fallback", [False, True], ids=["langchain", "fallback"])
def test_mcp_argument_names_cannot_replace_dispatch_context(monkeypatch, fallback):
    if fallback:
        monkeypatch.setattr(tool_resolver, "StructuredTool", None)
    payload = {
        "_function_name": "remote-argument",
        "_optional_names": ["owner"],
        "_runtime_context": {"project_id": "argument-project"},
        "owner": None,
    }
    context = {"project_id": "trusted-project"}
    monkeypatch.setattr(tool_resolver, "invoke_manifest_tool", lambda _manifest, **kwargs: kwargs)
    [tool] = tool_resolver.build_manifest_dispatch_tools(
        _project_manifest({"type": "object", "additionalProperties": True}),
        runtime_context=context,
    )

    assert json.loads(tool.invoke(payload)) == {
        "function_name": "memory.search",
        "runtime_context": context,
        "arguments": payload,
    }


def test_workflow_calls_mcp_directly_with_structured_arguments(monkeypatch, input_schema):
    from core.application.orchestration import connector_tool_runtime
    from core.application.orchestration.connector_step_runtime import execute_connector_step
    from core.application.registry.registry_manager import RegistryManager

    manifest = _project_manifest(input_schema)
    runtime_logger = SimpleNamespace(cursor=object())
    registry = SimpleNamespace(tools=SimpleNamespace(get_tool=lambda _tool_id: manifest))
    monkeypatch.setattr(RegistryManager, "instance", lambda: registry)
    monkeypatch.setattr(connector_tool_runtime, "_shared_logger", lambda: runtime_logger)
    monkeypatch.setattr(
        connector_tool_runtime, "_emit_connector_tool_event", lambda *_a, **_k: None
    )
    monkeypatch.setenv("FS_ALLOW_PRIVATE_REMOTE_URLS", "1")
    payload = {"filters": [{"kind": "fact", "tags": ["approved"]}], "owner": None}
    step = {
        "step_id": "search-memory",
        "step_kind": "connector",
        "connector_id": "connector.memory.search",
        "input_mapping_jsonb": {"filters": "state.inputs.filters", "owner": "state.inputs.owner"},
    }
    state = step_run_state(
        step,
        account_id="account-test",
        project_id="project-test",
        tenant_id="tenant-test",
        workflow_id="workflow-test",
        job_id="job-test",
        inputs=payload,
    )
    with modern_mcp_server() as server:
        connector_row = {
            "connector_id": "memory-service",
            "account_id": "account-test",
            "project_id": "project-test",
            "tenant_id": "tenant-test",
            "enabled": True,
            "transport": "streamable_http",
            "base_url": server.url,
            "auth_mode": "none",
            "headers": {},
            "allowlisted_tools": ["memory.search"],
            "discovered_tools_jsonb": [
                {"remote_tool_name": "search", "input_schema": input_schema}
            ],
        }
        monkeypatch.setattr(
            tool_resolver, "_load_mcp_connector_row", lambda *_a, **_k: connector_row
        )

        result = execute_connector_step(step=step, state=state)

    assert result["ok"] is True, result
    assert result["result"] == {"name": "search"}
    [call] = [
        r.payload for r in server.requests if r.payload and r.payload.get("method") == "tools/call"
    ]
    assert call["params"]["name"] == "search"
    assert call["params"]["arguments"] == payload
    assert server.violations == []
