from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

_OPTIONAL_TEST_DEPS = {
    "fastapi",
    "starlette",
    "pydantic",
}


def _skip_only_missing_optional_dep(exc: Exception, *, context: str) -> None:
    if isinstance(exc, ModuleNotFoundError) and getattr(exc, "name", "") in _OPTIONAL_TEST_DEPS:
        pytest.skip(f"{context}: missing optional dependency '{exc.name}'")
    raise exc


def test_list_runs_hides_soft_deleted_jobs_and_projects():
    try:
        from core.infrastructure.monitoring import runs_storage
    except Exception as exc:  # pragma: no cover - optional deps in lightweight envs
        _skip_only_missing_optional_dep(exc, context="monitoring import")

    bl = MagicMock()
    cur = MagicMock()
    cur.__enter__ = MagicMock(return_value=cur)
    cur.__exit__ = MagicMock(return_value=False)
    cur.description = [
        ("id",),
        ("project_name",),
        ("task_id",),
        ("task_summary",),
        ("status",),
        ("started_at",),
        ("finished_at",),
    ]
    cur.fetchall.return_value = [
        (1, "demo-project", "task-1", "summary", "completed", None, None),
    ]
    bl.conn.cursor.return_value = cur
    bl._column_exists.side_effect = lambda table, column: (
        column == "deleted_at" and table in {"jobs", "projects"}
    )

    with (
        patch(
            "core.infrastructure.monitoring.runs_storage._observability_dsn",
            return_value="postgresql://example",
        ),
        patch.object(runs_storage, "PlatformConnectionStore", return_value=bl),
    ):
        rows = runs_storage.list_recent_runs_rows(
            project_name="demo-project",
            status="completed",
            limit=5,
        )

    assert len(rows) == 1
    assert rows[0]["project_name"] == "demo-project"
    executed_sql = str(cur.execute.call_args.args[0])
    assert "j.deleted_at IS NULL" in executed_sql
    assert "p.deleted_at IS NULL" in executed_sql


def test_api_runs_requires_operator_role():
    try:
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from core.entrypoints.admin.monitoring import router
    except Exception as exc:  # pragma: no cover - optional deps in lightweight envs
        _skip_only_missing_optional_dep(exc, context="monitoring route import")

    app = FastAPI()
    app.include_router(router)

    @app.middleware("http")
    async def mock_viewer(request, call_next):
        request.state.auth_user = {"username": "viewer", "roles": ["viewer"]}
        return await call_next(request)

    resp = TestClient(app).get("/api/runs")
    assert resp.status_code == 403


def test_api_runs_allows_operator_role():
    try:
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from core.entrypoints.admin.monitoring import router
    except Exception as exc:  # pragma: no cover - optional deps in lightweight envs
        _skip_only_missing_optional_dep(exc, context="monitoring route import")

    app = FastAPI()
    app.include_router(router)

    @app.middleware("http")
    async def mock_operator(request, call_next):
        request.state.auth_user = {"username": "ops", "roles": ["operator"]}
        return await call_next(request)

    with patch("core.entrypoints.admin.monitoring.MonitoringRunsService") as service_cls:
        service_cls.return_value.list_runs.return_value = [{"id": 1}]
        resp = TestClient(app).get("/api/runs")

    assert resp.status_code == 200
    assert resp.json() == [{"id": 1}]


def test_validate_reload_requires_admin_role():
    try:
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from core.entrypoints.admin.monitoring import router
    except Exception as exc:  # pragma: no cover - optional deps in lightweight envs
        _skip_only_missing_optional_dep(exc, context="monitoring route import")

    app = FastAPI()
    app.include_router(router)

    @app.middleware("http")
    async def mock_operator(request, call_next):
        request.state.auth_user = {"username": "ops", "roles": ["operator"]}
        return await call_next(request)

    registry = MagicMock()
    with patch("core.entrypoints.admin.monitoring._get_registry", return_value=registry):
        resp = TestClient(app).get("/api/validate?reload=true")

    assert resp.status_code == 403
    registry.reload_all.assert_not_called()


def test_registry_metadata_routes_require_operator_role():
    try:
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from core.entrypoints.admin.monitoring import router
    except Exception as exc:  # pragma: no cover - optional deps in lightweight envs
        _skip_only_missing_optional_dep(exc, context="monitoring route import")

    app = FastAPI()
    app.include_router(router)

    @app.middleware("http")
    async def mock_viewer(request, call_next):
        request.state.auth_user = {"username": "viewer", "roles": ["viewer"]}
        return await call_next(request)

    client = TestClient(app)

    assert client.get("/api/validate").status_code == 403
    assert client.get("/api/registry/stats").status_code == 403
    assert client.get("/api/graph").status_code == 403


def test_process_payload_redacts_active_lease_tokens():
    # FS-DEEP-CAND-002: the process-monitoring payload must not serialize the
    # authority-bearing lease_token to operator-readable callers; only a presence
    # flag is exposed for diagnostics.
    from datetime import UTC, datetime

    from core.application.monitoring import processes as monitoring_processes

    now = datetime.now(UTC)
    lease_rows = [
        {
            "project_id": "proj-1",
            "worker_instance_id": "worker-abc",
            "lease_token": "super-secret-lease-token-xyz",
            "heartbeat_at": now,
            "expires_at": now,
            "acquired_at": now,
            "active_job_id": "job-9",
            "drain_status": "",
        }
    ]

    with patch.object(
        monitoring_processes, "load_active_project_lease_rows", return_value=lease_rows
    ):
        leases = monitoring_processes._get_active_project_leases()

    assert isinstance(leases, list)
    row = leases[0]
    assert "lease_token" not in row
    assert row["lease_token_present"] is True
    # Non-secret monitoring fields remain available.
    assert row["worker_instance_id"] == "worker-abc"
    assert row["project_id"] == "proj-1"
    assert row["active_job_id"] == "job-9"
    # Defense in depth: the secret value must not leak anywhere in the payload.
    import json

    assert "super-secret-lease-token-xyz" not in json.dumps(leases)
