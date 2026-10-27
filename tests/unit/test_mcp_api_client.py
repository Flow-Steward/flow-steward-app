from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from core.entrypoints.mcp.server_api_client import (
    MCP_TOOL_ROUTES,
    AppApiClient,
    AppApiError,
)
from core.entrypoints.mcp.server_tool_scopes import MCP_TOOL_SCOPES


def test_every_public_mcp_tool_has_one_explicit_app_api_route() -> None:
    assert set(MCP_TOOL_ROUTES) == set(MCP_TOOL_SCOPES)
    assert len(MCP_TOOL_ROUTES) == 51
    for tool, route in MCP_TOOL_ROUTES.items():
        assert route.method in {"GET", "POST", "PUT"}, tool
        assert route.path.startswith("/api/") or route.path.startswith("/webhook/"), tool

    for removed in (
        "submit_task",
        "db_schema",
        "task_prepare_brief",
        "project_index_status",
        "kb_search",
        "kb_write",
        "project_profile_get",
        "project_profile_update",
    ):
        assert removed not in MCP_TOOL_ROUTES


def test_app_api_client_forwards_only_caller_context() -> None:
    observed: dict[str, object] = {}

    def _handler(request: httpx.Request) -> httpx.Response:
        observed.update(
            {
                "method": request.method,
                "path": request.url.path,
                "query": str(request.url.query),
                "authorization": request.headers.get("authorization"),
                "correlation": request.headers.get("x-correlation-id"),
                "idempotency": request.headers.get("idempotency-key"),
                "body": json.loads(request.content),
            }
        )
        return httpx.Response(200, json={"ok": True, "data": {"id": "result-1"}})

    client = AppApiClient(
        base_url="http://fs-app:8099",
        transport=httpx.MockTransport(_handler),
    )

    result = client.request(
        "POST",
        "/api/reports/generate",
        bearer="caller-secret",
        params={"limit": 5},
        json_body={"report_type": "operations"},
        correlation_id="corr-1",
        idempotency_key="idem-1",
    )

    assert result == {"ok": True, "data": {"id": "result-1"}}
    assert observed == {
        "method": "POST",
        "path": "/api/reports/generate",
        "query": "b'limit=5'",
        "authorization": "Bearer caller-secret",
        "correlation": "corr-1",
        "idempotency": "idem-1",
        "body": {"report_type": "operations"},
    }


def test_app_api_client_never_substitutes_a_service_credential() -> None:
    observed: list[str | None] = []
    client = AppApiClient(
        base_url="http://fs-app:8099",
        transport=httpx.MockTransport(
            lambda request: (
                observed.append(request.headers.get("authorization"))
                or httpx.Response(200, json={"ok": True})
            )
        ),
        allow_anonymous_dev=False,
    )

    with pytest.raises(ValueError, match="caller bearer"):
        client.request("GET", "/api/projects", bearer="")

    assert observed == []


def test_app_api_client_uses_only_explicit_dev_proxy_for_anonymous_delegation() -> None:
    observed: dict[str, str | None] = {}

    def _handler(request: httpx.Request) -> httpx.Response:
        observed["authorization"] = request.headers.get("authorization")
        observed["dev_proxy"] = request.headers.get("x-flow-steward-mcp-dev-auth-disabled")
        return httpx.Response(200, json={"ok": True})

    client = AppApiClient(
        base_url="http://fs-app:8099",
        transport=httpx.MockTransport(_handler),
        allow_anonymous_dev=True,
    )

    assert client.request("GET", "/api/projects", bearer="") == {"ok": True}
    assert observed == {"authorization": None, "dev_proxy": "1"}


def test_app_api_client_async_path_forwards_caller_without_get_body() -> None:
    observed: dict[str, object] = {}

    def _handler(request: httpx.Request) -> httpx.Response:
        observed.update(
            {
                "method": request.method,
                "authorization": request.headers.get("authorization"),
                "content": request.content,
            }
        )
        return httpx.Response(200, json={"status": "disabled"})

    client = AppApiClient(
        base_url="http://fs-app:8099",
        transport=httpx.MockTransport(_handler),
    )

    result = asyncio.run(
        client.request_async(
            "GET",
            "/api/system/maintenance",
            bearer="caller-secret",
        )
    )

    assert result == {"status": "disabled"}
    assert observed == {
        "method": "GET",
        "authorization": "Bearer caller-secret",
        "content": b"",
    }


@pytest.mark.parametrize("status", [400, 401, 403, 404, 409, 422, 503])
def test_app_api_client_maps_http_errors_without_hiding_status(status: int) -> None:
    client = AppApiClient(
        base_url="http://fs-app:8099",
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(status, json={"error": "bounded failure"})
        ),
    )

    with pytest.raises(AppApiError) as raised:
        client.request("GET", "/api/projects", bearer="caller-secret")

    assert raised.value.status_code == status
    assert raised.value.payload == {"error": "bounded failure"}
