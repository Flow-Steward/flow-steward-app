"""The Core API guard for module features (ADR 0082, section 3)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import APIRouter, Depends, FastAPI
from fastapi.testclient import TestClient

from core.application.settings.platform_contracts import (
    CoreEntitlementService,
    FeatureNotEntitledError,
)
from core.entrypoints.admin.deps import require_entitlement
from core.entrypoints.api.platform_module_loading import MODULE_SERVICE_ERROR_TYPES
from core.infrastructure.runtime.http_app_lifecycle import (
    _register_core_exception_handlers,
)


class _AccountFeatures:
    def __init__(self, features_by_account: dict[str, set[str]]) -> None:
        self._features_by_account = features_by_account
        self.asked_for: list[str] = []

    def features_for(self, account_id: str) -> set[str]:
        self.asked_for.append(account_id)
        return set(self._features_by_account.get(account_id, set()))

    def limits_for(self, account_id: str) -> dict[str, int]:
        _ = account_id
        return {}


def _client(source: _AccountFeatures, *, account_id: str | None) -> TestClient:
    service = CoreEntitlementService()
    service.register_entitlement_source(source)
    app = FastAPI()
    app.state.runtime_container = SimpleNamespace(
        registries={"platform_contracts": {"entitlement_service": service}}
    )
    _register_core_exception_handlers(app, service_error_types=MODULE_SERVICE_ERROR_TYPES)

    @app.middleware("http")
    async def _inject_auth(request, call_next):
        request.state.auth_user = (
            None if account_id is None else {"username": "ada", "account_id": account_id}
        )
        return await call_next(request)

    router = APIRouter(
        prefix="/api/modules/acme.example",
        dependencies=[Depends(require_entitlement("Feature_A"))],
    )

    @router.post("/exports")
    def create_export() -> dict[str, str]:
        return {"status": "created"}

    app.include_router(router)
    return TestClient(app, raise_server_exceptions=False)


def test_an_account_without_the_feature_gets_403_with_code_and_feature() -> None:
    source = _AccountFeatures({"acc-entitled": {"feature_a"}})

    response = _client(source, account_id="acc-plain").post("/api/modules/acme.example/exports")

    assert response.status_code == 403
    body = response.json()
    assert body["code"] == "feature_not_entitled"
    assert body["details"] == {"feature": "feature_a"}
    assert "feature_a" in body["message"]


def test_an_entitled_account_passes() -> None:
    source = _AccountFeatures({"acc-entitled": {"feature_a"}})

    response = _client(source, account_id="acc-entitled").post("/api/modules/acme.example/exports")

    assert response.status_code == 200
    assert response.json() == {"status": "created"}


def test_the_guard_asks_for_the_account_of_the_request() -> None:
    source = _AccountFeatures({"acc-entitled": {"feature_a"}})

    _client(source, account_id="acc-other").post("/api/modules/acme.example/exports")
    _client(source, account_id="acc-entitled").post("/api/modules/acme.example/exports")

    assert source.asked_for == ["acc-other", "acc-entitled"]


def test_a_request_without_an_account_is_refused_without_asking_a_source() -> None:
    source = _AccountFeatures({"": {"feature_a"}})

    response = _client(source, account_id=None).post("/api/modules/acme.example/exports")

    assert response.status_code == 403
    assert response.json()["code"] == "feature_not_entitled"
    assert source.asked_for == []


def test_an_app_without_an_entitlement_service_refuses() -> None:
    app = FastAPI()
    _register_core_exception_handlers(app, service_error_types=MODULE_SERVICE_ERROR_TYPES)

    @app.middleware("http")
    async def _inject_auth(request, call_next):
        request.state.auth_user = {"account_id": "acc-entitled"}
        return await call_next(request)

    @app.get("/api/modules/acme.example/report", dependencies=[Depends(require_entitlement("x"))])
    def report() -> dict[str, str]:
        return {"status": "ok"}

    response = TestClient(app, raise_server_exceptions=False).get(
        "/api/modules/acme.example/report"
    )

    assert response.status_code == 403
    assert response.json()["details"] == {"feature": "x"}


def test_core_registers_the_refusal_handler() -> None:
    app = FastAPI()

    _register_core_exception_handlers(app, service_error_types=MODULE_SERVICE_ERROR_TYPES)

    assert FeatureNotEntitledError in app.exception_handlers


def test_a_guard_needs_a_feature_name() -> None:
    with pytest.raises(ValueError):
        require_entitlement("  ")
