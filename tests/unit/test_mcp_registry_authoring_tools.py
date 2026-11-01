"""Agent, skill, enablement and model-option MCP tools are thin API adapters.

The unit half records what each tool sends; the chain half runs the registered
tool through the real HTTP client into the real admin routes and application
services (with a stubbed registry store), so a conflict or a disabled object
reaches the MCP caller exactly as the route produced it.
"""

from __future__ import annotations

import copy
import inspect
import json
from types import SimpleNamespace
from typing import Any, ClassVar
from unittest.mock import MagicMock, patch

import httpx
import pytest
from mcp.server.mcpserver.exceptions import ToolError

from core.application.auth.auth_http_runtime import _HTTP_SAFE_METHODS, _required_http_scopes
from core.application.management.command_models_registry_agents_skills import (
    AgentPatch,
    AgentSave,
    SkillCreate,
    SkillPatch,
)
from core.application.management.registry_authoring_schema import (
    agent_authoring_schema,
    skill_authoring_schema,
)
from core.application.management.registry_name_conflicts import (
    ID_CONFLICT_CODE,
    NAME_CONFLICT_CODE,
)
from core.entrypoints.mcp import server_api_tool, server_caller_identity
from core.entrypoints.mcp.server_api_client import MCP_TOOL_ROUTES, AppApiClient, AppApiError
from core.entrypoints.mcp.server_discovery import visible_mcp_tool_names
from core.entrypoints.mcp.server_maintenance import mcp_tool_is_mutation
from core.entrypoints.mcp.server_tool_scopes import MCP_TOOL_SCOPES

NEW_TOOLS = {
    "agent_authoring_schema",
    "skill_authoring_schema",
    "agent_list",
    "agent_get",
    "agent_create",
    "agent_update",
    "agent_validate",
    "agent_set_enabled",
    "agent_skill_assign",
    "agent_skill_unassign",
    "agent_skill_set_instructions",
    "skill_list",
    "skill_get",
    "skill_create",
    "skill_update",
    "skill_inputs_set",
    "skill_outputs_set",
    "skill_validate",
    "skill_set_enabled",
    "workflow_set_enabled",
    "model_options",
}
WRITE_TOOLS = {name for name in NEW_TOOLS if MCP_TOOL_ROUTES[name].method != "GET"}
READ_ONLY_SCOPES = [
    "read:*",
    "registry.read",
    "registry:*",
    "workflows.read",
    "jobs.read",
    "projects.read",
]


class _RecordingClient:
    responses: ClassVar[list[object]] = []
    requests: ClassVar[list[dict[str, object]]] = []

    def request(self, method: str, path: str, **kwargs: object) -> object:
        self.requests.append({"method": method, "path": path, **kwargs})
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


@pytest.fixture
def recorded(monkeypatch: pytest.MonkeyPatch) -> type[_RecordingClient]:
    monkeypatch.setattr(_RecordingClient, "responses", [{"status": "ok"}])
    monkeypatch.setattr(_RecordingClient, "requests", [])
    monkeypatch.setattr(server_api_tool, "AppApiClient", _RecordingClient)
    monkeypatch.setattr(server_api_tool, "_caller_bearer", lambda: "caller-token")
    return _RecordingClient


def _tool(name: str) -> Any:
    from core.entrypoints.mcp.server import mcp

    return mcp._tool_manager._tools[name]


# --------------------------------------------------------------------------- #
# Inventory, scopes, maintenance                                              #
# --------------------------------------------------------------------------- #


def test_every_new_tool_is_registered_routed_scoped_and_maps_to_its_api_only() -> None:
    from core.entrypoints.mcp.server import mcp

    tools = mcp._tool_manager._tools
    assert set(tools) >= NEW_TOOLS
    for name in NEW_TOOLS:
        assert name in MCP_TOOL_ROUTES, name
        assert name in MCP_TOOL_SCOPES, name
        source = inspect.getsource(tools[name].fn)
        assert f'api_tool_response("{name}", locals())' in source, name
        assert "core.application" not in source, name
        assert "core.infrastructure" not in source, name
        assert str(tools[name].description or "").strip(), name


def test_agent_and_skill_tools_need_admin_all_like_their_admin_only_routes() -> None:
    for name in NEW_TOOLS:
        path = MCP_TOOL_ROUTES[name].path
        if path.startswith(("/api/agents", "/api/skills", "/api/workflows/")):
            assert MCP_TOOL_SCOPES[name] == ("admin:all",), name


def test_read_only_tokens_neither_see_nor_pass_the_route_scope_of_any_write() -> None:
    for scope in READ_ONLY_SCOPES:
        assert visible_mcp_tool_names([scope], auth_enabled=True).isdisjoint(WRITE_TOOLS), scope
    for name in WRITE_TOOLS:
        route = MCP_TOOL_ROUTES[name]
        assert route.method not in _HTTP_SAFE_METHODS
        path = route.path.format(agent_id="agent_1", skill_id="skill_1", workflow_id="wf_1")
        http_scopes = set(
            _required_http_scopes(
                path,
                route.method,
                admin_required=True,
                project_callback_write=False,
                registry_read=True,
                registry_write=True,
            )
        )
        assert http_scopes == {"admin:all"}, name
        assert mcp_tool_is_mutation(name) is True, name


def test_reads_stay_reads_during_maintenance_even_when_only_admin_may_call_them() -> None:
    for name in NEW_TOOLS - WRITE_TOOLS:
        assert mcp_tool_is_mutation(name) is False, name
    assert visible_mcp_tool_names(["registry.read"], auth_enabled=True) & NEW_TOOLS == {
        "model_options"
    }
    assert {"agent_authoring_schema", "skill_authoring_schema"} <= visible_mcp_tool_names(
        ["read:*"], auth_enabled=True
    )


# --------------------------------------------------------------------------- #
# Delegation: method, path and exactly the arguments the caller passed         #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "tool, arguments, method, path, params, body",
    [
        ("agent_authoring_schema", {}, "GET", "/api/agent-authoring/schema", {}, None),
        ("skill_authoring_schema", {}, "GET", "/api/skill-authoring/schema", {}, None),
        (
            "agent_list",
            {"search": None, "enabled_only": False},
            "GET",
            "/api/agents",
            {"enabled_only": False},
            None,
        ),
        ("agent_get", {"agent_id": "writer"}, "GET", "/api/agents/writer", {}, None),
        (
            "agent_validate",
            {"agent_id": "writer"},
            "GET",
            "/api/agents/writer/contract-validation",
            {},
            None,
        ),
        (
            "agent_create",
            {"agent": {"agent_id": "writer", "display_name": "Writer"}},
            "POST",
            "/api/agents",
            None,
            {"agent_id": "writer", "display_name": "Writer"},
        ),
        (
            "agent_update",
            {"agent_id": "writer", "changes": {"config": {"model": ""}}},
            "PATCH",
            "/api/agents/writer",
            None,
            {"config": {"model": ""}},
        ),
        (
            "agent_set_enabled",
            {"agent_id": "writer", "enabled": False},
            "PATCH",
            "/api/agents/writer",
            None,
            {"enabled": False},
        ),
        (
            "agent_skill_assign",
            {"agent_id": "writer", "skill_id": "summarize", "skill_prompt": None},
            "POST",
            "/api/agents/writer/skills",
            None,
            {"skill_id": "summarize"},
        ),
        (
            "agent_skill_assign",
            {"agent_id": "writer", "skill_id": "summarize", "skill_prompt": "Be brief."},
            "POST",
            "/api/agents/writer/skills",
            None,
            {"skill_id": "summarize", "skill_prompt": "Be brief."},
        ),
        (
            "agent_skill_unassign",
            {"agent_id": "writer", "skill_id": "summarize"},
            "DELETE",
            "/api/agents/writer/skills/summarize",
            None,
            None,
        ),
        (
            "agent_skill_set_instructions",
            {"agent_id": "writer", "skill_id": "summarize", "skill_prompt": "Cite sources."},
            "PUT",
            "/api/agents/writer/skills/summarize/prompt",
            None,
            {"skill_prompt": "Cite sources."},
        ),
        ("skill_list", {"search": "sum"}, "GET", "/api/skills", {"search": "sum"}, None),
        ("skill_get", {"skill_id": "summarize"}, "GET", "/api/skills/summarize", {}, None),
        (
            "skill_validate",
            {"skill_id": "summarize"},
            "GET",
            "/api/skills/summarize/contract-validation",
            {},
            None,
        ),
        (
            "skill_create",
            {"skill": {"skill_id": "summarize", "display_name": "Summarize"}},
            "POST",
            "/api/skills",
            None,
            {"skill_id": "summarize", "display_name": "Summarize"},
        ),
        (
            "skill_update",
            {"skill_id": "summarize", "changes": {"description": "Short."}},
            "PATCH",
            "/api/skills/summarize",
            None,
            {"description": "Short."},
        ),
        (
            "skill_inputs_set",
            {"skill_id": "summarize", "inputs": [{"name": "text"}]},
            "PUT",
            "/api/skills/summarize/inputs",
            None,
            [{"name": "text"}],
        ),
        (
            "skill_outputs_set",
            {"skill_id": "summarize", "outputs": []},
            "PUT",
            "/api/skills/summarize/outputs",
            None,
            [],
        ),
        (
            "skill_set_enabled",
            {"skill_id": "summarize", "enabled": True},
            "PATCH",
            "/api/skills/summarize",
            None,
            {"enabled": True},
        ),
        (
            "workflow_set_enabled",
            {"workflow_id": "stock_import", "enabled": True, "expected_revision": 4},
            "PATCH",
            "/api/workflows/stock_import",
            None,
            {"enabled": True, "expected_revision": 4},
        ),
        ("model_options", {}, "GET", "/api/model-selection/options", {}, None),
    ],
)
def test_tool_sends_one_request_with_only_the_passed_fields(
    recorded, tool, arguments, method, path, params, body
) -> None:
    result = json.loads(server_api_tool.api_tool_response(tool, arguments))

    assert result["ok"] is True
    assert result["tool"] == tool
    (request,) = recorded.requests
    assert request["method"] == method
    assert request["path"] == path
    assert request["bearer"] == "caller-token"
    assert request["params"] == params
    assert request["json_body"] == body


def test_patch_tools_never_send_fields_the_caller_left_out(recorded) -> None:
    server_api_tool.api_tool_response(
        "agent_update", {"agent_id": "writer", "changes": {"description": "New."}}
    )
    assert recorded.requests[0]["json_body"] == {"description": "New."}
    assert "enabled" not in recorded.requests[0]["json_body"]


def test_create_and_enable_signatures_have_no_hidden_defaults() -> None:
    parameters = {name: _tool(name).parameters for name in NEW_TOOLS}
    assert set(parameters["agent_create"]["required"]) == {"agent"}
    assert set(parameters["agent_update"]["required"]) == {"agent_id", "changes"}
    assert set(parameters["agent_set_enabled"]["required"]) == {"agent_id", "enabled"}
    assert set(parameters["skill_set_enabled"]["required"]) == {"skill_id", "enabled"}
    assert set(parameters["workflow_set_enabled"]["required"]) == {
        "workflow_id",
        "enabled",
        "expected_revision",
    }
    assert "toggle" not in " ".join(MCP_TOOL_ROUTES[name].path for name in NEW_TOOLS)


@pytest.mark.parametrize(
    "payload, code, details",
    [
        (
            {
                "detail": {
                    "error": NAME_CONFLICT_CODE,
                    "code": NAME_CONFLICT_CODE,
                    "message": "Agent name 'Writer' is already used. Choose another name.",
                    "entity_type": "agent",
                    "matches": [{"id": "writer", "display_name": "Writer", "used_by": []}],
                    "matches_truncated": False,
                    "suggested_display_name": "Writer 2",
                }
            },
            NAME_CONFLICT_CODE,
            True,
        ),
        (
            {"detail": {"error": "Contract validation failed", "issues": [{"code": "x"}]}},
            "invalid_request",
            True,
        ),
        ({"detail": "Agent 'ghost' not found"}, "not_found", False),
    ],
)
def test_refusals_keep_the_route_error_payload_intact(recorded, payload, code, details) -> None:
    status = 409 if code == NAME_CONFLICT_CODE else 400 if details else 404
    recorded.responses[:] = [AppApiError(status, copy.deepcopy(payload))]

    with pytest.raises(ToolError) as refused:
        server_api_tool.api_tool_response("agent_create", {"agent": {"agent_id": "writer"}})

    envelope = json.loads(str(refused.value))
    assert envelope["ok"] is False
    assert envelope["error"]["code"] == code
    assert envelope["error"]["status_code"] == status
    if details:
        assert envelope["error"]["details"] == payload["detail"]
        assert envelope["error"]["message"] == (
            payload["detail"].get("message") or payload["detail"]["error"]
        )
    else:
        assert "details" not in envelope["error"]
        assert envelope["error"]["message"] == payload["detail"]


def test_request_validation_errors_keep_their_field_errors(recorded) -> None:
    payload = {
        "code": "validation_error",
        "message": "Request validation failed",
        "details": {"errors": [{"loc": ["body", "skills"], "msg": "refused"}]},
    }
    recorded.responses[:] = [AppApiError(422, payload)]

    with pytest.raises(ToolError) as refused:
        server_api_tool.api_tool_response(
            "agent_update", {"agent_id": "writer", "changes": {"skills": []}}
        )

    error = json.loads(str(refused.value))["error"]
    assert error["code"] == "validation_error"
    assert error["details"] == payload["details"]


# --------------------------------------------------------------------------- #
# Schemas come from the route models                                          #
# --------------------------------------------------------------------------- #


def test_authoring_schemas_are_the_route_request_models() -> None:
    agent = agent_authoring_schema()
    skill = skill_authoring_schema()

    assert agent.create["json_schema"] == AgentSave.model_json_schema()
    assert agent.patch["json_schema"] == AgentPatch.model_json_schema()
    assert skill.create["json_schema"] == SkillCreate.model_json_schema()
    assert skill.patch["json_schema"] == SkillPatch.model_json_schema()
    semantics = " ".join(agent.patch["semantics"])
    assert "null never clears" in semantics
    assert "merge shallowly" in semantics
    assert "inherits the project" in semantics


def test_admin_routes_validate_with_the_published_models() -> None:
    from core.entrypoints.admin.agents import router as agent_router
    from core.entrypoints.admin.skills import router as skill_router

    bodies = {
        (method, route.path): route.body_field.field_info.annotation
        for router in (agent_router, skill_router)
        for route in router.routes
        for method in getattr(route, "methods", ())
        if getattr(route, "body_field", None) is not None
    }
    assert bodies[("POST", "/api/agents")] is AgentSave
    assert bodies[("PATCH", "/api/agents/{agent_id}")] is AgentPatch
    assert bodies[("POST", "/api/skills")] is SkillCreate
    assert bodies[("PATCH", "/api/skills/{skill_id}")] is SkillPatch


# --------------------------------------------------------------------------- #
# MCP -> HTTP API -> application service chain                                #
# --------------------------------------------------------------------------- #

VALID = {"valid": True, "errors": [], "warnings": []}


@pytest.fixture
def api(monkeypatch: pytest.MonkeyPatch):
    from fastapi.testclient import TestClient

    from core.entrypoints.admin.api import create_admin_router
    from core.entrypoints.api.app_factory import create_app

    app = create_app()
    paths = {str(getattr(route, "path", "")) for route in app.router.routes}
    if "/api/agents" not in paths or "/api/agent-authoring/schema" not in paths:
        app.include_router(create_admin_router())
    app.state.local_auth_enabled_override = lambda: False
    app.state.external_auth_enabled_override = lambda: False
    test_client = TestClient(app, raise_server_exceptions=False)

    def forward(request: httpx.Request) -> httpx.Response:
        # The MCP client's real request, served by the real app.
        served = test_client.request(
            request.method,
            request.url.path,
            params=request.url.params,
            headers=dict(request.headers),
            content=request.content or None,
        )
        headers = {
            key: value
            for key, value in served.headers.items()
            if key.lower() not in {"content-encoding", "content-length", "transfer-encoding"}
        }
        return httpx.Response(served.status_code, headers=headers, content=served.content)

    client = AppApiClient(
        base_url="http://testserver",
        transport=httpx.MockTransport(forward),
        allow_anonymous_dev=False,
    )
    monkeypatch.setattr(server_api_tool, "AppApiClient", lambda: client)
    monkeypatch.setattr(
        server_caller_identity, "get_access_token", lambda: SimpleNamespace(token="caller-token")
    )
    return test_client


def _agent_db() -> MagicMock:
    db = MagicMock()
    rows = {
        "writer": {
            "agent_id": "writer",
            "display_name": "Report Writer",
            "enabled": False,
            "description": "Drafts reports.",
            "config": {"model": "gpt-4.1-mini", "temperature": 0.2},
            "prompts": {"system": "Write."},
            "skills": [{"skill_id": "summarize", "skill_prompt": "Be brief.", "sort_order": 0}],
            "tools": [],
        }
    }
    db.get_agent.side_effect = lambda agent_id: copy.deepcopy(rows.get(agent_id))
    db.list_agents.return_value = [copy.deepcopy(row) for row in rows.values()]
    return db


def _call(name: str, **arguments: Any) -> dict[str, Any]:
    return json.loads(_tool(name).fn(**arguments))


def _refusal(name: str, **arguments: Any) -> dict[str, Any]:
    tool_fn = _tool(name).fn
    with pytest.raises(ToolError) as refused:
        tool_fn(**arguments)
    return json.loads(str(refused.value))["error"]


def test_chain_reads_a_disabled_agent_and_patches_only_the_passed_field(api) -> None:
    import core.entrypoints.admin.agents as agent_routes

    db = _agent_db()
    with (
        patch.object(agent_routes, "_get_db", return_value=db),
        patch.object(agent_routes, "_get_registry", return_value=MagicMock()),
        patch.object(agent_routes, "_get_agent_validation", return_value=VALID),
    ):
        read = _call("agent_get", agent_id="writer")
        updated = _call("agent_update", agent_id="writer", changes={"config": {"model": ""}})

    assert read["ok"] is True
    assert read["data"]["agent_id"] == "writer"
    assert read["data"]["enabled"] is False
    assert read["data"]["skills"][0]["skill_prompt"] == "Be brief."
    assert updated["data"]["status"] == "updated"
    saved = db.save_agent.call_args.args[0]
    # Shallow merge: the emptied model inherits the project route, the rest stays.
    assert saved["config"] == {"temperature": 0.2}
    assert saved["description"] == "Drafts reports."
    assert saved["enabled"] is False


def test_chain_create_with_a_taken_id_reports_the_service_conflict_and_saves_nothing(api) -> None:
    import core.entrypoints.admin.agents as agent_routes

    db = _agent_db()
    with (
        patch.object(agent_routes, "_get_db", return_value=db),
        patch.object(agent_routes, "_get_registry", return_value=MagicMock()),
        patch.object(agent_routes, "_get_agent_validation", return_value=VALID),
    ):
        id_conflict = _refusal(
            "agent_create", agent={"agent_id": "writer", "display_name": "Another Writer"}
        )
        name_conflict = _refusal(
            "agent_create", agent={"agent_id": "writer_two", "display_name": " report  WRITER "}
        )

    assert id_conflict["status_code"] == 409
    assert id_conflict["code"] == ID_CONFLICT_CODE
    assert id_conflict["details"]["code"] == ID_CONFLICT_CODE
    assert id_conflict["details"]["id"] == "writer"
    assert id_conflict["details"]["suggested_id"] not in {None, "writer"}
    assert name_conflict["code"] == NAME_CONFLICT_CODE
    assert [match["id"] for match in name_conflict["details"]["matches"]] == ["writer"]
    assert name_conflict["details"]["suggested_display_name"]
    db.save_agent.assert_not_called()


def test_chain_skill_name_conflict_and_schema_reach_the_mcp_caller(api) -> None:
    import core.entrypoints.admin.skills as skill_routes

    db = MagicMock()
    db.get_skill.return_value = None
    db.list_skills.return_value = [
        {"skill_id": "summary", "display_name": "Summarize", "enabled": False}
    ]
    with (
        patch.object(skill_routes, "_get_db", return_value=db),
        patch.object(skill_routes, "_get_registry", return_value=MagicMock()),
        patch.object(skill_routes, "_get_skill_validation", return_value=VALID),
    ):
        conflict = _refusal(
            "skill_create", skill={"skill_id": "summary_two", "display_name": "SUMMARIZE"}
        )
        schema = _call("skill_authoring_schema")

    assert conflict["code"] == NAME_CONFLICT_CODE
    assert conflict["details"]["matches"][0]["id"] == "summary"
    assert conflict["details"]["matches"][0]["enabled"] is False
    db.save_skill.assert_not_called()
    assert schema["data"]["entity_type"] == "skill"
    assert schema["data"]["patch"]["json_schema"] == SkillPatch.model_json_schema()
