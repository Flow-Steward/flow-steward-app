from functools import partial
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from core.application.auth import auth_http_runtime, is_admin_only_route, serve_auth_runtime
from core.application.auth.principal import ResolvedPrincipal
from core.application.auth.request_principal_runtime import (
    normalize_live_principal,
    roles_intersect,
)
from core.application.auth.resolve import normalize_principal
from core.application.management.scope import ScopeContext
from core.entrypoints.api.workflows_builder_run import create_builder_workflow_run_router
from core.infrastructure.runtime.http_app_constants import _AUTH_REGISTRY_READ_PATTERNS
from core.infrastructure.runtime.serve_request_runtime import build_path_pattern_matcher

PROJECT_ID = "00000000-0000-0000-0000-000000000007"


def _raw_user(source, role, principal_type):
    return {
        "id": "admin-user",
        "username": "admin",
        "is_active": True,
        "account_id": "00000000-0000-0000-0000-000000000002",
        "auth_source": source,
        "principal_type": principal_type,
        "roles": [role],
        "scopes": ["admin:all"],
    }


def _normalize_user(user, *, default_auth_source):
    return normalize_live_principal(
        user,
        default_auth_source=default_auth_source,
        normalize_principal_fn=normalize_principal,
        resolved_principal_type=ResolvedPrincipal,
    )


def _auth_kwargs(user):
    return {
        "local_auth_enabled_fn": lambda: True,
        "external_auth_enabled": False,
        "is_public_auth_path_fn": lambda _path: False,
        "route_registered_fn": lambda _path, _method: True,
        "request_principal_resolver_fn": lambda _request, _bl: None,
        "normalize_live_principal_fn": _normalize_user,
        "get_logger_only_fn": lambda: object(),
        "auth_tables_available_fn": lambda _bl: True,
        "get_user_by_session_token_fn": lambda _bl, token: (
            user if token == "session-token" else None
        ),
        "local_auth_bootstrap_required_fn": lambda _bl=None: False,
        "auth_account_tables_available_fn": lambda _bl: False,
        "roles_intersect_fn": roles_intersect,
        "is_admin_only_route_fn": is_admin_only_route,
        "is_registry_read_route_fn": build_path_pattern_matcher(
            patterns=_AUTH_REGISTRY_READ_PATTERNS
        ),
        "is_self_service_auth_write_route_fn": lambda _path: False,
        "csrf_request_valid_fn": partial(
            serve_auth_runtime.csrf_request_valid,
            csrf_header_name="x-csrf-token",
            csrf_cookie_name="fs_csrf",
            same_origin_request_fn=lambda _request: False,
        ),
        "auth_cookie_name": "fs_session",
        "auth_unsafe_methods": {"POST", "PUT", "PATCH", "DELETE"},
        "auth_admin_roles": {"admin"},
        "auth_write_roles": {"admin", "operator"},
        "err_database_not_configured": "Database not configured",
        "err_auth_tables_not_available": "Auth tables unavailable",
        "err_authentication_required": "Authentication required",
        "err_account_disabled": "Account is disabled",
        "err_admin_role_required": "Admin role required",
        "err_operator_role_required": "Operator role required",
        "path_bootstrap": "/bootstrap",
        "path_login": "/login",
    }


def _router(service):
    return create_builder_workflow_run_router(
        project_command_service=service,
        request_scope=lambda *_args, **_kwargs: ScopeContext(
            account_id="account", actor_id="admin"
        ),
        service_error_response=lambda exc: JSONResponse(exc.payload, status_code=exc.status_code),
        api_error_response=lambda _exc: JSONResponse({"error": "failed"}, status_code=500),
    )


def _client(
    *, source="local", role="admin", principal_type="user", normalize=True, full_middleware=False
):
    received = []

    class Service:
        def start_builder_workflow_run(self, workflow_id, command, **kwargs):
            received.append((workflow_id, command, kwargs))
            return SimpleNamespace(
                dump=lambda: {"job_id": "normal-91", "project_id": PROJECT_ID, "status": "pending"}
            )

    app = FastAPI()
    raw_user = _raw_user(source, role, principal_type)

    @app.middleware("http")
    async def principal(request, call_next):
        if full_middleware:
            return await auth_http_runtime.local_auth_middleware(
                request, call_next, **_auth_kwargs(raw_user)
            )
        user = (
            _normalize_user(raw_user, default_auth_source=source).model_dump()
            if normalize
            else raw_user
        )
        request.state.auth_source = user["auth_source"]
        request.state.auth_user = user
        return await call_next(request)

    app.include_router(_router(Service))
    client = TestClient(app)
    if full_middleware:
        client.cookies.set("fs_session", "session-token")
        client.cookies.set("fs_csrf", "csrf-token")
    return client, received


def _body():
    return {
        "project_id": PROJECT_ID,
        "expected_revision": 7,
        "acknowledged": True,
        "summary": "Run saved workflow",
        "inputs": {"supplier": "acme"},
        "files": [],
    }


@pytest.mark.parametrize("source", ["local", "external"])
def test_builder_route_maps_normalized_saved_request_and_exact_job(source):
    client, received = _client(source=source)
    response = client.post(
        "/api/workflows/stock/run", json=_body(), headers={"Idempotency-Key": "retry"}
    )
    assert response.status_code == 200
    assert response.json()["job_id"] == "normal-91"
    workflow, body, kwargs = received[0]
    assert workflow == "stock"
    assert body.inputs == {"supplier": "acme"}
    assert body.expected_revision == 7
    assert kwargs["idempotency_key"] == "retry"
    assert kwargs["scope"].actor_id == "admin"


@pytest.mark.parametrize(
    "source,role,principal_type,header",
    [
        ("pat", "admin", "user", {}),
        ("machine", "admin", "service_account", {}),
        ("mcp_dev", "admin", "system_actor", {}),
        ("local", "admin", "service_account", {}),
        ("external", "admin", "service_account", {}),
        ("local", "operator", "user", {}),
        ("local", "viewer", "user", {}),
        ("external", "operator", "user", {}),
        ("external", "viewer", "user", {}),
        ("local", "admin", "user", {"Authorization": "Bearer project-mcp-token"}),
        ("external", "admin", "user", {"Authorization": "Bearer external-token"}),
    ],
)
def test_builder_route_rejects_tokens_and_readonly_principals(source, role, principal_type, header):
    client, received = _client(source=source, role=role, principal_type=principal_type)
    response = client.post("/api/workflows/stock/run", json=_body(), headers=header)
    assert response.status_code == 403
    assert received == []


@pytest.mark.parametrize("principal_type", ["user", "system_actor"])
def test_builder_route_rejects_forged_local_admin_principal_shape(principal_type):
    client, received = _client(principal_type=principal_type, normalize=False)
    response = client.post("/api/workflows/stock/run", json=_body())
    assert response.status_code == 403
    assert received == []


@pytest.mark.parametrize(
    "headers,expected_status,expected_payload",
    [
        (
            {"x-csrf-token": "csrf-token"},
            200,
            {
                "job_id": "normal-91",
                "project_id": PROJECT_ID,
                "status": "pending",
                "deduplicated": False,
                "queue": "",
            },
        ),
        ({}, 403, {"error": "CSRF validation failed"}),
        (
            {"Authorization": "Bearer session-token"},
            403,
            {"detail": "An administrator browser session is required"},
        ),
    ],
    ids=["cookie-admin", "missing-csrf", "bearer-admin"],
)
def test_builder_route_preserves_real_cookie_middleware_contract(
    headers, expected_status, expected_payload
):
    client, received = _client(full_middleware=True)
    response = client.post("/api/workflows/stock/run", json=_body(), headers=headers)
    assert response.status_code == expected_status
    assert response.json() == expected_payload
    assert len(received) == (1 if expected_status == 200 else 0)


@pytest.mark.parametrize(
    "override",
    [{"test_mode": False}, {"expected_revision": 0}, {"acknowledged": False}, {"extra_state": {}}],
)
def test_builder_route_rejects_untrusted_or_missing_authority(override):
    client, received = _client()
    response = client.post("/api/workflows/stock/run", json={**_body(), **override})
    assert response.status_code == 422
    assert received == []
