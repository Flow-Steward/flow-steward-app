from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from core.application.management.errors import (
    ServiceError,
    ServiceForbiddenError,
)
from core.entrypoints.admin.project_history import create_project_history_router
from core.entrypoints.admin.project_schedules import create_project_schedules_router
from core.entrypoints.admin.settings_retention import create_settings_retention_router
from core.entrypoints.api.trash import create_api_trash_router


class _SeqCursor:
    def __init__(
        self,
        *,
        fetchone_seq: list[Any] | None = None,
        fetchall_seq: list[Any] | None = None,
        execute_side_effect: Exception | None = None,
        rowcount: int = 0,
    ) -> None:
        self._fetchone_seq = list(fetchone_seq or [])
        self._fetchall_seq = list(fetchall_seq or [])
        self._execute_side_effect = execute_side_effect
        self.rowcount = rowcount
        self.executions: list[tuple[str, Any]] = []

    def __enter__(self) -> _SeqCursor:
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        return False

    def execute(self, _query: str, _params: Any = None) -> None:
        self.executions.append((_query, _params))
        if self._execute_side_effect is not None:
            raise self._execute_side_effect

    def fetchone(self) -> Any:
        if self._fetchone_seq:
            return self._fetchone_seq.pop(0)
        return None

    def fetchall(self) -> Any:
        if self._fetchall_seq:
            return self._fetchall_seq.pop(0)
        return []


class _Conn:
    def __init__(self, cursor: _SeqCursor) -> None:
        self._cursor = cursor
        self.committed = False
        self.rolled_back = False

    def cursor(self, **_kwargs: Any) -> _SeqCursor:
        return self._cursor

    def commit(self) -> None:
        self.committed = True

    def rollback(self) -> None:
        self.rolled_back = True


def _client_with_state(
    router, *, auth_user: dict[str, Any] | None = None, auth_source: str = "", principal: Any = None
) -> TestClient:
    app = FastAPI()

    @app.middleware("http")
    async def _inject_state(request: Request, call_next):
        if auth_user is not None:
            request.state.auth_user = dict(auth_user)
        if auth_source:
            request.state.auth_source = auth_source
        if principal is not None:
            request.state.auth_principal = principal
        return await call_next(request)

    app.include_router(router)
    return TestClient(app, raise_server_exceptions=False)


def _service_error_response(exc: ServiceError, *_args: Any, **_kwargs: Any) -> JSONResponse:
    return JSONResponse(
        {"service_error": exc.message}, status_code=getattr(exc, "status_code", 500)
    )


def _api_error_response(exc: Exception, *_args: Any, **_kwargs: Any) -> JSONResponse:
    return JSONResponse({"api_error": str(exc)}, status_code=500)


def test_api_trash_list_and_actions_cover_error_branches() -> None:
    cursor = _SeqCursor(
        fetchall_seq=[
            [
                {
                    "entity_id": 1,
                    "label": "Demo",
                    "deleted_at": datetime(2026, 3, 29, 10, 0, tzinfo=UTC),
                    "deleted_by": "alice",
                }
            ]
        ]
    )
    bl = SimpleNamespace(conn=_Conn(cursor))

    class _TrashSvc:
        def __init__(self) -> None:
            self.mode = "ok"
            self.last_selection = None

        def trash(self, *_args: Any, **_kwargs: Any) -> Any:
            if self.mode == "service_error":
                raise ServiceError("trash-failed", status_code=409)
            if self.mode == "error":
                raise RuntimeError("trash-boom")
            return SimpleNamespace(dump=lambda: {"status": "trashed"})

        def restore(self, *_args: Any, **_kwargs: Any) -> Any:
            if self.mode == "service_error":
                raise ServiceError("restore-failed", status_code=404)
            if self.mode == "error":
                raise RuntimeError("restore-boom")
            return SimpleNamespace(dump=lambda: {"status": "restored"})

        def plan(self, *_args: Any, **_kwargs: Any) -> Any:
            if self.mode == "service_error":
                raise ServiceError("plan-failed", status_code=400)
            if self.mode == "error":
                raise RuntimeError("plan-boom")
            return SimpleNamespace(
                dump=lambda: {"status": "planned", "tree": {"node_id": "project:1"}}
            )

        def execute_plan(self, *_args: Any, **kwargs: Any) -> Any:
            if self.mode == "service_error":
                raise ServiceError("execute-failed", status_code=400)
            if self.mode == "error":
                raise RuntimeError("execute-boom")
            self.last_selection = kwargs.get("selection")
            return SimpleNamespace(dump=lambda: {"status": "hard_deleted"})

        def hard_delete(self, *_args: Any, **_kwargs: Any) -> Any:
            if self.mode == "service_error":
                raise ServiceError("hard-delete-failed", status_code=400)
            if self.mode == "error":
                raise RuntimeError("hard-delete-boom")
            return SimpleNamespace(dump=lambda: {"status": "deleted"})

    svc = _TrashSvc()

    def _table_exists(name: str) -> bool:
        if name == "jobs":
            raise RuntimeError("simulated-table-check-error")
        return name == "projects"

    def _column_exists(_table: str, _column: str) -> bool:
        return True

    router = create_api_trash_router(
        require_authenticated_user=lambda _request, **_kwargs: {"id": 1},
        auth_admin_roles={"admin"},
        get_logger_only=lambda: bl,
        real_dict_cursor_kwargs=dict,
        request_scope=lambda _request: SimpleNamespace(actor_name="alice"),
        trash_lifecycle_service=lambda: svc,
        service_error_response=_service_error_response,
        api_error_response=_api_error_response,
        err_admin_role_required="admin-required",
        err_database_not_configured="db-missing",
    )
    client = _client_with_state(router)

    listed = client.get("/api/trash")
    assert listed.status_code == 200
    assert listed.json()["total"] == 1
    assert listed.json()["items"][0]["category"] == "application"

    assert client.post("/api/trash/project/1/trash").json()["status"] == "trashed"
    assert client.post("/api/trash/project/1/restore").json()["status"] == "restored"
    assert (
        client.post("/api/trash/project/1/plan", json={"action_type": "restore"}).json()["status"]
        == "planned"
    )
    assert (
        client.post(
            "/api/trash/project/1/execute",
            json={
                "action_type": "hard_delete",
                "selected_node_ids": ["project:1"],
                "plan_version": "version-from-plan",
            },
        ).json()["status"]
        == "hard_deleted"
    )
    assert svc.last_selection == {
        "selected_node_ids": ["project:1"],
        "plan_version": "version-from-plan",
    }
    assert client.post("/api/trash/registry_guardrail/1/hard-delete").json()["status"] == "deleted"

    svc.mode = "service_error"
    assert client.post("/api/trash/project/1/trash").status_code == 409
    assert client.post("/api/trash/project/1/restore").status_code == 404
    assert (
        client.post("/api/trash/project/1/plan", json={"action_type": "restore"}).status_code == 400
    )
    assert (
        client.post(
            "/api/trash/project/1/execute",
            json={"action_type": "hard_delete", "plan_version": "version-from-plan"},
        ).status_code
        == 400
    )
    assert client.post("/api/trash/registry_guardrail/1/hard-delete").status_code == 400

    svc.mode = "error"
    assert client.post("/api/trash/project/1/trash").status_code == 500
    assert client.post("/api/trash/project/1/restore").status_code == 500
    assert (
        client.post("/api/trash/project/1/plan", json={"action_type": "restore"}).status_code == 500
    )
    assert (
        client.post(
            "/api/trash/project/1/execute",
            json={"action_type": "hard_delete", "plan_version": "version-from-plan"},
        ).status_code
        == 500
    )
    assert client.post("/api/trash/registry_guardrail/1/hard-delete").status_code == 500


def test_api_trash_list_supports_entity_type_filter_and_hides_non_visible_workflows() -> None:
    cursor = _SeqCursor(
        fetchall_seq=[
            [
                {
                    "entity_id": "wf-visible",
                    "label": "Visible workflow",
                    "deleted_at": datetime(2026, 3, 29, 10, 0, tzinfo=UTC),
                    "deleted_by": "alice",
                }
            ]
        ]
    )
    bl = SimpleNamespace(conn=_Conn(cursor))

    def _table_exists(name: str) -> bool:
        return name == "registry_workflows"

    def _column_exists(table: str, column: str) -> bool:
        return table == "registry_workflows" and column in {"deleted_by", "visible_in_catalog"}

    router = create_api_trash_router(
        require_authenticated_user=lambda _request, **_kwargs: {"id": 1},
        auth_admin_roles={"admin"},
        get_logger_only=lambda: bl,
        real_dict_cursor_kwargs=dict,
        request_scope=lambda _request: SimpleNamespace(actor_name="alice"),
        trash_lifecycle_service=lambda: SimpleNamespace(),
        service_error_response=_service_error_response,
        api_error_response=_api_error_response,
        err_admin_role_required="admin-required",
        err_database_not_configured="db-missing",
    )
    client = _client_with_state(router)
    bl._table_exists = _table_exists
    bl._column_exists = _column_exists

    listed = client.get("/api/trash?entity_type=registry_workflow&limit=5")
    assert listed.status_code == 200
    body = listed.json()
    assert body["total"] == 1
    assert body["items"][0]["entity_type"] == "registry_workflow"
    assert body["items"][0]["category"] == "registry"

    invalid = client.get("/api/trash?entity_type=not-real")
    assert invalid.status_code == 400
    assert invalid.json()["error"] == "Unsupported trash entity_type"


def test_mcp_trash_list_and_actions_keep_restricted_project_scope() -> None:
    own_project = "20000000-0000-0000-0000-000000000001"
    foreign_project = "20000000-0000-0000-0000-000000000002"
    tenant_id = "00000000-0000-0000-0000-000000000001"
    account_id = "00000000-0000-0000-0000-000000000002"
    cursor = _SeqCursor(
        fetchall_seq=[
            [
                {
                    "entity_id": "mcp-own",
                    "label": "Own MCP",
                    "deleted_at": datetime(2026, 3, 29, 10, 0, tzinfo=UTC),
                    "deleted_by": "alice",
                    "project_id": own_project,
                    "tenant_id": tenant_id,
                }
            ]
        ]
    )
    bl = SimpleNamespace(conn=_Conn(cursor))
    requested_projects: list[str] = []
    action_scopes: list[Any] = []

    def _request_scope(_request, *, project_id: str | None = None):
        requested_projects.append(str(project_id or ""))
        return SimpleNamespace(
            account_id=account_id,
            tenant_id=tenant_id,
            project_id=project_id,
            allowed_project_ids=frozenset({own_project}),
            actor_name="alice",
        )

    class _TrashSvc:
        @staticmethod
        def _result(status: str):
            return SimpleNamespace(dump=lambda: {"status": status})

        def plan(self, *_args, **kwargs):
            action_scopes.append(kwargs["scope"])
            return self._result("planned")

        def execute_plan(self, *_args, **kwargs):
            action_scopes.append(kwargs["scope"])
            return self._result("executed")

        def restore(self, *_args, **kwargs):
            action_scopes.append(kwargs["scope"])
            return self._result("restored")

        def hard_delete(self, *_args, **kwargs):
            action_scopes.append(kwargs["scope"])
            return self._result("deleted")

    svc = _TrashSvc()
    get_logger_only = MagicMock(return_value=bl)
    router = create_api_trash_router(
        require_authenticated_user=lambda _request, **_kwargs: {"id": 1},
        auth_admin_roles={"admin"},
        get_logger_only=get_logger_only,
        real_dict_cursor_kwargs=dict,
        request_scope=_request_scope,
        trash_lifecycle_service=lambda: svc,
        service_error_response=_service_error_response,
        api_error_response=_api_error_response,
        err_admin_role_required="admin-required",
        err_database_not_configured="db-missing",
    )
    bl._table_exists = lambda table: table == "mcp_connectors"
    bl._column_exists = lambda table, column: (
        table == "mcp_connectors"
        and column
        in {
            "account_id",
            "project_id",
            "tenant_id",
            "deleted_by",
        }
    )
    client = _client_with_state(router)

    listed = client.get("/api/trash?entity_type=mcp_connector")
    assert listed.status_code == 200
    assert [item["entity_id"] for item in listed.json()["items"]] == ["mcp-own"]
    query, params = cursor.executions[-1]
    assert "tenant_id = %s" in query
    assert "project_id = ANY(%s)" in query
    assert own_project in params[-1]
    assert foreign_project not in params[-1]

    suffix = f"?project_id={own_project}"
    assert client.post(f"/api/trash/mcp_connector/mcp-own/restore{suffix}").status_code == 200
    assert (
        client.post(
            f"/api/trash/mcp_connector/mcp-own/plan{suffix}",
            json={"action_type": "restore"},
        ).status_code
        == 200
    )
    assert (
        client.post(
            f"/api/trash/mcp_connector/mcp-own/execute{suffix}",
            json={"action_type": "hard_delete", "plan_version": "revision"},
        ).status_code
        == 200
    )
    direct_mcp_purge = client.post(f"/api/trash/mcp_connector/mcp-own/hard-delete{suffix}")
    assert direct_mcp_purge.status_code == 409
    assert "lifecycle plan" in direct_mcp_purge.text.lower()
    assert requested_projects == ["", own_project, own_project, own_project, own_project]
    assert len(action_scopes) == 3
    assert all(scope.project_id == own_project for scope in action_scopes)

    logger_calls_before_foreign_request = get_logger_only.call_count
    foreign_suffix = f"?project_id={foreign_project}"
    assert (
        client.get(f"/api/trash?entity_type=mcp_connector&project_id={foreign_project}").status_code
        == 403
    )
    assert get_logger_only.call_count == logger_calls_before_foreign_request
    assert (
        client.post(f"/api/trash/mcp_connector/mcp-own/restore{foreign_suffix}").status_code == 403
    )
    assert (
        client.post(
            f"/api/trash/mcp_connector/mcp-own/plan{foreign_suffix}",
            json={"action_type": "restore"},
        ).status_code
        == 403
    )
    assert (
        client.post(
            f"/api/trash/mcp_connector/mcp-own/execute{foreign_suffix}",
            json={"action_type": "hard_delete", "plan_version": "revision"},
        ).status_code
        == 403
    )
    assert (
        client.post(f"/api/trash/mcp_connector/mcp-own/hard-delete{foreign_suffix}").status_code
        == 403
    )
    assert len(action_scopes) == 3


def test_api_trash_list_rejects_foreign_account_before_database_access() -> None:
    get_logger_only = MagicMock()
    router = create_api_trash_router(
        require_authenticated_user=lambda _request, **_kwargs: {"id": 1},
        auth_admin_roles={"admin"},
        get_logger_only=get_logger_only,
        real_dict_cursor_kwargs=dict,
        request_scope=lambda _request: SimpleNamespace(
            account_id="11111111-1111-1111-1111-111111111111",
            actor_name="foreign-admin",
        ),
        trash_lifecycle_service=lambda: SimpleNamespace(),
        service_error_response=_service_error_response,
        api_error_response=_api_error_response,
        err_admin_role_required="admin-required",
        err_database_not_configured="db-missing",
    )
    client = _client_with_state(router)

    response = client.get("/api/trash")

    assert response.status_code == 403
    assert "default account" in response.text
    get_logger_only.assert_not_called()


def test_project_history_api_routes_success_and_error_paths() -> None:
    class _Q:
        def __init__(self) -> None:
            self.mode = "ok"

        def list_history(self, *_args: Any, **_kwargs: Any) -> Any:
            if self.mode == "service_error":
                raise ServiceError("history-failed", status_code=400)
            if self.mode == "error":
                raise RuntimeError("history-boom")
            return SimpleNamespace(dump=lambda: {"history": [1, 2, 3]})

        def get_history_diff(self, *_args: Any, **_kwargs: Any) -> Any:
            if self.mode == "service_error":
                raise ServiceError("diff-failed", status_code=404)
            if self.mode == "error":
                raise RuntimeError("diff-boom")
            return SimpleNamespace(dump=lambda: {"diff": {"x": 1}})

        def get_history_page(self, *_args: Any, **_kwargs: Any) -> Any:
            if self.mode == "service_error_404":
                raise ServiceError("missing-project", status_code=404)
            if self.mode == "service_error_500":
                raise ServiceError("broken-project", status_code=500)
            if self.mode == "error":
                raise RuntimeError("page-boom")
            return SimpleNamespace(
                project={"id": 1},
                revisions=[{"id": 7}],
                selected_revision={"id": 7},
                compare_entry=None,
                diff_payload={"delta": True},
            )

    class _C:
        def __init__(self) -> None:
            self.mode = "ok"

        def restore_project_settings_revision(self, *_args: Any, **_kwargs: Any) -> Any:
            if self.mode == "service_error_404":
                raise ServiceError("missing", status_code=404)
            if self.mode == "service_error":
                raise ServiceError("restore-failed", status_code=400)
            if self.mode == "error":
                raise RuntimeError("restore-boom")
            return SimpleNamespace(
                dump=lambda: {"new_revision_id": "22222222-2222-2222-2222-222222222222"}
            )

    q = _Q()
    c = _C()

    router = create_project_history_router(
        project_query_service=lambda: q,
        project_command_service=lambda: c,
        request_scope=lambda _request, **_kwargs: {"scope": "ok"},
        resolve_actor_username=lambda _request, _requested, default="system": default,
        service_error_response=_service_error_response,
        api_error_response=_api_error_response,
    )
    client = _client_with_state(router)

    assert client.get("/api/projects/1/history").json()["history"] == [1, 2, 3]
    assert client.get("/api/projects/1/history/7/diff").json()["diff"] == {"x": 1}
    revision_id = "11111111-1111-1111-1111-111111111111"
    assert (
        client.post(f"/api/projects/1/history/{revision_id}/restore").json()["new_revision_id"]
        == "22222222-2222-2222-2222-222222222222"
    )

    q.mode = "service_error"
    assert client.get("/api/projects/1/history").status_code == 400
    q.mode = "error"
    assert client.get("/api/projects/1/history/7/diff").status_code == 500

    c.mode = "service_error_404"
    assert client.post(f"/api/projects/1/history/{revision_id}/restore").status_code == 404
    c.mode = "service_error"
    assert client.post(f"/api/projects/1/history/{revision_id}/restore").status_code == 400
    c.mode = "error"
    assert client.post(f"/api/projects/1/history/{revision_id}/restore").status_code == 500


def test_settings_retention_routes_cover_success_and_error_paths() -> None:
    state = {"mode": "ok", "deny_auth": False, "actor": "alice"}
    calls: list[tuple[str, str]] = []
    updated_at = datetime(2026, 4, 5, 12, 30, tzinfo=UTC)

    class _Logger:
        def get_retention_settings(self, *, defaults=None, account_id=None):
            calls.append(("get", str(account_id or "")))
            if state["mode"] == "get_error":
                raise RuntimeError("retention-get-failed")
            payload = dict(defaults or {})
            payload.update(
                {
                    "account_id": account_id,
                    "action_logs_days": 45,
                    "job_logs_days": 60,
                    "trash_days": 14,
                    "terminal_job_checkpoints_days": 75,
                    "updated_at": updated_at,
                    "updated_by": "alice",
                }
            )
            return payload

        def save_retention_settings(self, payload, *, actor="system", account_id=None):
            calls.append(("save", str(account_id or "")))
            if state["mode"] == "save_value":
                raise ValueError("invalid-days")
            if state["mode"] == "save_runtime":
                raise RuntimeError("settings unavailable")
            if state["mode"] == "save_error":
                raise Exception("save-boom")
            saved = dict(payload)
            saved["account_id"] = account_id
            saved["updated_by"] = actor
            saved["updated_at"] = updated_at
            return saved

    def _require_authenticated_user(_request: Request, **_kwargs: Any) -> dict[str, Any]:
        if state["deny_auth"]:
            raise PermissionError("denied")
        return {"id": 1}

    router = create_settings_retention_router(
        require_authenticated_user=_require_authenticated_user,
        auth_admin_roles={"admin"},
        get_logger_only=lambda: _Logger(),
        request_scope=lambda _request: SimpleNamespace(account_id="acc_enterprise"),
        resolve_actor_username=lambda _request, _fallback, default="system": (
            state["actor"] or default
        ),
    )
    client = _client_with_state(router)

    api_get = client.get("/api/settings/retention")
    assert api_get.status_code == 200
    assert calls == [("get", "acc_enterprise")]
    assert api_get.json()["settings"]["action_logs_days"] == 45
    assert api_get.json()["defaults"]["trash_days"] == 30
    assert api_get.json()["defaults"]["terminal_job_checkpoints_days"] == 30
    assert api_get.json()["defaults"]["revisions_keep"] == 5
    assert api_get.json()["settings"]["terminal_job_checkpoints_days"] == 75
    assert api_get.json()["settings"]["updated_at"] == "2026-04-05T12:30:00+00:00"

    api_save = client.post(
        "/api/settings/retention",
        json={
            "action_logs_days": 20,
            "job_logs_days": 25,
            "trash_days": 10,
            "terminal_job_checkpoints_days": 12,
        },
    )
    assert api_save.status_code == 200
    assert api_save.json()["status"] == "saved"
    assert api_save.json()["settings"]["account_id"] == "acc_enterprise"
    assert api_save.json()["settings"]["updated_by"] == "alice"
    assert api_save.json()["settings"]["updated_at"] == "2026-04-05T12:30:00+00:00"

    invalid_json = client.post(
        "/api/settings/retention",
        content="not-json",
        headers={"Content-Type": "application/json"},
    )
    assert invalid_json.status_code == 400

    state["mode"] = "get_error"
    api_unavailable = client.get("/api/settings/retention")
    assert api_unavailable.status_code == 503
    assert api_unavailable.json()["available"] is False

    state["mode"] = "save_value"
    save_value = client.post(
        "/api/settings/retention",
        json={"action_logs_days": 5, "job_logs_days": 6, "trash_days": 7},
    )
    assert save_value.status_code == 400

    state["mode"] = "save_runtime"
    save_runtime = client.post(
        "/api/settings/retention",
        json={"action_logs_days": 5, "job_logs_days": 6, "trash_days": 7},
    )
    assert save_runtime.status_code == 503

    state["mode"] = "save_error"
    save_error = client.post(
        "/api/settings/retention",
        json={"action_logs_days": 5, "job_logs_days": 6, "trash_days": 7},
    )
    assert save_error.status_code == 500

    state["deny_auth"] = True
    denied_api = client.get("/api/settings/retention")
    assert denied_api.status_code == 403
    assert client.get("/settings/retention", follow_redirects=False).status_code == 404


def test_project_schedules_routes_cover_success_and_error_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _CommandSvc:
        def __init__(self) -> None:
            self.mode = "ok"

        def create_scheduled_job(self, *_args: Any, **_kwargs: Any) -> Any:
            if self.mode == "create_service_404":
                raise ServiceError("missing", status_code=404)
            if self.mode == "create_error":
                raise RuntimeError("create-boom")
            return SimpleNamespace(
                dump=lambda: {
                    "scheduled_job_id": 21,
                    "enabled": self.mode != "create_paused",
                    "next_run_at": None if self.mode == "create_paused" else "2026-03-30T12:00:00Z",
                }
            )

        def toggle_scheduled_job(self, *_args: Any, **_kwargs: Any) -> Any:
            if self.mode == "toggle_service":
                raise ServiceError("toggle-failed", status_code=400)
            if self.mode == "toggle_error":
                raise RuntimeError("toggle-boom")
            return SimpleNamespace(enabled=True)

        def delete_scheduled_job(self, *_args: Any, **_kwargs: Any) -> None:
            if self.mode == "delete_service":
                raise ServiceError("delete-failed", status_code=400)
            if self.mode == "delete_error":
                raise RuntimeError("delete-boom")

        def run_scheduled_job_now(self, *_args: Any, **_kwargs: Any) -> Any:
            if self.mode == "run_service":
                raise ServiceError("run-failed", status_code=400)
            if self.mode == "run_error":
                raise RuntimeError("run-boom")
            if self.mode == "run_skipped":
                return SimpleNamespace(status="skipped", job_id=None, reason="already-running")
            if self.mode == "run_failed":
                return SimpleNamespace(status="failed", job_id=None, reason="bad-state")
            return SimpleNamespace(status="enqueued", job_id=77, reason="")

    class _QuerySvc:
        def __init__(self) -> None:
            self.mode = "ok"

        def list_scheduled_jobs(self, *_args: Any, **_kwargs: Any) -> Any:
            if self.mode == "list_service":
                raise ServiceError("list-failed", status_code=404)
            if self.mode == "list_error":
                raise RuntimeError("list-boom")
            return SimpleNamespace(dump=lambda: {"scheduled_jobs": [{"id": 1}]})

    cursor = _SeqCursor()
    conn = _Conn(cursor)
    bl = SimpleNamespace(
        conn=conn,
        get_project=lambda project_id: {"id": int(project_id), "schedule_timezone": "UTC"},
        list_scheduled_jobs=lambda _project_id, **_kwargs: [
            {"id": 1, "enabled": True, "status": "active", "cron_expr": "0 * * * *"}
        ],
    )

    auth = {"enabled": True, "deny": False}
    command = _CommandSvc()
    query = _QuerySvc()

    def _require_auth(_request: Request, **_kwargs: Any) -> dict[str, Any]:
        if auth["deny"]:
            raise PermissionError("denied")
        return {"id": 1, "username": "admin"}

    def _cron_next_run(_expr: str, from_dt: datetime, *, schedule_tz: str = "UTC") -> datetime:
        _ = schedule_tz
        return from_dt + timedelta(hours=1)

    router = create_project_schedules_router(
        local_auth_enabled=lambda: auth["enabled"],
        external_auth_enabled=lambda: False,
        require_authenticated_user=_require_auth,
        get_logger_only=lambda: bl,
        request_scope=lambda _request, **_kwargs: {"scope": "ok"},
        project_command_service=lambda: command,
        project_query_service=lambda: query,
        resolve_actor_username=lambda _request, _requested, default="operator": default,
        service_error_response=_service_error_response,
        api_error_response=_api_error_response,
        cron_next_run_at=_cron_next_run,
        normalize_cron_expr=lambda value: str(value or "").strip(),
        parse_cron_expression=lambda _expr: {"ok": True},
        cron_human_summary=lambda expr, _tz: f"summary:{expr}",
        err_authentication_required="auth-required",
        err_invalid_json_body="invalid-json",
        err_database_not_configured="db-missing",
        err_project_not_found="project-missing",
    )

    client = _client_with_state(router)

    # schedule-timezone: auth denied, invalid json, invalid tz, project missing, forbidden, failed recalc, DB error, success
    auth["deny"] = True
    assert (
        client.patch(
            "/api/projects/1/schedule-timezone", json={"schedule_timezone": "UTC"}
        ).status_code
        == 401
    )
    auth["deny"] = False

    bad_json = client.patch(
        "/api/projects/1/schedule-timezone",
        content="not-json",
        headers={"content-type": "application/json"},
    )
    assert bad_json.status_code == 400

    invalid_tz = client.patch(
        "/api/projects/1/schedule-timezone", json={"schedule_timezone": "Bad/Timezone"}
    )
    assert invalid_tz.status_code == 422

    bl_missing = SimpleNamespace(
        conn=conn, get_project=lambda _pid: None, list_scheduled_jobs=lambda _pid, **_kwargs: []
    )
    router_missing = create_project_schedules_router(
        local_auth_enabled=lambda: auth["enabled"],
        external_auth_enabled=lambda: False,
        require_authenticated_user=_require_auth,
        get_logger_only=lambda: bl_missing,
        request_scope=lambda _request, **_kwargs: {"scope": "ok"},
        project_command_service=lambda: command,
        project_query_service=lambda: query,
        resolve_actor_username=lambda _request, _requested, default="operator": default,
        service_error_response=_service_error_response,
        api_error_response=_api_error_response,
        cron_next_run_at=_cron_next_run,
        normalize_cron_expr=lambda value: str(value or "").strip(),
        parse_cron_expression=lambda _expr: {"ok": True},
        cron_human_summary=lambda expr, _tz: f"summary:{expr}",
        err_authentication_required="auth-required",
        err_invalid_json_body="invalid-json",
        err_database_not_configured="db-missing",
        err_project_not_found="project-missing",
    )
    client_missing = _client_with_state(router_missing)
    assert (
        client_missing.patch(
            "/api/projects/1/schedule-timezone", json={"schedule_timezone": "UTC"}
        ).status_code
        == 404
    )

    monkeypatch.setattr(
        "core.entrypoints.admin.project_schedules.require_project_access",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(ServiceForbiddenError("forbidden")),
    )
    forbidden = client.patch("/api/projects/1/schedule-timezone", json={"schedule_timezone": "UTC"})
    assert forbidden.status_code == 403
    monkeypatch.setattr(
        "core.entrypoints.admin.project_schedules.require_project_access",
        lambda *_args, **_kwargs: None,
    )

    # failed recalc
    router_failed_recalc = create_project_schedules_router(
        local_auth_enabled=lambda: auth["enabled"],
        external_auth_enabled=lambda: False,
        require_authenticated_user=_require_auth,
        get_logger_only=lambda: bl,
        request_scope=lambda _request, **_kwargs: {"scope": "ok"},
        project_command_service=lambda: command,
        project_query_service=lambda: query,
        resolve_actor_username=lambda _request, _requested, default="operator": default,
        service_error_response=_service_error_response,
        api_error_response=_api_error_response,
        cron_next_run_at=lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("recalc-boom")
        ),
        normalize_cron_expr=lambda value: str(value or "").strip(),
        parse_cron_expression=lambda _expr: {"ok": True},
        cron_human_summary=lambda expr, _tz: f"summary:{expr}",
        err_authentication_required="auth-required",
        err_invalid_json_body="invalid-json",
        err_database_not_configured="db-missing",
        err_project_not_found="project-missing",
    )
    client_failed_recalc = _client_with_state(router_failed_recalc)
    assert (
        client_failed_recalc.patch(
            "/api/projects/1/schedule-timezone", json={"schedule_timezone": "Europe/Vilnius"}
        ).status_code
        == 409
    )

    # DB error branch
    bl_fail_commit = SimpleNamespace(
        conn=_Conn(_SeqCursor(execute_side_effect=RuntimeError("db-boom"))),
        get_project=lambda project_id: {"id": int(project_id), "schedule_timezone": "UTC"},
        list_scheduled_jobs=lambda _project_id, **_kwargs: [
            {"id": 1, "enabled": True, "status": "active", "cron_expr": "0 * * * *"}
        ],
    )
    router_db_error = create_project_schedules_router(
        local_auth_enabled=lambda: auth["enabled"],
        external_auth_enabled=lambda: False,
        require_authenticated_user=_require_auth,
        get_logger_only=lambda: bl_fail_commit,
        request_scope=lambda _request, **_kwargs: {"scope": "ok"},
        project_command_service=lambda: command,
        project_query_service=lambda: query,
        resolve_actor_username=lambda _request, _requested, default="operator": default,
        service_error_response=_service_error_response,
        api_error_response=_api_error_response,
        cron_next_run_at=_cron_next_run,
        normalize_cron_expr=lambda value: str(value or "").strip(),
        parse_cron_expression=lambda _expr: {"ok": True},
        cron_human_summary=lambda expr, _tz: f"summary:{expr}",
        err_authentication_required="auth-required",
        err_invalid_json_body="invalid-json",
        err_database_not_configured="db-missing",
        err_project_not_found="project-missing",
    )
    client_db_error = _client_with_state(router_db_error)
    assert (
        client_db_error.patch(
            "/api/projects/1/schedule-timezone", json={"schedule_timezone": "Europe/Vilnius"}
        ).status_code
        == 500
    )

    ok_tz = client.patch(
        "/api/projects/1/schedule-timezone", json={"schedule_timezone": "Europe/Vilnius"}
    )
    assert ok_tz.status_code == 200
    assert ok_tz.json()["recalculated_jobs"] == 1

    # JSON scheduled-jobs mutation endpoints are the canonical React contract.
    command.mode = "ok"
    api_create = client.post(
        "/api/projects/1/scheduled-jobs",
        json={
            "workflow_id": "wf",
            "cron_expr": "0 * * * *",
            "enabled": True,
        },
    )
    assert api_create.status_code == 201
    assert api_create.json()["scheduled_job_id"] == 21

    command.mode = "create_service_404"
    assert (
        client.post(
            "/api/projects/1/scheduled-jobs",
            json={
                "workflow_id": "wf",
                "cron_expr": "0 * * * *",
                "enabled": True,
            },
        ).status_code
        == 404
    )
    command.mode = "create_error"
    assert (
        client.post(
            "/api/projects/1/scheduled-jobs",
            json={
                "workflow_id": "wf",
                "cron_expr": "0 * * * *",
                "enabled": True,
            },
        ).status_code
        == 500
    )

    command.mode = "ok"
    assert (
        client.patch("/api/projects/1/scheduled-jobs/10/toggle", json={"enabled": True}).status_code
        == 200
    )
    assert (
        client.patch(
            "/api/projects/1/scheduled-jobs/10/concurrency",
            json={
                "concurrency_scope": "workflow",
                "concurrency_key": "wf",
                "catchup_policy": "latest_only",
            },
        ).status_code
        == 404
    )
    assert client.delete("/api/projects/1/scheduled-jobs/10").status_code == 200
    assert client.post("/api/projects/1/scheduled-jobs/10/run-now").status_code == 200

    command.mode = "toggle_service"
    assert (
        client.patch("/api/projects/1/scheduled-jobs/10/toggle", json={"enabled": True}).status_code
        == 400
    )
    command.mode = "toggle_error"
    assert (
        client.patch("/api/projects/1/scheduled-jobs/10/toggle", json={"enabled": True}).status_code
        == 500
    )

    command.mode = "delete_service"
    assert client.delete("/api/projects/1/scheduled-jobs/10").status_code == 400
    command.mode = "delete_error"
    assert client.delete("/api/projects/1/scheduled-jobs/10").status_code == 500

    command.mode = "run_service"
    assert client.post("/api/projects/1/scheduled-jobs/10/run-now").status_code == 400
    command.mode = "run_error"
    assert client.post("/api/projects/1/scheduled-jobs/10/run-now").status_code == 500

    # api scheduled jobs branches
    query.mode = "ok"
    assert client.get("/api/projects/1/scheduled-jobs").json()["scheduled_jobs"][0]["id"] == 1
    query.mode = "list_service"
    assert client.get("/api/projects/1/scheduled-jobs").status_code == 404
    query.mode = "list_error"
    assert client.get("/api/projects/1/scheduled-jobs").status_code == 500

    # cron-preview branches
    auth["deny"] = True
    assert client.get("/api/projects/1/cron-preview?cron_expr=0+9+*+*+*").status_code == 401
    auth["deny"] = False

    router_no_bl = create_project_schedules_router(
        local_auth_enabled=lambda: auth["enabled"],
        external_auth_enabled=lambda: False,
        require_authenticated_user=_require_auth,
        get_logger_only=lambda: None,
        request_scope=lambda _request, **_kwargs: {"scope": "ok"},
        project_command_service=lambda: command,
        project_query_service=lambda: query,
        resolve_actor_username=lambda _request, _requested, default="operator": default,
        service_error_response=_service_error_response,
        api_error_response=_api_error_response,
        cron_next_run_at=_cron_next_run,
        normalize_cron_expr=lambda value: str(value or "").strip(),
        parse_cron_expression=lambda _expr: {"ok": True},
        cron_human_summary=lambda expr, _tz: f"summary:{expr}",
        err_authentication_required="auth-required",
        err_invalid_json_body="invalid-json",
        err_database_not_configured="db-missing",
        err_project_not_found="project-missing",
    )
    client_no_bl = _client_with_state(router_no_bl)
    assert client_no_bl.get("/api/projects/1/cron-preview?cron_expr=0+9+*+*+*").status_code == 503

    no_project = SimpleNamespace(get_project=lambda _pid: None)
    router_no_project = create_project_schedules_router(
        local_auth_enabled=lambda: auth["enabled"],
        external_auth_enabled=lambda: False,
        require_authenticated_user=_require_auth,
        get_logger_only=lambda: no_project,
        request_scope=lambda _request, **_kwargs: {"scope": "ok"},
        project_command_service=lambda: command,
        project_query_service=lambda: query,
        resolve_actor_username=lambda _request, _requested, default="operator": default,
        service_error_response=_service_error_response,
        api_error_response=_api_error_response,
        cron_next_run_at=_cron_next_run,
        normalize_cron_expr=lambda value: str(value or "").strip(),
        parse_cron_expression=lambda _expr: {"ok": True},
        cron_human_summary=lambda expr, _tz: f"summary:{expr}",
        err_authentication_required="auth-required",
        err_invalid_json_body="invalid-json",
        err_database_not_configured="db-missing",
        err_project_not_found="project-missing",
    )
    client_no_project = _client_with_state(router_no_project)
    assert (
        client_no_project.get("/api/projects/1/cron-preview?cron_expr=0+9+*+*+*").status_code == 404
    )

    assert client.get("/api/projects/1/cron-preview?cron_expr=").status_code == 400

    router_invalid_cron = create_project_schedules_router(
        local_auth_enabled=lambda: auth["enabled"],
        external_auth_enabled=lambda: False,
        require_authenticated_user=_require_auth,
        get_logger_only=lambda: bl,
        request_scope=lambda _request, **_kwargs: {"scope": "ok"},
        project_command_service=lambda: command,
        project_query_service=lambda: query,
        resolve_actor_username=lambda _request, _requested, default="operator": default,
        service_error_response=_service_error_response,
        api_error_response=_api_error_response,
        cron_next_run_at=_cron_next_run,
        normalize_cron_expr=lambda value: str(value or "").strip(),
        parse_cron_expression=lambda _expr: (_ for _ in ()).throw(ValueError("bad-cron")),
        cron_human_summary=lambda expr, _tz: f"summary:{expr}",
        err_authentication_required="auth-required",
        err_invalid_json_body="invalid-json",
        err_database_not_configured="db-missing",
        err_project_not_found="project-missing",
    )
    client_invalid_cron = _client_with_state(router_invalid_cron)
    assert (
        client_invalid_cron.get("/api/projects/1/cron-preview?cron_expr=bad+cron").status_code
        == 400
    )

    router_value_error = create_project_schedules_router(
        local_auth_enabled=lambda: auth["enabled"],
        external_auth_enabled=lambda: False,
        require_authenticated_user=_require_auth,
        get_logger_only=lambda: bl,
        request_scope=lambda _request, **_kwargs: {"scope": "ok"},
        project_command_service=lambda: command,
        project_query_service=lambda: query,
        resolve_actor_username=lambda _request, _requested, default="operator": default,
        service_error_response=_service_error_response,
        api_error_response=_api_error_response,
        cron_next_run_at=lambda *_args, **_kwargs: (_ for _ in ()).throw(ValueError("tz-error")),
        normalize_cron_expr=lambda value: str(value or "").strip(),
        parse_cron_expression=lambda _expr: {"ok": True},
        cron_human_summary=lambda expr, _tz: f"summary:{expr}",
        err_authentication_required="auth-required",
        err_invalid_json_body="invalid-json",
        err_database_not_configured="db-missing",
        err_project_not_found="project-missing",
    )
    client_value_error = _client_with_state(router_value_error)
    assert (
        client_value_error.get("/api/projects/1/cron-preview?cron_expr=0+9+*+*+*").status_code
        == 400
    )

    router_runtime_error = create_project_schedules_router(
        local_auth_enabled=lambda: auth["enabled"],
        external_auth_enabled=lambda: False,
        require_authenticated_user=_require_auth,
        get_logger_only=lambda: bl,
        request_scope=lambda _request, **_kwargs: {"scope": "ok"},
        project_command_service=lambda: command,
        project_query_service=lambda: query,
        resolve_actor_username=lambda _request, _requested, default="operator": default,
        service_error_response=_service_error_response,
        api_error_response=_api_error_response,
        cron_next_run_at=lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("preview-boom")
        ),
        normalize_cron_expr=lambda value: str(value or "").strip(),
        parse_cron_expression=lambda _expr: {"ok": True},
        cron_human_summary=lambda expr, _tz: f"summary:{expr}",
        err_authentication_required="auth-required",
        err_invalid_json_body="invalid-json",
        err_database_not_configured="db-missing",
        err_project_not_found="project-missing",
    )
    client_runtime_error = _client_with_state(router_runtime_error)
    assert (
        client_runtime_error.get("/api/projects/1/cron-preview?cron_expr=0+9+*+*+*").status_code
        == 500
    )

    ok_preview = client.get("/api/projects/1/cron-preview?cron_expr=0+9+*+*+*&count=3")
    assert ok_preview.status_code == 200
    assert ok_preview.json()["summary"].startswith("summary:")
    assert len(ok_preview.json()["next_runs"]) == 3
