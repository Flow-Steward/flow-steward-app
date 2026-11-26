"""Every MCP tool answers with a real object, not JSON inside a string.

The 2026-11-17 MCP probe (F5) found 72 of 74 tools declaring
``outputSchema: {result: string}``: their ``structuredContent`` was the
envelope serialized into a string field, so a client that reads structured
content (as the spec recommends) got a string. ``job_status`` returned no
structured content at all. Each tool now publishes the envelope as its output
schema and returns it as an object; the text block still carries its JSON.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any, ClassVar
from unittest.mock import AsyncMock

import pytest
from mcp import Client

from core.entrypoints.mcp import server_api_tool, server_maintenance
from core.entrypoints.mcp.server import mcp

_ENVELOPE_FIELDS = {"ok", "tool", "data", "meta"}


class _App:
    requests: ClassVar[list[dict[str, Any]]] = []

    def request(self, method: str, path: str, **kwargs: Any) -> object:
        self.requests.append({"method": method, "path": path})
        if path == "/api/jobs/job-1/status":
            return {"job_id": "job-1", "status": "running"}
        if path == "/api/catalog/search":
            return {"items": [{"detail_url": "/extensions?find=acme.shop#marketplace"}]}
        return {"projects": [{"id": "p-1", "name": "Sales"}]}


@pytest.fixture(autouse=True)
def _admin_caller(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_App, "requests", [])
    monkeypatch.setattr(server_api_tool, "AppApiClient", _App)
    monkeypatch.setattr(server_api_tool, "_caller_bearer", lambda: "caller-token")
    monkeypatch.setattr(
        "mcp.server.auth.middleware.auth_context.get_access_token",
        lambda: SimpleNamespace(scopes=["admin:all"], token="caller-token"),
    )


def _run(coro_factory: Any) -> Any:
    async def _session() -> Any:
        async with Client(mcp) as client:
            return await coro_factory(client)

    return asyncio.run(_session())


def test_tools_list_publishes_the_envelope_object_for_every_tool() -> None:
    tools = _run(lambda client: client.list_tools()).tools

    assert len(tools) == 78
    for tool in tools:
        schema = tool.output_schema
        assert schema is not None, tool.name
        assert schema["type"] == "object", tool.name
        assert set(schema["properties"]) == _ENVELOPE_FIELDS, tool.name
        assert schema["properties"]["ok"] == {"title": "Ok", "type": "boolean"}, tool.name
        assert "result" not in schema["properties"], tool.name
        # The discovery middleware adds annotations without dropping the schema.
        assert tool.annotations is not None, tool.name


@pytest.mark.parametrize(
    ("tool", "arguments"),
    [("project_list", {}), ("job_status", {"job_id": "job-1"})],
)
def test_tools_call_returns_the_envelope_as_structured_content(
    tool: str, arguments: dict[str, Any]
) -> None:
    # The SDK client checks structuredContent against the published outputSchema.
    result = _run(lambda client: client.call_tool(tool, arguments))

    assert result.is_error is False
    structured = result.structured_content
    assert isinstance(structured, dict)
    assert set(structured) == _ENVELOPE_FIELDS
    assert structured["ok"] is True
    assert structured["tool"] == tool
    assert isinstance(structured["data"], dict)
    assert json.loads(result.content[0].text) == structured


def test_catalog_search_uses_the_api_without_resolving_a_project(monkeypatch) -> None:
    monkeypatch.setattr(server_api_tool, "_public_origin", lambda: "https://flow.example.test")

    result = _run(lambda client: client.call_tool("catalog_search", {"query": "shop"}))

    assert not result.is_error
    assert _App.requests == [{"method": "GET", "path": "/api/catalog/search"}]
    assert result.structured_content["data"]["items"] == [
        {"detail_url": "https://flow.example.test/extensions?find=acme.shop#marketplace"}
    ]


@pytest.mark.parametrize(
    ("tool", "arguments", "method", "path", "body"),
    [
        (
            "project_create",
            {"project": {"name": "Sales", "workspace_id": "00000000-0000-0000-0000-000000000003"}},
            "POST",
            "/api/projects",
            {"name": "Sales", "workspace_id": "00000000-0000-0000-0000-000000000003"},
        ),
        (
            "project_update",
            {
                "project_id": "00000000-0000-0000-0000-000000000010",
                "changes": {"name": "New name", "description": ""},
            },
            "PUT",
            "/api/projects/00000000-0000-0000-0000-000000000010",
            {"name": "New name", "description": ""},
        ),
    ],
)
def test_project_writes_delegate_once_with_the_callers_token_and_exact_body(
    monkeypatch: pytest.MonkeyPatch,
    tool: str,
    arguments: dict[str, Any],
    method: str,
    path: str,
    body: dict[str, str],
) -> None:
    response = {"project_id": "00000000-0000-0000-0000-000000000010", "name": body["name"]}

    def request(_self: _App, method: str, path: str, **kwargs: Any) -> object:
        _App.requests.append(
            {
                "method": method,
                "path": path,
                "bearer": kwargs["bearer"],
                "body": kwargs["json_body"],
            }
        )
        return response

    monkeypatch.setattr(_App, "request", request)
    monkeypatch.setattr(
        server_maintenance.AppApiClient,
        "request_async",
        AsyncMock(return_value={"status": "disabled"}),
    )

    result = _run(lambda client: client.call_tool(tool, arguments))

    assert not result.is_error, result.content
    assert _App.requests == [
        {"method": method, "path": path, "bearer": "caller-token", "body": body}
    ]
    assert result.structured_content["data"] == response


def test_a_refusal_stays_a_tool_error_with_the_error_envelope_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Refusing:
        def request(self, method: str, path: str, **kwargs: Any) -> object:
            raise server_api_tool.AppApiError(404, {"error": "Job not found"})

    monkeypatch.setattr(server_api_tool, "AppApiClient", _Refusing)

    result = _run(lambda client: client.call_tool("job_status", {"job_id": "missing"}))

    assert result.is_error is True
    assert result.structured_content is None
    text = result.content[0].text
    envelope = json.loads(text[text.index("{") :])
    assert envelope["ok"] is False
    assert envelope["tool"] == "job_status"
    assert envelope["error"]["status_code"] == 404
