from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import MagicMock

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from core.application.management import ServiceError
from core.entrypoints.api import model_providers


def test_list_and_delete_offload_complete_provider_use_cases(monkeypatch) -> None:
    service = MagicMock()
    service.list_model_providers.return_value = [{"id": "provider-1"}]
    service.delete_model_provider.return_value = {
        "id": "provider-1",
        "status": "deleted",
    }
    offloaded: list[str] = []

    async def _to_thread(function, *args, **kwargs):
        offloaded.append(function._mock_name)
        return function(*args, **kwargs)

    monkeypatch.setattr(model_providers.asyncio, "to_thread", _to_thread)
    app = FastAPI()
    app.include_router(
        model_providers.create_api_model_providers_router(
            model_provider_command_service=lambda: service,
            request_scope=lambda _request: SimpleNamespace(account_id="acc-1"),
            service_error_response=lambda error: JSONResponse(
                {"error": str(error)}, status_code=400
            ),
            api_error_response=lambda error: JSONResponse({"error": str(error)}, status_code=500),
        )
    )
    client = TestClient(app)

    assert client.get("/api/model-providers").status_code == 200
    assert client.delete("/api/model-providers/provider-1").status_code == 200
    assert offloaded == ["list_model_providers", "delete_model_provider"]


def test_list_provider_rows_serializes_verification_timestamps() -> None:
    service = MagicMock()
    service.list_model_providers.return_value = [
        {"id": "provider-1", "verified_at": datetime(2026, 7, 26, tzinfo=UTC)}
    ]
    app = FastAPI()
    app.include_router(
        model_providers.create_api_model_providers_router(
            model_provider_command_service=lambda: service,
            request_scope=lambda _request: SimpleNamespace(account_id="acc-1"),
            service_error_response=lambda error: JSONResponse(
                {"error": str(error)}, status_code=400
            ),
            api_error_response=lambda error: JSONResponse({"error": str(error)}, status_code=500),
        )
    )

    response = TestClient(app).get("/api/model-providers")

    assert response.status_code == 200
    assert response.json()["providers"][0]["verified_at"] == "2026-07-26T00:00:00+00:00"


def test_reverify_offloads_generation_bound_provider_probe(monkeypatch) -> None:
    service = MagicMock()
    service.reverify_model_provider.return_value = {
        "id": "provider-1",
        "verification_status": "passed",
    }
    offloaded: list[str] = []

    async def _to_thread(function, *args, **kwargs):
        offloaded.append(function._mock_name)
        return function(*args, **kwargs)

    monkeypatch.setattr(model_providers.asyncio, "to_thread", _to_thread)
    app = FastAPI()
    app.include_router(
        model_providers.create_api_model_providers_router(
            model_provider_command_service=lambda: service,
            request_scope=lambda _request: SimpleNamespace(account_id="acc-1"),
            service_error_response=lambda error: JSONResponse(
                {"error": str(error)}, status_code=400
            ),
            api_error_response=lambda error: JSONResponse({"error": str(error)}, status_code=500),
        )
    )

    response = TestClient(app).post("/api/model-providers/provider-1/verify")

    assert response.status_code == 200
    service.reverify_model_provider.assert_called_once()
    assert offloaded == ["reverify_model_provider"]


def test_reverify_rate_limit_returns_retry_after_without_reaching_upstream() -> None:
    service = MagicMock()
    service.reverify_model_provider.side_effect = ServiceError(
        "Model provider verification rate limit exceeded",
        status_code=429,
        payload={
            "code": "rate_limited",
            "message": "Model provider verification rate limit exceeded",
            "retry_after": 41,
        },
    )
    app = FastAPI()
    app.include_router(
        model_providers.create_api_model_providers_router(
            model_provider_command_service=lambda: service,
            request_scope=lambda _request: SimpleNamespace(account_id="acc-1"),
            service_error_response=lambda error: JSONResponse(
                error.payload, status_code=error.status_code
            ),
            api_error_response=lambda error: JSONResponse({"error": str(error)}, status_code=500),
        )
    )

    response = TestClient(app).post("/api/model-providers/provider-1/verify")

    assert response.status_code == 429
    assert response.headers["Retry-After"] == "41"
    assert response.json()["code"] == "rate_limited"


def test_provider_mutations_reject_unknown_json_fields() -> None:
    service = MagicMock()
    app = FastAPI()
    app.include_router(
        model_providers.create_api_model_providers_router(
            model_provider_command_service=lambda: service,
            request_scope=lambda _request: SimpleNamespace(account_id="acc-1"),
            service_error_response=lambda error: JSONResponse(
                {"error": str(error)}, status_code=400
            ),
            api_error_response=lambda error: JSONResponse({"error": str(error)}, status_code=500),
        )
    )
    client = TestClient(app)
    payload = {
        "name": "Provider",
        "provider_type": "openai_compatible",
        "base_url": "https://provider.example/v1",
        "default_model": "model-a",
        "upstream_revision": "revision-2026-07-21",
        "unknown_secret": "must-not-be-accepted",
    }

    assert client.post("/api/model-providers", json=payload).status_code == 422
    assert client.put("/api/model-providers/provider-1", json=payload).status_code == 422
    service.create_model_provider.assert_not_called()
    service.update_model_provider.assert_not_called()


def test_provider_mutation_accepts_upstream_revision_evidence() -> None:
    service = MagicMock()
    service.create_model_provider.return_value = {
        "id": "provider-1",
        "verification_status": "passed",
    }
    app = FastAPI()
    app.include_router(
        model_providers.create_api_model_providers_router(
            model_provider_command_service=lambda: service,
            request_scope=lambda _request: SimpleNamespace(account_id="acc-1"),
            service_error_response=lambda error: JSONResponse(
                {"error": str(error)}, status_code=400
            ),
            api_error_response=lambda error: JSONResponse({"error": str(error)}, status_code=500),
        )
    )

    response = TestClient(app).post(
        "/api/model-providers",
        json={
            "name": "Provider",
            "provider_type": "openai_compatible",
            "base_url": "https://provider.example/v1",
            "default_model": "model-a",
            "upstream_revision": "sha256:revision-a",
        },
    )

    assert response.status_code == 201
    command = service.create_model_provider.call_args.args[0]
    assert command["upstream_revision"] == "sha256:revision-a"
