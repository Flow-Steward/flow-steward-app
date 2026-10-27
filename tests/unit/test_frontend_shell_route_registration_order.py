from __future__ import annotations

from types import SimpleNamespace

from fastapi import APIRouter, FastAPI

from core.entrypoints.api import route_bindings, route_registration


def test_frontend_shell_routes_are_registered_before_internal_admin_surfaces(monkeypatch) -> None:
    calls: list[str] = []

    monkeypatch.setattr(
        route_registration,
        "_register_frontend_shell_routes",
        lambda app: calls.append("frontend_shell"),
    )
    monkeypatch.setattr(
        route_registration,
        "_register_internal_admin_surfaces",
        lambda app: calls.append("internal_admin"),
    )
    monkeypatch.setattr(
        route_registration,
        "_register_foundation_routes",
        lambda app: calls.append("foundation"),
    )
    monkeypatch.setattr(
        route_registration,
        "_register_api_route_bindings",
        lambda app: calls.append("api_bindings"),
    )
    monkeypatch.setattr(
        route_registration,
        "_register_auth_route_bindings",
        lambda app: calls.append("auth_bindings"),
    )
    monkeypatch.setattr(
        route_registration,
        "_register_ui_route_bindings",
        lambda app: calls.append("ui_bindings"),
    )
    monkeypatch.setattr(
        route_registration,
        "_register_job_and_webhook_routes",
        lambda app: calls.append("job_webhook"),
    )

    route_registration._register_core_routes(FastAPI())

    assert calls == [
        "frontend_shell",
        "internal_admin",
        "foundation",
        "api_bindings",
        "auth_bindings",
        "ui_bindings",
        "job_webhook",
    ]


def test_auth_route_registration_preserves_optional_and_explicit_lockout_store(monkeypatch) -> None:
    captured: dict[str, object] = {}
    cleared: list[tuple[str, object]] = []
    omitted = object()
    store = object()

    def capture_registration(_app, **kwargs):
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(route_registration, "_register_auth_routes", capture_registration)

    def clear_user_login_failures(username, *, bl=omitted):
        cleared.append((username, bl))

    monkeypatch.setattr(
        route_registration,
        "_auth_login_clear_user_failures",
        clear_user_login_failures,
    )

    route_registration._register_auth_route_bindings(FastAPI())
    clear_failures = captured["auth_login_clear_user_failures"]
    assert callable(clear_failures)
    clear_failures("fallback-user")
    clear_failures("locked-user", bl=store)

    assert cleared == [("fallback-user", omitted), ("locked-user", store)]


def test_api_route_registration_wires_oauth_to_integration_store(monkeypatch) -> None:
    captured: dict[str, object] = {}
    connection_store = object()
    oauth_store = SimpleNamespace(
        conn=object(),
        get_project=lambda _project_id: object(),
        get_extension_install_for_operator=lambda _install_id, **_kwargs: object(),
    )
    app = FastAPI()
    app.state.runtime_container = SimpleNamespace(
        service_factories={
            "connection_store": lambda: connection_store,
            "integration_store": lambda: oauth_store,
        },
        metadata={},
    )

    def capture_registration(_app, **kwargs):
        captured.update(kwargs)

    monkeypatch.setattr(route_registration, "_register_api_routes", capture_registration)

    route_registration._register_api_route_bindings(app)

    get_oauth_store = captured["get_extension_oauth_store"]
    resolved_store = get_oauth_store()
    assert resolved_store is oauth_store
    assert resolved_store is not connection_store
    assert callable(resolved_store.get_project)
    assert callable(resolved_store.get_extension_install_for_operator)
    assert resolved_store.conn is not None


def test_oauth_callback_handler_uses_integration_store(monkeypatch) -> None:
    captured: dict[str, object] = {}
    project_store = object()
    event_command_service = object()
    integration_store = SimpleNamespace(
        get_project=lambda _project_id: object(),
        get_extension_install_for_operator=lambda _install_id, **_kwargs: object(),
    )
    app = FastAPI()
    app.state.runtime_container = SimpleNamespace(
        service_factories={
            "identity_store": object,
            "project_store": lambda: project_store,
            "integration_store": lambda: integration_store,
            "event_command_service": lambda: event_command_service,
        },
        metadata={},
    )

    monkeypatch.setattr(
        route_registration,
        "_register_provider_webhook_routes",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        route_registration,
        "_register_workflow_entrypoint_routes",
        lambda *_args, **_kwargs: None,
    )

    def capture_callback_router(**kwargs):
        captured.update(kwargs)
        return APIRouter()

    def capture_callback_handler(store):
        captured["oauth_callback_store"] = store
        return lambda **_values: {}

    monkeypatch.setattr(
        route_registration,
        "_create_extension_callbacks_router",
        capture_callback_router,
    )
    monkeypatch.setattr(
        route_registration,
        "_build_extension_oauth_callback_handler",
        capture_callback_handler,
    )

    route_registration._register_job_and_webhook_routes(app)
    captured["oauth_authorization_callback"]()

    assert captured["oauth_callback_store"] is integration_store
    assert captured["oauth_callback_store"] is not project_store
    assert captured["event_command_service_factory"]() is event_command_service


def test_extension_oauth_router_receives_its_explicit_store_factory(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def oauth_store_factory():
        return object()

    sentinel_router = object()

    def create_router(**kwargs):
        captured.update(kwargs)
        return sentinel_router

    monkeypatch.setattr(route_bindings, "create_extension_oauth_router", create_router)

    result = route_bindings._build_extension_oauth_router(
        get_extension_oauth_store=oauth_store_factory,
        request_scope=object(),
        require_authenticated_user=object(),
        service_error_response=object(),
        api_error_response=object(),
    )

    assert result is sentinel_router
    assert captured["get_logger_only"] is oauth_store_factory
