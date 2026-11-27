"""External sign-in routes, route rules and identity-link administration (ADR 0083)."""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any, ClassVar

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.application.auth.enforce import is_public_auth_path
from core.application.auth.external_sign_in_admin import ExternalSignInAdmin
from core.application.auth.external_sign_in_service import (
    ExternalSignInRefused,
    SignInResult,
    StartResult,
)
from core.application.auth.route_permissions import route_admin_only, route_permission
from core.application.management.errors import ServiceForbiddenError
from core.entrypoints.api import external_sign_in_routes as routes

PUBLIC = ("options", "start", "callback")


@pytest.mark.parametrize("name", PUBLIC)
def test_the_public_routes_need_no_session_and_no_permission(name: str) -> None:
    path = f"/api/auth/external/{name}"

    assert is_public_auth_path(path)
    match = route_permission("GET", path)
    assert match is not None and match.rule.permission is None


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/api/settings/external-sign-in/identities"),
        ("DELETE", "/api/settings/external-sign-in/identities/abc"),
    ],
)
def test_the_administration_routes_are_admin_only(method: str, path: str) -> None:
    assert route_admin_only(method, path)
    assert route_permission(method, path).rule.permission == "settings.manage"
    assert not is_public_auth_path(path)


# --- identity-link administration ---------------------------------------------------


def test_only_a_platform_administrator_manages_identity_links() -> None:
    admin = ExternalSignInAdmin(
        connection=lambda: pytest.fail("no database access expected"),
        audit=lambda **_kwargs: None,
    )
    operator = SimpleNamespace(principal_type="operator", account_id=str(uuid.uuid4()))
    token = SimpleNamespace(
        principal_type="admin", account_id=str(uuid.uuid4()), token_scopes=frozenset({"read:*"})
    )

    for scope in (operator, token, None):
        with pytest.raises(ServiceForbiddenError):
            admin.list_identities(scope)
        with pytest.raises(ServiceForbiddenError):
            admin.unlink_identity(scope, str(uuid.uuid4()))


def test_core_serves_no_sign_in_configuration_routes(client) -> None:
    paths = {getattr(route, "path", "") for route in client.app.routes}

    assert not any(path.startswith("/api/settings/external-sign-in/providers") for path in paths)
    assert "/api/auth/external/providers" not in paths


# --- the HTTP transport ---------------------------------------------------------------


class _FakeService:
    calls: ClassVar[list[tuple[str, Any]]] = []
    refuse: ClassVar[str] = ""

    def __init__(self, **_kwargs: Any) -> None:
        pass

    def public_options(self, hint=""):
        type(self).calls.append(("options", hint))
        options = [{"provider_key": "p0", "display_name": "Everyone", "login_path": "/w"}]
        if hint == "acme":
            options.append({"provider_key": "p1", "display_name": "Acme ID", "login_path": "/x"})
        return {"options": options, "account_hint_supported": True}

    def start(self, **kwargs):
        type(self).calls.append(("start", kwargs))
        if type(self).refuse:
            raise ExternalSignInRefused(type(self).refuse, return_path=kwargs["next_path"])
        if kwargs["method"] == "header":
            return SignInResult(
                return_path="/", session_token="header-session", session_ttl_hours=1
            )
        return StartResult(
            authorization_url="https://ticket.acme.test/login?state=s", browser_binding="bind"
        )

    def callback(self, **kwargs):
        type(self).calls.append(("callback", kwargs))
        if type(self).refuse:
            raise ExternalSignInRefused(type(self).refuse, return_path="/jobs")
        return SignInResult(
            return_path="/jobs", session_token="session-token", session_ttl_hours=12
        )


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    _FakeService.calls = []
    _FakeService.refuse = ""
    monkeypatch.setattr(routes, "ExternalSignInService", _FakeService)
    app = FastAPI()
    app.include_router(
        routes.create_external_sign_in_router(
            get_logger_only=lambda: object(),
            connection_for=lambda _bl: pytest.fail("no database access expected"),
            external_sign_in_port=lambda: SimpleNamespace(header_source=None),
            entitlement_service=lambda: None,
            session_issue_guard=lambda **_kwargs: None,
            request_scope=lambda request: None,
            service_error_response=lambda exc: None,
            api_error_response=lambda exc: None,
            log_auth_audit_event=lambda _bl, **_kwargs: None,
            public_base_url=lambda: "https://flow.acme.test",
            auth_cookie_secure=lambda: True,
            auth_cookie_name="fs_session",
            auth_csrf_cookie_name="fs_csrf",
            auth_session_ttl_hours=12,
        )
    )
    return TestClient(app, base_url="https://flow.acme.test", follow_redirects=False)


def test_start_redirects_out_with_a_browser_binding_cookie(client) -> None:
    response = client.get(
        "/api/auth/external/start?method=acme-ticket&key=p1&next=//attacker.example"
    )

    assert response.status_code == 303
    assert response.headers["location"].startswith("https://ticket.acme.test/login")
    cookie = response.headers["set-cookie"]
    assert cookie.startswith(f"{routes.BINDING_COOKIE}=bind")
    assert "HttpOnly" in cookie and "SameSite=lax" in cookie and "Secure" in cookie
    assert "Path=/api/auth/external" in cookie
    [(_name, kwargs)] = _FakeService.calls
    assert kwargs["next_path"] == "/"
    assert kwargs["method"] == "acme-ticket" and kwargs["key"] == "p1"
    assert kwargs["callback_url"] == "https://flow.acme.test/api/auth/external/callback"
    assert kwargs["link_user_id"] is None


def test_the_callback_sets_the_session_and_returns_to_the_path(client) -> None:
    client.cookies.set(routes.BINDING_COOKIE, "bind", path="/api/auth/external")

    response = client.get("/api/auth/external/callback?state=s&code=c")

    assert response.status_code == 303
    assert response.headers["location"] == "/jobs"
    cookies = response.headers.get_list("set-cookie")
    session = next(item for item in cookies if item.startswith("fs_session="))
    assert "HttpOnly" in session and "SameSite=lax" in session
    assert any(item.startswith("fs_csrf=") and "HttpOnly" not in item for item in cookies)
    [(_name, kwargs)] = _FakeService.calls
    assert kwargs["browser_binding"] == "bind"


def test_a_refused_callback_returns_to_the_sign_in_page_without_a_session(client) -> None:
    _FakeService.refuse = "identity_not_linked"

    response = client.get("/api/auth/external/callback?state=s&code=c")

    assert response.status_code == 303
    assert response.headers["location"].startswith("/login?flash=")
    assert "next=%2Fjobs" in response.headers["location"]
    assert not any(
        item.startswith("fs_session=") for item in response.headers.get_list("set-cookie")
    )
    assert "identity_not_linked" not in response.headers["location"]


def test_linking_needs_a_local_session(client) -> None:
    response = client.get("/api/auth/external/start?method=acme-ticket&key=p1&intent=link")

    assert response.status_code == 303
    assert response.headers["location"].startswith("/login?")
    assert _FakeService.calls == []


def test_a_method_that_does_not_redirect_signs_in_at_once(client) -> None:
    response = client.get(
        "/api/auth/external/start?method=header&key=trusted", headers={"X-Acme-User": "pat"}
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/"
    cookies = response.headers.get_list("set-cookie")
    assert any(item.startswith("fs_session=header-session") for item in cookies)
    [(_name, kwargs)] = _FakeService.calls
    assert kwargs["headers"]["x-acme-user"] == "pat"


def test_there_is_no_route_per_method(client) -> None:
    assert client.get("/api/auth/external/header").status_code == 404


def test_the_option_list_names_no_account_without_a_hint(client) -> None:
    response = client.get("/api/auth/external/options")

    assert response.status_code == 200
    assert response.json() == {
        "options": [{"provider_key": "p0", "display_name": "Everyone", "login_path": "/w"}],
        "account_hint_supported": True,
    }
    assert response.headers["cache-control"] == "no-store"
    assert _FakeService.calls == [("options", "")]


def test_an_account_hint_adds_that_accounts_options(client) -> None:
    response = client.get("/api/auth/external/options?account=acme")

    assert [item["provider_key"] for item in response.json()["options"]] == ["p0", "p1"]
    assert _FakeService.calls == [("options", "acme")]


def test_account_hint_lookups_are_throttled_per_client(client) -> None:
    for _ in range(routes.HINT_MAX_ATTEMPTS):
        assert client.get("/api/auth/external/options?account=acme").status_code == 200

    refused = client.get("/api/auth/external/options?account=other")

    assert refused.status_code == 429
    assert int(refused.headers["Retry-After"]) >= 1
    assert len(_FakeService.calls) == routes.HINT_MAX_ATTEMPTS
    # The installation-wide list (no hint) names no account and stays open.
    assert client.get("/api/auth/external/options").status_code == 200


# --- an external session is not a local one -----------------------------------------


@pytest.mark.parametrize(
    ("token_source", "refused"),
    [("cookie", False), ("", False), ("bearer", True)],
)
def test_an_external_session_is_interactive_only_from_its_cookie(
    token_source: str, refused: bool
) -> None:
    """Sessions from external sign-in now resolve as ``external``; a bearer stays a bearer."""
    from core.application.auth import auth_http_runtime
    from core.application.auth.principal import ResolvedPrincipal

    request = SimpleNamespace(headers={})
    response = auth_http_runtime._oauth_request_refusal(
        request,
        principal=ResolvedPrincipal(id="u", auth_source="external", account_id="a"),
        path="/api/auth/multi-factor/enroll",
        method="POST",
        token_source=token_source,
    )

    assert (response is not None) is refused


def test_a_sign_in_with_several_companies_goes_to_the_chooser(client, monkeypatch) -> None:
    def callback(self, **kwargs):
        type(self).calls.append(("callback", kwargs))
        return SignInResult(
            return_path="/jobs",
            session_token="session-token",
            session_ttl_hours=12,
            choose_account=True,
        )

    monkeypatch.setattr(_FakeService, "callback", callback)
    client.cookies.set(routes.BINDING_COOKIE, "bind", path="/api/auth/external")

    response = client.get("/api/auth/external/callback?state=s&code=c")

    assert response.status_code == 303
    assert response.headers["location"] == "/login?choose_account=1&next=%2Fjobs"
    assert any(item.startswith("fs_session=") for item in response.headers.get_list("set-cookie"))
