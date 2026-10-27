"""A soft-deleted account-global Tool remains discoverable and recoverable."""

from __future__ import annotations

import json
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from core.application.auth.config import DEFAULT_TENANT_ID
from core.application.management.command_services_tool import ToolCommandService
from core.application.management.command_services_trash import TrashLifecycleService
from core.application.management.scope import ScopeContext
from core.application.registry.db_store import RegistryDBStore
from core.entrypoints.admin import tools
from core.entrypoints.api.trash import create_api_trash_router
from core.infrastructure.db import psycopg_runtime
from tests._db_env import resolve_test_db_dsn


@pytest.fixture(autouse=True)
def require_audit_database(pg_conn):
    assert pg_conn.info.dbname == "flow_steward_audit"
    assert pg_conn.info.host == "pg-audit"


@pytest.fixture
def tool_lifecycle(pg_conn, default_account, monkeypatch):
    tool_id = f"trash.discovery.{uuid4().hex}"
    project_id = str(uuid4())
    with pg_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO projects (id, name, account_id, tenant_id) VALUES (%s, %s, %s, %s)",
            (project_id, "Tool discovery project", default_account, DEFAULT_TENANT_ID),
        )
        cur.execute(
            "INSERT INTO registry_tools (tool_id, display_name, account_id, enabled) "
            "VALUES (%s, %s, %s, false)",
            (tool_id, "Recoverable Tool", default_account),
        )
    pg_conn.commit()
    store = RegistryDBStore(resolve_test_db_dsn())
    monkeypatch.setattr(store, "_trigger_public_contract_projection_sync", lambda: None)
    state = SimpleNamespace(
        scope=ScopeContext(
            account_id=str(default_account),
            tenant_id=DEFAULT_TENANT_ID,
            allowed_project_ids=frozenset({project_id}),
            actor_id="tool-trash-regression",
            principal_type="admin",
        )
    )
    command = ToolCommandService(
        get_db=lambda: store,
        reload_registry=lambda: None,
        get_tool_validation=lambda *_args: {},
        format_validation_error_detail=lambda **_kwargs: {},
    )
    lifecycle = TrashLifecycleService(get_logger=lambda: store)
    monkeypatch.setattr(tools, "_tool_command_service", lambda: command)
    monkeypatch.setattr(tools, "_request_scope", lambda _request: state.scope)

    def request_scope(_request, *, project_id=None):
        return state.scope.model_copy(update={"project_id": project_id})

    app = FastAPI()
    app.include_router(tools.router)
    app.include_router(
        create_api_trash_router(
            require_authenticated_user=lambda *_args, **_kwargs: {"roles": ["admin"]},
            auth_admin_roles={"admin"},
            get_logger_only=lambda: store,
            real_dict_cursor_kwargs=lambda: {
                "cursor_factory": psycopg_runtime.extras.RealDictCursor
            },
            request_scope=request_scope,
            trash_lifecycle_service=lambda: lifecycle,
            service_error_response=lambda error: JSONResponse(
                {"error": str(error)}, status_code=error.status_code
            ),
            api_error_response=lambda error: JSONResponse({"error": str(error)}, status_code=500),
            err_admin_role_required="Admin required",
            err_database_not_configured="Database unavailable",
        )
    )
    try:
        with TestClient(app) as client:
            yield SimpleNamespace(
                client=client, store=store, tool_id=tool_id, project_id=project_id, state=state
            )
    finally:
        if store._conn is not None:
            store._conn.close()
        pg_conn.rollback()
        with pg_conn.cursor() as cur:
            cur.execute("DELETE FROM registry_changelog WHERE entity_id = %s", (tool_id,))
            cur.execute("DELETE FROM registry_tools WHERE tool_id = %s", (tool_id,))
            cur.execute("DELETE FROM projects WHERE id = %s", (project_id,))
        pg_conn.commit()


@pytest.mark.parametrize("restricted_projects", [False, True])
def test_account_global_tool_delete_list_restore_round_trip(tool_lifecycle, restricted_projects):
    fixture = tool_lifecycle
    if not restricted_projects:
        fixture.state.scope = fixture.state.scope.model_copy(
            update={"allowed_project_ids": frozenset()}
        )
    response = fixture.client.delete(f"/api/tools/{fixture.tool_id}")
    assert response.status_code == 200, response.text
    assert fixture.store.get_tool(fixture.tool_id) is None
    tombstone = fixture.store.get_tool_including_trashed(fixture.tool_id)
    assert tombstone["deleted_at"] is not None
    assert tombstone["project_id"] is None

    response = fixture.client.get("/api/trash?entity_type=registry_tool")
    assert response.status_code == 200, response.text
    assert fixture.tool_id in {item["entity_id"] for item in response.json()["items"]}
    response = fixture.client.get(
        f"/api/trash?entity_type=registry_tool&project_id={fixture.project_id}"
    )
    assert response.status_code == 200, response.text
    assert fixture.tool_id not in {item["entity_id"] for item in response.json()["items"]}

    response = fixture.client.post(f"/api/trash/registry_tool/{fixture.tool_id}/restore")
    assert response.status_code == 200, response.text
    restored = fixture.store.get_tool(fixture.tool_id)
    assert restored["tool_id"] == fixture.tool_id
    assert restored["display_name"] == "Recoverable Tool"
    assert restored["enabled"] is False
    assert restored["deleted_at"] is None
    response = fixture.client.get("/api/trash?entity_type=registry_tool")
    assert response.status_code == 200, response.text
    assert fixture.tool_id not in {item["entity_id"] for item in response.json()["items"]}


@pytest.mark.parametrize(
    ("project_case", "implementation", "foreign_account", "foreign_tenant", "visible"),
    [
        ("global", {}, False, False, True),
        ("global", {}, True, False, False),
        ("own", {}, False, False, True),
        ("own", {}, True, False, False),
        ("own", {}, False, True, False),
        ("disallowed", {}, False, False, False),
        ("global", {"type": "mcp"}, False, False, False),
        ("global", {"kind": "mcp", "owner_tenant_id": DEFAULT_TENANT_ID}, False, False, False),
        ("own", {"type": "mcp", "owner_tenant_id": DEFAULT_TENANT_ID}, False, False, True),
        ("own", {"type": "mcp"}, False, False, False),
        ("own", {"type": "mcp", "owner_tenant_id": "foreign"}, False, False, False),
    ],
)
def test_tool_trash_query_keeps_account_project_tenant_and_mcp_boundaries(
    tool_lifecycle, project_case, implementation, foreign_account, foreign_tenant, visible
):
    fixture = tool_lifecycle
    project_id = None if project_case == "global" else fixture.project_id
    account_id = str(uuid4()) if foreign_account else fixture.state.scope.account_id
    with fixture.store.conn.cursor() as cur:
        cur.execute(
            "UPDATE registry_tools SET deleted_at = now(), project_id = %s, "
            "account_id = %s, implementation = %s::jsonb WHERE tool_id = %s",
            (project_id, account_id, json.dumps(implementation), fixture.tool_id),
        )
    if project_case == "disallowed":
        fixture.state.scope = fixture.state.scope.model_copy(
            update={"allowed_project_ids": frozenset({str(uuid4())})}
        )
    if foreign_tenant:
        fixture.state.scope = fixture.state.scope.model_copy(update={"tenant_id": str(uuid4())})
    before = fixture.store.get_tool_including_trashed(fixture.tool_id)
    response = fixture.client.get("/api/trash?entity_type=registry_tool")
    assert response.status_code == 200, response.text
    listed_ids = {item["entity_id"] for item in response.json()["items"]}
    assert (fixture.tool_id in listed_ids) is visible
    assert fixture.store.get_tool_including_trashed(fixture.tool_id) == before
