"""Maintenance HTTP classification and middleware tests."""

from __future__ import annotations

import inspect
from collections.abc import Callable

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.application.maintenance.contracts import MaintenanceStatusResult
from core.entrypoints.api.system_maintenance import create_system_maintenance_router
from core.infrastructure.runtime.maintenance_http import (
    build_maintenance_http_middleware,
    maintenance_request_decision,
    runtime_maintenance_state,
    runtime_maintenance_status,
)


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/static/app.js"),
        ("GET", "/health/live"),
        ("GET", "/api/system/maintenance"),
        ("GET", "/api/projects/p1"),
        ("GET", "/api/events/stream"),
        ("GET", "/health/deep"),
        ("POST", "/api/auth/login"),
        ("POST", "/api/auth/logout"),
        ("POST", "/api/auth/switch-account"),
    ],
)
def test_maintenance_allows_required_read_and_session_surfaces(method: str, path: str) -> None:
    assert maintenance_request_decision(method, path, authenticated=True).allowed is True


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("POST", "/webhook/extensions/acme.demo/events/token"),
        ("POST", "/api/jobs"),
        ("PATCH", "/api/projects/p1"),
        ("POST", "/api/auth/password-reset/request"),
        ("GET", "/webhook/extensions/acme.demo/events/token"),
        ("GET", "/api/integrations/oauth/callback"),
        ("GET", "/verify-email-change"),
    ],
)
def test_maintenance_blocks_mutations_and_explicit_safe_method_side_effects(
    method: str, path: str
) -> None:
    decision = maintenance_request_decision(method, path, authenticated=True)

    assert decision.allowed is False
    assert decision.code == "maintenance_mode_enabled"


def _client(*, active: Callable[[], bool], calls: list[str]) -> TestClient:
    app = FastAPI()
    app.middleware("http")(build_maintenance_http_middleware(maintenance_active=active))

    @app.post("/api/jobs")
    async def submit_job() -> dict[str, bool]:
        calls.append("submit")
        return {"ok": True}

    @app.get("/api/projects/p1")
    async def read_project() -> dict[str, bool]:
        calls.append("read")
        return {"ok": True}

    return TestClient(app)


def test_rejected_mutation_returns_stable_503_without_calling_handler() -> None:
    calls: list[str] = []

    response = _client(active=lambda: True, calls=calls).post("/api/jobs")

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "maintenance_mode_enabled"
    assert response.headers["Retry-After"] == "60"
    assert calls == []


def test_disabled_maintenance_and_safe_reads_reach_handlers() -> None:
    calls: list[str] = []
    disabled = _client(active=lambda: False, calls=calls)
    enabled = _client(active=lambda: True, calls=calls)

    assert disabled.post("/api/jobs").status_code == 200
    assert enabled.get("/api/projects/p1").status_code == 200
    assert calls == ["submit", "read"]


def test_status_endpoint_remains_available_during_maintenance() -> None:
    app = FastAPI()
    app.middleware("http")(build_maintenance_http_middleware(maintenance_active=lambda: True))
    app.include_router(
        create_system_maintenance_router(
            status_provider=lambda: MaintenanceStatusResult(
                status="enabled",
                owner_kind="manual",
            )
        )
    )

    response = TestClient(app).get("/api/system/maintenance")

    assert response.status_code == 200
    assert response.json() == {
        "status": "enabled",
        "activated_at": None,
        "owner_kind": "manual",
    }


def test_runtime_marker_is_parsed_as_technical_state_without_application_contract(
    tmp_path,
) -> None:
    marker = tmp_path / "maintenance-state.json"
    marker.write_text(
        '{"schema_version":1,"phase":"enabled","owner_kind":"manual",'
        '"owner_id":"manual-maintenance","activated_at":"2026-08-28T10:00:00Z"}',
        encoding="utf-8",
    )

    state = runtime_maintenance_state(marker)

    assert state is not None
    assert state["phase"] == "enabled"
    assert "core.application" not in inspect.getsource(runtime_maintenance_status)


def test_http_registration_keeps_correlation_middleware_outside_maintenance() -> None:
    from pathlib import Path

    source = (
        Path(__file__).resolve().parents[2] / "core/infrastructure/runtime/http_app_lifecycle.py"
    ).read_text(encoding="utf-8")
    registration = source[source.index("def _register_core_http_middleware") :]

    assert registration.index("maintenance_http_middleware") < registration.index(
        "_correlation_logging_middleware"
    )
