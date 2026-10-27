from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from core.application.reports.ui_page_registry import (
    register_core_settings_pages,
    register_page,
    resolve_registered_navigation_sections,
    resolve_registered_pages,
    resolve_registered_settings_pages,
)
from core.application.settings.ui_i18n import build_client_i18n_payload
from core.entrypoints.api.app_bootstrap import create_api_app_bootstrap_router


class _EntitlementServiceStub:
    def snapshot(self) -> dict[str, bool]:
        return {"external_sign_in": True, "identity_access": True}


def _api_error_response(exc, request=None):
    _ = request
    return JSONResponse({"error": str(exc)}, status_code=500)


def _build_client(
    *,
    with_auth: bool,
    bootstrap_required: bool = False,
    local_auth_enabled: bool = True,
    app_edition: str = "community",
) -> TestClient:
    app = FastAPI()

    if with_auth:

        @app.middleware("http")
        async def _inject_auth(request, call_next):
            request.state.auth_source = "local"
            request.state.auth_user = {
                "id": 7,
                "username": "admin",
                "email": "admin@example.test",
                "display_name": "Admin User",
                "roles": ["admin"],
                "account_id": "acc_default",
                "account_ids": ["acc_default", "acc_ops"],
                "account_memberships": [
                    {"account_id": "acc_default", "account_name": "Default", "is_default": True}
                ],
                "effective_permissions": {
                    "can_read": True,
                    "can_mutate": True,
                    "can_use_reports": True,
                },
                "timezone": "UTC",
                "principal_type": "user",
            }
            return await call_next(request)

    app.include_router(
        create_api_app_bootstrap_router(
            request_principal=(
                lambda _request: (
                    type("Principal", (), {"principal_type": "admin"})() if with_auth else None
                )
            ),
            local_auth_enabled=lambda: local_auth_enabled,
            local_auth_bootstrap_required=lambda: bootstrap_required,
            external_auth_enabled=False,
            current_app_edition=lambda: app_edition,
            platform_contract=lambda key: (
                _EntitlementServiceStub() if key == "entitlement_service" else None
            ),
            build_client_i18n_payload=lambda request, auth_user=None: {
                "locale": "en",
                "fallback_locale": "en",
                "supported_locales": [
                    {"code": "en", "label": "English", "native_label": "English"}
                ],
                "messages": {"shell.nav.dashboard": "Dashboard"},
                "auth_user_present": bool(auth_user),
                "path": str(request.url.path),
            },
            settings_pages_resolver=lambda auth_user=None, locale=None: [
                {
                    "page_key": "retention",
                    "title": "Retention",
                    "path": "/settings/retention",
                    "settings_tab": True,
                    "auth_user_present": bool(auth_user),
                }
            ],
            registered_pages_resolver=lambda auth_user=None, locale=None: [
                {
                    "key": "private_fixture",
                    "page_key": "private_fixture",
                    "label": "Private Fixture",
                    "title": "Private Fixture",
                    "path": "/private/fixture",
                    "component_key": "private.fixture",
                    "auth_user_present": bool(auth_user),
                }
            ],
            shell_navigation_resolver=lambda auth_user=None, locale=None: {
                "system": [
                    {
                        "page_key": "extensions",
                        "nav_label": "Extensions",
                        "path": "/extensions",
                        "auth_user_present": bool(auth_user),
                    }
                ]
            },
            external_sign_in_resolver=lambda: {
                "providers_path": "/external-sign-in/providers",
            },
            api_error_response=_api_error_response,
        )
    )

    return TestClient(app, raise_server_exceptions=False)


def test_api_app_bootstrap_returns_public_payload_for_anonymous_requests() -> None:
    client = _build_client(with_auth=False, app_edition="private")

    response = client.get("/api/app/bootstrap")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["auth"]["authenticated"] is False
    assert body["auth"]["user"] is None
    assert body["auth"]["bootstrap_required"] is False
    assert body["environment"]["edition"] == "community"
    assert body["environment"] == {"edition": "community", "mode": "production"}
    assert body["feature_flags"] == {}
    assert body["auth"]["external_sign_in"]["providers_path"] == "/external-sign-in/providers"
    assert body["i18n"]["locale"] == "en"
    assert body["settings_pages"] == []
    assert body["registered_pages"] == []
    assert body["shell_navigation"] == {}


def test_api_app_bootstrap_includes_user_permissions_and_memberships() -> None:
    client = _build_client(with_auth=True, app_edition="private")

    response = client.get("/api/app/bootstrap")

    assert response.status_code == 200
    body = response.json()
    assert body["auth"]["authenticated"] is True
    assert body["auth"]["bootstrap_required"] is False
    assert body["auth"]["auth_source"] == "local"
    assert body["auth"]["user"]["username"] == "admin"
    assert body["auth"]["user"]["account_memberships"][0]["account_id"] == "acc_default"
    assert body["auth"]["permissions"]["can_mutate"] is True
    assert body["environment"]["edition"] == "private"
    assert body["feature_flags"]["identity_access"] is True
    assert body["auth"]["external_sign_in"]["providers_path"] == "/external-sign-in/providers"
    assert body["i18n"]["auth_user_present"] is True
    assert body["settings_pages"][0]["auth_user_present"] is True
    assert body["registered_pages"][0]["component_key"] == "private.fixture"
    assert body["registered_pages"][0]["auth_user_present"] is True
    assert body["shell_navigation"]["system"][0]["auth_user_present"] is True


def test_api_app_bootstrap_exposes_bootstrap_required_flag() -> None:
    client = _build_client(with_auth=False, bootstrap_required=True)

    response = client.get("/api/app/bootstrap")

    assert response.status_code == 200
    assert response.json()["auth"]["bootstrap_required"] is True


def test_api_app_bootstrap_keeps_admin_metadata_when_auth_is_disabled() -> None:
    client = _build_client(with_auth=False, local_auth_enabled=False)

    response = client.get("/api/app/bootstrap")

    assert response.status_code == 200
    body = response.json()
    assert body["feature_flags"]["identity_access"] is True
    assert body["settings_pages"][0]["page_key"] == "retention"
    assert body["settings_pages"][0]["auth_user_present"] is False
    assert body["registered_pages"][0]["path"] == "/private/fixture"
    assert body["shell_navigation"]["system"][0]["path"] == "/extensions"


def test_bootstrap_has_only_active_permissions_and_environment() -> None:
    payload = _build_client(with_auth=True).get("/api/app/bootstrap").json()
    assert payload["auth"]["permissions"] == {"can_read": True, "can_mutate": True}
    assert payload["environment"] == {"edition": "community", "mode": "production"}


@pytest.mark.parametrize(
    "principal,roles",
    [("viewer", []), ("", []), ("admin", "viewer"), ("admin", ["viewer"]), ("admin", {"viewer"})],
)
def test_effective_permissions_preserves_read_only_role_ceiling(principal, roles) -> None:
    from core.application.auth.effective_permissions import resolve_effective_permissions

    assert resolve_effective_permissions(
        principal_type=principal,
        effective_permissions={"can_read": False, "can_mutate": True, "roles": roles},
    ) == {"can_read": False, "can_mutate": False}


def test_effective_permissions_preserves_explicit_mutation_denial() -> None:
    from core.application.auth.effective_permissions import resolve_effective_permissions

    assert resolve_effective_permissions(
        principal_type="admin",
        effective_permissions={"can_mutate": False},
    ) == {"can_read": True, "can_mutate": False}


@pytest.mark.parametrize(
    "profile_locale,query,cookie,accept_language,expected_locale,expected_title",
    [
        ("en", "?lang=lt", "", "", "lt", "Bendra"),
        ("lt", "?locale=en", "", "", "en", "General"),
        ("", "", "lt", "en", "lt", "Bendra"),
        ("", "", "", "lt-LT,en;q=0.8", "lt", "Bendra"),
    ],
)
def test_bootstrap_core_metadata_matches_effective_interface_locale(
    profile_locale, query, cookie, accept_language, expected_locale, expected_title
) -> None:
    registries = {}
    register_core_settings_pages(registries)
    general = registries["settings_pages"]["general"]
    general["component_key"] = "settings.general"
    registries["registered_pages"]["general"] = general
    registries["navigation_items"]["general"] = general
    register_page(
        registries,
        page_key="vendor",
        title="Vendor supplied title",
        path="/vendor",
        component_key="vendor.page",
    )
    app = FastAPI()

    @app.middleware("http")
    async def inject_auth(request, call_next):
        request.state.auth_user = {"roles": ["admin"], "locale": profile_locale}
        return await call_next(request)

    def resolve_metadata(resolver, *, auth_user=None, locale=None):
        return resolver(registries, auth_user=auth_user, include_core=True, locale=locale)

    app.include_router(
        create_api_app_bootstrap_router(
            request_principal=lambda _request: None,
            local_auth_enabled=lambda: True,
            local_auth_bootstrap_required=lambda: False,
            external_auth_enabled=False,
            current_app_edition=lambda: "community",
            platform_contract=lambda _key: None,
            build_client_i18n_payload=build_client_i18n_payload,
            settings_pages_resolver=lambda **kwargs: resolve_metadata(
                resolve_registered_settings_pages, **kwargs
            ),
            registered_pages_resolver=lambda **kwargs: resolve_metadata(
                resolve_registered_pages, **kwargs
            ),
            shell_navigation_resolver=lambda **kwargs: resolve_metadata(
                resolve_registered_navigation_sections, **kwargs
            ),
            api_error_response=_api_error_response,
        )
    )
    client = TestClient(app, raise_server_exceptions=False)
    if cookie:
        client.cookies.set("ui-locale", cookie)

    response = client.get(
        f"/api/app/bootstrap{query}", headers={"accept-language": accept_language}
    )

    assert response.status_code == 200
    body = response.json()
    assert body["i18n"]["locale"] == expected_locale
    assert body["i18n"]["messages"]["registry.workflow_detail.settings.general"] == expected_title
    settings = {page["page_key"]: page for page in body["settings_pages"]}
    assert settings["general"]["title"] == expected_title
    pages = {page["page_key"]: page for page in body["registered_pages"]}
    assert pages["general"]["title"] == expected_title
    assert pages["vendor"]["title"] == "Vendor supplied title"
    navigation = {
        page["page_key"]: page for rows in body["shell_navigation"].values() for page in rows
    }
    assert navigation["general"]["nav_label"] == expected_title


@pytest.fixture(autouse=True)
def _isolate_installation_markers(monkeypatch) -> None:
    monkeypatch.delenv("ENVIRONMENT", raising=False)
    monkeypatch.delenv("FS_STACK_ENVIRONMENT", raising=False)


@pytest.mark.parametrize(
    ("environment", "stack_environment", "expected"),
    [
        ("development", "local_development", "development"),
        ("development", None, "development"),
        (None, "local_development", "development"),
        (None, None, "production"),
        ("production", "production", "production"),
        ("development", "production", "production"),
        ("production", "development", "production"),
        ("development", "unknown", "production"),
        ("unknown", "development", "production"),
        ("development", "", "production"),
    ],
)
def test_bootstrap_projects_canonical_server_mode_without_changing_edition(
    monkeypatch, environment: str | None, stack_environment: str | None, expected: str
) -> None:
    for marker, value in (
        ("ENVIRONMENT", environment),
        ("FS_STACK_ENVIRONMENT", stack_environment),
    ):
        if value is not None:
            monkeypatch.setenv(marker, value)
    client = _build_client(with_auth=True, app_edition="private")

    response = client.get("/api/app/bootstrap?mode=development")

    assert response.status_code == 200
    assert response.json()["environment"] == {"edition": "private", "mode": expected}


def test_bootstrap_mode_ignores_client_and_unregistered_development_hints(monkeypatch) -> None:
    for marker in ("FS_RUNTIME_ENVIRONMENT", "APP_ENV", "NODE_ENV", "SENTRY_ENVIRONMENT"):
        monkeypatch.setenv(marker, "development")
    client = _build_client(with_auth=True, app_edition="private")

    response = client.get(
        "/api/app/bootstrap?mode=development", headers={"X-Environment": "development"}
    )

    assert response.status_code == 200
    assert response.json()["environment"] == {"edition": "private", "mode": "production"}


def test_bootstrap_mode_refreshes_after_server_switch(monkeypatch) -> None:
    monkeypatch.setenv("ENVIRONMENT", "development")
    monkeypatch.setenv("FS_STACK_ENVIRONMENT", "development")
    client = _build_client(with_auth=True)
    assert client.get("/api/app/bootstrap").json()["environment"]["mode"] == "development"

    monkeypatch.setenv("ENVIRONMENT", "production")

    assert client.get("/api/app/bootstrap").json()["environment"]["mode"] == "production"
