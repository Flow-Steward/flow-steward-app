"""Workflow plan restoration records its lifecycle transition atomically."""

from __future__ import annotations

import json
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException
from psycopg.errors import DivisionByZero

from core.application.auth.config import DEFAULT_TENANT_ID
from core.application.management.command_services_trash import TrashLifecycleService
from core.application.management.errors import ServiceNotFoundError, ServiceValidationError
from core.application.management.scope import ScopeContext
from core.application.registry.db_store import RegistryDBStore
from core.application.workflows.workflow_history_snapshot import history_snapshot
from core.infrastructure.db.platform_stores import PlatformLifecycleStore
from tests._db_env import resolve_test_db_dsn


@pytest.fixture(autouse=True)
def require_audit_database(pg_conn):
    assert (pg_conn.info.host, pg_conn.info.dbname) == (
        "pg-audit",
        "flow_steward_audit",
    )


@pytest.fixture
def owned_workflow_lifecycle(pg_conn, default_account):
    workflow_key = f"fixture-plan-history-{uuid4().hex}"
    workflow_id = workflow_key
    registry = None
    store = None
    scope = ScopeContext(
        account_id=str(default_account),
        actor_id="workflow-plan-history-owner",
        principal_type="admin",
    )
    try:
        store = PlatformLifecycleStore(resolve_test_db_dsn())
        registry = RegistryDBStore(resolve_test_db_dsn())
        with pg_conn.cursor() as cur:
            cur.execute(
                "INSERT INTO registry_workflows "
                "(workflow_key, display_name, enabled, account_id, scope_kind, settings) "
                "VALUES (%s, 'Fixture plan restore history', false, %s, 'account', "
                '\'{"fixture_marker": "unchanged"}\'::jsonb) RETURNING workflow_id',
                (workflow_key, default_account),
            )
            workflow_id = str(cur.fetchone()[0])
        pg_conn.commit()
        yield SimpleNamespace(
            id=workflow_id,
            scope=scope,
            conn=pg_conn,
            registry=registry,
            store=store,
            service=TrashLifecycleService(get_logger=lambda: store),
        )
    finally:
        pg_conn.rollback()
        try:
            with pg_conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM registry_changelog WHERE entity_type = 'workflow' AND entity_id = %s",
                    (workflow_id,),
                )
                cur.execute(
                    "DELETE FROM registry_workflows WHERE workflow_id = %s",
                    (workflow_id,),
                )
            pg_conn.commit()
        finally:
            if registry is not None and registry._pool is not None:
                registry._pool.closeall()
            elif registry is not None and registry._conn is not None:
                registry._conn.close()
            if store is not None and store._direct_conn is not None:
                store._direct_conn.close()


@pytest.fixture
def foreign_account_workflow_lifecycle(owned_workflow_lifecycle):
    base = owned_workflow_lifecycle
    account_id = str(uuid4())
    account_key = f"fixture-workflow-history-{uuid4().hex}"
    workflow_id = f"fixture-foreign-plan-history-{uuid4().hex}"
    try:
        with base.conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    EXISTS (
                        SELECT 1 FROM information_schema.tables
                        WHERE table_schema = 'public' AND table_name = 'accounts'
                    ) AS accounts_present,
                    EXISTS (
                        SELECT 1 FROM information_schema.columns
                        WHERE table_schema = 'public'
                          AND table_name = 'registry_workflows'
                          AND column_name = 'account_id'
                    ) AS workflow_account_present
                """
            )
            row = cur.fetchone()
            evidence = tuple(row.values()) if isinstance(row, dict) else tuple(row)
            assert evidence == (True, True)
            cur.execute(
                """
                INSERT INTO accounts (id, tenant_id, account_key, display_name, is_active)
                VALUES (%s, %s, %s, 'Foreign workflow history fixture', true)
                """,
                (account_id, DEFAULT_TENANT_ID, account_key),
            )
            cur.execute(
                """
                INSERT INTO registry_workflows
                    (workflow_key, display_name, enabled, account_id, scope_kind, settings)
                VALUES (%s, 'Foreign workflow history fixture', false, %s, 'account', '{}'::jsonb)
                """,
                (workflow_id, account_id),
            )
        base.conn.commit()
        fixture = SimpleNamespace(
            id=workflow_id,
            scope=ScopeContext(
                account_id=account_id,
                actor_id="foreign-workflow-history-owner",
                principal_type="admin",
            ),
            conn=base.conn,
            registry=base.registry,
            service=base.service,
            request_account_id=base.scope.account_id,
        )
        yield fixture
    finally:
        base.conn.rollback()
        with base.conn.cursor() as cur:
            cur.execute(
                "DELETE FROM registry_changelog WHERE entity_type = 'workflow' AND entity_id = %s",
                (workflow_id,),
            )
            cur.execute(
                "DELETE FROM registry_workflows WHERE workflow_id = %s",
                (workflow_id,),
            )
            cur.execute("DELETE FROM accounts WHERE id = %s", (account_id,))
        base.conn.commit()


def _history(fixture):
    with fixture.conn.cursor() as cur:
        cur.execute(
            "SELECT action, snapshot, author FROM registry_changelog "
            "WHERE entity_type = 'workflow' AND entity_id = %s "
            "ORDER BY created_at DESC, id DESC",
            (fixture.id,),
        )
        rows = cur.fetchall()
    fixture.conn.commit()
    return rows


def _history_state(fixture):
    with fixture.conn.cursor() as cur:
        cur.execute(
            "SELECT row_to_json(changelog) FROM registry_changelog AS changelog "
            "WHERE entity_type = 'workflow' AND entity_id = %s "
            "ORDER BY created_at DESC, id DESC",
            (fixture.id,),
        )
        rows = [row[0] for row in cur.fetchall()]
    fixture.conn.commit()
    return rows


def _workflow_snapshot(fixture):
    with fixture.conn.cursor() as cur:
        cur.execute(
            "SELECT row_to_json(w) FROM registry_workflows w WHERE workflow_id = %s",
            (fixture.id,),
        )
        row = cur.fetchone()[0]
    fixture.conn.commit()
    return row


def _restore_lifecycle_marker(fixture):
    fixture.service.trash("registry_workflow", fixture.id, scope=fixture.scope)
    plan = fixture.service.plan(
        "registry_workflow", fixture.id, action_type="restore", scope=fixture.scope
    )
    result = fixture.service.execute_plan(
        "registry_workflow",
        fixture.id,
        action_type="restore",
        selection={
            "plan_version": plan.plan_version,
            "selected_node_ids": [plan.tree["node_id"]],
        },
        scope=fixture.scope,
    )
    assert result.status == "restored"
    history = fixture.registry.get_entity_history("workflow", fixture.id, limit=10)
    marker = history[0]
    assert marker is not None
    assert marker["action"] == "restore"
    assert marker["revision_kind"] == "lifecycle"
    assert marker["revision"] is None
    assert (
        fixture.registry.get_changelog_entry(
            str(marker["id"]), entity_type="workflow", entity_id=fixture.id
        )
        == marker
    )
    assert fixture.registry.get_entity_revision("workflow", fixture.id, revision=1) == history[1]
    return marker


def _history_request(scope: ScopeContext):
    return SimpleNamespace(
        state=SimpleNamespace(
            scope_context=scope,
            auth_user={"username": scope.actor_name, "roles": ["operator"]},
        )
    )


def test_workflow_plan_restore_records_history(owned_workflow_lifecycle):
    fixture = owned_workflow_lifecycle
    content_snapshot = {
        "workflow_id": fixture.id,
        "display_name": "Fixture plan restore history",
        "settings": {"fixture_marker": "unchanged"},
    }
    fixture.registry.log_change(
        "workflow",
        fixture.id,
        "create",
        content_snapshot,
        "workflow-content-author",
    )
    fixture.service.trash("registry_workflow", fixture.id, scope=fixture.scope)
    assert [row[0] for row in _history(fixture)] == ["trash", "create"]
    plan = fixture.service.plan(
        "registry_workflow", fixture.id, action_type="restore", scope=fixture.scope
    )

    result = fixture.service.execute_plan(
        "registry_workflow",
        fixture.id,
        action_type="restore",
        selection={
            "plan_version": plan.plan_version,
            "selected_node_ids": [plan.tree["node_id"]],
        },
        scope=fixture.scope,
    )

    assert result.status == "restored"
    with fixture.conn.cursor() as cur:
        cur.execute(
            "SELECT deleted_at, enabled, account_id, settings FROM registry_workflows "
            "WHERE workflow_id = %s",
            (fixture.id,),
        )
        row = cur.fetchone()
    fixture.conn.commit()
    assert row[:2] == (None, False)
    assert str(row[2]) == fixture.scope.account_id
    assert row[3] == {"fixture_marker": "unchanged"}
    history = _history(fixture)
    assert [row[0] for row in history] == ["restore", "trash", "create"]
    assert history[0][1:] == (
        {
            "lifecycle_action": "restore",
            "entity_type": "workflow",
            "entity_id": fixture.id,
            "actor": "workflow-plan-history-owner",
        },
        "workflow-plan-history-owner",
    )
    assert history[2] == (
        "create",
        content_snapshot,
        "workflow-content-author",
    )

    changelog = fixture.registry.get_changelog(
        entity_type="workflow",
        entity_id=fixture.id,
        limit=10,
    )
    projected_history = fixture.registry.get_entity_history(
        entity_type="workflow",
        entity_id=fixture.id,
        limit=10,
    )
    assert [(row["action"], row["revision_kind"]) for row in changelog] == [
        ("restore", "lifecycle"),
        ("trash", "lifecycle"),
        ("create", "content"),
    ]
    assert [(row["action"], row["revision_kind"]) for row in projected_history] == [
        ("restore", "lifecycle"),
        ("trash", "lifecycle"),
        ("create", "content"),
    ]
    restore_entry, trash_entry, content_entry = projected_history
    assert trash_entry["snapshot"]["history_schema_version"] == 1
    assert trash_entry["snapshot"]["revision"] == 1
    assert trash_entry["revision"] is None
    assert fixture.registry.get_entity_revision("workflow", fixture.id, revision=1) == trash_entry
    assert (
        fixture.registry.get_changelog_entry(
            str(restore_entry["id"]),
            entity_type="workflow",
            entity_id=fixture.id,
        )
        == restore_entry
    )

    lifecycle_diff = fixture.registry.diff_entity_history(
        "workflow",
        fixture.id,
        from_entry_id=str(content_entry["id"]),
        to_entry_id=str(restore_entry["id"]),
    )
    assert lifecycle_diff["from_entry_id"] == str(content_entry["id"])
    assert lifecycle_diff["to_entry_id"] == str(restore_entry["id"])
    assert lifecycle_diff["comparison_kind"] == "lifecycle"
    assert lifecycle_diff["changes"] == []
    assert lifecycle_diff["summary"] == {}
    assert _history(fixture) == history


def test_workflow_delete_projects_as_lifecycle_without_mutating_full_snapshot(
    owned_workflow_lifecycle,
):
    fixture = owned_workflow_lifecycle
    predelete = fixture.registry.get_workflow(fixture.id)
    assert predelete is not None
    assert predelete["enabled"] is False
    assert str(predelete["account_id"]) == fixture.scope.account_id
    assert predelete["settings"] == {"fixture_marker": "unchanged"}
    fixture.registry.log_change(
        "workflow",
        fixture.id,
        "update",
        predelete,
        "workflow-content-author",
    )

    fixture.registry.delete_workflow(fixture.id, author=fixture.scope.actor_name)

    with fixture.conn.cursor() as cur:
        cur.execute(
            "SELECT deleted_at, enabled, account_id, settings, revision "
            "FROM registry_workflows WHERE workflow_id = %s",
            (fixture.id,),
        )
        tombstone = cur.fetchone()
    fixture.conn.commit()
    assert tombstone[0] is not None
    assert tombstone[1] is False
    assert str(tombstone[2]) == fixture.scope.account_id
    assert tombstone[3] == {"fixture_marker": "unchanged"}
    assert tombstone[4] == predelete["revision"] + 1

    raw_history = _history_state(fixture)
    raw_delete = next(row for row in raw_history if row["action"] == "delete")
    assert raw_delete["author"] == fixture.scope.actor_name
    assert raw_delete["snapshot"] == json.loads(
        json.dumps(history_snapshot(predelete), default=str)
    )

    changelog = fixture.registry.get_changelog(
        entity_type="workflow",
        entity_id=fixture.id,
        limit=10,
    )
    projected_history = fixture.registry.get_entity_history(
        entity_type="workflow",
        entity_id=fixture.id,
        limit=10,
    )
    assert [(row["action"], row["revision_kind"]) for row in changelog] == [
        ("delete", "lifecycle"),
        ("update", "content"),
    ]
    assert [(row["action"], row["revision_kind"]) for row in projected_history] == [
        ("delete", "lifecycle"),
        ("update", "content"),
    ]
    delete_entry, content_entry = projected_history
    assert fixture.registry.get_entity_revision("workflow", fixture.id, revision=1) == delete_entry
    assert (
        fixture.registry.get_changelog_entry(
            str(delete_entry["id"]),
            entity_type="workflow",
            entity_id=fixture.id,
        )
        == delete_entry
    )

    for older, newer in (
        (content_entry, delete_entry),
        (delete_entry, content_entry),
        (delete_entry, delete_entry),
    ):
        lifecycle_diff = fixture.registry.diff_entity_history(
            "workflow",
            fixture.id,
            from_entry_id=str(older["id"]),
            to_entry_id=str(newer["id"]),
        )
        assert lifecycle_diff["from_entry_id"] == str(older["id"])
        assert lifecycle_diff["to_entry_id"] == str(newer["id"])
        assert lifecycle_diff["comparison_kind"] == "lifecycle"
        assert lifecycle_diff["changes"] == []
        assert lifecycle_diff["summary"] == {}

    assert _history_state(fixture) == raw_history


def test_workflow_revision_marker_denies_url_outside_workflow_allowlist(
    owned_workflow_lifecycle, monkeypatch
):
    from core.entrypoints.admin import workflows_routes_history as routes

    fixture = owned_workflow_lifecycle
    _restore_lifecycle_marker(fixture)
    history_before = _history_state(fixture)
    restricted_scope = ScopeContext(
        account_id=fixture.scope.account_id,
        actor_id="workflow-history-restricted-reader",
        principal_type="operator",
        allowed_workflow_ids=frozenset({"some-other-workflow"}),
    )
    monkeypatch.setattr(routes, "_get_db", lambda: fixture.registry)

    request = _history_request(restricted_scope)
    with pytest.raises(HTTPException) as raised:
        routes.get_workflow_revision(fixture.id, 1, request)

    assert raised.value.status_code == 403
    assert _history_state(fixture) == history_before


def test_workflow_revision_marker_uses_current_row_account_scope(
    foreign_account_workflow_lifecycle, monkeypatch
):
    from core.entrypoints.admin import workflows_routes_history as routes

    fixture = foreign_account_workflow_lifecycle
    _restore_lifecycle_marker(fixture)
    history_before = _history_state(fixture)
    default_account_scope = ScopeContext(
        account_id=fixture.request_account_id,
        actor_id="default-account-workflow-reader",
        principal_type="operator",
        allowed_workflow_ids=frozenset({fixture.id}),
    )
    monkeypatch.setattr(routes, "_get_db", lambda: fixture.registry)

    request = _history_request(default_account_scope)
    with pytest.raises(HTTPException) as raised:
        routes.get_workflow_revision(fixture.id, 1, request)

    assert raised.value.status_code == 403
    assert _history_state(fixture) == history_before


def test_workflow_revision_marker_returns_metadata_for_matching_current_row(
    owned_workflow_lifecycle, monkeypatch
):
    from core.entrypoints.admin import workflows_routes_history as routes

    fixture = owned_workflow_lifecycle
    marker = _restore_lifecycle_marker(fixture)
    history_before = _history_state(fixture)
    matching_scope = ScopeContext(
        account_id=fixture.scope.account_id,
        actor_id="workflow-history-matching-reader",
        principal_type="operator",
        allowed_workflow_ids=frozenset({fixture.id}),
    )
    monkeypatch.setattr(routes, "_get_db", lambda: fixture.registry)

    response = routes.get_workflow_revision(
        fixture.id,
        1,
        _history_request(matching_scope),
    )

    assert response["id"] != marker["id"]
    assert response["entity_type"] == "workflow"
    assert response["entity_id"] == fixture.id
    assert response["action"] == "trash"
    assert response["revision_kind"] == "lifecycle"
    snapshot = response["snapshot"]
    assert snapshot["history_schema_version"] == 1
    assert snapshot["revision"] == 1
    assert snapshot["workflow_id"] == fixture.id
    assert snapshot["settings"] == {"fixture_marker": "unchanged"}
    assert all(isinstance(snapshot[key], list) for key in ("steps", "groups", "for_each"))
    assert snapshot["portability"]["workflow_id"] == fixture.id
    assert snapshot["portability"]["extension_owned"] is False
    assert snapshot["portability"]["protected"] is False
    assert snapshot["portability"]["full_export_allowed"] is True
    assert snapshot["portability"]["clone_allowed"] is True
    assert snapshot["portability"]["structure_change_allowed"] is True
    history = routes.get_workflow_history(
        fixture.id,
        _history_request(matching_scope),
        limit=10,
    )
    assert history[0]["id"] == marker["id"]
    assert history[0]["action"] == "restore"
    assert history[0]["revision_kind"] == "lifecycle"
    assert history[0]["revision"] is None
    assert history[0]["snapshot"]["lifecycle_action"] == "restore"
    assert history[0]["snapshot"]["entity_type"] == "workflow"
    assert history[0]["snapshot"]["entity_id"] == fixture.id
    assert history[0]["snapshot"]["actor"] == "workflow-plan-history-owner"
    assert history[0]["snapshot"]["portability"]["structure_change_allowed"] is True
    assert _history_state(fixture) == history_before


def test_workflow_plan_restore_rolls_back_real_audit_write(owned_workflow_lifecycle, monkeypatch):
    fixture = owned_workflow_lifecycle
    fixture.service.trash("registry_workflow", fixture.id, scope=fixture.scope)
    before = _workflow_snapshot(fixture)
    history_before = _history(fixture)
    plan = fixture.service.plan(
        "registry_workflow", fixture.id, action_type="restore", scope=fixture.scope
    )
    real_writer = fixture.service._log_registry_changelog_entry
    observed = []

    def write_then_fail(cur, **kwargs):
        real_writer(cur, **kwargs)
        cur.execute(
            "SELECT EXISTS (SELECT 1 FROM registry_workflows "
            "WHERE workflow_id = %s AND deleted_at IS NULL) AS restored, "
            "EXISTS (SELECT 1 FROM registry_changelog "
            "WHERE entity_type = 'workflow' AND entity_id = %s AND action = 'restore') AS recorded",
            (fixture.id, fixture.id),
        )
        row = cur.fetchone()
        evidence = tuple(row.values()) if isinstance(row, dict) else tuple(row)
        assert evidence == (True, True)
        observed.append(evidence)
        cur.execute("SELECT 1 / 0")

    monkeypatch.setattr(fixture.service, "_log_registry_changelog_entry", write_then_fail)
    with pytest.raises(DivisionByZero) as raised:
        fixture.service.execute_plan(
            "registry_workflow",
            fixture.id,
            action_type="restore",
            selection={
                "plan_version": plan.plan_version,
                "selected_node_ids": [plan.tree["node_id"]],
            },
            scope=fixture.scope,
        )
    assert raised.value.sqlstate == "22012"
    assert observed == [(True, True)]
    assert _workflow_snapshot(fixture) == before
    assert _history(fixture) == history_before


@pytest.mark.parametrize("boundary", ["foreign-account", "stale-plan"])
def test_workflow_plan_restore_denial_keeps_tombstone_and_history(
    owned_workflow_lifecycle, boundary
):
    fixture = owned_workflow_lifecycle
    fixture.service.trash("registry_workflow", fixture.id, scope=fixture.scope)
    before = _workflow_snapshot(fixture)
    history_before = _history(fixture)
    plan = fixture.service.plan(
        "registry_workflow", fixture.id, action_type="restore", scope=fixture.scope
    )
    scope = fixture.scope
    if boundary == "foreign-account":
        scope = ScopeContext(
            account_id="00000000-0000-0000-0000-000000000099",
            actor_id="foreign-workflow-plan-admin",
            principal_type="admin",
        )
    with pytest.raises(
        ServiceNotFoundError if boundary == "foreign-account" else ServiceValidationError
    ):
        fixture.service.execute_plan(
            "registry_workflow",
            fixture.id,
            action_type="restore",
            selection={
                "plan_version": "stale-version" if boundary == "stale-plan" else plan.plan_version,
                "selected_node_ids": [plan.tree["node_id"]],
            },
            scope=scope,
        )
    assert _workflow_snapshot(fixture) == before
    assert _history(fixture) == history_before
