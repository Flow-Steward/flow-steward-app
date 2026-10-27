"""Workflow import must refuse a request clearly instead of crashing on it.

Regression for the workflow E2E ledger findings F7, F32 and F34, all on the same
import surface:

* F34 — a `project_connection` resolution that omits `project_id` reached the
  database as an empty string and Postgres raised
  `invalid input syntax for type uuid: ""`. The exception was unhandled, so the
  client got a bare 500 with no body at all. Adding `project_id` made the same
  call succeed, which confirms both the cause and that the feature works.
* F32 — `resolutions` must be a list, and sending the more natural `{}` produced
  a bare `Input should be a valid list` with no hint that an empty list is what
  is wanted.
* F7 — an optional "Target workflow ID" left blank must not become an invalid
  identifier.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi import HTTPException

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import logging as _b06_logging

# B06: both public HTTP consumers translate an actual PostgreSQL create conflict.
from functools import partial as _b06_partial

from fastapi import FastAPI as _B06FastAPI
from fastapi.testclient import TestClient as _B06TestClient

from core.application.management.query_services import ProjectQueryService as _B06ProjectQuery
from core.application.management.scope import ScopeContext as _B06ScopeContext
from core.application.management.workflow_operator_service import (
    WorkflowOperatorService as _B06WorkflowOperator,
)
from core.entrypoints.admin import workflows_routes_listing_import as _b06_admin_import
from core.entrypoints.admin import workflows_scope as _b06_admin_scope
from core.entrypoints.admin.workflows_models import WorkflowImportRequest
from core.entrypoints.admin.workflows_scope import _enforce_import_resolution_scope
from core.entrypoints.api.route_bindings import request_admin_authority as _b06_admin_authority
from core.entrypoints.api.workflow_operator import (
    create_workflow_operator_router as _b06_operator_router,
)
from core.infrastructure.runtime import serve_request_runtime as _b06_http_errors
from tests.unit import test_workflow_portability as _b06_portability_tests

b06_import_audit = _b06_portability_tests.b06_import_audit


PROJECT_ID = "3cd3c569-8dc7-448f-a8f8-ed88ae770f8d"
CONNECTION_ID = "conn_5227da7a32b84e9c"


class _Scope:
    account_id = "acc-1"
    allowed_project_ids = None
    allowed_workspace_ids = None
    allowed_workflow_ids = None
    workspace_id = ""
    tenant_id = ""
    actor_id = "admin"
    principal_type = "admin"
    role = "admin"


class _DB:
    """Stands in for the store, and fails the way Postgres does on an empty uuid."""

    def __init__(self):
        self.connection_lookups: list[tuple[str, str]] = []

    def get_project(self, project_id: str):
        if not str(project_id or "").strip():
            raise AssertionError("an empty project id must never reach the store")
        return {"id": project_id, "project_id": project_id, "account_id": "acc-1"}

    def get_project_connection(self, *, project_id: str, connection_id: str):
        if not str(project_id or "").strip():
            raise ValueError('invalid input syntax for type uuid: ""')
        self.connection_lookups.append((project_id, connection_id))
        return {"connection_id": connection_id, "project_id": project_id}


def _resolution(**overrides) -> dict:
    payload = {
        "dependency_type": "project_connection",
        "dependency_key": f"project_connection:{CONNECTION_ID}",
        "action": "map_existing",
        "target_ref": CONNECTION_ID,
        "project_id": PROJECT_ID,
    }
    payload.update(overrides)
    return payload


# ---------------------------------------------------------------------------
# F34
# ---------------------------------------------------------------------------


def test_a_resolution_with_a_project_id_resolves_the_connection():
    db = _DB()

    _enforce_import_resolution_scope(db, _Scope(), [_resolution()])

    assert db.connection_lookups == [(PROJECT_ID, CONNECTION_ID)]


@pytest.mark.parametrize("missing", [{}, {"project_id": ""}, {"project_id": None}])
def test_a_project_connection_resolution_without_a_project_id_is_a_4xx(missing):
    """The empty project id used to reach Postgres and surface as a bare 500."""
    resolution = _resolution()
    resolution.pop("project_id", None)
    resolution.update(missing)
    db = _DB()

    _raises_input_98_1 = _Scope()
    with pytest.raises(HTTPException) as excinfo:
        _enforce_import_resolution_scope(db, _raises_input_98_1, [resolution])

    assert 400 <= excinfo.value.status_code < 500
    detail = str(excinfo.value.detail)
    assert "project_id" in detail
    assert db.connection_lookups == [], "nothing may be looked up without a project"


def test_a_resolution_that_needs_no_connection_lookup_is_untouched():
    """A resolution that names no target connection needs no project either."""
    db = _DB()

    _enforce_import_resolution_scope(
        db, _Scope(), [_resolution(action="skip", target_ref="", project_id="")]
    )

    assert db.connection_lookups == []


def test_unrelated_resolutions_are_left_alone():
    db = _DB()

    _enforce_import_resolution_scope(
        db,
        _Scope(),
        [{"dependency_type": "agent", "dependency_key": "agent:writer", "action": "map_existing"}],
    )

    assert db.connection_lookups == []


# ---------------------------------------------------------------------------
# F32 and F7
# ---------------------------------------------------------------------------


def test_an_empty_target_workflow_id_is_treated_as_absent():
    """The optional rename field left blank must not become an invalid id."""
    assert WorkflowImportRequest(bundle_yaml="x", target_workflow_id="").target_workflow_id is None
    assert (
        WorkflowImportRequest(bundle_yaml="x", target_workflow_id="   ").target_workflow_id is None
    )
    assert WorkflowImportRequest(bundle_yaml="x").target_workflow_id is None


def test_resolutions_default_to_an_empty_list():
    assert WorkflowImportRequest(bundle_yaml="x").resolutions == []


def test_a_non_list_resolutions_value_says_what_is_wanted():
    """`{}` is the natural first guess; the error must name the empty list."""
    with pytest.raises(ValueError) as excinfo:
        WorkflowImportRequest(bundle_yaml="x", resolutions={})

    message = str(excinfo.value)
    assert "resolutions" in message
    assert "[]" in message or "empty list" in message


def _b06_http_app(monkeypatch, audit, surface):
    scope = _B06ScopeContext(
        account_id=audit.account_id,
        project_id=audit.project_id,
        actor_id="B06 importer",
        principal_type="admin",
        auth_source="session",
        allowed_project_ids=frozenset({audit.project_id}),
    )
    user = {
        "id": "B06 importer",
        "username": "B06 importer",
        "roles": ["admin"],
        "account_id": audit.account_id,
        "auth_source": "session",
        "scopes": [],
        "principal_type": "user",
    }
    app = _B06FastAPI()

    @app.middleware("http")
    async def supply_known_fixture_identity(request, call_next):
        request.state.scope_context = scope
        request.state.auth_user = user
        return await call_next(request)

    if surface == "admin":
        # Outer store composition only: retain the real service factory, mapper,
        # scope/dependency checks, apply_import and HTTP translation.
        monkeypatch.setattr(_b06_admin_scope, "_get_db", lambda: audit.store)
        monkeypatch.setattr(
            _b06_admin_scope, "_platform_logger_for_admin_catalog", lambda _db: audit.integration
        )
        monkeypatch.setattr(_b06_admin_import, "_get_db", lambda: audit.store)
        monkeypatch.setattr(
            _b06_admin_import, "_platform_logger_for_admin_catalog", lambda _db: audit.integration
        )
        app.include_router(_b06_admin_import.router)
        return app, "/api/workflows/import/apply"

    def require_authenticated_user(request, *, required_roles=None):
        assert request.state.scope_context == scope
        if required_roles:
            assert set(required_roles) & set(user["roles"])
        return user

    project_query = _B06ProjectQuery(
        get_logger=lambda: audit.projects,
        serialize_projects_grid_row=dict,
        get_queue_depth=lambda _priority: 0,
        get_ready_queue_depth=lambda: 0,
    )

    def operator(request):
        return _B06WorkflowOperator(
            get_logger=lambda: audit.projects,
            project_query_service=lambda: project_query,
            registry_db_provider=lambda: audit.store,
            registry_manager_provider=lambda: _b06_portability_tests._B06Namespace(),
            get_project_store=lambda: audit.projects,
            get_integration_store=lambda: audit.integration,
            authoring_authorizer=_b06_admin_authority(
                request,
                require_authenticated_user=require_authenticated_user,
                auth_admin_roles={"admin"},
            ),
        )

    correlation = _b06_partial(
        _b06_http_errors.request_correlation_id, new_id_fn=lambda: "B06 correlation"
    )
    envelope = _b06_http_errors.build_error_payload_factory(
        err_request_failed="Request failed", default_correlation_id_fn=lambda: "B06 correlation"
    )
    app.include_router(
        _b06_operator_router(
            workflow_operator_service=operator,
            request_scope=lambda request, **_kwargs: request.state.scope_context,
            service_error_response=_b06_partial(
                _b06_http_errors.service_error_response,
                error_payload=envelope,
                request_correlation_id=correlation,
                error_code_for_status=_b06_http_errors.error_code_for_status,
                err_request_failed="Request failed",
            ),
            api_error_response=_b06_partial(
                _b06_http_errors.api_error_response,
                logger=_b06_logging.getLogger(__name__),
                error_payload=envelope,
                request_correlation_id=correlation,
            ),
        )
    )
    return app, f"/api/projects/{audit.project_id}/workflow-imports"


@pytest.mark.parametrize("surface", ["admin", "project"])
def test_atomic_import_create_conflict_is_typed_409(b06_import_audit, monkeypatch, surface):
    audit = b06_import_audit
    target_id = audit.allocate(f"http_{surface}")
    bundle = _b06_portability_tests._b06_bundle(target_id)
    assert audit.service.preflight_import(bundle)["can_apply"] is True
    race = _b06_portability_tests._b06_compete_before_create(monkeypatch, audit, target_id)
    app, path = _b06_http_app(monkeypatch, audit, surface)
    body = {"bundle_yaml": bundle, "resolutions": []}
    if surface == "admin":
        body["project_id"] = audit.project_id
    with _B06TestClient(app, raise_server_exceptions=False) as client:
        response = client.post(path, json=body)

    assert race["calls"] == 1
    assert isinstance(race["refused"], _b06_portability_tests._B06WorkflowCreateConflictError)
    # Existing unhandled real conflict is 500; both public surfaces must return409.
    assert response.status_code == 409, response.text
    detail = response.json()
    if surface == "admin":
        detail = detail["detail"]
        assert detail["error"] == "workflow_id_conflict"
        assert "code" not in detail
        errors = detail["errors"]
    else:
        assert detail["code"] == "conflict"
        errors = detail["errors"]
        assert detail["details"]["context"]["errors"] == errors
    # Only the established safe dependency identity, never a competing DB row.
    assert errors == [
        {
            "dependency_type": "workflow",
            "ref": target_id,
            "status": "conflict",
            "code": "workflow_id_conflict",
            "target_workflow_id": target_id,
        }
    ]
    assert audit.store._get_tx_conn() is None
    assert _b06_portability_tests._b06_stored(audit.observer, target_id) == race["committed"]
    assert (
        audit.observer.execute(
            "SELECT 1 FROM project_workflows WHERE project_id=%s AND workflow_id=%s",
            (audit.project_id, target_id),
        ).fetchall()
        == []
    )
