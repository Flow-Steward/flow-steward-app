"""Account boundary of the workflow routes (ADR 0020, step C4b).

Workflow identity is account-qualified, so a caller of any account reaches the
workflow routes, and every workflow read or write of the request works on the
caller's own account: an explicit ``account_id`` names it, and a store call that
names none uses the account the router binds for the request.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.application.auth.config import DEFAULT_ACCOUNT_ID
from core.application.management.scope import normalize_scope
from core.application.registry.workflow_account import (
    WorkflowAccountMissingError,
    bound_workflow_account,
    installation_account_scope,
    workflow_account_scope,
    workflow_owner,
)
from core.entrypoints.admin import workflows as workflow_routes
from core.entrypoints.admin import workflows_routes_listing_import as import_routes

FOREIGN_ACCOUNT_ID = "11111111-1111-1111-1111-111111111111"


def _client_for_account(account_id: str) -> TestClient:
    app = FastAPI()
    app.include_router(workflow_routes.router)

    @app.middleware("http")
    async def _inject_scope(request, call_next):
        request.state.auth_user = {
            "account_id": DEFAULT_ACCOUNT_ID,
            "username": "admin",
            "roles": ["admin"],
        }
        request.state.scope_context = normalize_scope(
            account_id=account_id,
            actor_id="admin@example.com",
            principal_type="admin",
        )
        return await call_next(request)

    return TestClient(app, raise_server_exceptions=False)


@pytest.mark.parametrize("account_id", [DEFAULT_ACCOUNT_ID, FOREIGN_ACCOUNT_ID])
def test_the_workflow_list_is_read_for_the_callers_account(monkeypatch, account_id: str) -> None:
    seen: dict[str, object] = {}
    db = MagicMock()

    def list_workflows(**_kwargs):
        # The route names no account: the store call works on the bound one.
        seen["bound"] = workflow_owner()
        return []

    db.list_workflows.side_effect = list_workflows
    portability = MagicMock()
    portability.list_catalog_extension_associations.return_value = []
    monkeypatch.setattr(import_routes, "_get_db", lambda: db)
    monkeypatch.setattr(import_routes, "_get_workflow_portability_service", lambda _db: portability)
    monkeypatch.setattr(
        import_routes, "projection_aware_catalog_rows", lambda _db, _k, _i, rows, **_kw: rows
    )

    response = _client_for_account(account_id).get("/api/workflows")

    assert response.status_code == 200, response.text
    assert seen == {"bound": account_id}


def test_an_unbound_call_is_refused_instead_of_using_the_default_account() -> None:
    # ADR 0020, C5: a call that names no account outside a request or a job is
    # refused; it never falls back to the installation account.
    assert bound_workflow_account() is None
    with pytest.raises(WorkflowAccountMissingError):
        workflow_owner()
    assert workflow_owner(FOREIGN_ACCOUNT_ID) == FOREIGN_ACCOUNT_ID


def test_an_account_scope_without_an_account_is_refused() -> None:
    with pytest.raises(WorkflowAccountMissingError), workflow_account_scope(""):
        pass
    assert bound_workflow_account() is None


def test_installation_paths_bind_the_installation_account_explicitly() -> None:
    with installation_account_scope() as owner:
        assert owner == DEFAULT_ACCOUNT_ID
        assert workflow_owner() == DEFAULT_ACCOUNT_ID
    assert bound_workflow_account() is None


def test_a_job_binding_ends_with_the_job() -> None:
    with workflow_account_scope(FOREIGN_ACCOUNT_ID) as owner:
        assert owner == FOREIGN_ACCOUNT_ID
        assert workflow_owner() == FOREIGN_ACCOUNT_ID
        # An explicit account still wins over the binding.
        assert workflow_owner(DEFAULT_ACCOUNT_ID) == DEFAULT_ACCOUNT_ID
    assert bound_workflow_account() is None
    with pytest.raises(WorkflowAccountMissingError):
        workflow_owner()
