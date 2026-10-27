"""Project relation validation through real HTTP, service and disposable SQL owners."""

import os
from types import SimpleNamespace
from uuid import uuid4

import psycopg
import pytest
from psycopg.conninfo import conninfo_to_dict

from core.application.management.command_models import ProjectCreateInput, ProjectUpdateInput
from core.application.management.command_services import ProjectCommandService
from core.application.management.command_services_trash import TrashLifecycleService
from core.application.management.errors import ServiceNotFoundError, ServiceValidationError
from core.application.management.scope import ScopeContext
from core.application.workflows.serve_workflow_helpers import build_project_policy_from_form
from tests._tenant_fixtures import TENANT_ID, create_two_tenants, delete_two_tenants
from tests._xdist_isolation import audit_database_name
from tests.support.platform_test_store import PlatformTestStore
from tests.unit.test_routes_projects_api import _client


@pytest.fixture
def project_audit_database():
    # Same fail-closed audit-only boundary as test_registry_step_identity.
    assert os.environ.get("FS_AUDIT_ISOLATED") == "1"
    dsn = os.environ["FS_DB_DSN"]
    info = conninfo_to_dict(dsn)
    assert (info.get("host"), info.get("dbname"), info.get("user")) == (
        "pg-audit",
        audit_database_name(),
        "audit",
    )
    with psycopg.connect(dsn) as conn:
        tenants = create_two_tenants(conn)
        conn.commit()
        try:
            yield SimpleNamespace(dsn=dsn, conn=conn, tenants=tenants)
        finally:
            conn.rollback()
            delete_two_tenants(conn, tenants)
            conn.commit()


@pytest.fixture
def project_update_fixture(project_audit_database):
    disposable_db_conn = project_audit_database.conn
    two_tenants = project_audit_database.tenants
    owner = two_tenants.a
    project_id = owner.shared.id
    workflow_id = f"fixture.workspace.{uuid4().hex}"
    connection_id = f"fixture.connection.{uuid4().hex}"
    target_id = str(uuid4())
    with disposable_db_conn.cursor() as cur:
        cur.execute(
            "UPDATE projects SET account_id = %s WHERE id = %s", (owner.account_id, project_id)
        )
        cur.execute(
            "INSERT INTO project_policies (project_id, concurrency_cap) VALUES (%s, %s)",
            (project_id, 2),
        )
        cur.execute(
            "INSERT INTO registry_workflows (workflow_key, display_name, account_id) VALUES (%s, %s, %s)",
            (workflow_id, "Fixture workflow", owner.account_id),
        )
        cur.execute(
            "INSERT INTO project_workflows (project_id, workflow_id) VALUES (%s, %s)",
            (project_id, workflow_id),
        )
        cur.execute(
            "INSERT INTO workspace_data_connections (connection_id, tenant_id, account_id, workspace_id, display_name) VALUES (%s, %s, %s, %s, %s)",
            (connection_id, TENANT_ID, owner.account_id, owner.workspace_id, "Fixture connection"),
        )
        cur.execute(
            "INSERT INTO project_data_connections (project_id, connection_id) VALUES (%s, %s)",
            (project_id, connection_id),
        )
        cur.execute(
            "INSERT INTO workspaces (id, tenant_id, account_id, workspace_key, display_name) VALUES (%s, %s, %s, %s, %s)",
            (target_id, TENANT_ID, owner.account_id, f"target-{uuid4().hex}", "Target"),
        )
    disposable_db_conn.commit()
    store = PlatformTestStore(project_audit_database.dsn)
    service = ProjectCommandService(
        get_logger=lambda: store,
        build_project_policy_from_form=build_project_policy_from_form,
        list_workflow_choices=lambda **_kwargs: [{"workflow_id": workflow_id}],
    )
    scope = ScopeContext(account_id=owner.account_id, tenant_id=TENANT_ID, principal_type="admin")

    class Query:
        def get_project_edit_form(self, *_args, **_kwargs):
            return SimpleNamespace(
                project=store.get_project(project_id), project_workflow_ids=[workflow_id]
            )

    class Command:
        def create_project(self, input_payload, **_kwargs):
            return service.create_project(ProjectCreateInput(**vars(input_payload)), scope=scope)

        def update_project(self, incoming_id, input_payload, **_kwargs):
            return service.update_project(
                incoming_id, ProjectUpdateInput(**vars(input_payload)), scope=scope
            )

    fixture = SimpleNamespace(
        store=store,
        service=service,
        scope=scope,
        owner=owner,
        other=two_tenants.b,
        project_id=project_id,
        target_id=target_id,
        workflow_id=workflow_id,
        connection_id=connection_id,
        conn=disposable_db_conn,
        client=_client(project_query_service=Query, project_command_service=Command),
    )
    try:
        yield fixture
    finally:
        # Delete only this fixture's additional rows; two_tenants owns its own cleanup.
        disposable_db_conn.rollback()
        with disposable_db_conn.cursor() as cur:
            cur.execute("DELETE FROM project_workflows WHERE project_id = %s", (project_id,))
            cur.execute(
                "DELETE FROM workspace_data_connections WHERE connection_id = %s", (connection_id,)
            )
            cur.execute("DELETE FROM registry_workflows WHERE workflow_key = %s", (workflow_id,))
            cur.execute(
                "UPDATE projects SET workspace_id = %s WHERE id = %s",
                (owner.workspace_id, project_id),
            )
            cur.execute("DELETE FROM workspaces WHERE id = %s", (target_id,))
        disposable_db_conn.commit()


def _snapshot(fixture, project_id=None):
    result = {}
    with fixture.conn.cursor() as cur:
        for table, identity in [
            ("projects", "id"),
            ("project_policies", "project_id"),
            ("project_workflows", "project_id"),
            ("project_data_connections", "project_id"),
            ("project_settings_revisions", "project_id"),
        ]:
            cur.execute(
                f"SELECT row_to_json(t) FROM {table} t WHERE {identity} = %s ORDER BY row_to_json(t)::text",
                (fixture.project_id if project_id is None else project_id,),
            )
            result[table] = cur.fetchall()
    fixture.conn.rollback()
    return result


def _owned_project_snapshot(fixture):
    result = {}
    with fixture.conn.cursor() as cur:
        for table, identity in [
            ("projects", "id"),
            ("project_policies", "project_id"),
            ("project_workflows", "project_id"),
            ("project_data_connections", "project_id"),
            ("project_settings_revisions", "project_id"),
        ]:
            cur.execute(
                f"SELECT row_to_json(t) FROM {table} t WHERE {identity} IN "
                "(SELECT id FROM projects WHERE workspace_id = ANY(%s::uuid[])) "
                "ORDER BY row_to_json(t)::text",
                ([fixture.owner.workspace_id, fixture.target_id, fixture.other.workspace_id],),
            )
            result[table] = cur.fetchall()
    fixture.conn.rollback()
    return result


def _set_target_workspace_lifecycle(fixture, lifecycle):
    if lifecycle in {"trashed", "archived_trashed"}:
        result = TrashLifecycleService(get_logger=lambda: fixture.store).trash_workspace_with_plan(
            fixture.target_id, scope=fixture.scope
        )
        assert result["status"] == "trashed"
    if lifecycle in {"archived", "archived_trashed"}:
        with fixture.conn.cursor() as cur:
            cur.execute(
                "UPDATE workspaces SET status = 'archived' WHERE id = %s",
                (fixture.target_id,),
            )
        fixture.conn.commit()
    with fixture.conn.cursor() as cur:
        cur.execute("SELECT status, deleted_at FROM workspaces WHERE id = %s", (fixture.target_id,))
        status, deleted_at = cur.fetchone()
    fixture.conn.rollback()
    assert status == ("active" if lifecycle == "trashed" else "archived")
    assert (deleted_at is not None) == (lifecycle in {"trashed", "archived_trashed"})


@pytest.mark.parametrize("lifecycle", ["trashed", "archived", "archived_trashed"])
def test_project_create_rejects_unavailable_workspace_without_mutation(
    project_update_fixture, monkeypatch, lifecycle
):
    fixture = project_update_fixture
    fixture.scope.workspace_id = fixture.owner.workspace_id
    profile_payloads = []
    monkeypatch.setattr(fixture.service, "_schedule_project_profile_sync", profile_payloads.append)
    # Hold the selected target before another normal command soft-Trashes it.
    payload = {
        "name": "Stale workspace create must be refused",
        "workspace_id": fixture.target_id,
        "workflow_ids": [fixture.workflow_id],
    }
    _set_target_workspace_lifecycle(fixture, lifecycle)
    before = _owned_project_snapshot(fixture)
    try:
        response = fixture.client.post("/api/projects", json=payload)
        after = _owned_project_snapshot(fixture)
        assert response.status_code == 404, response.text
        assert response.json() == {"detail": "Workspace not found"}
        assert after == before
        assert profile_payloads == []
    finally:
        # This target was empty before the request; remove unexpected RED creates
        # and their cascading children only from this exact fixture-owned target.
        fixture.conn.rollback()
        with fixture.conn.cursor() as cur:
            cur.execute("DELETE FROM projects WHERE workspace_id = %s", (fixture.target_id,))
        fixture.conn.commit()


@pytest.mark.parametrize(
    "case,expected_status,expected_detail",
    [
        ("missing", 404, "Workspace not found"),
        ("foreign_account", 404, "Workspace not found"),
        ("foreign_tenant", 404, "Workspace not found"),
        ("restricted", 403, "Workspace is outside allowed scope"),
    ],
)
def test_project_create_preserves_workspace_scope_without_mutation(
    project_update_fixture, monkeypatch, case, expected_status, expected_detail
):
    fixture = project_update_fixture
    profile_payloads = []
    monkeypatch.setattr(fixture.service, "_schedule_project_profile_sync", profile_payloads.append)
    target = fixture.target_id
    if case == "missing":
        target = str(uuid4())
    elif case == "foreign_account":
        target = fixture.other.workspace_id
    elif case == "foreign_tenant":
        with fixture.conn.cursor() as cur:
            cur.execute(
                "UPDATE workspaces SET tenant_id = %s WHERE id = %s",
                (str(uuid4()), target),
            )
        fixture.conn.commit()
    else:
        fixture.scope.allowed_workspace_ids = frozenset({fixture.owner.workspace_id})
    name = f"Refused scoped create {uuid4().hex}"
    before = _owned_project_snapshot(fixture)
    try:
        response = fixture.client.post("/api/projects", json={"name": name, "workspace_id": target})
        assert response.status_code == expected_status, response.text
        assert response.json() == {"detail": expected_detail}
        assert _owned_project_snapshot(fixture) == before
        assert profile_payloads == []
    finally:
        fixture.conn.rollback()
        with fixture.conn.cursor() as cur:
            cur.execute(
                "DELETE FROM projects WHERE name = %s AND workspace_id = %s",
                (name, target),
            )
        fixture.conn.commit()


def test_project_create_accepts_active_selected_workspace(project_update_fixture, monkeypatch):
    fixture = project_update_fixture
    fixture.scope.workspace_id = fixture.owner.workspace_id
    fixture.scope.allowed_workspace_ids = frozenset({fixture.owner.workspace_id, fixture.target_id})
    profile_payloads = []
    monkeypatch.setattr(fixture.service, "_schedule_project_profile_sync", profile_payloads.append)
    try:
        response = fixture.client.post(
            "/api/projects",
            json={
                "name": "Active selected workspace create",
                "workspace_id": fixture.target_id,
                "workflow_ids": [fixture.workflow_id],
            },
        )
        assert response.status_code == 201, response.text
        project_id = response.json()["project_id"]
        project = fixture.store.get_project(project_id)
        assert str(project["workspace_id"]) == fixture.target_id
        assert str(project["account_id"]) == fixture.owner.account_id
        assert str(project["tenant_id"]) == TENANT_ID
        assert project["deleted_at"] is None
        persisted = _snapshot(fixture, project_id)
        assert [row[0]["workflow_id"] for row in persisted["project_workflows"]] == [
            fixture.workflow_id
        ]
        assert len(profile_payloads) == 1
        assert profile_payloads[0]["id"] == project_id
        assert profile_payloads[0]["account_id"] == fixture.owner.account_id
    finally:
        fixture.conn.rollback()
        with fixture.conn.cursor() as cur:
            cur.execute("DELETE FROM projects WHERE workspace_id = %s", (fixture.target_id,))
        fixture.conn.commit()


@pytest.mark.parametrize("operation", ["update", "move"])
@pytest.mark.parametrize("lifecycle", ["trashed", "archived", "archived_trashed"])
def test_project_update_and_move_reject_unavailable_workspace_without_mutation(
    project_update_fixture, monkeypatch, operation, lifecycle
):
    fixture = project_update_fixture
    profile_payloads = []
    monkeypatch.setattr(fixture.service, "_schedule_project_profile_sync", profile_payloads.append)
    _set_target_workspace_lifecycle(fixture, lifecycle)
    before = _owned_project_snapshot(fixture)
    if operation == "update":
        response = fixture.client.put(
            f"/api/projects/{fixture.project_id}",
            json={
                "name": "Must not be saved",
                "workspace_id": fixture.target_id,
                "data_connection_ids": [],
            },
        )
        assert response.status_code == 404, response.text
        assert response.json() == {"detail": "Project not found"}
    else:
        with pytest.raises(ServiceNotFoundError, match=r"^Workspace not found$") as refused:
            fixture.service.move_project_to_workspace(
                fixture.project_id, fixture.target_id, scope=fixture.scope
            )
        assert refused.value.status_code == 404
        assert refused.value.payload == {"error": "Workspace not found"}
    assert _owned_project_snapshot(fixture) == before
    assert profile_payloads == []


def test_project_move_accepts_active_workspace_and_preserves_relations(project_update_fixture):
    fixture = project_update_fixture
    fixture.scope.allowed_workspace_ids = frozenset({fixture.owner.workspace_id, fixture.target_id})
    before = _snapshot(fixture)
    result = fixture.service.move_project_to_workspace(
        fixture.project_id, fixture.target_id, scope=fixture.scope
    )
    assert result.status == "moved"
    assert result.project_id == fixture.project_id
    after = _snapshot(fixture)
    assert after["projects"][0][0]["workspace_id"] == fixture.target_id
    assert after["projects"][0][0]["name"] == before["projects"][0][0]["name"]
    assert after["projects"][0][0]["account_id"] == fixture.owner.account_id
    assert after["projects"][0][0]["tenant_id"] == TENANT_ID
    for table in (
        "project_policies",
        "project_workflows",
        "project_data_connections",
        "project_settings_revisions",
    ):
        assert after[table] == before[table]


@pytest.mark.parametrize("name", ["", " ", "\t\n", "\u00a0"])
@pytest.mark.parametrize("transport", ["api", "service"])
def test_blank_project_update_name_is_rejected_without_mutation(
    project_update_fixture, monkeypatch, name, transport
):
    fixture = project_update_fixture
    profile_payloads = []
    monkeypatch.setattr(fixture.service, "_schedule_project_profile_sync", profile_payloads.append)
    before = _snapshot(fixture)
    payload = {"name": name, "workspace_id": fixture.owner.workspace_id}
    if transport == "api":
        response = fixture.client.put(f"/api/projects/{fixture.project_id}", json=payload)
        assert response.status_code == 422, response.text
        assert "name" in response.json()["detail"].lower()
    else:
        update = ProjectUpdateInput(**payload)
        with pytest.raises(ServiceValidationError) as refused:
            fixture.service.update_project(fixture.project_id, update, scope=fixture.scope)
        assert refused.value.status_code == 422
    assert _snapshot(fixture) == before
    assert profile_payloads == []


@pytest.mark.parametrize("name", ["", " ", "\t\n", "\u00a0"])
def test_blank_project_create_name_is_rejected_before_persistence(
    project_update_fixture, monkeypatch, name
):
    fixture = project_update_fixture
    profile_payloads = []
    monkeypatch.setattr(fixture.service, "_schedule_project_profile_sync", profile_payloads.append)
    before = _owned_project_snapshot(fixture)
    try:
        create = ProjectCreateInput(name=name, workspace_id=fixture.target_id)
        with pytest.raises(ServiceValidationError) as refused:
            fixture.service.create_project(create, scope=fixture.scope)
        assert refused.value.status_code == 422
        assert _owned_project_snapshot(fixture) == before
        assert profile_payloads == []
    finally:
        # The target Workspace is new and empty; clean unexpected RED creates
        # only in that exact fixture-owned Workspace, never the source install.
        fixture.conn.rollback()
        with fixture.conn.cursor() as cur:
            cur.execute("DELETE FROM projects WHERE workspace_id = %s", (fixture.target_id,))
        fixture.conn.commit()


def test_valid_project_create_name_is_trimmed(project_update_fixture):
    fixture = project_update_fixture
    try:
        result = fixture.service.create_project(
            ProjectCreateInput(name="  Named project  ", workspace_id=fixture.target_id),
            scope=fixture.scope,
        )
        assert result.name == "Named project"
        assert fixture.store.get_project(result.project_id)["name"] == "Named project"
    finally:
        fixture.conn.rollback()
        with fixture.conn.cursor() as cur:
            cur.execute("DELETE FROM projects WHERE workspace_id = %s", (fixture.target_id,))
        fixture.conn.commit()


@pytest.mark.parametrize(
    "case,expected", [("missing", 404), ("foreign", 403), ("deleted", 404), ("restricted", 403)]
)
def test_blank_name_does_not_bypass_project_scope(project_update_fixture, case, expected):
    fixture = project_update_fixture
    project_id = fixture.project_id
    if case == "missing":
        project_id = str(uuid4())
    elif case == "foreign":
        project_id = fixture.other.shared.id
    elif case == "restricted":
        fixture.scope.allowed_project_ids = frozenset({fixture.owner.private.id})
    else:
        with fixture.conn.cursor() as cur:
            cur.execute("UPDATE projects SET deleted_at = now() WHERE id = %s", (project_id,))
        fixture.conn.commit()
    before = _owned_project_snapshot(fixture)
    target_before = _snapshot(fixture, project_id)
    response = fixture.client.put(
        f"/api/projects/{project_id}",
        json={"name": "", "workspace_id": fixture.owner.workspace_id},
    )
    assert response.status_code == expected, response.text
    assert _owned_project_snapshot(fixture) == before
    assert _snapshot(fixture, project_id) == target_before


@pytest.mark.parametrize(
    "case,expected",
    [
        ("missing", 404),
        ("foreign_account", 404),
        ("foreign_tenant", 404),
        ("restricted", 403),
        ("blank", 422),
        ("malformed", 400),
    ],
)
def test_project_update_rejects_invalid_workspace_without_mutation(
    project_update_fixture, case, expected
):
    fixture = project_update_fixture
    target = fixture.target_id
    if case == "missing":
        target = "00000000-0000-0000-0000-000000000099"
    elif case == "foreign_account":
        target = fixture.other.workspace_id
    elif case == "foreign_tenant":
        with fixture.conn.cursor() as cur:
            cur.execute(
                "UPDATE workspaces SET tenant_id = %s WHERE id = %s",
                ("00000000-0000-0000-0000-000000000099", target),
            )
        fixture.conn.commit()
    elif case == "restricted":
        fixture.scope.allowed_workspace_ids = frozenset({fixture.owner.workspace_id})
    elif case == "blank":
        target = " "
    elif case == "malformed":
        target = "not-a-workspace-uuid"
    before = _snapshot(fixture)
    response = fixture.client.put(
        f"/api/projects/{fixture.project_id}",
        json={"workspace_id": target, "description": "Must not be saved"},
    )
    assert response.status_code == expected, response.text
    assert _snapshot(fixture) == before


@pytest.mark.parametrize("move", [False, True])
def test_project_partial_update_retains_relations_and_allows_own_workspace(
    project_update_fixture, move
):
    fixture = project_update_fixture
    before = _snapshot(fixture)
    payload = {"description": "Metadata saved"}
    if move:
        payload["workspace_id"] = fixture.target_id
        # Existing connection is owned by the old Workspace; clear it explicitly.
        payload["data_connection_ids"] = []
    response = fixture.client.put(f"/api/projects/{fixture.project_id}", json=payload)
    assert response.status_code == 200, response.text
    after = _snapshot(fixture)
    project = after["projects"][0][0]
    assert project["name"] == before["projects"][0][0]["name"]
    assert project["description"] == "Metadata saved"
    assert project["workspace_id"] == (fixture.target_id if move else fixture.owner.workspace_id)
    assert [row[0]["workflow_id"] for row in after["project_workflows"]] == [fixture.workflow_id]
    assert [row[0]["connection_id"] for row in after["project_data_connections"]] == (
        [] if move else [fixture.connection_id]
    )


def test_deleted_project_is_not_updated(project_update_fixture):
    fixture = project_update_fixture
    with fixture.conn.cursor() as cur:
        cur.execute("UPDATE projects SET deleted_at = now() WHERE id = %s", (fixture.project_id,))
    fixture.conn.commit()
    before = _snapshot(fixture)
    response = fixture.client.put(
        f"/api/projects/{fixture.project_id}",
        json={"name": "Should not change", "workspace_id": fixture.target_id},
    )
    assert response.status_code == 404, response.text
    assert _snapshot(fixture) == before


@pytest.mark.parametrize("case", ["missing", "cross_workspace", "duplicate"])
def test_rejected_connections_roll_back_whole_project_update(project_update_fixture, case):
    fixture = project_update_fixture
    payload = {"name": "Must not be persisted", "description": "Must also roll back"}
    if case == "missing":
        payload["data_connection_ids"] = ["00000000-0000-0000-0000-000000000099"]
    elif case == "cross_workspace":
        payload["workspace_id"] = fixture.target_id
        payload["data_connection_ids"] = [fixture.connection_id]
    else:
        payload["data_connection_ids"] = [fixture.connection_id, fixture.connection_id]
    before = _snapshot(fixture)
    response = fixture.client.put(f"/api/projects/{fixture.project_id}", json=payload)
    assert response.status_code == 400, response.text
    assert _snapshot(fixture) == before


def test_valid_connection_update_preserves_sparse_fields(project_update_fixture):
    fixture = project_update_fixture
    before = _snapshot(fixture)
    response = fixture.client.put(
        f"/api/projects/{fixture.project_id}",
        json={"name": "  Accepted update  ", "data_connection_ids": [fixture.connection_id]},
    )
    assert response.status_code == 200, response.text
    after = _snapshot(fixture)
    assert after["projects"][0][0]["name"] == "Accepted update"
    assert after["projects"][0][0]["workspace_id"] == fixture.owner.workspace_id
    assert [row[0]["workflow_id"] for row in after["project_workflows"]] == [fixture.workflow_id]
    assert [row[0]["connection_id"] for row in after["project_data_connections"]] == [
        fixture.connection_id
    ]
    assert after["project_policies"] == before["project_policies"]


@pytest.mark.parametrize("transport", ["api", "service_with_policy"])
def test_connection_failure_after_relation_write_rolls_back_all_rows(
    project_update_fixture, monkeypatch, transport
):
    fixture = project_update_fixture
    real_setter = fixture.store.set_project_data_connections

    def fail_after_write(project_id, connection_ids):
        real_setter(project_id, connection_ids)
        raise ValueError("Forced downstream connection failure")

    monkeypatch.setattr(fixture.store, "set_project_data_connections", fail_after_write)
    before = _snapshot(fixture)
    if transport == "api":
        response = fixture.client.put(
            f"/api/projects/{fixture.project_id}",
            json={"name": "Must roll back", "data_connection_ids": []},
        )
        assert response.status_code == 400, response.text
    else:
        command = ProjectUpdateInput(
            name="Must roll back",
            workspace_id=fixture.owner.workspace_id,
            workflow_ids=[fixture.workflow_id],
            data_connection_ids=[],
            form_data={"policy_concurrency_cap": "3", "data_connection_ids": []},
        )
        with pytest.raises(ServiceValidationError, match="Forced downstream"):
            fixture.service.update_project(fixture.project_id, command, scope=fixture.scope)
    assert _snapshot(fixture) == before
