"""Fail-closed OSS boundary for globally keyed workflow creation paths."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.application.management.scope import normalize_scope
from core.entrypoints.admin import workflows as workflow_routes
from core.entrypoints.admin import workflows_routes_listing_import as import_routes
from core.entrypoints.admin import workflows_routes_read as read_routes
from core.entrypoints.admin import workflows_routes_write as write_routes

FOREIGN_ACCOUNT_ID = "11111111-1111-1111-1111-111111111111"


@pytest.mark.parametrize(
    ("path", "body", "route_module"),
    [
        (
            "/api/workflows",
            {"workflow_id": "foreign_workflow", "display_name": "Foreign workflow"},
            write_routes,
        ),
        ("/api/workflows/source_workflow/clone", {}, write_routes),
        ("/api/workflows/import/apply", {}, import_routes),
    ],
)
def test_foreign_account_workflow_creation_paths_reject_before_database_access(
    monkeypatch,
    path: str,
    body: dict,
    route_module,
) -> None:
    get_db = MagicMock()
    monkeypatch.setattr(route_module, "_get_db", get_db)
    app = FastAPI()
    app.include_router(workflow_routes.router)

    @app.middleware("http")
    async def _inject_foreign_scope(request, call_next):
        request.state.auth_user = {"username": "admin", "roles": ["admin"]}
        request.state.scope_context = normalize_scope(
            account_id=FOREIGN_ACCOUNT_ID,
            actor_id="foreign-admin@example.com",
            principal_type="admin",
        )
        return await call_next(request)

    response = TestClient(app, raise_server_exceptions=False).post(path, json=body)

    assert response.status_code == 403
    assert "default account" in response.text
    get_db.assert_not_called()


@pytest.mark.parametrize(
    "path",
    [
        "/api/workflows",
        "/api/workflows?search=global",
        "/api/workflows/source_workflow",
        "/api/workflows/meta/callable-workflows",
    ],
)
def test_foreign_account_workflow_catalog_rejects_before_database_access(
    monkeypatch,
    path: str,
) -> None:
    listing_get_db = MagicMock()
    read_get_db = MagicMock()
    monkeypatch.setattr(import_routes, "_get_db", listing_get_db)
    monkeypatch.setattr(read_routes, "_get_db", read_get_db)
    app = FastAPI()
    app.include_router(workflow_routes.router)

    @app.middleware("http")
    async def _inject_foreign_scope(request, call_next):
        request.state.auth_user = {"username": "admin", "roles": ["admin"]}
        request.state.scope_context = normalize_scope(
            account_id=FOREIGN_ACCOUNT_ID,
            actor_id="foreign-admin@example.com",
            principal_type="admin",
        )
        return await call_next(request)

    response = TestClient(app, raise_server_exceptions=False).get(path)

    assert response.status_code == 403
    assert "default account" in response.text
    listing_get_db.assert_not_called()
    read_get_db.assert_not_called()
