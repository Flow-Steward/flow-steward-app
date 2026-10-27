from __future__ import annotations

import json
from typing import ClassVar

from fastapi.testclient import TestClient


class _RecordingClient:
    requests: ClassVar[list[dict[str, object]]] = []

    def request(self, method: str, path: str, **kwargs: object) -> object:
        self.requests.append({"method": method, "path": path, **kwargs})
        return {"projects": []}


def _call_tool(client: TestClient, name: str) -> dict:
    response = client.post(
        "/mcp",
        headers={
            "Authorization": "Bearer transport-caller-token",
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
            "MCP-Protocol-Version": "2026-07-28",
            "Mcp-Method": "tools/call",
            "Mcp-Name": name,
        },
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": name,
                "arguments": {},
                "_meta": {
                    "io.modelcontextprotocol/protocolVersion": "2026-07-28",
                    "io.modelcontextprotocol/clientInfo": {
                        "name": "caller-identity-test",
                        "version": "1.0.0",
                    },
                    "io.modelcontextprotocol/clientCapabilities": {},
                },
            },
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_auth_disabled_transport_delegates_explicit_bearer_to_api_backed_project_list(
    monkeypatch,
) -> None:
    from mcp.server import MCPServer
    from mcp.server.transport_security import TransportSecuritySettings

    from core.entrypoints.mcp import server_api_tool
    from core.entrypoints.mcp.server_caller_identity import McpCallerIdentityMiddleware

    monkeypatch.setattr(_RecordingClient, "requests", [])
    monkeypatch.setattr(server_api_tool, "AppApiClient", _RecordingClient)
    server = MCPServer(
        name="caller-identity-project-list",
        middleware=[McpCallerIdentityMiddleware(auth_enabled=False)],
    )

    @server.tool(name="project_list")
    def project_list() -> str:
        return server_api_tool.api_tool_response("project_list", {})

    app = server.streamable_http_app(
        json_response=True,
        stateless_http=True,
        transport_security=TransportSecuritySettings(allowed_hosts=["localhost"]),
    )
    with TestClient(app, base_url="http://localhost") as client:
        payload = _call_tool(client, "project_list")

    result = payload["result"]
    text = next(item["text"] for item in result["content"] if item["type"] == "text")
    assert json.loads(text)["ok"] is True
    assert _RecordingClient.requests[0]["bearer"] == "transport-caller-token"


def test_identity_middleware_is_outermost_for_mutation_maintenance_and_handler() -> None:
    from mcp.server import MCPServer
    from mcp.server.transport_security import TransportSecuritySettings

    from core.entrypoints.mcp.server_caller_identity import (
        McpCallerIdentityMiddleware,
        current_caller_bearer,
    )
    from core.entrypoints.mcp.server_maintenance import McpMaintenanceMiddleware

    observed: list[tuple[str, str]] = []

    async def maintenance_active(bearer: str) -> bool:
        observed.append(("maintenance", bearer))
        return False

    server = MCPServer(
        name="caller-identity-mutation",
        middleware=[
            McpCallerIdentityMiddleware(auth_enabled=False),
            McpMaintenanceMiddleware(maintenance_active=maintenance_active),
        ],
    )

    @server.tool(name="identity_probe")
    def identity_probe() -> str:
        observed.append(("handler", current_caller_bearer()))
        return json.dumps({"ok": True})

    app = server.streamable_http_app(
        json_response=True,
        stateless_http=True,
        transport_security=TransportSecuritySettings(allowed_hosts=["localhost"]),
    )
    with TestClient(app, base_url="http://localhost") as client:
        payload = _call_tool(client, "identity_probe")

    assert payload["result"]["isError"] is False
    assert observed == [
        ("maintenance", "transport-caller-token"),
        ("handler", "transport-caller-token"),
    ]
