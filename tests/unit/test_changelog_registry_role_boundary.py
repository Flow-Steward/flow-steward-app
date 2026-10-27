"""Authorization contract for entity-scoped generic changelog reads."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable

import pytest
from starlette.requests import Request
from starlette.responses import Response

from core.application.auth.auth_http_runtime import build_local_auth_middleware_handler
from core.application.auth.principal import ResolvedPrincipal
from core.application.auth.request_principal_runtime import roles_intersect
from core.infrastructure.runtime import http_app_auth_runtime

_ENTITY_TYPES = (
    "agent",
    "skill",
    "tool",
    "workflow",
    "guardrail",
    "data_contract",
    "field",
    "published_block",
)
_READ_SUFFIXES = ("history", "history/1", "diff")
_SAFE_METHODS = ("GET", "HEAD", "OPTIONS")
_TOKEN_IDENTITIES = (
    pytest.param("pat", "user", id="pat"),
    pytest.param("machine", "service_account", id="service-account"),
)


def _request(path: str, *, method: str) -> Request:
    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": method,
            "scheme": "https",
            "path": path,
            "raw_path": path.encode(),
            "query_string": b"",
            "headers": [],
            "client": ("testclient", 50000),
            "server": ("testserver", 443),
            "root_path": "",
        }
    )


def _principal(
    role: str,
    *,
    auth_source: str = "external",
    principal_type: str = "user",
    scopes: list[str] | None = None,
) -> ResolvedPrincipal:
    return ResolvedPrincipal(
        id=f"{role}-1",
        username=role,
        roles=[role],
        auth_source=auth_source,
        principal_type=principal_type,
        scopes=list(scopes or []),
    )


def _auth_handler(
    principal: ResolvedPrincipal | None,
) -> Callable[[Request, Callable[[Request], Awaitable[Response]]], Awaitable[Response]]:
    return build_local_auth_middleware_handler(
        local_auth_enabled_fn=lambda: False,
        external_auth_enabled=True,
        is_public_auth_path_fn=lambda _path: False,
        route_registered_fn=lambda _path, _method: True,
        request_principal_resolver_fn=lambda _request, _bl: principal,
        normalize_live_principal_fn=lambda user, **_kwargs: user,
        get_logger_only_fn=lambda: object(),
        auth_tables_available_fn=lambda _bl: True,
        get_user_by_session_token_fn=lambda _bl, _token: None,
        local_auth_bootstrap_required_fn=lambda _bl=None: False,
        auth_account_tables_available_fn=lambda _bl: False,
        roles_intersect_fn=roles_intersect,
        is_admin_only_route_fn=http_app_auth_runtime._is_admin_only_route,
        is_registry_read_route_fn=http_app_auth_runtime._is_registry_read_route,
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


def _authorize(
    path: str,
    *,
    method: str,
    principal: ResolvedPrincipal | None,
) -> Response:
    async def _call_next(_request: Request) -> Response:
        return Response(status_code=204)

    return asyncio.run(_auth_handler(principal)(_request(path, method=method), _call_next))


@pytest.mark.parametrize("entity_type", _ENTITY_TYPES)
@pytest.mark.parametrize("suffix", _READ_SUFFIXES)
def test_generic_entity_changelog_reads_require_operator_for_every_safe_method(
    entity_type: str,
    suffix: str,
) -> None:
    """A same-account viewer must not bypass the registry-read role boundary."""
    path = f"/api/changelog/{entity_type}/owned-entity/{suffix}"

    for method in _SAFE_METHODS:
        assert _authorize(path, method=method, principal=None).status_code == 401
        assert _authorize(path, method=method, principal=_principal("viewer")).status_code == 403
        assert _authorize(path, method=method, principal=_principal("operator")).status_code == 204
        assert _authorize(path, method=method, principal=_principal("admin")).status_code == 204


@pytest.mark.parametrize("entity_type", _ENTITY_TYPES)
def test_generic_entity_changelog_restore_remains_admin_only(entity_type: str) -> None:
    path = f"/api/changelog/{entity_type}/owned-entity/restore"

    assert _authorize(path, method="POST", principal=None).status_code == 401
    assert _authorize(path, method="POST", principal=_principal("viewer")).status_code == 403
    assert _authorize(path, method="POST", principal=_principal("operator")).status_code == 403
    assert _authorize(path, method="POST", principal=_principal("admin")).status_code == 204


def test_generic_entity_changelog_machine_read_keeps_registry_scope_contract() -> None:
    path = "/api/changelog/agent/owned-agent/history"

    assert (
        _authorize(
            path,
            method="GET",
            principal=_principal(
                "operator",
                auth_source="machine",
                principal_type="service_account",
                scopes=["registry.read"],
            ),
        ).status_code
        == 204
    )
    assert (
        _authorize(
            path,
            method="GET",
            principal=_principal(
                "operator",
                auth_source="machine",
                principal_type="service_account",
                scopes=["projects.read"],
            ),
        ).status_code
        == 403
    )


@pytest.mark.parametrize(("auth_source", "principal_type"), _TOKEN_IDENTITIES)
@pytest.mark.parametrize("method", _SAFE_METHODS)
@pytest.mark.parametrize("grant", ["read:*", "registry.read", "registry:*", "admin:all"])
def test_changelog_role_gate_preserves_existing_token_read_grants(
    auth_source: str, principal_type: str, method: str, grant: str
) -> None:
    response = _authorize(
        "/api/changelog/agent/owned-agent/history",
        method=method,
        principal=_principal(
            "operator",
            auth_source=auth_source,
            principal_type=principal_type,
            scopes=[grant],
        ),
    )

    # 204 means middleware admission, not execution of the changelog handler.
    assert response.status_code == 204


@pytest.mark.parametrize(("auth_source", "principal_type"), _TOKEN_IDENTITIES)
@pytest.mark.parametrize("method", _SAFE_METHODS)
@pytest.mark.parametrize("scopes", [[], ["projects.read"]], ids=["empty", "wrong-family"])
def test_changelog_role_gate_does_not_replace_required_token_scopes(
    auth_source: str, principal_type: str, method: str, scopes: list[str]
) -> None:
    response = _authorize(
        "/api/changelog/agent/owned-agent/history",
        method=method,
        principal=_principal(
            "admin",
            auth_source=auth_source,
            principal_type=principal_type,
            scopes=scopes,
        ),
    )

    assert response.status_code == 403
    assert response.body == b'{"error":"Missing required token scope"}'


@pytest.mark.parametrize(("auth_source", "principal_type"), _TOKEN_IDENTITIES)
@pytest.mark.parametrize(
    ("method", "role", "scopes", "expected_status", "expected_error"),
    [
        pytest.param("POST", "admin", ["command:*"], 204, None, id="admin-command"),
        pytest.param(
            "POST",
            "operator",
            ["command:*"],
            403,
            "Admin role required",
            id="command-does-not-grant-admin-role",
        ),
        pytest.param(
            "POST",
            "admin",
            ["read:*"],
            403,
            "Missing required token scope",
            id="read-does-not-grant-command",
        ),
        pytest.param(
            "POST",
            "admin",
            ["projects.read"],
            403,
            "Missing required token scope",
            id="wrong-family-does-not-grant-command",
        ),
        pytest.param(
            "POST",
            "admin",
            [],
            403,
            "Missing required token scope",
            id="admin-role-does-not-grant-token-scopes",
        ),
        pytest.param(
            "DELETE",
            "admin",
            ["command:*"],
            403,
            "Missing required token scope",
            id="delete-keeps-admin-scope",
        ),
    ],
)
def test_changelog_restore_role_gate_preserves_command_and_delete_scope_boundaries(
    auth_source: str,
    principal_type: str,
    method: str,
    role: str,
    scopes: list[str],
    expected_status: int,
    expected_error: str | None,
) -> None:
    response = _authorize(
        "/api/changelog/agent/owned-agent/restore",
        method=method,
        principal=_principal(
            role,
            auth_source=auth_source,
            principal_type=principal_type,
            scopes=scopes,
        ),
    )

    # The DELETE case pins middleware policy for a registered path; it does not
    # claim the restore endpoint itself registers DELETE.
    assert response.status_code == expected_status
    if expected_error is not None:
        assert json.loads(response.body) == {"error": expected_error}


@pytest.mark.parametrize(("auth_source", "principal_type"), _TOKEN_IDENTITIES)
@pytest.mark.parametrize(
    ("grant", "expected_status"),
    [("read:*", 403), ("registry.read", 204)],
)
def test_changelog_scope_compatibility_does_not_widen_other_registry_reads(
    auth_source: str, principal_type: str, grant: str, expected_status: int
) -> None:
    response = _authorize(
        "/api/providers",
        method="GET",
        principal=_principal(
            "operator",
            auth_source=auth_source,
            principal_type=principal_type,
            scopes=[grant],
        ),
    )

    assert response.status_code == expected_status


@pytest.mark.parametrize("path", ["/api/changelog-extra", "/api/changelogs/agent/history"])
def test_changelog_role_matcher_does_not_capture_neighboring_path_prefixes(path: str) -> None:
    assert not http_app_auth_runtime._is_registry_read_route(path)
    response = _authorize(path, method="GET", principal=_principal("viewer"))
    assert response.status_code == 204
