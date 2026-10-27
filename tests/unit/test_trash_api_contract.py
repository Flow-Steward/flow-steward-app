from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from core.entrypoints.api.trash import create_api_trash_router


@pytest.mark.parametrize("entity_type", ["project", "workspace", "mcp_connector"])
def test_direct_root_hard_delete_requires_revision_bound_lifecycle_plan(
    entity_type: str,
) -> None:
    service = Mock()
    service.hard_delete.return_value = SimpleNamespace(dump=lambda: {"status": "hard_deleted"})
    scope = SimpleNamespace(actor_name="release-auditor")
    app = FastAPI()
    app.include_router(
        create_api_trash_router(
            require_authenticated_user=lambda *_args, **_kwargs: {},
            auth_admin_roles={"admin"},
            get_logger_only=lambda: None,
            real_dict_cursor_kwargs=dict,
            request_scope=lambda _request, **_kwargs: scope,
            trash_lifecycle_service=lambda: service,
            service_error_response=lambda error: JSONResponse(
                {"error": str(error)}, status_code=error.status_code
            ),
            api_error_response=lambda error: JSONResponse({"error": str(error)}, status_code=500),
            err_admin_role_required="admin required",
            err_database_not_configured="database unavailable",
        )
    )

    response = TestClient(app).post(f"/api/trash/{entity_type}/root-1/hard-delete")

    assert response.status_code == 409
    assert "lifecycle plan" in response.text.lower()
    service.hard_delete.assert_not_called()


def test_workspace_delete_execute_forwards_explicit_connection_actions() -> None:
    service = Mock()
    service.trash_workspace_with_plan.return_value = {"status": "trashed"}
    scope = SimpleNamespace(actor_name="release-auditor")
    app = FastAPI()
    app.include_router(
        create_api_trash_router(
            require_authenticated_user=lambda *_args, **_kwargs: {},
            auth_admin_roles={"admin"},
            get_logger_only=lambda: None,
            real_dict_cursor_kwargs=dict,
            request_scope=lambda _request: scope,
            trash_lifecycle_service=lambda: service,
            service_error_response=lambda error: JSONResponse(
                {"error": str(error)}, status_code=400
            ),
            api_error_response=lambda error: JSONResponse({"error": str(error)}, status_code=500),
            err_admin_role_required="admin required",
            err_database_not_configured="database unavailable",
        )
    )

    response = TestClient(app).post(
        "/api/trash/workspace/workspace-1/delete-execute",
        json={
            "project_actions": [],
            "connection_actions": [
                {
                    "connection_id": "connection-1",
                    "action": "reassign",
                    "target_workspace_id": "workspace-2",
                }
            ],
        },
    )

    assert response.status_code == 200
    service.trash_workspace_with_plan.assert_called_once_with(
        "workspace-1",
        deleted_by="release-auditor",
        project_actions=[],
        connection_actions=[
            {
                "connection_id": "connection-1",
                "action": "reassign",
                "target_workspace_id": "workspace-2",
            }
        ],
        scope=scope,
    )


class _TrashCursor:
    def __init__(self, executed: list[tuple[str, tuple]]) -> None:
        self._executed = executed

    def __enter__(self) -> "_TrashCursor":
        return self

    def __exit__(self, *_exc) -> bool:
        return False

    def execute(self, sql, params=()) -> None:
        self._executed.append((str(sql), tuple(params)))

    def fetchall(self) -> list:
        return []


def _list_trash(entity_type: str, columns: set[str]) -> list[tuple[str, tuple]]:
    from core.application.auth.config import DEFAULT_ACCOUNT_ID
    from core.application.management.scope import ScopeContext

    executed: list[tuple[str, tuple]] = []
    logger = SimpleNamespace(
        conn=SimpleNamespace(cursor=lambda **_kwargs: _TrashCursor(executed)),
        _table_exists=lambda _table: True,
        _column_exists=lambda _table, column: column in columns,
    )
    scope = ScopeContext(account_id=DEFAULT_ACCOUNT_ID, actor_id="auditor", principal_type="admin")
    app = FastAPI()
    app.include_router(
        create_api_trash_router(
            require_authenticated_user=lambda *_args, **_kwargs: {},
            auth_admin_roles={"admin"},
            get_logger_only=lambda: logger,
            real_dict_cursor_kwargs=dict,
            request_scope=lambda _request, **_kwargs: scope,
            trash_lifecycle_service=lambda: Mock(),
            service_error_response=lambda error: JSONResponse({"error": str(error)}, 400),
            api_error_response=lambda error: JSONResponse({"error": str(error)}, 500),
            err_admin_role_required="admin required",
            err_database_not_configured="database unavailable",
        )
    )
    response = TestClient(app).get("/api/trash", params={"entity_type": entity_type})
    assert response.status_code == 200
    return executed


def test_trash_list_leaves_out_a_hooks_storage_workflow() -> None:
    # No ``visible_in_catalog`` column: the marker alone must hide the row.
    executed = _list_trash("registry_workflow", {"deleted_at", "account_id"})

    assert len(executed) == 1
    sql = executed[0][0]
    assert "FROM registry_workflows" in sql
    assert "NOT (settings ? 'hook_storage')" in sql


def test_trash_list_of_other_entities_has_no_hook_storage_filter() -> None:
    executed = _list_trash("registry_agent", {"deleted_at"})

    assert executed
    assert "hook_storage" not in executed[0][0]
