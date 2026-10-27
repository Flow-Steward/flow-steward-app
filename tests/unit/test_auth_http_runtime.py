from __future__ import annotations

import asyncio

import pytest
from starlette.requests import Request
from starlette.responses import Response

from core.application.auth import auth_http_runtime, is_admin_only_route, serve_auth_runtime
from core.application.auth.principal import ResolvedPrincipal
from core.application.auth.request_principal_runtime import roles_intersect


def _request(
    path: str, *, method: str = "POST", headers: list[tuple[bytes, bytes]] | None = None
) -> Request:
    request_headers = headers if headers is not None else [(b"cookie", b"session=session-token")]
    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": method,
            "scheme": "http",
            "path": path,
            "raw_path": path.encode(),
            "query_string": b"",
            "headers": request_headers,
            "client": ("testclient", 50000),
            "server": ("testserver", 80),
            "root_path": "",
        }
    )


def test_mcp_dev_system_principal_requires_both_explicit_flag_and_header(
    monkeypatch,
) -> None:
    request = _request(
        "/api/projects",
        method="GET",
        headers=[(b"x-flow-steward-mcp-dev-auth-disabled", b"1")],
    )

    monkeypatch.delenv("FS_MCP_AUTH_DISABLED", raising=False)
    assert auth_http_runtime.mcp_dev_system_principal(request) is None

    monkeypatch.setenv("FS_MCP_AUTH_DISABLED", "1")
    principal = auth_http_runtime.mcp_dev_system_principal(request)

    assert principal is not None
    assert principal.principal_type == "system_actor"
    assert principal.account_id == "00000000-0000-0000-0000-000000000002"
    assert principal.roles == ["admin"]
    assert principal.scopes == ["admin:all"]


def test_mcp_dev_system_principal_rejects_flag_without_internal_header(monkeypatch) -> None:
    monkeypatch.setenv("FS_MCP_AUTH_DISABLED", "1")

    assert (
        auth_http_runtime.mcp_dev_system_principal(
            _request("/api/projects", method="GET", headers=[])
        )
        is None
    )


def _run_middleware_with_principal(
    *,
    path: str,
    method: str = "POST",
    principal: ResolvedPrincipal | None,
    admin_only: bool = False,
    registry_read: bool = False,
    cookie_auth: bool = False,
    csrf_valid: bool = True,
) -> Response:
    async def _call_next(_request: Request) -> Response:
        return Response(status_code=204)

    return asyncio.run(
        auth_http_runtime.local_auth_middleware(
            _request(path, method=method, headers=None if cookie_auth else []),
            _call_next,
            local_auth_enabled_fn=lambda: cookie_auth,
            external_auth_enabled=not cookie_auth,
            is_public_auth_path_fn=lambda _path: False,
            route_registered_fn=lambda _path, _method: True,
            request_principal_resolver_fn=lambda _request, _bl: principal,
            normalize_live_principal_fn=lambda user, **_kwargs: user,
            get_logger_only_fn=lambda: object(),
            auth_tables_available_fn=lambda _bl: True,
            get_user_by_session_token_fn=lambda _bl, _token: principal,
            local_auth_bootstrap_required_fn=lambda _bl=None: False,
            auth_account_tables_available_fn=lambda _bl: False,
            roles_intersect_fn=roles_intersect,
            is_admin_only_route_fn=lambda _path: admin_only,
            is_registry_read_route_fn=lambda _path: registry_read,
            is_self_service_auth_write_route_fn=lambda _path: False,
            csrf_request_valid_fn=lambda _request: csrf_valid,
            auth_cookie_name="session",
            auth_unsafe_methods={"POST", "PUT", "PATCH", "DELETE"},
            auth_admin_roles={"admin"},
            auth_write_roles={"admin", "operator"},
            err_database_not_configured="Database not configured",
            err_auth_tables_not_available="Auth tables unavailable",
            err_authentication_required="Authentication required",
            err_account_disabled="Account is disabled",
            err_admin_role_required="Admin role required",
            err_operator_role_required="Operator role required",
            path_bootstrap="/bootstrap",
            path_login="/login",
        )
    )


def test_both_auth_modes_disabled_fail_closed_in_production(monkeypatch) -> None:
    # Production configuration needs a salt even when this test is the first
    # importer of security.config; do not depend on an earlier dev-mode import.
    monkeypatch.setenv("SECURITY_PII_SALT", "unit-test-production-auth-pii-salt")
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.delenv("FS_AUTH_ALLOW_DISABLED", raising=False)

    async def _call_next(_request: Request) -> Response:
        return Response(status_code=204)

    response = asyncio.run(
        auth_http_runtime.local_auth_middleware(
            _request("/api/projects", method="GET", headers=[]),
            _call_next,
            local_auth_enabled_fn=lambda: False,
            external_auth_enabled=False,
            is_public_auth_path_fn=lambda _path: False,
            route_registered_fn=lambda _path, _method: True,
            request_principal_resolver_fn=lambda _request, _bl: None,
            normalize_live_principal_fn=lambda user, **_kwargs: user,
            get_logger_only_fn=lambda: object(),
            auth_tables_available_fn=lambda _bl: True,
            get_user_by_session_token_fn=lambda _bl, _token: None,
            local_auth_bootstrap_required_fn=lambda _bl=None: False,
            auth_account_tables_available_fn=lambda _bl: False,
            roles_intersect_fn=roles_intersect,
            is_admin_only_route_fn=lambda _path: False,
            is_registry_read_route_fn=lambda _path: False,
            is_self_service_auth_write_route_fn=lambda _path: False,
            csrf_request_valid_fn=lambda _request: True,
            auth_cookie_name="session",
            auth_unsafe_methods={"POST", "PUT", "PATCH", "DELETE"},
            auth_admin_roles={"admin"},
            auth_write_roles={"admin", "operator"},
            err_database_not_configured="Database not configured",
            err_auth_tables_not_available="Auth tables unavailable",
            err_authentication_required="Authentication required",
            err_account_disabled="Account is disabled",
            err_admin_role_required="Admin role required",
            err_operator_role_required="Operator role required",
            path_bootstrap="/bootstrap",
            path_login="/login",
        )
    )

    assert response.status_code == 503
    assert b"Authentication is not configured" in response.body


def test_both_auth_modes_disabled_allows_explicit_dev_override(monkeypatch) -> None:
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("FS_AUTH_ALLOW_DISABLED", "1")

    async def _call_next(_request: Request) -> Response:
        return Response(status_code=204)

    response = asyncio.run(
        auth_http_runtime.local_auth_middleware(
            _request("/api/projects", method="GET", headers=[]),
            _call_next,
            local_auth_enabled_fn=lambda: False,
            external_auth_enabled=False,
            is_public_auth_path_fn=lambda _path: False,
            route_registered_fn=lambda _path, _method: True,
            request_principal_resolver_fn=lambda _request, _bl: None,
            normalize_live_principal_fn=lambda user, **_kwargs: user,
            get_logger_only_fn=lambda: object(),
            auth_tables_available_fn=lambda _bl: True,
            get_user_by_session_token_fn=lambda _bl, _token: None,
            local_auth_bootstrap_required_fn=lambda _bl=None: False,
            auth_account_tables_available_fn=lambda _bl: False,
            roles_intersect_fn=roles_intersect,
            is_admin_only_route_fn=lambda _path: False,
            is_registry_read_route_fn=lambda _path: False,
            is_self_service_auth_write_route_fn=lambda _path: False,
            csrf_request_valid_fn=lambda _request: True,
            auth_cookie_name="session",
            auth_unsafe_methods={"POST", "PUT", "PATCH", "DELETE"},
            auth_admin_roles={"admin"},
            auth_write_roles={"admin", "operator"},
            err_database_not_configured="Database not configured",
            err_auth_tables_not_available="Auth tables unavailable",
            err_authentication_required="Authentication required",
            err_account_disabled="Account is disabled",
            err_admin_role_required="Admin role required",
            err_operator_role_required="Operator role required",
            path_bootstrap="/bootstrap",
            path_login="/login",
        )
    )

    assert response.status_code == 204


def test_email_change_self_service_route_allows_local_viewer_without_operator_role() -> None:
    path = "/api/users/me/email-change/request"
    captured: dict[str, object] = {}

    async def _call_next(request: Request) -> Response:
        captured["auth_user"] = request.state.auth_user
        return Response(status_code=204)

    response = asyncio.run(
        auth_http_runtime.local_auth_middleware(
            _request(path),
            _call_next,
            local_auth_enabled_fn=lambda: True,
            external_auth_enabled=False,
            is_public_auth_path_fn=lambda _path: False,
            route_registered_fn=lambda _path, _method: True,
            request_principal_resolver_fn=lambda _request, _bl: None,
            normalize_live_principal_fn=lambda user, **_kwargs: ResolvedPrincipal(
                id=str(user["id"]),
                username=str(user["username"]),
                roles=list(user["roles"]),
                auth_source="local",
            ),
            get_logger_only_fn=lambda: object(),
            auth_tables_available_fn=lambda _bl: True,
            get_user_by_session_token_fn=lambda _bl, _token: {
                "id": "viewer-1",
                "username": "viewer",
                "roles": ["viewer"],
                "is_active": True,
            },
            local_auth_bootstrap_required_fn=lambda _bl=None: False,
            auth_account_tables_available_fn=lambda _bl: False,
            roles_intersect_fn=roles_intersect,
            is_admin_only_route_fn=lambda _path: False,
            is_registry_read_route_fn=lambda _path: False,
            is_self_service_auth_write_route_fn=serve_auth_runtime.is_self_service_auth_write_route,
            csrf_request_valid_fn=lambda _request: True,
            auth_cookie_name="session",
            auth_unsafe_methods={"POST", "PUT", "PATCH", "DELETE"},
            auth_admin_roles={"admin"},
            auth_write_roles={"admin", "operator"},
            err_database_not_configured="Database not configured",
            err_auth_tables_not_available="Auth tables unavailable",
            err_authentication_required="Authentication required",
            err_account_disabled="Account is disabled",
            err_admin_role_required="Admin role required",
            err_operator_role_required="Operator role required",
            path_bootstrap="/bootstrap",
            path_login="/login",
        )
    )

    assert response.status_code == 204
    assert captured["auth_user"]["roles"] == ["viewer"]


def test_pat_self_service_token_create_still_requires_command_scope(monkeypatch) -> None:
    path = "/api/auth/tokens"
    monkeypatch.setattr(
        "core.application.auth.resolve.resolve_pat_principal",
        lambda _bl, _token: {
            "id": "user-1",
            "username": "operator",
            "roles": ["operator"],
            "auth_source": "pat",
            "scopes": ["notify:dispatch"],
            "is_active": True,
        },
    )
    monkeypatch.setattr(
        "core.application.auth.resolve.resolve_machine_principal",
        lambda _bl, _token: None,
    )

    async def _call_next(_request: Request) -> Response:
        return Response(status_code=204)

    response = asyncio.run(
        auth_http_runtime.local_auth_middleware(
            _request(
                path,
                headers=[(b"authorization", b"Bearer scoped-token")],
            ),
            _call_next,
            local_auth_enabled_fn=lambda: True,
            external_auth_enabled=False,
            is_public_auth_path_fn=lambda _path: False,
            route_registered_fn=lambda _path, _method: True,
            request_principal_resolver_fn=lambda _request, _bl: None,
            normalize_live_principal_fn=lambda user, **_kwargs: ResolvedPrincipal(
                id=str(user["id"]),
                username=str(user["username"]),
                roles=list(user["roles"]),
                auth_source=str(user.get("auth_source") or "local"),
                scopes=list(user.get("scopes") or []),
            ),
            get_logger_only_fn=lambda: object(),
            auth_tables_available_fn=lambda _bl: True,
            get_user_by_session_token_fn=lambda _bl, _token: None,
            local_auth_bootstrap_required_fn=lambda _bl=None: False,
            auth_account_tables_available_fn=lambda _bl: False,
            roles_intersect_fn=roles_intersect,
            is_admin_only_route_fn=lambda _path: False,
            is_registry_read_route_fn=lambda _path: False,
            is_self_service_auth_write_route_fn=lambda candidate: candidate == path,
            csrf_request_valid_fn=lambda _request: True,
            auth_cookie_name="session",
            auth_unsafe_methods={"POST", "PUT", "PATCH", "DELETE"},
            auth_admin_roles={"admin"},
            auth_write_roles={"admin", "operator"},
            err_database_not_configured="Database not configured",
            err_auth_tables_not_available="Auth tables unavailable",
            err_authentication_required="Authentication required",
            err_account_disabled="Account is disabled",
            err_admin_role_required="Admin role required",
            err_operator_role_required="Operator role required",
            path_bootstrap="/bootstrap",
            path_login="/login",
        )
    )

    assert response.status_code == 403
    assert b"Missing required token scope" in response.body


@pytest.mark.parametrize(
    "path",
    [
        "/api/auth/optional-self-service/setup",
        "/api/auth/optional-self-service/confirm",
        "/api/auth/optional-self-service/remove",
    ],
)
def test_pat_optional_self_service_routes_still_require_command_scope(
    monkeypatch,
    path: str,
) -> None:
    monkeypatch.setattr(
        "core.application.auth.resolve.resolve_pat_principal",
        lambda _bl, _token: {
            "id": "user-1",
            "username": "operator",
            "roles": ["operator"],
            "auth_source": "pat",
            "scopes": ["notify:dispatch"],
            "is_active": True,
        },
    )
    monkeypatch.setattr(
        "core.application.auth.resolve.resolve_machine_principal",
        lambda _bl, _token: None,
    )

    async def _call_next(_request: Request) -> Response:
        return Response(status_code=204)

    response = asyncio.run(
        auth_http_runtime.local_auth_middleware(
            _request(
                path,
                headers=[(b"authorization", b"Bearer scoped-token")],
            ),
            _call_next,
            local_auth_enabled_fn=lambda: True,
            external_auth_enabled=False,
            is_public_auth_path_fn=lambda _path: False,
            route_registered_fn=lambda _path, _method: True,
            request_principal_resolver_fn=lambda _request, _bl: None,
            normalize_live_principal_fn=lambda user, **_kwargs: ResolvedPrincipal(
                id=str(user["id"]),
                username=str(user["username"]),
                roles=list(user["roles"]),
                auth_source=str(user.get("auth_source") or "local"),
                scopes=list(user.get("scopes") or []),
            ),
            get_logger_only_fn=lambda: object(),
            auth_tables_available_fn=lambda _bl: True,
            get_user_by_session_token_fn=lambda _bl, _token: None,
            local_auth_bootstrap_required_fn=lambda _bl=None: False,
            auth_account_tables_available_fn=lambda _bl: False,
            roles_intersect_fn=roles_intersect,
            is_admin_only_route_fn=lambda _path: False,
            is_registry_read_route_fn=lambda _path: False,
            is_self_service_auth_write_route_fn=lambda candidate: candidate == path,
            csrf_request_valid_fn=lambda _request: True,
            auth_cookie_name="session",
            auth_unsafe_methods={"POST", "PUT", "PATCH", "DELETE"},
            auth_admin_roles={"admin"},
            auth_write_roles={"admin", "operator"},
            err_database_not_configured="Database not configured",
            err_auth_tables_not_available="Auth tables unavailable",
            err_authentication_required="Authentication required",
            err_account_disabled="Account is disabled",
            err_admin_role_required="Admin role required",
            err_operator_role_required="Operator role required",
            path_bootstrap="/bootstrap",
            path_login="/login",
        )
    )

    assert response.status_code == 403
    assert b"Missing required token scope" in response.body


def test_pat_write_requires_command_scope() -> None:
    response = _run_middleware_with_principal(
        path="/api/projects",
        principal=ResolvedPrincipal(
            id="pat-1",
            username="pat",
            roles=["operator"],
            auth_source="pat",
            scopes=["notify:dispatch"],
        ),
    )

    assert response.status_code == 403
    assert b"Missing required token scope" in response.body


def test_pat_write_allows_command_scope() -> None:
    response = _run_middleware_with_principal(
        path="/api/projects",
        principal=ResolvedPrincipal(
            id="pat-1",
            username="pat",
            roles=["operator"],
            auth_source="pat",
            scopes=["command:*"],
        ),
    )

    assert response.status_code == 204


def test_machine_notification_dispatch_allows_notify_scope() -> None:
    response = _run_middleware_with_principal(
        path="/api/projects/7/notifications/dispatch",
        principal=ResolvedPrincipal(
            id="svc-1",
            username="svc",
            roles=["operator"],
            auth_source="machine",
            principal_type="service_account",
            scopes=["notify:dispatch"],
        ),
    )

    assert response.status_code == 204


def test_machine_external_agent_route_allows_matching_agent_scope() -> None:
    response = _run_middleware_with_principal(
        path="/api/external-agents/claim",
        principal=ResolvedPrincipal(
            id="svc-1",
            username="svc",
            roles=["operator"],
            auth_source="machine",
            principal_type="service_account",
            scopes=["agent:claim"],
        ),
    )

    assert response.status_code == 204


def test_machine_external_agent_route_rejects_wrong_agent_scope() -> None:
    response = _run_middleware_with_principal(
        path="/api/external-agents/claim",
        principal=ResolvedPrincipal(
            id="svc-1",
            username="svc",
            roles=["operator"],
            auth_source="machine",
            principal_type="service_account",
            scopes=["agent:heartbeat"],
        ),
    )

    assert response.status_code == 403
    assert b"Missing required token scope" in response.body


def test_machine_admin_route_requires_admin_scope_even_for_admin_role() -> None:
    response = _run_middleware_with_principal(
        path="/api/preview",
        method="GET",
        admin_only=True,
        principal=ResolvedPrincipal(
            id="svc-1",
            username="svc",
            roles=["admin"],
            auth_source="machine",
            principal_type="service_account",
            scopes=["command:*"],
        ),
    )

    assert response.status_code == 403
    assert b"Missing required token scope" in response.body


@pytest.mark.parametrize(
    "path,method",
    [
        ("/api/auth/users", "GET"),
        ("/api/auth/users", "POST"),
        ("/api/auth/users/77", "PUT"),
        ("/api/auth/accounts", "GET"),
        ("/api/auth/accounts", "POST"),
        ("/api/auth/accounts/acc-1/disable", "POST"),
        ("/api/model-providers", "POST"),
        ("/api/model-providers/provider-1", "PUT"),
        ("/api/model-providers/provider-1", "DELETE"),
    ],
)
def test_auth_admin_routes_require_admin_all_token_scope(path: str, method: str) -> None:
    assert is_admin_only_route(path) is True

    response = _run_middleware_with_principal(
        path=path,
        method=method,
        admin_only=is_admin_only_route(path),
        principal=ResolvedPrincipal(
            id="pat-1",
            username="admin-pat",
            roles=["admin"],
            auth_source="pat",
            scopes=["command:*"],
        ),
    )

    assert response.status_code == 403
    assert b"Missing required token scope" in response.body


@pytest.mark.parametrize(
    "path",
    [
        "/api/projects/proj-1/workflows/weekly_digest/contract",
        "/api/projects/proj-1/workflows/weekly_digest/validate",
        "/api/projects/proj-1/workflows/weekly_digest/suggestions",
    ],
)
def test_project_workflow_diagnostics_are_not_admin_only(path: str) -> None:
    # Read-only project workflow diagnostics are operator/viewer-readable for parity
    # with workflow-catalog / workflow-entrypoints (which are not admin-only).
    assert is_admin_only_route(path) is False


@pytest.mark.parametrize(
    "path",
    [
        "/api/projects/proj-1/workflows",  # PUT bindings (write) stays admin
        "/api/projects/proj-1/providers/openai",  # provider mgmt stays admin
    ],
)
def test_project_workflow_writes_and_providers_stay_admin_only(path: str) -> None:
    assert is_admin_only_route(path) is True


@pytest.mark.parametrize(
    "path",
    [
        "/api/workflows",
        "/api/workflows/weekly_digest",
        "/api/workflows/weekly_digest/contract",
    ],
)
def test_global_workflow_catalog_is_not_admin_only(path: str) -> None:
    # The global workflow registry catalog is operator-readable via the registry-read
    # gate, not admin-only. Keeping it out of the admin-only list is what lets
    # operators load the catalog (writes are still admin via the registry-write gate).
    assert is_admin_only_route(path) is False


def test_operator_can_read_global_workflow_catalog() -> None:
    response = _run_middleware_with_principal(
        path="/api/workflows",
        method="GET",
        registry_read=True,
        principal=ResolvedPrincipal(
            id="op-1", username="operator", roles=["operator"], auth_source="session"
        ),
    )
    assert response.status_code == 204


def test_viewer_cannot_read_global_workflow_catalog() -> None:
    response = _run_middleware_with_principal(
        path="/api/workflows",
        method="GET",
        registry_read=True,
        principal=ResolvedPrincipal(
            id="vw-1", username="viewer", roles=["viewer"], auth_source="session"
        ),
    )
    assert response.status_code == 403
    assert b"Operator role required" in response.body


def test_viewer_cannot_read_extension_archive_lifecycle_capability() -> None:
    response = _run_middleware_with_principal(
        path="/api/extensions/capabilities/archive-lifecycle",
        method="GET",
        registry_read=True,
        principal=ResolvedPrincipal(
            id="vw-1", username="viewer", roles=["viewer"], auth_source="session"
        ),
    )

    assert response.status_code == 403
    assert b"Operator role required" in response.body


@pytest.mark.parametrize(
    "path",
    [
        "/api/extensions/fixture.extension",
        "/api/extensions/fixture.extension/compiled-manifest",
    ],
)
@pytest.mark.parametrize("role", ["admin", "operator", "viewer"])
def test_authenticated_roles_can_read_exact_extension_detail(path: str, role: str) -> None:
    response = _run_middleware_with_principal(
        path=path,
        method="GET",
        registry_read=True,
        principal=ResolvedPrincipal(
            id=f"{role}-1", username=role, roles=[role], auth_source="session"
        ),
    )

    assert response.status_code == 204


@pytest.mark.parametrize(
    "path",
    [
        "/api/extensions/fixture.extension",
        "/api/extensions/fixture.extension/compiled-manifest",
    ],
)
def test_anonymous_cannot_read_exact_extension_detail(path: str) -> None:
    response = _run_middleware_with_principal(
        path=path,
        method="GET",
        registry_read=True,
        principal=None,
    )

    assert response.status_code == 401
    assert b"Authentication required" in response.body


@pytest.mark.parametrize(
    "path",
    [
        "/api/extensions",
        "/api/extensions/marketplace",
        "/api/extensions/fixture.extension/preview",
        "/api/extensions/fixture.extension/callbacks",
        "/api/extensions/fixture.extension/uninstall-plan",
    ],
)
def test_viewer_cannot_read_extension_admin_surfaces(path: str) -> None:
    response = _run_middleware_with_principal(
        path=path,
        method="GET",
        registry_read=True,
        principal=ResolvedPrincipal(
            id="viewer-1", username="viewer", roles=["viewer"], auth_source="session"
        ),
    )

    assert response.status_code == 403
    assert b"Operator role required" in response.body


def test_operator_cannot_write_global_workflow_catalog() -> None:
    response = _run_middleware_with_principal(
        path="/api/workflows",
        method="POST",
        registry_read=True,
        principal=ResolvedPrincipal(
            id="op-1", username="operator", roles=["operator"], auth_source="session"
        ),
    )
    assert response.status_code == 403
    assert b"Admin role required" in response.body


@pytest.mark.parametrize(
    "path",
    [
        "/api/extensions/fixture.extension/callbacks/updates",
        "/api/extensions/fixture.extension/callbacks/registration-1/rotate",
        "/api/extensions/fixture.extension/callbacks/registration-1/status",
    ],
)
@pytest.mark.parametrize("role", ["admin", "operator"])
def test_project_managers_can_write_exact_extension_callback_routes(path: str, role: str) -> None:
    response = _run_middleware_with_principal(
        path=path,
        method="POST",
        registry_read=True,
        principal=ResolvedPrincipal(
            id=f"{role}-1", username=role, roles=[role], auth_source="session"
        ),
    )

    assert response.status_code == 204


def test_operator_can_invoke_exact_project_scoped_extension_action_route() -> None:
    response = _run_middleware_with_principal(
        path="/api/extensions/fixture.extension/action/test_runtime_connection",
        method="POST",
        registry_read=True,
        principal=ResolvedPrincipal(
            id="op-1", username="operator", roles=["operator"], auth_source="session"
        ),
    )

    assert response.status_code == 204


def test_viewer_cannot_invoke_exact_project_scoped_extension_action_route() -> None:
    response = _run_middleware_with_principal(
        path="/api/extensions/fixture.extension/action/test_runtime_connection",
        method="POST",
        registry_read=True,
        principal=ResolvedPrincipal(
            id="viewer-1", username="viewer", roles=["viewer"], auth_source="session"
        ),
    )

    assert response.status_code == 403
    assert b"Operator role required" in response.body


@pytest.mark.parametrize(
    ("scopes", "expected_status"),
    [
        (["projects.write"], 204),
        (["command:*"], 204),
        (["notify:dispatch"], 403),
    ],
)
def test_operator_pat_extension_action_requires_project_or_command_scope(
    scopes: list[str], expected_status: int
) -> None:
    response = _run_middleware_with_principal(
        path="/api/extensions/fixture.extension/action/test_runtime_connection",
        method="POST",
        registry_read=True,
        principal=ResolvedPrincipal(
            id="pat-1",
            username="operator-pat",
            roles=["operator"],
            auth_source="pat",
            scopes=scopes,
        ),
    )

    assert response.status_code == expected_status


def test_extension_action_route_near_miss_remains_admin_only() -> None:
    response = _run_middleware_with_principal(
        path="/api/extensions/fixture.extension/action/test_runtime_connection/extra",
        method="POST",
        registry_read=True,
        principal=ResolvedPrincipal(
            id="op-1", username="operator", roles=["operator"], auth_source="session"
        ),
    )

    assert response.status_code == 403
    assert b"Admin role required" in response.body


@pytest.mark.parametrize(
    "path",
    [
        "/api/extensions/fixture.extension/callbacks/updates",
        "/api/extensions/fixture.extension/callbacks/registration-1/rotate",
        "/api/extensions/fixture.extension/callbacks/registration-1/status",
    ],
)
def test_viewer_cannot_write_extension_callback_routes(path: str) -> None:
    response = _run_middleware_with_principal(
        path=path,
        method="POST",
        registry_read=True,
        principal=ResolvedPrincipal(
            id="viewer-1", username="viewer", roles=["viewer"], auth_source="session"
        ),
    )

    assert response.status_code == 403
    assert b"Operator role required" in response.body


def test_operator_pat_can_write_project_callback_with_project_scope() -> None:
    response = _run_middleware_with_principal(
        path="/api/extensions/fixture.extension/callbacks/updates",
        method="POST",
        registry_read=True,
        principal=ResolvedPrincipal(
            id="pat-1",
            username="operator-pat",
            roles=["operator"],
            auth_source="pat",
            scopes=["projects.write"],
        ),
    )

    assert response.status_code == 204


def test_operator_pat_cannot_write_project_callback_with_unrelated_scope() -> None:
    response = _run_middleware_with_principal(
        path="/api/extensions/fixture.extension/callbacks/updates",
        method="POST",
        registry_read=True,
        principal=ResolvedPrincipal(
            id="pat-1",
            username="operator-pat",
            roles=["operator"],
            auth_source="pat",
            scopes=["notify:dispatch"],
        ),
    )

    assert response.status_code == 403
    assert b"Missing required token scope" in response.body


def test_callback_write_near_miss_remains_admin_only() -> None:
    response = _run_middleware_with_principal(
        path="/api/extensions/fixture.extension/callbacks/updates/delete",
        method="POST",
        registry_read=True,
        principal=ResolvedPrincipal(
            id="op-1", username="operator", roles=["operator"], auth_source="session"
        ),
    )

    assert response.status_code == 403
    assert b"Admin role required" in response.body


def test_auth_admin_routes_allow_admin_all_token_scope() -> None:
    path = "/api/auth/accounts"
    assert is_admin_only_route(path) is True

    response = _run_middleware_with_principal(
        path=path,
        method="POST",
        admin_only=is_admin_only_route(path),
        principal=ResolvedPrincipal(
            id="pat-1",
            username="admin-pat",
            roles=["admin"],
            auth_source="pat",
            scopes=["admin:all"],
        ),
    )

    assert response.status_code == 204


@pytest.mark.parametrize(
    "path",
    [
        "/api/extensions/descriptors/import/file",
        "/api/extensions/descriptors/import/url",
        "/api/extensions/runtime/token",
    ],
)
def test_extension_lifecycle_routes_are_admin_only(path: str) -> None:
    # FS-DEEP-CAND-042/043/044: these lifecycle/runtime routes must be matched
    # by an admin-only middleware pattern (operators are rejected at the gate).
    assert is_admin_only_route(path) is True


@pytest.mark.parametrize(
    "path",
    [
        "/api/extensions",
        "/api/extensions/fixture.extension/invoke",
        "/api/extensions/fixture.extension/action/toggle_route",
        "/api/extensions/fixture.extension/query/list",
        "/api/extensions/install",
        "/api/extensions/scan",
    ],
)
def test_operator_reachable_extension_routes_are_not_admin_only(path: str) -> None:
    # The new lifecycle patterns must NOT over-gate operator-reachable extension
    # routes (invoke/action/query/list/install/scan stay non-admin at the route
    # layer; their own service-layer scope checks apply).
    assert is_admin_only_route(path) is False


@pytest.mark.parametrize(
    "path",
    [
        # FS-DEEP-CAND-017: watchdog recovery (kill/retry) is destructive.
        "/api/watchdog/jobs/job-123/recover",
        "/api/watchdog/jobs/abc-def/recover",
    ],
)
def test_watchdog_recover_routes_are_admin_only(path: str) -> None:
    assert is_admin_only_route(path) is True


@pytest.mark.parametrize(
    "path",
    [
        # The read-only watchdog scan (GET) stays operator-readable; only the POST
        # recover mutation is admin-gated.
        "/api/watchdog/jobs/scan",
    ],
)
def test_watchdog_scan_read_route_is_not_admin_only(path: str) -> None:
    assert is_admin_only_route(path) is False


def test_machine_jobs_read_requires_jobs_read_scope() -> None:
    response = _run_middleware_with_principal(
        path="/api/jobs",
        method="GET",
        principal=ResolvedPrincipal(
            id="svc-1",
            username="svc",
            roles=["operator"],
            auth_source="machine",
            principal_type="service_account",
            scopes=["mcp:use"],
        ),
    )

    assert response.status_code == 403
    assert b"Missing required token scope" in response.body


def test_machine_jobs_read_allows_jobs_read_scope() -> None:
    response = _run_middleware_with_principal(
        path="/api/jobs",
        method="GET",
        principal=ResolvedPrincipal(
            id="svc-1",
            username="svc",
            roles=["operator"],
            auth_source="machine",
            principal_type="service_account",
            scopes=["jobs.read"],
        ),
    )

    assert response.status_code == 204


def _pat(*, roles: list[str], scopes: list[str]) -> ResolvedPrincipal:
    return ResolvedPrincipal(
        id="user-1", username="owner", roles=roles, auth_source="pat", scopes=scopes
    )


@pytest.mark.parametrize("scope,expected", [("workflows.write", 204), ("jobs.write", 403)])
def test_direct_workflow_launch_requires_workflow_write_scope(scope, expected):
    response = _run_middleware_with_principal(
        path="/api/workflow-runs",
        method="POST",
        principal=_pat(roles=["operator"], scopes=[scope]),
    )
    assert response.status_code == expected


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("POST", "/api/projects"),
        ("PUT", "/api/projects/p1"),
        ("PUT", "/api/projects/p1/policy"),
        ("PUT", "/api/projects/p1/workflows"),
    ],
)
def test_a_ui_token_with_projects_write_configures_a_project(method, path) -> None:
    """T1 acceptance: PUT .../workflows still answered 403 "Missing required token
    scope" -- the path is admin-only, and the admin branch ran before projects.write.
    Through the middleware, with the route flags the application computes."""
    from core.infrastructure.runtime.http_app_auth_runtime import (
        _is_admin_only_route,
        _is_registry_read_route,
    )

    response = _run_middleware_with_principal(
        path=path,
        method=method,
        principal=_pat(roles=["admin"], scopes=["projects.write"]),
        admin_only=bool(_is_admin_only_route(path)),
        registry_read=bool(_is_registry_read_route(path)),
    )

    assert response.status_code == 204


def test_linking_workflows_stays_an_administrators_whatever_the_scope() -> None:
    """projects.write lets an administrator delegate it, never an operator do it."""
    response = _run_middleware_with_principal(
        path="/api/projects/p1/workflows",
        method="PUT",
        principal=_pat(roles=["operator"], scopes=["projects.write"]),
        admin_only=True,
    )

    assert response.status_code == 403
    assert b"Admin role required" in response.body


@pytest.mark.parametrize("role", ["viewer", "operator"])
def test_authoring_validate_allows_current_workflow_read_roles_with_read_token(role):
    response = _run_middleware_with_principal(
        path="/api/projects/p1/workflow-authoring/validate",
        principal=_pat(roles=[role], scopes=["workflows.read"]),
    )
    assert response.status_code == 204


@pytest.mark.parametrize(
    "roles,scopes,expected",
    [
        (["admin"], ["admin:all"], 204),
        (["admin"], ["workflows.write"], 403),
        (["operator"], ["admin:all"], 403),
    ],
)
def test_authoring_save_requires_both_current_admin_role_and_scope(roles, scopes, expected):
    for path in (
        "/api/projects/p1/workflow-authoring/drafts",
        "/api/projects/p1/workflow-imports",
    ):
        response = _run_middleware_with_principal(
            path=path,
            principal=_pat(roles=roles, scopes=scopes),
        )
        assert response.status_code == expected, path


@pytest.mark.parametrize(
    "path,method",
    [
        ("/api/projects/p1/workflow-authoring/validate/extra", "POST"),
        ("/api/projects/p1/workflow-authoring/validate", "PUT"),
    ],
)
def test_authoring_validate_near_misses_do_not_skip_write_role_gate(path, method):
    response = _run_middleware_with_principal(
        path=path,
        method=method,
        principal=_pat(roles=["viewer"], scopes=["admin:all"]),
    )
    assert response.status_code == 403


@pytest.mark.parametrize("csrf_valid,expected", [(False, 403), (True, 204)])
def test_authoring_read_post_preserves_cookie_csrf_enforcement(csrf_valid, expected):
    response = _run_middleware_with_principal(
        path="/api/projects/p1/workflow-authoring/validate",
        cookie_auth=True,
        csrf_valid=csrf_valid,
        principal=ResolvedPrincipal(
            id="viewer", username="viewer", roles=["viewer"], auth_source="local"
        ),
    )
    assert response.status_code == expected
    if not csrf_valid:
        assert b"CSRF validation failed" in response.body


@pytest.mark.parametrize(
    "method,suffix,allowed_scope",
    [
        ("GET", "", "projects.read"),
        ("POST", "/effect/resolution", "projects.write"),
    ],
)
def test_direct_effect_recovery_routes_use_project_token_scopes(method, suffix, allowed_scope):
    principal = ResolvedPrincipal(
        username="operator",
        roles=["operator"],
        scopes=[allowed_scope],
        auth_source="pat",
        principal_type="operator",
    )
    response = _run_middleware_with_principal(
        path=f"/api/projects/project/extensions/install/external-effects{suffix}",
        method=method,
        principal=principal,
    )
    assert response.status_code == 204
    denied = principal.model_copy(update={"scopes": ["jobs.read"]})
    assert (
        _run_middleware_with_principal(
            path=f"/api/projects/project/extensions/install/external-effects{suffix}",
            method=method,
            principal=denied,
        ).status_code
        == 403
    )


def test_direct_effect_resolution_denies_viewers_and_invalid_csrf():
    path = "/api/projects/project/extensions/install/external-effects/effect/resolution"
    viewer = ResolvedPrincipal(
        username="viewer",
        roles=["viewer"],
        scopes=["projects.write"],
        auth_source="pat",
        principal_type="viewer",
    )
    assert _run_middleware_with_principal(path=path, principal=viewer).status_code == 403
    operator = ResolvedPrincipal(
        username="operator",
        roles=["operator"],
        scopes=["projects.write"],
        auth_source="local",
        principal_type="operator",
    )
    assert (
        _run_middleware_with_principal(
            path=path, principal=operator, cookie_auth=True, csrf_valid=False
        ).status_code
        == 403
    )


def test_near_miss_direct_resolution_path_does_not_gain_project_write_scope():
    principal = ResolvedPrincipal(
        username="operator",
        roles=["operator"],
        scopes=["projects.write"],
        auth_source="pat",
        principal_type="operator",
    )
    assert (
        _run_middleware_with_principal(
            path="/api/projects/project/extensions/install/external-effects/effect/resolution/other",
            principal=principal,
        ).status_code
        == 403
    )


@pytest.mark.parametrize(
    "role,csrf_valid,expected",
    [
        ("admin", True, 204),
        ("admin", False, 403),
        ("operator", True, 403),
        ("viewer", True, 403),
    ],
)
def test_builder_normal_run_preserves_editor_role_and_cookie_csrf(role, csrf_valid, expected):
    from core.infrastructure.runtime.http_app_constants import _AUTH_REGISTRY_READ_PATTERNS
    from core.infrastructure.runtime.serve_request_runtime import build_path_pattern_matcher

    is_registry_read_route = build_path_pattern_matcher(patterns=_AUTH_REGISTRY_READ_PATTERNS)

    path = "/api/workflows/stock/run"
    response = _run_middleware_with_principal(
        path=path,
        cookie_auth=True,
        csrf_valid=csrf_valid,
        admin_only=is_admin_only_route(path),
        registry_read=is_registry_read_route(path),
        principal=ResolvedPrincipal(id="user", username="user", roles=[role], auth_source="local"),
    )
    assert response.status_code == expected
