from __future__ import annotations

import pytest

_OPTIONAL_TEST_DEPS = {"fastapi", "starlette", "pydantic", "jinja2"}


def _import_health_module():
    try:
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from core.entrypoints.api import health
    except Exception as exc:
        if isinstance(exc, ModuleNotFoundError) and getattr(exc, "name", "") in _OPTIONAL_TEST_DEPS:
            pytest.skip(f"health route tests require optional dependency '{exc.name}'")
        raise

    app = FastAPI()
    app.include_router(health.router)
    return health, TestClient(app, raise_server_exceptions=False)


@pytest.mark.parametrize("path", ["/health", "/api/health", "/health/live"])
def test_health_aliases_include_build_version(path: str) -> None:
    health, client = _import_health_module()

    response = client.get(path)

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["build_version"] == health.APP_VERSION
    assert body["build_sha"] == health._BUILD_SHA
    assert body["image_digest"] == health._IMAGE_DIGEST
    assert body["service"] == "platform-orchestrator"
    assert isinstance(body["time"], str)
    assert body["time"]


def test_live_health_payload_uses_app_version_constant() -> None:
    health, _ = _import_health_module()

    payload = health._live_health_payload()

    assert payload["build_version"] == health.APP_VERSION
    assert payload["build_sha"] == health._BUILD_SHA
    assert payload["image_digest"] == health._IMAGE_DIGEST
