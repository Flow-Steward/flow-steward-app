from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest

from core.entrypoints.mcp.server_maintenance import McpMaintenanceMiddleware


def test_mutation_maintenance_gate_uses_caller_backed_app_status() -> None:
    observed: list[str] = []
    downstream_calls: list[object] = []

    async def _maintenance_active(bearer: str) -> bool:
        observed.append(bearer)
        return True

    async def _call_next(ctx: object) -> object:
        downstream_calls.append(ctx)
        return {"ok": True}

    middleware = McpMaintenanceMiddleware(
        maintenance_active=_maintenance_active,
        caller_bearer=lambda: "caller-token",
    )
    result = asyncio.run(
        middleware(
            SimpleNamespace(method="tools/call", params={"name": "job_cancel"}),
            _call_next,
        )
    )

    assert observed == ["caller-token"]
    assert downstream_calls == []
    assert result.is_error is True
    payload = json.loads(result.content[0].text)
    assert payload["error"]["code"] == "maintenance_mode_enabled"


def test_read_tool_does_not_query_maintenance_or_block() -> None:
    observed: list[str] = []

    async def _maintenance_active(bearer: str) -> bool:
        observed.append(bearer)
        return True

    async def _call_next(_ctx: object) -> object:
        return {"ok": True}

    middleware = McpMaintenanceMiddleware(
        maintenance_active=_maintenance_active,
        caller_bearer=lambda: "caller-token",
    )
    result = asyncio.run(
        middleware(
            SimpleNamespace(method="tools/call", params={"name": "project_list"}),
            _call_next,
        )
    )

    assert result == {"ok": True}
    assert observed == []


def test_empty_bearer_uses_the_canonical_app_maintenance_status() -> None:
    remote_checks: list[str] = []
    downstream_calls: list[object] = []

    async def _maintenance_active(bearer: str) -> bool:
        remote_checks.append(bearer)
        return False

    async def _call_next(ctx: object) -> object:
        downstream_calls.append(ctx)
        return {"ok": True}

    middleware = McpMaintenanceMiddleware(
        maintenance_active=_maintenance_active,
        caller_bearer=lambda: "",
    )
    result = asyncio.run(
        middleware(
            SimpleNamespace(method="tools/call", params={"name": "job_cancel"}),
            _call_next,
        )
    )

    assert result == {"ok": True}
    assert remote_checks == [""]
    assert len(downstream_calls) == 1


def test_empty_bearer_still_blocks_when_the_app_reports_maintenance() -> None:
    async def _maintenance_active(bearer: str) -> bool:
        assert bearer == ""
        return True

    async def _call_next(_ctx: object) -> object:
        raise AssertionError("maintenance must block the mutation")

    middleware = McpMaintenanceMiddleware(
        maintenance_active=_maintenance_active,
        caller_bearer=lambda: "",
    )
    result = asyncio.run(
        middleware(
            SimpleNamespace(method="tools/call", params={"name": "job_cancel"}),
            _call_next,
        )
    )

    assert result.is_error is True
    payload = json.loads(result.content[0].text)
    assert payload["error"]["code"] == "maintenance_mode_enabled"


@pytest.mark.parametrize(
    ("scope", "maintenance_enabled", "expected_status"),
    [("jobs.write", False, 200), ("jobs.read", False, 403), ("jobs.write", True, 503)],
    ids=["write-token", "read-token-scope-denied", "real-maintenance-blocks-write"],
)
def test_status_permission_denial_defers_to_authoritative_api_guards(
    monkeypatch, scope, maintenance_enabled, expected_status
) -> None:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    import core.entrypoints.mcp.server_maintenance as maintenance_adapter
    from core.application.auth import is_admin_only_route
    from core.application.auth.principal import ResolvedPrincipal
    from core.entrypoints.mcp.server_api_client import AppApiClient, AppApiError
    from core.infrastructure.runtime.maintenance_http import build_maintenance_http_middleware
    from tests.unit.test_auth_http_runtime import _run_middleware_with_principal

    principal = ResolvedPrincipal(
        id="fixture-caller",
        username="fixture-caller",
        auth_source="pat",
        roles=["operator"],
        scopes=[scope],
    )
    app = FastAPI()
    app.middleware("http")(
        build_maintenance_http_middleware(maintenance_active=lambda: maintenance_enabled)
    )
    mutations = []

    @app.post("/api/jobs/fixture-job/cancel")
    async def cancel_job():
        mutations.append("cancel")
        return {"ok": True}

    observed = []
    api = TestClient(app)

    async def transport(request: httpx.Request) -> httpx.Response:
        observed.append((request.method, request.url.path, request.headers["Authorization"]))
        # Use the existing test seam for the real HTTP role/scope middleware,
        # then the real maintenance middleware; no fabricated permission policy.
        auth_response = await asyncio.to_thread(
            _run_middleware_with_principal,
            path=request.url.path,
            method=request.method,
            principal=principal,
            admin_only=is_admin_only_route(request.url.path),
        )
        if auth_response.status_code != 204:
            return httpx.Response(auth_response.status_code, content=auth_response.body)
        response = await asyncio.to_thread(
            api.request, request.method, request.url.path, content=request.content
        )
        return httpx.Response(response.status_code, content=response.content)

    def client():
        return AppApiClient(base_url="http://fs-app", transport=httpx.MockTransport(transport))

    monkeypatch.setattr(maintenance_adapter, "AppApiClient", client)
    middleware = McpMaintenanceMiddleware(caller_bearer=lambda: "fixture-caller-token")

    async def call_next(_ctx):
        return await client().request_async(
            "POST", "/api/jobs/fixture-job/cancel", bearer="fixture-caller-token"
        )

    ctx = SimpleNamespace(method="tools/call", params={"name": "job_cancel"})
    if expected_status == 200:
        assert asyncio.run(middleware(ctx, call_next)) == {"ok": True}
    else:
        call = middleware(ctx, call_next)
        with pytest.raises(AppApiError) as exc:
            asyncio.run(call)
        assert exc.value.status_code == expected_status
        if expected_status == 403:
            assert "Missing required token scope" in str(exc.value.payload)
        else:
            assert exc.value.payload["error"]["code"] == "maintenance_mode_enabled"

    assert observed == [
        ("GET", "/api/system/maintenance", "Bearer fixture-caller-token"),
        ("POST", "/api/jobs/fixture-job/cancel", "Bearer fixture-caller-token"),
    ]
    assert mutations == (["cancel"] if expected_status == 200 else [])


@pytest.mark.parametrize("status", [200, 401, 404, 500, 503])
def test_status_transport_failure_remains_fail_closed(monkeypatch, status) -> None:
    import core.entrypoints.mcp.server_maintenance as maintenance_adapter
    from core.entrypoints.mcp.server_api_client import AppApiClient

    monkeypatch.setattr(
        maintenance_adapter,
        "AppApiClient",
        lambda: AppApiClient(
            base_url="http://fs-app",
            transport=httpx.MockTransport(lambda _request: httpx.Response(status, json={})),
        ),
    )

    async def call_next(_ctx):
        raise AssertionError("An unavailable maintenance check must not invoke a mutation")

    result = asyncio.run(
        McpMaintenanceMiddleware(caller_bearer=lambda: "fixture-caller-token")(
            SimpleNamespace(method="tools/call", params={"name": "job_cancel"}), call_next
        )
    )
    assert result.is_error is True
    assert json.loads(result.content[0].text)["error"]["code"] == "maintenance_mode_enabled"


def test_unknown_tool_cannot_defer_an_inaccessible_maintenance_status(monkeypatch) -> None:
    import core.entrypoints.mcp.server_maintenance as maintenance_adapter
    from core.entrypoints.mcp.server_api_client import AppApiClient

    monkeypatch.setattr(
        maintenance_adapter,
        "AppApiClient",
        lambda: AppApiClient(
            base_url="http://fs-app",
            transport=httpx.MockTransport(lambda _request: httpx.Response(403, json={})),
        ),
    )

    async def call_next(_ctx):
        raise AssertionError("Only registered API mutation routes may defer to their guard")

    result = asyncio.run(
        McpMaintenanceMiddleware(caller_bearer=lambda: "fixture-caller-token")(
            SimpleNamespace(method="tools/call", params={"name": "unknown_tool"}), call_next
        )
    )
    assert result.is_error is True


def test_every_deferred_api_mutation_is_protected_by_the_canonical_maintenance_policy() -> None:
    from core.entrypoints.mcp.server_api_client import MCP_TOOL_ROUTES
    from core.entrypoints.mcp.server_maintenance import mcp_tool_is_mutation
    from core.infrastructure.runtime.maintenance_http import maintenance_request_decision

    for tool, route in MCP_TOOL_ROUTES.items():
        if mcp_tool_is_mutation(tool):
            assert route.method in {"POST", "PUT", "PATCH", "DELETE"}, tool
            assert not maintenance_request_decision(
                route.method, route.path, authenticated=True
            ).allowed, tool


def test_authoring_validate_is_logical_read_but_save_is_mutation():
    from core.entrypoints.mcp.server_maintenance import mcp_tool_is_mutation
    from core.infrastructure.runtime.maintenance_http import maintenance_request_decision

    assert mcp_tool_is_mutation("workflow_draft_validate") is False
    assert mcp_tool_is_mutation("workflow_draft_save") is True
    assert mcp_tool_is_mutation("workflow_apply_changes") is True
    assert mcp_tool_is_mutation("workflow_definition") is False
    # Logical MCP read classification does not grant a HTTP maintenance exception.
    assert (
        maintenance_request_decision(
            "POST", "/api/projects/p1/workflow-authoring/validate", True
        ).allowed
        is False
    )
