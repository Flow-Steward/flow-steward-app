"""ADR 0066 amendment (ADR 0075): admin configures, operator works, viewer reads.

The matrix runs through the real middleware with the route flags the application
computes, for a browser session, a person's own token and a service account. People
are decided by the ADR 0076 route table and the stored role permissions.
"""

from __future__ import annotations

import asyncio
import re

import pytest
from starlette.requests import Request
from starlette.responses import Response

from core.application.auth import auth_http_runtime
from core.application.auth.permissions import OPERATOR_PERMISSIONS, SYSTEM_ROLE_PERMISSIONS
from core.application.auth.principal import ResolvedPrincipal
from core.application.auth.request_principal_runtime import roles_intersect
from core.application.auth.route_permissions import route_allowed
from core.entrypoints.mcp.server_api_client import MCP_TOOL_ROUTES
from core.infrastructure.runtime.http_app_auth_runtime import (
    _is_registry_read_route,
)


def _request(path: str, method: str) -> Request:
    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": method,
            "scheme": "http",
            "path": path,
            "raw_path": path.encode(),
            "query_string": b"",
            "headers": [],
            "client": ("testclient", 50000),
            "server": ("testserver", 80),
            "root_path": "",
        }
    )


def _run(path: str, method: str, principal: ResolvedPrincipal) -> Response:
    async def _call_next(_request: Request) -> Response:
        return Response(status_code=204)

    return asyncio.run(
        auth_http_runtime.local_auth_middleware(
            _request(path, method),
            _call_next,
            local_auth_enabled_fn=lambda: False,
            external_auth_enabled=True,
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
            is_registry_read_route_fn=lambda candidate: bool(_is_registry_read_route(candidate)),
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
            load_role_permissions_fn=lambda _bl: SYSTEM_ROLE_PERMISSIONS,
        )
    )


def _person(role: str) -> ResolvedPrincipal:
    return ResolvedPrincipal(id=f"{role}-1", username=role, roles=[role], auth_source="external")


def _personal_token(role: str) -> ResolvedPrincipal:
    """A person's own token (PAT, agent access, MCP OAuth): the owner's role, broad scopes."""
    return ResolvedPrincipal(
        id=f"{role}-pat",
        username=role,
        roles=[role],
        auth_source="pat",
        # The widest token a person could hold: the owner's role still decides.
        scopes=[
            "admin:all",
            "command:*",
            "workflows.write",
            "jobs.write",
            "reports.write",
            "secrets:write",
            "secrets:bind",
        ],
    )


def _service_account(scopes: list[str]) -> ResolvedPrincipal:
    return ResolvedPrincipal(
        id="sa-1",
        username="robot",
        roles=["operator"],
        auth_source="machine",
        principal_type="service_account",
        scopes=scopes,
    )


OPERATOR_WORK = [
    ("POST", "/api/workflows/wf-1/run"),
    ("POST", "/api/workflow-runs"),
    ("POST", "/api/approvals/thread-1"),
    ("POST", "/api/jobs/job-1/cancel"),
    ("POST", "/api/jobs/job-1/restart"),
    ("POST", "/api/jobs/job-1/retry-step"),
    ("POST", "/api/jobs/job-1/acknowledge"),
    ("POST", "/api/jobs/job-1/unacknowledge"),
    ("POST", "/api/jobs/job-1/decision-overrides"),
    ("POST", "/api/reports/generate"),
    ("POST", "/api/projects/p1/notifications/dispatch"),
    ("POST", "/api/projects/p1/workflow-preview"),
    ("POST", "/api/secrets"),
    ("PATCH", "/api/secrets/s1"),
    ("PUT", "/api/secrets/s1/value"),
    ("POST", "/api/extensions/fixture.extension/action/sync"),
    ("POST", "/api/extensions/fixture.extension/query/orders"),
    ("POST", "/api/projects/p1/extensions/i1/external-effects/e1/resolution"),
    ("PATCH", "/api/users/me/locale"),
    ("POST", "/api/auth/agent-access/tokens"),
]

MOVED_TO_ADMIN = [
    # The project's configuration, including the general update that changed
    # workflow bindings and policy past their admin-only routes.
    ("POST", "/api/projects"),
    ("PUT", "/api/projects/p1"),
    ("PUT", "/api/projects/p1/workflows"),
    ("PUT", "/api/projects/p1/policy"),
    ("POST", "/api/projects/p1/move-workspace"),
    ("POST", "/api/projects/p1/notification-routes"),
    ("POST", "/api/projects/p1/history/r1/restore"),
    ("POST", "/api/projects/p1/jobs/bulk-delete"),
    ("POST", "/api/projects/p1/scheduled-jobs"),
    ("PUT", "/api/projects/p1/scheduled-jobs/sj1"),
    # Connections, their credentials and the workflow's choice of connection.
    ("POST", "/api/projects/p1/connections"),
    ("PUT", "/api/projects/p1/connections/c1"),
    ("POST", "/api/projects/p1/connections/c1/secrets"),
    ("POST", "/api/projects/p1/connections/c1/oauth/start"),
    ("PUT", "/api/projects/p1/workflows/wf-1/connection-slots/crm"),
    # Secret configuration: binding, unbinding, approval, deletion.
    ("PUT", "/api/projects/p1/connections/c1/secret-bindings/api_key"),
    ("DELETE", "/api/projects/p1/connections/c1/secret-bindings/api_key"),
    ("PUT", "/api/projects/p1/workflows/wf-1/secret-bindings/api_key"),
    ("DELETE", "/api/projects/p1/workflows/wf-1/secret-bindings/api_key"),
    ("POST", "/api/projects/p1/connections/c1/secret-approval"),
    ("POST", "/api/projects/p1/workflows/wf-1/secret-approval"),
    ("POST", "/api/projects/p1/secret-approvals"),
    ("DELETE", "/api/secrets/s1"),
    # Extension wiring, diagnostics and generic commands.
    ("POST", "/api/extensions/fixture.extension/callbacks/updates"),
    ("POST", "/api/extensions/fixture.extension/invoke"),
    ("POST", "/api/jobs/job-1/replay-step/execute"),
    ("POST", "/api/events/ev-1/redrive"),
    ("POST", "/api/commands"),
    # Authoring stays the admin's, as before.
    ("POST", "/api/workflows/wf-1/changes"),
    ("POST", "/api/projects/p1/workflow-authoring/drafts"),
]


#: A token cannot mint another token: these stay interactive-only, as before.
_INTERACTIVE_ONLY = {("POST", "/api/auth/agent-access/tokens")}


@pytest.mark.parametrize(("method", "path"), OPERATOR_WORK)
@pytest.mark.parametrize("make_principal", [_person, _personal_token])
def test_operator_does_the_work_in_every_channel(method, path, make_principal) -> None:
    response = _run(path, method, make_principal("operator"))

    if make_principal is _personal_token and (method, path) in _INTERACTIVE_ONLY:
        assert response.status_code == 403
        assert b"interactive browser session" in response.body
        return
    assert response.status_code == 204, response.body


@pytest.mark.parametrize(("method", "path"), OPERATOR_WORK)
def test_viewer_makes_no_operator_work_write(method, path) -> None:
    response = _run(path, method, _person("viewer"))

    assert response.status_code == 403


def _password_session(role: str) -> ResolvedPrincipal:
    return ResolvedPrincipal(id=f"{role}-local", username=role, roles=[role], auth_source="local")


@pytest.mark.parametrize("role", ["viewer", "operator", "admin"])
@pytest.mark.parametrize("make_principal", [_password_session, _personal_token])
def test_viewer_can_switch_account(role, make_principal) -> None:
    """ADR 0085: every role moves to another of its companies (the route checks the role
    there); a viewer is not stuck in the company they act in."""
    response = _run("/api/auth/switch-account", "POST", make_principal(role))

    assert response.status_code == 204, response.body


def test_an_external_viewer_session_still_needs_the_external_own_account_permission() -> None:
    """An external session is pinned to its account (the route refuses it, ADR 0083); its
    own-account writes keep the open owner question's rule."""
    assert _run("/api/auth/switch-account", "POST", _person("viewer")).status_code == 403


@pytest.mark.parametrize(("method", "path"), MOVED_TO_ADMIN)
@pytest.mark.parametrize("make_principal", [_person, _personal_token])
def test_configuration_writes_need_the_admin_role(method, path, make_principal) -> None:
    operator = _run(path, method, make_principal("operator"))
    admin = _run(path, method, make_principal("admin"))

    assert operator.status_code == 403
    assert b"Admin role required" in operator.body
    assert admin.status_code == 204, admin.body


def _operator_may_write(method: str, path: str) -> bool:
    return method != "GET" and route_allowed(method, path, OPERATOR_PERMISSIONS)


@pytest.mark.parametrize(("method", "path"), MOVED_TO_ADMIN)
def test_no_configuration_write_needs_only_an_operator_permission(method, path) -> None:
    assert not _operator_may_write(method, path)


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("POST", "/api/workflows/wf-1/run/extra"),
        ("POST", "/api/workflow-runs/x"),
        ("POST", "/api/jobs/job-1/cancel/now"),
        ("GET", "/api/secrets"),
        ("DELETE", "/api/secrets/s1"),
        ("POST", "/api/secrets/s1/value"),
    ],
)
def test_near_miss_paths_are_not_operator_work(method, path) -> None:
    assert not _operator_may_write(method, path)


@pytest.mark.parametrize(
    ("method", "path", "scopes"),
    [
        ("PUT", "/api/projects/p1", ["projects.write"]),
        ("POST", "/api/projects/p1/connections", ["command:*"]),
        ("PUT", "/api/projects/p1/connections/c1", ["command:*"]),
        ("DELETE", "/api/secrets/s1", ["secrets:write"]),
        ("PUT", "/api/projects/p1/connections/c1/secret-bindings/api_key", ["secrets:bind"]),
    ],
)
def test_service_account_authority_is_unchanged(method, path, scopes) -> None:
    response = _run(path, method, _service_account(scopes))

    assert response.status_code == 204, response.body


#: The MCP tools an operator uses for the work; every other writing tool authors
#: or configures and needs the admin role, exactly as in the UI.
_OPERATOR_MCP_TOOLS = {
    "dispatch_project_notification",
    "job_acknowledge",
    "job_approval_decide",
    "job_cancel",
    "job_restart",
    "job_unacknowledge",
    "report_generate",
    "workflow_trigger",
}
#: Validation is a POST that changes nothing; every role may run it, as before.
_READ_LIKE_MCP_TOOLS = {"workflow_draft_validate"}


def _concrete(path: str) -> str:
    return re.sub(r"\{[^}]+\}", "x", path)


_MCP_WRITE_TOOLS = sorted(
    name
    for name, route in MCP_TOOL_ROUTES.items()
    if route.method != "GET" and name not in _READ_LIKE_MCP_TOOLS
)


@pytest.mark.parametrize("tool", _MCP_WRITE_TOOLS)
def test_operator_mcp_tools_match_ui_rights(tool) -> None:
    route = MCP_TOOL_ROUTES[tool]
    path = _concrete(route.path)

    through_mcp = _run(path, route.method, _personal_token("operator"))
    in_the_browser = _run(path, route.method, _person("operator"))

    assert through_mcp.status_code == in_the_browser.status_code
    expected = 204 if tool in _OPERATOR_MCP_TOOLS else 403
    assert through_mcp.status_code == expected, (tool, through_mcp.body)
