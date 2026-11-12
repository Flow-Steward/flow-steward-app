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


@pytest.mark.parametrize("ready", [True, False])
def test_public_readiness_exposes_only_component_states(monkeypatch, ready):
    health, client = _import_health_module()
    diagnostic = "backend connection failed at /srv/private/extensions with private detail"
    checks = {
        "postgres": {"ok": ready, "error": diagnostic, "dsn": diagnostic},
        "memory_backends": {
            "ok": ready,
            "matrix": {"backend": diagnostic},
            "workflow_knowledge": {"errors": [diagnostic]},
        },
    }
    monkeypatch.setattr(health.deps, "get_logger_only", lambda: object())
    monkeypatch.setattr(
        health.deps, "compute_readiness", lambda _bl: {"ready": ready, "checks": checks}
    )

    response = client.get("/health/ready")

    assert response.status_code == (200 if ready else 503)
    assert response.json()["ready"] is ready
    assert response.json()["checks"] == {name: {"ok": ready} for name in checks}
    assert diagnostic not in response.text
    # The public projection must not destroy the protected operator diagnostics.
    assert checks["postgres"]["error"] == diagnostic


@pytest.mark.parametrize("mutation", ["none", "failure", "boolean", "digest", "recovery", "count"])
def test_public_readiness_preserves_release_transition_safety(monkeypatch, mutation):
    from core.application.tooling.ops.deployment_doctor_live import (
        _expected_release_transition_only,
    )

    health, client = _import_health_module()
    stale = "Workflow knowledge index is stale for the requested account/project scope."
    checks = {
        "postgres": {"ok": True},
        "lifecycle_recovery": {"ok": False, "state": "mutation_in_progress"},
        "memory_backends": {
            "ok": False,
            "error": stale,
            "workflow_knowledge": {
                "error": stale,
                "graph": {"available": True},
                "vector": {"available": True},
                "scope": {
                    "freshness": "stale",
                    "empty": False,
                    "digest_fresh": False,
                    "catalog_digest": "a" * 64,
                    "stored_digest": "b" * 64,
                },
            },
        },
        "workflow_knowledge_sync": {
            "ok": False,
            "detail": "workflow knowledge sync is stale by 1500s (limit 1200s)",
            "last_failure_reason": "",
            "consecutive_failures": 0,
        },
    }
    if mutation == "failure":
        checks["workflow_knowledge_sync"]["last_failure_reason"] = "private driver failure"
    elif mutation == "boolean":
        checks["postgres"]["ok"] = "false"
    elif mutation == "digest":
        checks["memory_backends"]["workflow_knowledge"]["scope"]["stored_digest"] = "a" * 64
    elif mutation == "recovery":
        checks["lifecycle_recovery"]["state"] = "recovery_required"
    elif mutation == "count":
        checks["workflow_knowledge_sync"]["consecutive_failures"] = "0"
    monkeypatch.setattr(health.deps, "get_logger_only", lambda: object())
    monkeypatch.setattr(
        health.deps, "compute_readiness", lambda _bl: {"ready": False, "checks": checks}
    )

    response = client.get("/health/ready")

    assert response.status_code == 503
    assert _expected_release_transition_only(response.json()) is (mutation == "none")
    assert "private driver failure" not in response.text


def test_public_readiness_preserves_maintenance_stuck_job_safety(monkeypatch):
    from core.application.tooling.ops.deployment_doctor_live import (
        _expected_lifecycle_mutation_only,
    )
    from core.entrypoints.cli.maintenance_adapters import _is_persisted_stuck_jobs_only

    health, client = _import_health_module()
    checks = {
        "postgres": {"ok": True},
        "lifecycle_recovery": {"ok": False, "state": "mutation_in_progress"},
    }
    monkeypatch.setattr(health.deps, "get_logger_only", lambda: object())
    monkeypatch.setattr(
        health.deps, "compute_readiness", lambda _bl: {"ready": False, "checks": checks}
    )
    assert _expected_lifecycle_mutation_only(client.get("/health/ready").json()) is True
    checks["stuck_jobs"] = {
        "ok": False,
        "persisted_count": 3,
        "detection_count": 0,
        "max_allowed": 0,
        "error": "",
    }
    body = client.get("/health/ready").json()
    assert _is_persisted_stuck_jobs_only("stuck_jobs", body["checks"]["stuck_jobs"]) is True
    checks["stuck_jobs"]["error"] = "private backend failure"
    response = client.get("/health/ready")
    assert (
        _is_persisted_stuck_jobs_only("stuck_jobs", response.json()["checks"]["stuck_jobs"])
        is False
    )
    assert "private backend failure" not in response.text
