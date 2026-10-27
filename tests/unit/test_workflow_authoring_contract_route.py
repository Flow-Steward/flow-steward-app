"""Opening the builder for a disabled workflow must not answer 500.

The admin contract route reused the run-gated projection, so a disabled draft
raised `ServiceNotFoundError` inside a route with no exception mapping and the
browser received HTTP 500 — twice per page load — for a workflow its author is
entitled to edit. Execution and operator paths keep treating an unavailable
workflow as unavailable; that distinction is the fix, not a relaxed gate.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.application.management.errors import ServiceNotFoundError
from core.application.management.scope import DEFAULT_ACCOUNT_ID, normalize_scope
from core.entrypoints.admin import workflows as workflow_routes
from core.entrypoints.admin import workflows_routes_read as read_routes

_WORKFLOW_ID = "draft_workflow"


def _disabled_workflow() -> dict:
    return {
        "workflow_id": _WORKFLOW_ID,
        "display_name": "Draft in progress",
        "enabled": False,
        "settings": {"callable": {"enabled": True, "output_mode": "none"}},
    }


@pytest.fixture
def contract_client(monkeypatch):
    db = MagicMock()
    db.get_workflow.return_value = _disabled_workflow()
    monkeypatch.setattr(read_routes, "_get_db", lambda *_a, **_k: db)
    monkeypatch.setattr(
        read_routes,
        "_resolve_active_workflow_scope_target",
        lambda *_a, **_k: (_disabled_workflow(), None),
    )
    app = FastAPI()
    app.include_router(workflow_routes.router)

    @app.middleware("http")
    async def _inject_scope(request, call_next):
        request.state.auth_user = {"username": "admin", "roles": ["admin"]}
        request.state.scope_context = normalize_scope(
            account_id=DEFAULT_ACCOUNT_ID,
            actor_id="author@example.com",
            principal_type="admin",
        )
        return await call_next(request)

    return TestClient(app, raise_server_exceptions=False)


def test_an_author_gets_the_contract_for_a_disabled_workflow(monkeypatch, contract_client) -> None:
    service = MagicMock()
    service.workflow_contract.return_value.dump.return_value = {
        "workflow_id": _WORKFLOW_ID,
        "interface": {},
    }
    monkeypatch.setattr(read_routes, "_workflow_operator_service", lambda **_k: service)

    response = contract_client.get(f"/api/workflows/{_WORKFLOW_ID}/contract")

    assert response.status_code == 200, response.text
    assert response.json()["workflow_id"] == _WORKFLOW_ID
    # Asked for as an author would: editing, not running.
    assert service.workflow_contract.call_args.kwargs["require_available"] is False


def test_a_missing_workflow_is_a_404_not_a_traceback(monkeypatch, contract_client) -> None:
    """Existence still fails closed, and it fails as a mapped status."""
    service = MagicMock()
    service.workflow_contract.side_effect = ServiceNotFoundError("Workflow 'gone' not found")
    monkeypatch.setattr(read_routes, "_workflow_operator_service", lambda **_k: service)

    response = contract_client.get(f"/api/workflows/{_WORKFLOW_ID}/contract")

    assert response.status_code == 404
    assert "Traceback" not in response.text


def test_no_5xx_reaches_the_builder_for_an_expected_service_error(
    monkeypatch, contract_client
) -> None:
    """The symptom, asserted directly."""
    service = MagicMock()
    service.workflow_contract.side_effect = ServiceNotFoundError(
        f"Workflow '{_WORKFLOW_ID}' not available"
    )
    monkeypatch.setattr(read_routes, "_workflow_operator_service", lambda **_k: service)

    response = contract_client.get(f"/api/workflows/{_WORKFLOW_ID}/contract")

    assert response.status_code < 500, response.text
