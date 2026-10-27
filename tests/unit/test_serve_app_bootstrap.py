from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.application.management.command_services_approval import ApprovalCommandService

_OPTIONAL_TEST_DEPS = {
    "psycopg_runtime",
    "fastapi",
    "starlette",
    "pydantic",
    "langgraph",
    "langchain_core",
}


def _import_serve():
    try:
        from tests import api_runtime_test_surface as api_runtime
    except Exception as exc:
        if isinstance(exc, ModuleNotFoundError) and getattr(exc, "name", "") in _OPTIONAL_TEST_DEPS:
            pytest.skip(f"api_runtime import requires optional dependency '{exc.name}'")
        raise
    return api_runtime


def test_serve_app_exposes_runtime_container() -> None:
    api_runtime = _import_serve()

    container = api_runtime.app.state.runtime_container

    assert callable(api_runtime.create_app)
    assert container.app is api_runtime.app
    assert not hasattr(container, "templates")
    assert not hasattr(container, "render")
    edition = str(container.metadata.get("edition") or "").strip().lower()
    assert edition in {"community", "private"}
    assert isinstance(container.metadata.get("optional_private_modules_loaded"), list)
    assert isinstance(api_runtime.app.state.optional_private_modules_loaded, list)
    assert "project_query_service" in container.service_factories
    assert "db" in container.service_factories
    assert api_runtime.app.state.bootstrap_stages == list(container.metadata["bootstrap_stages"])


def test_http_approval_composition_constructs_current_production_service() -> None:
    serve = _import_serve()

    service = serve.app.state.runtime_container.service_factories["approval_command_service"]()

    assert isinstance(service, ApprovalCommandService)


def test_serve_app_registers_core_settings_pages() -> None:
    api_runtime = _import_serve()

    container = api_runtime.app.state.runtime_container
    settings_pages = container.registries["settings_pages"]
    navigation_items = container.registries["navigation_items"]

    assert "extensions" not in settings_pages
    assert "service_accounts" in settings_pages
    assert "retention" in settings_pages
    assert "identity" not in settings_pages
    assert {"settings_home", "extensions", "account", "users"} <= set(navigation_items)


def test_serve_shell_navigation_exposes_extensions_surface_by_default() -> None:
    api_runtime = _import_serve()
    from core.entrypoints.api import route_registration

    navigation = route_registration._app_shell_navigation(
        api_runtime.app,
        auth_user={"roles": ["admin"]},
    )

    assert "system" in navigation
    assert "extensions" in {row["page_key"] for row in navigation["system"]}


def test_composed_bootstrap_passes_effective_locale_to_settings_metadata() -> None:
    api_runtime = _import_serve()
    app = FastAPI()
    app.router.routes.append(
        next(route for route in api_runtime.app.routes if route.path == "/api/app/bootstrap")
    )

    @app.middleware("http")
    async def inject_auth(request, call_next):
        request.state.auth_user = {"roles": ["admin"], "locale": "en"}
        return await call_next(request)

    response = TestClient(app).get("/api/app/bootstrap?lang=lt")

    assert response.status_code == 200
    payload = response.json()
    assert payload["i18n"]["locale"] == "lt"
    pages = {page["page_key"]: page for page in payload["settings_pages"]}
    assert pages["general"]["title"] == "Bendra"


def test_serve_registers_registered_frontend_shell_paths_without_route_resolver_collision(
    monkeypatch,
) -> None:
    api_runtime = _import_serve()
    from core.entrypoints.api import route_registration

    app = FastAPI()

    monkeypatch.setattr(
        route_registration._ui_page_registry,
        "resolve_registered_shell_paths",
        lambda _registries: ("/optional/fixture",),
    )

    app.state.runtime_container = api_runtime.app.state.runtime_container
    route_registration._register_registered_frontend_shell_routes(app)

    paths = {str(getattr(route, "path", "") or "") for route in app.routes}
    assert "/optional/fixture" in paths


def test_serve_app_optional_modules_match_current_edition() -> None:
    api_runtime = _import_serve()

    edition = (
        str(api_runtime.app.state.runtime_container.metadata.get("edition") or "").strip().lower()
    )
    optional_modules = list(
        getattr(api_runtime.app.state, "optional_private_modules_loaded", []) or []
    )

    if edition == "private":
        assert optional_modules
    else:
        assert optional_modules == []


def test_serve_app_registers_platform_contracts() -> None:
    api_runtime = _import_serve()

    container = api_runtime.app.state.runtime_container
    contracts = container.registries["platform_contracts"]

    assert {
        "authorization_service",
        "entitlement_service",
        "audit_sink",
        "secret_provider",
        "extension_governance_policy",
    } <= set(contracts)
    assert "authorization_service" in container.service_factories
    assert "extension_governance_policy" in container.service_factories


def test_management_chat_routes_are_retired_but_operations_remain() -> None:
    api_runtime = _import_serve()
    paths = {route.path for route in api_runtime.app.routes if hasattr(route, "path")}
    assert not any(path.startswith("/api/chat/") for path in paths)
    assert "/api/assistant-settings" not in paths
    assert {"/api/app/bootstrap", "/api/reports/generate", "/api/jobs"} <= paths
