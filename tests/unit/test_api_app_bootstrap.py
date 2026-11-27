from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from core.application.auth.config import DEFAULT_ACCOUNT_ID
from core.application.reports.ui_page_registry import (
    page_registration_scope,
    register_core_settings_pages,
    register_page,
    resolve_registered_navigation_sections,
    resolve_registered_pages,
    resolve_registered_settings_pages,
)
from core.application.settings.ui_i18n import build_client_i18n_payload
from core.entrypoints.api.app_bootstrap import create_api_app_bootstrap_router


class _EntitlementServiceStub:
    """Entitles only ``acc_default``; any other account gets its own empty snapshot."""

    def __init__(self) -> None:
        self.asked_for: list[str] = []

    def snapshot(self, account_id: str) -> dict[str, bool]:
        self.asked_for.append(account_id)
        if account_id == "acc_default":
            return {"feature_a": True, "feature_b": True}
        return {}


def _api_error_response(exc, request=None):
    _ = request
    return JSONResponse({"error": str(exc)}, status_code=500)


def _build_client(
    *,
    with_auth: bool,
    bootstrap_required: bool = False,
    local_auth_enabled: bool = True,
    app_edition: str = "community",
    stored_date_time_format: tuple[str, str] = ("MM/DD/YYYY", "12h"),
    account_id: str = "acc_default",
    entitlement_service: _EntitlementServiceStub | None = None,
    account_memberships: list[dict] | None = None,
) -> TestClient:
    app = FastAPI()
    entitlements = entitlement_service or _EntitlementServiceStub()

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
                "account_id": account_id,
                "account_ids": ["acc_default", "acc_ops"],
                "account_memberships": account_memberships
                or [{"account_id": "acc_default", "account_name": "Default", "is_default": True}],
                "effective_permissions": {
                    "can_read": True,
                    "can_mutate": True,
                    "can_use_reports": True,
                },
                "timezone": "UTC",
                "principal_type": "user",
                # Set by the auth middleware from the stored role permissions.
                "permissions": ["users.manage", "account.self", "jobs.read"],
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
            platform_contract=lambda key: entitlements if key == "entitlement_service" else None,
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
            date_time_format_reader=lambda _user: stored_date_time_format,
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
    assert body["auth"]["user"]["date_format"] == "MM/DD/YYYY"
    assert body["auth"]["user"]["time_format"] == "12h"
    assert body["auth"]["permissions"]["can_mutate"] is True
    # ADR 0076: the person's permission keys, sorted, beside the roles for now.
    assert body["auth"]["user"]["permissions"] == ["account.self", "jobs.read", "users.manage"]
    assert body["auth"]["user"]["roles"] == ["admin"]
    assert body["environment"]["edition"] == "private"
    assert body["feature_flags"]["feature_b"] is True
    assert body["auth"]["external_sign_in"]["providers_path"] == "/external-sign-in/providers"
    assert body["i18n"]["auth_user_present"] is True
    assert body["settings_pages"][0]["auth_user_present"] is True
    assert body["registered_pages"][0]["component_key"] == "private.fixture"
    assert body["registered_pages"][0]["auth_user_present"] is True
    assert body["shell_navigation"]["system"][0]["auth_user_present"] is True


def test_api_app_bootstrap_lists_the_persons_own_accounts_for_the_account_switch() -> None:
    memberships = [
        {
            "account_id": "acc_default",
            "display_name": "Default",
            "is_active": True,
            "is_default": True,
        },
        {
            "account_id": "acc_ops",
            "display_name": "Operations",
            "is_active": True,
            "is_default": False,
        },
        {"account_id": "acc_old", "display_name": "Old", "is_active": False, "is_default": False},
    ]
    client = _build_client(with_auth=True, account_id="acc_ops", account_memberships=memberships)

    body = client.get("/api/app/bootstrap").json()

    # The session's current account and the person's own memberships, with the
    # names and the active flag the switcher draws; nothing about other people.
    assert body["auth"]["user"]["account_id"] == "acc_ops"
    assert body["auth"]["user"]["account_memberships"] == memberships
    assert body["auth"]["account_memberships"] == memberships


def test_api_app_bootstrap_gives_a_signed_in_user_the_file_preview_limit(monkeypatch) -> None:
    """ADR 0080: a file over the server's preview limit is offered as a download only."""
    monkeypatch.setenv("FS_FILE_PREVIEW_MAX_BYTES", "4096")

    signed_in = _build_client(with_auth=True).get("/api/app/bootstrap").json()
    anonymous = _build_client(with_auth=False).get("/api/app/bootstrap").json()

    assert signed_in["limits"]["file_preview"] == {"max_bytes": 4096}
    assert anonymous["limits"]["file_preview"] == {}


def test_api_app_bootstrap_gives_a_user_without_a_valid_choice_the_default_formats() -> None:
    client = _build_client(with_auth=True, stored_date_time_format=("", "am/pm"))

    user = client.get("/api/app/bootstrap").json()["auth"]["user"]

    assert (user["date_format"], user["time_format"]) == ("D MMM YYYY", "24h")


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
    # Without a signed-in account nothing is entitled (ADR 0082, section 3).
    assert body["feature_flags"] == {}
    assert body["settings_pages"][0]["page_key"] == "retention"
    assert body["settings_pages"][0]["auth_user_present"] is False
    assert body["registered_pages"][0]["path"] == "/private/fixture"
    assert body["shell_navigation"]["system"][0]["path"] == "/extensions"


def test_api_app_bootstrap_feature_flags_belong_to_the_signed_in_account() -> None:
    entitlements = _EntitlementServiceStub()

    entitled = _build_client(with_auth=True, entitlement_service=entitlements)
    other = _build_client(with_auth=True, account_id="acc_ops", entitlement_service=entitlements)

    assert entitled.get("/api/app/bootstrap").json()["feature_flags"] == {
        "feature_a": True,
        "feature_b": True,
    }
    assert other.get("/api/app/bootstrap").json()["feature_flags"] == {}
    assert entitlements.asked_for == ["acc_default", "acc_ops"]


def test_api_app_bootstrap_does_not_ask_for_an_anonymous_snapshot() -> None:
    entitlements = _EntitlementServiceStub()

    _build_client(with_auth=False, entitlement_service=entitlements).get("/api/app/bootstrap")

    assert entitlements.asked_for == []


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
        request.state.auth_user = {
            "account_id": DEFAULT_ACCOUNT_ID,
            "roles": ["admin"],
            "locale": profile_locale,
        }
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


def test_a_principal_without_role_permissions_gets_none() -> None:
    """A service account calling the bootstrap holds no role permissions (ADR 0076 §3)."""
    from core.entrypoints.api.app_bootstrap import _user_summary

    summary = _user_summary({"id": "sa-1", "roles": ["operator"]})

    assert summary is not None
    assert summary["permissions"] is None


def test_a_person_without_a_project_limit_gets_no_project_permissions() -> None:
    from core.entrypoints.api.app_bootstrap import _user_summary

    summary = _user_summary({"id": "u-1", "roles": ["operator"], "permissions": ["jobs.read"]})

    assert summary is not None
    assert summary["project_permissions"] is None


def test_a_limited_person_gets_sorted_permissions_per_project() -> None:
    """ADR 0076 §8: the interface disables actions per project from these keys."""
    from core.entrypoints.api.app_bootstrap import _user_summary

    summary = _user_summary(
        {
            "id": "u-1",
            "roles": ["operator"],
            "permissions": ["jobs.read", "jobs.run"],
            "project_permissions": {
                "p-1": frozenset({"jobs.run", "jobs.read"}),
                "p-2": ["jobs.read", " ", "jobs.read"],
                "": ["jobs.run"],
                "p-3": "jobs.run",
            },
        }
    )

    assert summary is not None
    assert summary["project_permissions"] == {
        "p-1": ["jobs.read", "jobs.run"],
        "p-2": ["jobs.read"],
    }


def test_a_limited_person_with_no_projects_left_gets_an_empty_map() -> None:
    """The limit outlives the last project: an empty map, never ``None``."""
    from core.entrypoints.api.app_bootstrap import _user_summary

    summary = _user_summary({"id": "u-1", "roles": ["viewer"], "project_permissions": {}})

    assert summary is not None
    assert summary["project_permissions"] == {}


def test_bootstrap_sends_project_permissions_without_the_feature() -> None:
    """A stored limit keeps applying after the entitlement ends, so the keys are sent."""
    app = FastAPI()

    @app.middleware("http")
    async def inject_auth(request, call_next):
        request.state.auth_user = {
            "id": "u-1",
            "roles": ["operator"],
            "account_id": "acc_other",
            "permissions": ["jobs.read", "jobs.run"],
            "project_permissions": {"p-1": ["jobs.read"]},
        }
        return await call_next(request)

    app.include_router(
        create_api_app_bootstrap_router(
            request_principal=lambda _request: None,
            local_auth_enabled=lambda: True,
            local_auth_bootstrap_required=lambda: False,
            external_auth_enabled=False,
            current_app_edition=lambda: "community",
            platform_contract=lambda key: (
                _EntitlementServiceStub() if key == "entitlement_service" else None
            ),
            build_client_i18n_payload=lambda request, auth_user=None: {"locale": "en"},
            api_error_response=_api_error_response,
        )
    )

    body = TestClient(app, raise_server_exceptions=False).get("/api/app/bootstrap").json()

    assert body["feature_flags"] == {}
    assert body["auth"]["user"]["project_permissions"] == {"p-1": ["jobs.read"]}


def _registered_pages_client(registries: dict) -> TestClient:
    app = FastAPI()

    @app.middleware("http")
    async def inject_auth(request, call_next):
        request.state.auth_user = {
            "account_id": DEFAULT_ACCOUNT_ID,
            "roles": ["admin"],
            "locale": "en",
        }
        return await call_next(request)

    app.include_router(
        create_api_app_bootstrap_router(
            request_principal=lambda _request: None,
            local_auth_enabled=lambda: True,
            local_auth_bootstrap_required=lambda: False,
            external_auth_enabled=False,
            current_app_edition=lambda: "community",
            platform_contract=lambda _key: None,
            build_client_i18n_payload=build_client_i18n_payload,
            registered_pages_resolver=lambda **kwargs: resolve_registered_pages(
                registries, auth_user=kwargs.get("auth_user"), locale=kwargs.get("locale")
            ),
            api_error_response=_api_error_response,
        )
    )
    return TestClient(app, raise_server_exceptions=False)


def test_bootstrap_keeps_a_keyless_status_document_page() -> None:
    registries: dict = {}
    with page_registration_scope(module_id="acme.example"):
        register_page(
            registries,
            key="module_status",
            label="Module Status",
            path="/settings/module-status",
            settings_tab=True,
            control_surface={
                "kind": "status_document",
                "status_path": "/api/modules/acme.example/status",
                "upload_path": "/api/modules/acme.example/document",
                "fields": [{"key": "state", "label": "State", "type": "badge"}],
            },
        )

    response = _registered_pages_client(registries).get("/api/app/bootstrap")

    assert response.status_code == 200
    pages = {page["page_key"]: page for page in response.json()["registered_pages"]}
    surface = pages["module_status"]["control_surface"]
    assert surface["kind"] == "status_document"
    assert surface["status_path"] == "/api/modules/acme.example/status"
    assert surface["upload_path"] == "/api/modules/acme.example/document"
    assert surface["upload_field"] == "document"


def test_bootstrap_keeps_a_keyless_page_rendered_by_a_host_renderer() -> None:
    registries: dict = {}
    register_core_settings_pages(registries)
    surface = {
        "kind": "resource_admin",
        "description": "Module records",
        "resources": [
            {
                "key": "records",
                "title": "Records",
                "list_path": "/api/modules/acme.example/records",
                "item_path": "/api/modules/acme.example/records/{id}",
                "id_field": "id",
                "fields": [{"key": "name", "label": "Name", "type": "text"}],
            }
        ],
    }
    with page_registration_scope(module_id="acme.example"):
        register_page(
            registries,
            key="module_records",
            label="Module Records",
            path="/modules/records",
            control_surface=surface,
        )
    register_page(
        registries,
        key="vendor",
        label="Vendor",
        path="/vendor",
        component_key="vendor.page",
    )
    # A row that reaches the payload without a renderer is still dropped.
    registries["registered_pages"]["orphan"] = {
        "key": "orphan",
        "page_key": "orphan",
        "path": "/orphan",
        "component_key": "",
        "control_surface": {"kind": "dashboard"},
    }

    response = _registered_pages_client(registries).get("/api/app/bootstrap")

    assert response.status_code == 200
    pages = {page["page_key"]: page for page in response.json()["registered_pages"]}
    assert set(pages) == {"module_records", "vendor"}
    assert pages["module_records"]["component_key"] == ""
    assert pages["module_records"]["control_surface"]["kind"] == "resource_admin"
    assert pages["module_records"]["control_surface"]["resources"][0]["list_path"] == (
        "/api/modules/acme.example/records"
    )
    # Which module registered a page is a server-side diagnostic, not browser data.
    assert registries["registered_pages"]["module_records"]["source_module"] == "acme.example"
    assert "source_module" not in pages["module_records"]
    assert pages["vendor"]["component_key"] == "vendor.page"
