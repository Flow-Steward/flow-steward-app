from __future__ import annotations

from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from core.entrypoints.api.model_catalog import create_api_model_catalog_router
from core.entrypoints.api import model_catalog as model_catalog_api


def _build_client(*, get_logger_only, api_error_response):
    app = FastAPI()
    app.include_router(
        create_api_model_catalog_router(
            get_logger_only=get_logger_only,
            request_scope=lambda request, **kwargs: None,
            require_authenticated_user=lambda request, required_roles=None: None,
            resolve_actor_username=lambda request, requested=None: "admin",
            auth_admin_roles={"admin"},
            api_error_response=api_error_response,
        )
    )
    return TestClient(app, raise_server_exceptions=False)


def test_model_catalog_get_uses_safe_api_error_response(monkeypatch) -> None:
    called: dict[str, object] = {}

    def _api_error_response(exc, request=None, fallback_message="Internal server error", **kwargs):
        called["exc"] = exc
        called["fallback_message"] = fallback_message
        return JSONResponse({"error": fallback_message}, status_code=500)

    def _boom(*args, **kwargs):
        raise RuntimeError("catalog exploded")

    monkeypatch.setattr(
        "core.application.settings.model_catalog.get_full_catalog_for_settings", _boom
    )

    client = _build_client(
        get_logger_only=lambda: SimpleNamespace(conn=object()),
        api_error_response=_api_error_response,
    )

    response = client.get("/api/settings/model-catalog")

    assert response.status_code == 500
    assert response.json() == {"error": "Internal server error"}
    assert isinstance(called["exc"], RuntimeError)
    assert called["fallback_message"] == "Internal server error"
    assert "catalog exploded" not in response.text


def test_model_catalog_put_uses_safe_api_error_response(monkeypatch) -> None:
    called: dict[str, object] = {}

    def _api_error_response(exc, request=None, fallback_message="Internal server error", **kwargs):
        called["exc"] = exc
        called["fallback_message"] = fallback_message
        return JSONResponse({"error": fallback_message}, status_code=500)

    def _boom(*args, **kwargs):
        raise RuntimeError("save exploded")

    class _LoggerOnly:
        conn = object()

    monkeypatch.setattr("core.application.settings.model_catalog.save_visibility_policy", _boom)

    client = _build_client(
        get_logger_only=lambda: _LoggerOnly(),
        api_error_response=_api_error_response,
    )

    response = client.put("/api/settings/model-catalog", json={"providers": []})

    assert response.status_code == 500
    assert response.json() == {"error": "Internal server error"}
    assert isinstance(called["exc"], RuntimeError)
    assert called["fallback_message"] == "Internal server error"
    assert "save exploded" not in response.text


def test_model_selection_options_offloads_complete_catalog_read(monkeypatch) -> None:
    calls: list[str] = []

    async def _to_thread(function, *args, **kwargs):
        calls.append(function.__name__)
        return function(*args, **kwargs)

    monkeypatch.setattr(
        model_catalog_api,
        "asyncio",
        SimpleNamespace(to_thread=_to_thread),
        raising=False,
    )
    monkeypatch.setattr(
        "core.application.settings.model_catalog.get_effective_options_structured",
        lambda **_kwargs: {
            "provider_options": [],
            "provider_models": {},
            "all_models": [],
        },
    )
    client = _build_client(
        get_logger_only=lambda: SimpleNamespace(conn=object()),
        api_error_response=lambda exc, **_kwargs: JSONResponse(
            {"error": str(exc)}, status_code=500
        ),
    )

    assert client.get("/api/model-selection/options").status_code == 200
    assert calls == ["_load_model_selection_options"]


def test_model_selection_options_returns_safe_503_on_catalog_failure(monkeypatch) -> None:
    def _boom(**_kwargs):
        raise RuntimeError("database credentials leaked here")

    monkeypatch.setattr(
        "core.application.settings.model_catalog.get_effective_options_structured",
        _boom,
    )
    client = _build_client(
        get_logger_only=lambda: SimpleNamespace(conn=object()),
        api_error_response=lambda exc, **_kwargs: JSONResponse(
            {"error": "Internal server error"}, status_code=500
        ),
    )

    response = client.get("/api/model-selection/options")

    assert response.status_code == 503
    assert response.json() == {"detail": "Model catalog unavailable"}
    assert "credentials" not in response.text


def test_model_selection_options_returns_503_when_database_handle_is_missing() -> None:
    client = _build_client(
        get_logger_only=lambda: None,
        api_error_response=lambda exc, **_kwargs: JSONResponse(
            {"error": str(exc)}, status_code=500
        ),
    )

    response = client.get("/api/model-selection/options")

    assert response.status_code == 503
    assert response.json() == {"detail": "Model catalog unavailable"}


def test_catalog_settings_get_and_put_offload_complete_database_use_cases(monkeypatch) -> None:
    calls: list[str] = []

    async def _to_thread(function, *args, **kwargs):
        calls.append(function.__name__)
        return function(*args, **kwargs)

    monkeypatch.setattr(
        model_catalog_api,
        "asyncio",
        SimpleNamespace(to_thread=_to_thread),
        raising=False,
    )
    monkeypatch.setattr(
        "core.application.settings.model_catalog.get_full_catalog_for_settings",
        lambda **_kwargs: {"providers": []},
    )
    monkeypatch.setattr(
        "core.application.settings.model_catalog.save_visibility_policy",
        lambda *_args, **_kwargs: None,
    )
    client = _build_client(
        get_logger_only=lambda: SimpleNamespace(conn=object()),
        api_error_response=lambda exc, **_kwargs: JSONResponse(
            {"error": str(exc)}, status_code=500
        ),
    )

    assert client.get("/api/settings/model-catalog").status_code == 200
    assert client.put("/api/settings/model-catalog", json={"providers": []}).status_code == 200
    assert calls == ["_load_full_catalog_settings", "_save_model_catalog_settings"]


def test_catalog_settings_put_preserves_database_unavailable_status() -> None:
    client = _build_client(
        get_logger_only=lambda: None,
        api_error_response=lambda exc, **_kwargs: JSONResponse(
            {"error": str(exc)}, status_code=500
        ),
    )

    response = client.put("/api/settings/model-catalog", json={"providers": []})

    assert response.status_code == 503
    assert response.json()["detail"] == "Database not available"
