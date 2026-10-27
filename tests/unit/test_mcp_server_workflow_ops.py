from __future__ import annotations

import inspect
import json
from types import SimpleNamespace

import httpx
import pytest

from core.entrypoints.mcp import server_api_tool, server_caller_identity
from core.entrypoints.mcp.server import mcp
from core.entrypoints.mcp.server_api_client import MCP_TOOL_ROUTES, AppApiClient


def test_all_registered_tools_have_descriptions_and_api_routes() -> None:
    tools = mcp._tool_manager._tools
    assert len(tools) == 51
    assert set(tools) == set(MCP_TOOL_ROUTES)
    for name, tool in tools.items():
        assert str(tool.description or "").strip(), name
        assert name in MCP_TOOL_ROUTES


def test_public_input_contracts_keep_required_identity_fields() -> None:
    tools = mcp._tool_manager._tools
    required = {
        "capability_search": {"project_name", "query"},
        "capability_describe": {"project_name", "ref"},
        "workflow_examples_search": {"project_name", "query"},
        "workflow_draft_validate": {"project_name", "bundle_yaml"},
        "workflow_draft_save": {"project_name", "bundle_yaml"},
        "workflow_convert_group": {
            "workflow_id",
            "group_id",
            "child_workflow_id",
            "display_name",
            "expected_revision",
        },
        "workflow_apply_changes": {"workflow_id", "expected_revision", "operations"},
        "workflow_export": {"project_name", "workflow_id"},
        "workflow_stats": {"project_name", "workflow_id"},
        "workflow_import": {"project_name", "bundle_yaml"},
        "job_approval_decide": {
            "thread_id",
            "approval_id",
            "approved",
            "idempotency_key",
        },
    }
    for name, fields in required.items():
        assert fields <= set(tools[name].parameters.get("required") or []), name


def test_tool_bodies_are_protocol_mapping_only() -> None:
    for name, tool in mcp._tool_manager._tools.items():
        source = inspect.getsource(tool.fn)
        assert "api_tool_response" in source, name
        assert "core.infrastructure" not in source, name


def test_workflow_launch_discovery_schema_is_direct_and_project_scoped() -> None:
    tools = mcp._tool_manager._tools
    assert "trigger_workflow_entrypoint" not in tools
    for name in ("workflow_trigger", "workflow_contract"):
        assert {"project_name", "workflow_id"} <= set(tools[name].parameters.get("required") or [])
    assert "entry_key" not in tools["workflow_trigger"].parameters["properties"]


def test_builtin_workflow_ops_manifest_matches_direct_identity_schema() -> None:
    from core.application.registry.builtin_mcp_tools import builtin_mcp_tool_manifests

    functions = {
        item["name"]: item
        for item in builtin_mcp_tool_manifests()["workflow_ops"]["interface"]["functions"]
    }
    for name in ("workflow_trigger", "workflow_contract"):
        parameters = {item["name"]: item for item in functions[name]["parameters"]}
        assert parameters["project_name"]["required"] is True
        assert parameters["workflow_id"]["required"] is True
    assert {"entry_key", "mode", "task_id", "task_summary"}.isdisjoint(
        item["name"] for item in functions["workflow_trigger"]["parameters"]
    )


def test_mcp_does_not_advertise_external_webhook_entrypoints():
    from core.application.registry.builtin_mcp_tools import builtin_mcp_tool_manifests

    assert "workflow_entrypoint_list" not in mcp._tool_manager._tools
    functions = builtin_mcp_tool_manifests()["workflow_ops"]["interface"]["functions"]
    assert "workflow_entrypoint_list" not in {item["name"] for item in functions}


def test_direct_launch_schema_accepts_real_inline_file_uploads():
    assert "files" in mcp._tool_manager._tools["workflow_trigger"].parameters["properties"]


def test_job_result_discovery_exposes_existing_api_result_limit_bounds() -> None:
    properties = mcp._tool_manager._tools["job_result"].parameters["properties"]
    for name, default in (("include_messages_limit", 100), ("include_logs_limit", 50)):
        assert properties[name]["minimum"] == 1
        assert properties[name]["maximum"] == 2000
        assert properties[name]["default"] == default


def test_where_used_schema_keeps_locale_optional_and_identity_required() -> None:
    parameters = mcp._tool_manager._tools["workflow_where_used"].parameters
    assert set(parameters["required"]) == {"project_name", "workflow_id"}
    assert set(parameters["properties"]) == {"project_name", "workflow_id", "locale"}
    assert parameters["properties"]["locale"]["type"] == "string"
    assert parameters["properties"]["locale"]["default"] == ""


@pytest.mark.parametrize("locale_arguments, expected_locale", [({"locale": "lt"}, "lt"), ({}, "")])
def test_where_used_registered_tool_maps_locale_to_authenticated_get(
    monkeypatch: pytest.MonkeyPatch, locale_arguments: dict[str, str], expected_locale: str
) -> None:
    requests: list[httpx.Request] = []
    workflow_id = "extension.11111111-1111-4111-8111-111111111111.prepare_invoice"
    data = {"workflow_id": workflow_id, "consumers": []}

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/api/projects":
            return httpx.Response(
                200,
                json={
                    "data": {
                        "projects": [
                            {"id": "foreign-project", "name": "Other account"},
                            {"id": "project-1", "name": "Invoices"},
                        ]
                    }
                },
            )
        assert request.url.path == f"/api/projects/project-1/workflows/{workflow_id}/where-used"
        return httpx.Response(200, json={"data": data})

    client = AppApiClient(
        base_url="https://app.example.test",
        transport=httpx.MockTransport(respond),
        allow_anonymous_dev=False,
    )
    monkeypatch.setattr(server_api_tool, "AppApiClient", lambda: client)
    monkeypatch.setattr(
        server_caller_identity, "get_access_token", lambda: SimpleNamespace(token="caller-token")
    )
    tool = mcp._tool_manager._tools["workflow_where_used"]
    result = json.loads(
        tool.fn(project_name="Invoices", workflow_id=workflow_id, **locale_arguments)
    )

    assert result["ok"] is True
    assert result["tool"] == "workflow_where_used"
    assert result["data"] == data
    assert len(requests) == 2
    assert all(request.method == "GET" for request in requests)
    assert all(request.headers["Authorization"] == "Bearer caller-token" for request in requests)
    assert all(request.content == b"" for request in requests)
    assert requests[0].url.path == "/api/projects"
    assert dict(requests[0].url.params) == {"page": "1", "per_page": "200", "search": "Invoices"}
    assert requests[1].url.path == f"/api/projects/project-1/workflows/{workflow_id}/where-used"
    assert dict(requests[1].url.params) == {"locale": expected_locale}
    assert requests[1].headers["X-Correlation-ID"] == result["meta"]["correlation_id"]
