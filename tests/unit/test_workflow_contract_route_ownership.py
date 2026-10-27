"""One owner per public path for workflow contract lookup.

Two handlers were registered for GET `/api/workflows/{workflow_id}/contract`:
the admin authoring one first and the operator one after it. Starlette
dispatches the first match, so the run-gated handler was unreachable at that
URL — it existed, it was tested, and nothing could ever call it. Run-facing
semantics live on the project-scoped path, which is still gated.
"""

from __future__ import annotations

from collections import defaultdict
from unittest.mock import MagicMock

from fastapi import FastAPI

from core.entrypoints.admin import workflows as workflow_routes
from core.entrypoints.api.workflow_operator import create_workflow_operator_router

_CONTRACT_PATH = "/api/workflows/{workflow_id}/contract"


def _composed_app() -> FastAPI:
    """The two routers that between them own the workflow contract paths."""
    app = FastAPI()
    app.include_router(workflow_routes.router)
    app.include_router(
        create_workflow_operator_router(
            workflow_operator_service=lambda: MagicMock(),
            request_scope=lambda *_a, **_k: MagicMock(),
            service_error_response=lambda exc: {"error": str(exc)},
            api_error_response=lambda exc: {"error": str(exc)},
        )
    )
    return app


def _owners(app: FastAPI) -> dict[tuple[str, str], list[str]]:
    owners: dict[tuple[str, str], list[str]] = defaultdict(list)
    for route in app.routes:
        for method in getattr(route, "methods", None) or ():
            owners[(method, getattr(route, "path", ""))].append(getattr(route, "name", "?"))
    return owners


def test_the_workflow_contract_path_has_exactly_one_owner() -> None:
    owners = _owners(_composed_app())

    assert owners[("GET", _CONTRACT_PATH)] == ["get_workflow_contract"], owners[
        ("GET", _CONTRACT_PATH)
    ]


def test_no_workflow_contract_path_is_registered_twice() -> None:
    owners = _owners(_composed_app())

    duplicated = {
        key: names for key, names in owners.items() if len(names) > 1 and "contract" in key[1]
    }

    assert duplicated == {}, duplicated


def test_the_run_facing_project_contract_is_still_owned() -> None:
    """Removing the unreachable duplicate must not remove run-facing lookup."""
    owners = _owners(_composed_app())

    project_path = "/api/projects/{project_id}/workflows/{workflow_id}/contract"
    assert len(owners[("GET", project_path)]) == 1, owners[("GET", project_path)]
