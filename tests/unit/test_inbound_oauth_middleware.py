"""ADR 0047 decisions 4, 10, 12, 13 in ``local_auth_middleware`` (task5b F5, F6, F7, F10, Z7)."""

from __future__ import annotations

import asyncio

import pytest
from starlette.requests import Request
from starlette.responses import Response

from core.application.auth import auth_http_runtime
from core.application.auth.principal import ResolvedPrincipal
from core.application.auth.request_principal_runtime import roles_intersect
from core.application.auth.resolve import normalize_principal

SECRET = "f" * 40
ACCOUNT = "00000000-0000-0000-0000-000000000002"
OAUTH_PRINCIPAL = {
    "principal_type": "user",
    "id": "user-1",
    "username": "operator",
    "roles": ["operator"],
    "scopes": ["projects.read", "jobs.read"],
    "account_id": ACCOUNT,
    "is_active": True,
    "auth_source": "oauth",
    "oauth_grant_id": "grant-1",
    "oauth_client_id": "https://claude.ai/oauth/claude-code-client-metadata",
    "oauth_resource": "https://fs.example.com/mcp",
}


def _request(path: str, *, method: str, headers: list[tuple[bytes, bytes]]) -> Request:
    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": method,
            "scheme": "https",
            "path": path,
            "raw_path": path.encode(),
            "query_string": b"",
            "headers": headers,
            "client": ("testclient", 50000),
            "server": ("testserver", 443),
            "root_path": "",
        }
    )


class _Lookups:
    def __init__(self, *, session=None) -> None:
        self.calls: list[str] = []
        self.session = session

    def session_lookup(self, _bl, _token):
        self.calls.append("session")
        return self.session


def _run(
    monkeypatch,
    *,
    path: str,
    method: str = "GET",
    bearer: str = "",
    forward: str | None = None,
    cookie: str = "",
    lookups: _Lookups | None = None,
    oauth_principal: dict | None = None,
    pat_principal: dict | None = None,
    public: bool = False,
) -> tuple[Response, list[str], dict]:
    from core.application.auth import inbound_oauth_service, resolve

    lookups = lookups or _Lookups()
    seen: dict = {}

    def _oauth(_bl, token):
        lookups.calls.append("oauth")
        return dict(oauth_principal) if oauth_principal is not None else None

    def _pat(_bl, _token):
        lookups.calls.append("pat")
        return dict(pat_principal) if pat_principal is not None else None

    def _machine(_bl, _token):
        lookups.calls.append("machine")

    monkeypatch.setattr(inbound_oauth_service, "resolve_oauth_access_principal", _oauth)
    monkeypatch.setattr(resolve, "resolve_pat_principal", _pat)
    monkeypatch.setattr(resolve, "resolve_machine_principal", _machine)
    monkeypatch.setenv("FS_MCP_FORWARD_SECRET", SECRET)

    headers: list[tuple[bytes, bytes]] = []
    if bearer:
        headers.append((b"authorization", f"Bearer {bearer}".encode()))
    if forward is not None:
        headers.append((b"x-flow-steward-mcp-forward", forward.encode()))
    if cookie:
        headers.append((b"cookie", f"session={cookie}".encode()))

    async def _call_next(request: Request) -> Response:
        seen["auth_user"] = getattr(request.state, "auth_user", None)
        return Response(status_code=204)

    def _normalize(user, **kwargs):
        if isinstance(user, dict):
            user = {"auth_source": kwargs.get("default_auth_source", ""), **user}
        return normalize_principal(user)

    response = asyncio.run(
        auth_http_runtime.local_auth_middleware(
            _request(path, method=method, headers=headers),
            _call_next,
            local_auth_enabled_fn=lambda: True,
            external_auth_enabled=False,
            is_public_auth_path_fn=lambda _path: public,
            route_registered_fn=lambda _path, _method: True,
            request_principal_resolver_fn=lambda _request, _bl: None,
            normalize_live_principal_fn=_normalize,
            get_logger_only_fn=lambda: object(),
            auth_tables_available_fn=lambda _bl: True,
            get_user_by_session_token_fn=lookups.session_lookup,
            local_auth_bootstrap_required_fn=lambda _bl=None: False,
            auth_account_tables_available_fn=lambda _bl: False,
            roles_intersect_fn=roles_intersect,
            is_admin_only_route_fn=lambda _path: False,
            is_registry_read_route_fn=lambda _path: False,
            is_self_service_auth_write_route_fn=lambda _path: _path == "/api/auth/tokens",
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
    return response, lookups.calls, seen


def test_an_access_token_is_resolved_only_by_the_oauth_resolver(monkeypatch) -> None:
    """F10: an fs_oat_ bearer never reaches the session, PAT or machine lookup."""
    response, calls, seen = _run(
        monkeypatch,
        path="/api/projects",
        bearer="fs_oat_abc",
        forward=SECRET,
        oauth_principal=OAUTH_PRINCIPAL,
    )
    assert response.status_code == 204
    assert calls == ["oauth"]
    assert seen["auth_user"]["auth_source"] == "oauth"
    assert seen["auth_user"]["oauth_grant_id"] == "grant-1"


@pytest.mark.parametrize("bearer", ["fs_ort_abc", "fs_oac_abc"])
def test_codes_and_refresh_tokens_are_never_bearers(monkeypatch, bearer: str) -> None:
    response, calls, _seen = _run(monkeypatch, path="/api/projects", bearer=bearer)
    assert response.status_code == 401
    assert calls == []


def test_an_unknown_access_token_is_401_without_other_lookups(monkeypatch) -> None:
    response, calls, _seen = _run(
        monkeypatch, path="/api/projects", bearer="fs_oat_abc", forward=SECRET
    )
    assert response.status_code == 401
    assert calls == ["oauth"]


@pytest.mark.parametrize("forward", [None, "wrong-secret-of-sufficient-length-000000"])
def test_oauth_token_without_the_forward_secret_is_refused(monkeypatch, forward) -> None:
    """F5: a token sent straight to the public /api is not the MCP's back-end call."""
    response, _calls, _seen = _run(
        monkeypatch,
        path="/api/projects",
        bearer="fs_oat_abc",
        forward=forward,
        oauth_principal=OAUTH_PRINCIPAL,
    )
    assert response.status_code == 401
    assert response.body == b'{"error":"invalid_token"}'


def test_introspection_needs_no_forward_secret(monkeypatch) -> None:
    """F7."""
    response, _calls, _seen = _run(
        monkeypatch,
        path="/api/auth/introspect",
        bearer="fs_oat_abc",
        oauth_principal=OAUTH_PRINCIPAL,
    )
    assert response.status_code == 204


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("POST", "/api/auth/tokens"),
        ("GET", "/api/auth/tokens"),
        ("POST", "/api/auth/change-password"),
        ("GET", "/api/auth/oauth-grants"),
        ("POST", "/api/oauth/authorizations/x/decision"),
        ("PUT", "/api/settings/mcp-oauth"),
    ],
)
def test_oauth_token_cannot_mint_list_or_manage_credentials(monkeypatch, method, path) -> None:
    """F6: even from fs-mcp with the right secret."""
    response, _calls, _seen = _run(
        monkeypatch,
        path=path,
        method=method,
        bearer="fs_oat_abc",
        forward=SECRET,
        oauth_principal=OAUTH_PRINCIPAL,
    )
    assert response.status_code == 403


def test_a_pat_is_refused_on_interactive_only_routes(monkeypatch) -> None:
    """Z7: consent, grants and the switch accept a browser session only."""
    pat = {
        key: value
        for key, value in OAUTH_PRINCIPAL.items()
        if key not in {"oauth_grant_id", "oauth_client_id", "oauth_resource"}
    }
    pat["auth_source"] = "pat"
    response, calls, _seen = _run(
        monkeypatch,
        path="/api/oauth/authorizations/x",
        bearer="fs_pat_abc",
        pat_principal=pat,
    )
    assert calls == ["session", "pat"]
    assert response.status_code == 403


def test_a_session_bearer_is_refused_on_interactive_only_routes(monkeypatch) -> None:
    session_user = {
        "id": "user-1",
        "username": "admin",
        "roles": ["admin"],
        "account_id": ACCOUNT,
        "is_active": True,
    }
    response, calls, _seen = _run(
        monkeypatch,
        path="/api/auth/oauth-grants",
        bearer="session-token",
        lookups=_Lookups(session=session_user),
    )
    assert calls == ["session"]
    assert response.status_code == 403


def test_a_session_cookie_reaches_interactive_only_routes(monkeypatch) -> None:
    session_user = {
        "id": "user-1",
        "username": "admin",
        "roles": ["admin"],
        "account_id": ACCOUNT,
        "is_active": True,
    }
    response, _calls, seen = _run(
        monkeypatch,
        path="/api/auth/oauth-grants",
        cookie="session-token",
        lookups=_Lookups(session=session_user),
    )
    assert response.status_code == 204
    assert seen["auth_user"]["auth_source"] == "local"


@pytest.mark.parametrize(
    "path",
    [
        "/oauth/token",
        "/oauth/revoke",
        "/oauth/authorize",
        "/.well-known/oauth-authorization-server",
    ],
)
def test_protocol_endpoints_never_derive_identity_from_a_cookie(monkeypatch, path: str) -> None:
    lookups = _Lookups(session={"id": "user-1", "account_id": ACCOUNT, "roles": ["viewer"]})
    response, calls, seen = _run(
        monkeypatch, path=path, method="POST", cookie="session-token", lookups=lookups
    )
    assert response.status_code == 204
    assert calls == []
    assert seen["auth_user"] is None


def test_principal_model_carries_only_the_three_oauth_fields() -> None:
    principal = ResolvedPrincipal(**{key: OAUTH_PRINCIPAL[key] for key in ("id", "auth_source")})
    assert principal.oauth_grant_id == ""
    with pytest.raises(ValueError):
        ResolvedPrincipal(id="x", pat_id="p")  # extra="forbid" still holds
