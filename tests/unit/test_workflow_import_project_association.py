"""The project an operator picks on the import page must reach the stored row.

The page asks which project to import into and uses the answer to look up
dependency options, then never sent it with the apply request. Apply bound the
imported workflow to the request scope's project, which is empty for an admin
caller, so the workflow landed associated with nothing: the question had a
visible effect on the form and none on the result.

The service already stamps whatever `owner_project_id` it is given — that is
covered in `test_workflow_portability.py`. What is missing is the endpoint
carrying the operator's answer to it, and refusing one they may not reach.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
import yaml
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.application.management.scope import DEFAULT_ACCOUNT_ID, normalize_scope
from core.entrypoints.admin import workflows as workflow_routes
from core.entrypoints.admin import workflows_routes_listing_import as import_routes

_BUNDLE_YAML = yaml.safe_dump(
    {
        "schema_version": "platform.workflow_bundle.v2",
        "kind": "workflow_bundle",
        "metadata": {},
        "workflows": [
            {
                "workflow_id": "wf_incoming",
                "version": "1.0.0",
                "display_name": "Incoming",
                "description": "",
                "trigger": {},
                "settings": {},
                "participants": [],
                "tags": [],
                "steps": [],
                "groups": [],
            }
        ],
        "dependencies": {},
        "import_hints": {},
    },
    sort_keys=False,
)


@pytest.fixture
def portability() -> MagicMock:
    service = MagicMock()
    service.apply_import.return_value = {
        "status": "imported",
        "workflow_id": "wf_incoming",
        "imported_workflows": [{"workflow_id": "wf_incoming"}],
        "project_id": "project-allowed",
        "preflight": {},
    }
    return service


@pytest.fixture
def db() -> MagicMock:
    store = MagicMock()
    store.get_workflow.return_value = None
    return store


def _post(
    monkeypatch,
    db: MagicMock,
    portability: MagicMock,
    body: dict,
    *,
    registry: MagicMock | None = None,
):
    from core.entrypoints.admin import workflows_scope

    registry = registry or MagicMock()
    monkeypatch.setattr(import_routes, "_get_db", lambda *_a, **_k: db)
    monkeypatch.setattr(import_routes, "_get_registry", lambda *_a, **_k: registry)
    monkeypatch.setattr(workflows_scope, "_get_registry", lambda *_a, **_k: registry)
    monkeypatch.setattr(
        import_routes,
        "_get_workflow_portability_service",
        lambda *_a, **_k: portability,
    )
    app = FastAPI()
    app.include_router(workflow_routes.router)

    @app.middleware("http")
    async def _inject_scope(request, call_next):
        request.state.auth_user = {"username": "admin", "roles": ["admin"]}
        request.state.scope_context = normalize_scope(
            account_id=DEFAULT_ACCOUNT_ID,
            actor_id="admin@example.com",
            principal_type="admin",
            allowed_project_ids=frozenset({"project-allowed"}),
        )
        return await call_next(request)

    return TestClient(app, raise_server_exceptions=False).post(
        "/api/workflows/import/apply", json=body
    )


def test_the_selected_project_reaches_the_import(monkeypatch, db, portability) -> None:
    db.get_project.return_value = {"id": "project-allowed", "account_id": DEFAULT_ACCOUNT_ID}

    response = _post(
        monkeypatch,
        db,
        portability,
        {"bundle_yaml": _BUNDLE_YAML, "project_id": "project-allowed"},
    )

    assert response.status_code == 200, response.text
    assert portability.apply_import.call_args.kwargs["owner_project_id"] == "project-allowed"


def test_the_response_reads_the_association_back(monkeypatch, db, portability) -> None:
    """Writing it is half the fix; the page has to be able to confirm it."""
    db.get_project.return_value = {"id": "project-allowed", "account_id": DEFAULT_ACCOUNT_ID}

    response = _post(
        monkeypatch,
        db,
        portability,
        {"bundle_yaml": _BUNDLE_YAML, "project_id": "project-allowed"},
    )

    assert response.json()["project_id"] == "project-allowed"


def test_a_project_outside_the_caller_scope_is_refused(monkeypatch, db, portability) -> None:
    """Accepting the field from the caller must not widen what they can reach."""
    db.get_project.return_value = {"id": "project-other", "account_id": "account-other"}

    response = _post(
        monkeypatch,
        db,
        portability,
        {"bundle_yaml": _BUNDLE_YAML, "project_id": "project-other"},
    )

    assert response.status_code == 403
    portability.apply_import.assert_not_called()


def test_omitting_the_field_keeps_the_request_scope(monkeypatch, db, portability) -> None:
    """API callers that never send it keep the behaviour they have."""
    response = _post(monkeypatch, db, portability, {"bundle_yaml": _BUNDLE_YAML})

    assert response.status_code == 200, response.text
    assert portability.apply_import.call_args.kwargs["owner_project_id"] == ""


# ---------------------------------------------------------------------------
# Persisted by the atomic application use case, not the route
#
# `registry_workflows` has no project_id column and `save_workflow` never wrote
# one, so passing `owner_project_id` and echoing it back reported an
# association the database did not have. The application service now writes
# project_workflows inside the same transaction as every imported workflow and
# connection placeholder. The route must not repeat those writes after commit.
# ---------------------------------------------------------------------------


def test_the_route_does_not_write_binding_after_the_import_commits(
    monkeypatch, db, portability
) -> None:
    db.get_project.return_value = {"id": "project-allowed", "account_id": DEFAULT_ACCOUNT_ID}

    response = _post(
        monkeypatch,
        db,
        portability,
        {"bundle_yaml": _BUNDLE_YAML, "project_id": "project-allowed"},
    )

    assert response.status_code == 200, response.text
    db.add_project_workflow_binding.assert_not_called()


def test_multi_workflow_result_does_not_trigger_route_level_binding_writes(
    monkeypatch, db, portability
) -> None:
    db.get_project.return_value = {"id": "project-allowed", "account_id": DEFAULT_ACCOUNT_ID}
    portability.apply_import.return_value = {
        "status": "imported",
        "workflow_id": "wf_a",
        "imported_workflows": [{"workflow_id": "wf_a"}, {"workflow_id": "wf_b"}],
        "project_id": "project-allowed",
        "preflight": {},
    }

    _post(
        monkeypatch,
        db,
        portability,
        {"bundle_yaml": _BUNDLE_YAML, "project_id": "project-allowed"},
    )

    db.add_project_workflow_binding.assert_not_called()


def test_no_project_means_no_binding(monkeypatch, db, portability) -> None:
    _post(monkeypatch, db, portability, {"bundle_yaml": _BUNDLE_YAML})

    db.add_project_workflow_binding.assert_not_called()


def test_committed_import_stays_successful_when_narrow_cache_refresh_fails(
    monkeypatch, db, portability
) -> None:
    registry = MagicMock()
    registry.refresh_workflow_cache.side_effect = RuntimeError("refresh failed after commit")

    response = _post(
        monkeypatch,
        db,
        portability,
        {"bundle_yaml": _BUNDLE_YAML},
        registry=registry,
    )

    assert response.status_code == 200, response.text
    assert response.json()["workflow_id"] == "wf_incoming"
    registry.refresh_workflow_cache.assert_called_once_with()
    registry.reload_all.assert_not_called()
