"""Generic Guardrail lifecycle events stay visible in account-scoped history."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from core.application.management.command_services_trash import TrashLifecycleService
from core.application.management.errors import ServiceForbiddenError
from core.application.management.scope import ScopeContext
from core.application.registry.db_store import RegistryDBStore
from core.entrypoints.admin import changelog
from core.infrastructure.db.platform_stores import PlatformLifecycleStore
from tests._db_env import resolve_test_db_dsn


@pytest.fixture(autouse=True)
def require_audit_database(pg_conn):
    # These tests may mutate only unique rows in the disposable Docker gate DB.
    assert (pg_conn.info.host, pg_conn.info.dbname) == (
        "pg-audit",
        "flow_steward_audit",
    )


@pytest.fixture
def owned_guardrail_lifecycle(pg_conn, default_account):
    guardrail_id = str(uuid4())
    project_id = str(uuid4())
    other_project_id = str(uuid4())
    lifecycle_store = None
    registry_store = None
    scope = ScopeContext(
        account_id=str(default_account),
        actor_id="guardrail-lifecycle-owner",
        principal_type="admin",
    )
    try:
        lifecycle_store = PlatformLifecycleStore(resolve_test_db_dsn())
        registry_store = RegistryDBStore(resolve_test_db_dsn())
        with pg_conn.cursor() as cur:
            cur.execute(
                "INSERT INTO projects (id, name, account_id) "
                "VALUES (%s, 'Fixture guardrail lifecycle project', %s)",
                (project_id, default_account),
            )
            cur.execute(
                "INSERT INTO projects (id, name, account_id) "
                "VALUES (%s, 'Fixture guardrail moved project', %s)",
                (other_project_id, default_account),
            )
            cur.execute(
                "INSERT INTO registry_guardrails "
                "(id, display_name, enabled, mode, rule_type, severity, config, account_id, project_id) "
                "VALUES (%s, 'Fixture lifecycle guardrail', false, 'deterministic', "
                "'forbidden_patterns', 'warning', '{\"patterns\": [\"fixture-inert\"]}'::jsonb, %s, %s)",
                (guardrail_id, default_account, project_id),
            )
        pg_conn.commit()
        yield SimpleNamespace(
            id=guardrail_id,
            project_id=project_id,
            other_project_id=other_project_id,
            scope=scope,
            service=TrashLifecycleService(get_logger=lambda: lifecycle_store),
            registry=registry_store,
            conn=pg_conn,
        )
    finally:
        pg_conn.rollback()
        try:
            with pg_conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM registry_changelog "
                    "WHERE entity_type = 'guardrail' AND entity_id = %s",
                    (guardrail_id,),
                )
                cur.execute("DELETE FROM registry_guardrails WHERE id = %s", (guardrail_id,))
                cur.execute("DELETE FROM projects WHERE id = %s", (project_id,))
                cur.execute("DELETE FROM projects WHERE id = %s", (other_project_id,))
            pg_conn.commit()
        finally:
            if lifecycle_store is not None and lifecycle_store._direct_conn is not None:
                lifecycle_store._direct_conn.close()
            if registry_store is not None and registry_store._pool is not None:
                registry_store._pool.closeall()
            elif registry_store is not None and registry_store._conn is not None:
                registry_store._conn.close()


def test_guardrail_trash_restore_history_keeps_account_and_actor(owned_guardrail_lifecycle):
    fixture = owned_guardrail_lifecycle
    before = fixture.registry.get_guardrail(fixture.id, account_id=fixture.scope.account_id)

    fixture.service.trash(
        "registry_guardrail",
        fixture.id,
        deleted_by="guardrail-lifecycle-owner",
        scope=fixture.scope,
    )
    tombstone = fixture.registry.get_guardrail(
        fixture.id, include_trashed=True, account_id=fixture.scope.account_id
    )
    assert tombstone is not None
    assert tombstone["deleted_at"] is not None
    assert tombstone["deleted_by"] == "guardrail-lifecycle-owner"
    fixture.service.restore("registry_guardrail", fixture.id, scope=fixture.scope)

    restored = fixture.registry.get_guardrail(fixture.id, account_id=fixture.scope.account_id)
    assert restored["deleted_at"] is None
    assert restored["deleted_by"] is None
    for field in (
        "id",
        "account_id",
        "project_id",
        "enabled",
        "mode",
        "rule_type",
        "severity",
        "config",
    ):
        assert restored[field] == before[field]
    history = fixture.registry.get_entity_history(
        "guardrail", fixture.id, account_id=fixture.scope.account_id
    )
    assert [entry["action"] for entry in history] == ["restore", "trash"]
    for entry in history:
        assert str(entry["account_id"]) == fixture.scope.account_id
        assert entry["actor_id"] == "guardrail-lifecycle-owner"
        assert entry["actor_type"] == "admin"
        assert entry["author"] == "guardrail-lifecycle-owner"
        assert entry["snapshot"] == {
            "lifecycle_action": entry["action"],
            "entity_type": "guardrail",
            "entity_id": fixture.id,
            "actor": "guardrail-lifecycle-owner",
        }
    assert (
        fixture.registry.get_entity_history(
            "guardrail", fixture.id, account_id="00000000-0000-0000-0000-000000000099"
        )
        == []
    )


def test_guardrail_delete_projects_as_lifecycle_without_mutating_full_snapshot(
    owned_guardrail_lifecycle,
):
    fixture = owned_guardrail_lifecycle
    predelete = fixture.registry.get_guardrail(
        fixture.id,
        account_id=fixture.scope.account_id,
    )
    assert predelete is not None
    assert predelete["enabled"] is False
    assert str(predelete["account_id"]) == fixture.scope.account_id
    assert str(predelete["project_id"]) == fixture.project_id
    assert predelete["config"] == {"patterns": ["fixture-inert"]}
    fixture.registry.log_change(
        "guardrail",
        fixture.id,
        "update",
        predelete,
        "guardrail-content-author",
    )

    assert fixture.registry.delete_guardrail(
        fixture.id,
        author=fixture.scope.actor_name,
        account_id=fixture.scope.account_id,
        actor_id=fixture.scope.actor_id,
        actor_type=fixture.scope.principal_type,
    )

    with fixture.conn.cursor() as cur:
        cur.execute(
            "SELECT deleted_at, deleted_by, enabled, account_id, project_id, config "
            "FROM registry_guardrails WHERE id = %s",
            (fixture.id,),
        )
        tombstone = cur.fetchone()
        cur.execute(
            "SELECT row_to_json(changelog) FROM registry_changelog AS changelog "
            "WHERE entity_type = 'guardrail' AND entity_id = %s "
            "ORDER BY created_at DESC, id DESC",
            (fixture.id,),
        )
        raw_history = [row[0] for row in cur.fetchall()]
    fixture.conn.commit()
    assert tombstone[0] is not None
    assert tombstone[1] == fixture.scope.actor_id
    assert tombstone[2] is False
    assert str(tombstone[3]) == fixture.scope.account_id
    assert str(tombstone[4]) == fixture.project_id
    assert tombstone[5] == {"patterns": ["fixture-inert"]}
    raw_delete = next(row for row in raw_history if row["action"] == "delete")
    assert raw_delete["author"] == fixture.scope.actor_name
    assert str(raw_delete["account_id"]) == fixture.scope.account_id
    assert raw_delete["actor_id"] == fixture.scope.actor_id
    assert raw_delete["actor_type"] == fixture.scope.principal_type
    assert raw_delete["snapshot"] == json.loads(json.dumps(predelete, default=str))

    changelog = fixture.registry.get_changelog(
        entity_type="guardrail",
        entity_id=fixture.id,
        limit=10,
    )
    projected_history = fixture.registry.get_entity_history(
        entity_type="guardrail",
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
    account_history = fixture.registry.get_entity_history(
        "guardrail",
        fixture.id,
        account_id=fixture.scope.account_id,
    )
    assert [(row["action"], row["revision_kind"]) for row in account_history] == [
        ("delete", "lifecycle")
    ]
    assert (
        fixture.registry.get_entity_revision(
            "guardrail",
            fixture.id,
            revision=1,
            account_id=fixture.scope.account_id,
        )
        == account_history[0]
    )
    assert (
        fixture.registry.get_changelog_entry(
            str(delete_entry["id"]),
            entity_type="guardrail",
            entity_id=fixture.id,
            account_id=fixture.scope.account_id,
        )
        == account_history[0]
    )

    for older, newer in (
        (content_entry, delete_entry),
        (delete_entry, content_entry),
        (delete_entry, delete_entry),
    ):
        lifecycle_diff = fixture.registry.diff_entity_history(
            "guardrail",
            fixture.id,
            from_entry_id=str(older["id"]),
            to_entry_id=str(newer["id"]),
        )
        assert lifecycle_diff["from_entry_id"] == str(older["id"])
        assert lifecycle_diff["to_entry_id"] == str(newer["id"])
        assert lifecycle_diff["comparison_kind"] == "lifecycle"
        assert lifecycle_diff["changes"] == []
        assert lifecycle_diff["summary"] == {}

    with fixture.conn.cursor() as cur:
        cur.execute(
            "SELECT row_to_json(changelog) FROM registry_changelog AS changelog "
            "WHERE entity_type = 'guardrail' AND entity_id = %s "
            "ORDER BY created_at DESC, id DESC",
            (fixture.id,),
        )
        history_after_reads = [row[0] for row in cur.fetchall()]
    fixture.conn.commit()
    assert history_after_reads == raw_history


def test_foreign_guardrail_restore_does_not_write_history(owned_guardrail_lifecycle):
    fixture = owned_guardrail_lifecycle
    fixture.service.trash(
        "registry_guardrail",
        fixture.id,
        deleted_by="guardrail-lifecycle-owner",
        scope=fixture.scope,
    )
    history_before = fixture.registry.get_changelog("guardrail", fixture.id)
    tombstone = fixture.registry.get_guardrail(
        fixture.id, include_trashed=True, account_id=fixture.scope.account_id
    )
    assert tombstone is not None
    assert tombstone["deleted_at"] is not None
    assert tombstone["deleted_by"] == "guardrail-lifecycle-owner"
    foreign_scope = ScopeContext(
        account_id="00000000-0000-0000-0000-000000000099",
        actor_id="foreign-lifecycle-admin",
        principal_type="admin",
    )

    with pytest.raises(ServiceForbiddenError):
        fixture.service.restore("registry_guardrail", fixture.id, scope=foreign_scope)

    assert fixture.registry.get_changelog("guardrail", fixture.id) == history_before
    assert (
        fixture.registry.get_guardrail(
            fixture.id, include_trashed=True, account_id=fixture.scope.account_id
        )
        == tombstone
    )


@pytest.mark.parametrize("revision", [False, True], ids=["history", "revision"])
def test_guardrail_lifecycle_history_keeps_project_access_guard(
    owned_guardrail_lifecycle, revision
):
    fixture = owned_guardrail_lifecycle
    fixture.service.trash("registry_guardrail", fixture.id, scope=fixture.scope)
    fixture.service.restore("registry_guardrail", fixture.id, scope=fixture.scope)
    request = Request({"type": "http"})
    request.state.scope_context = ScopeContext(
        account_id=fixture.scope.account_id,
        allowed_project_ids=frozenset({"00000000-0000-4000-8000-000000000099"}),
    )

    def read_history():
        if revision:
            return [changelog.get_entity_revision(request, "guardrail", fixture.id, 1)]
        return changelog.get_entity_history(request, "guardrail", fixture.id, limit=50)

    with (
        patch.object(changelog, "_get_db", return_value=fixture.registry),
        pytest.raises(HTTPException) as raised,
    ):
        read_history()

    assert raised.value.status_code == 403

    request.state.scope_context = ScopeContext(
        account_id=fixture.scope.account_id,
        allowed_project_ids=frozenset({fixture.project_id}),
    )
    with patch.object(changelog, "_get_db", return_value=fixture.registry):
        history = read_history()
    assert [entry["action"] for entry in history] == (
        ["restore"] if revision else ["restore", "trash"]
    )


@pytest.mark.parametrize("boundary", ["foreign-account", "trashed", "missing"])
def test_guardrail_lifecycle_history_requires_current_owned_entity(
    owned_guardrail_lifecycle, boundary
):
    fixture = owned_guardrail_lifecycle
    fixture.service.trash("registry_guardrail", fixture.id, scope=fixture.scope)
    if boundary != "trashed":
        fixture.service.restore("registry_guardrail", fixture.id, scope=fixture.scope)
    if boundary == "missing":
        with fixture.conn.cursor() as cur:
            cur.execute("DELETE FROM registry_guardrails WHERE id = %s", (fixture.id,))
        fixture.conn.commit()
    request = Request({"type": "http"})
    request.state.scope_context = ScopeContext(
        account_id=(
            "00000000-0000-0000-0000-000000000099"
            if boundary == "foreign-account"
            else fixture.scope.account_id
        )
    )
    with (
        patch.object(changelog, "_get_db", return_value=fixture.registry),
        pytest.raises(HTTPException) as raised,
    ):
        changelog.get_entity_history(request, "guardrail", fixture.id, limit=50)
    assert raised.value.status_code == 404


@pytest.mark.parametrize("current_global", [False, True], ids=["moved-project", "global"])
def test_guardrail_history_authorizes_current_project_not_historical_marker(
    owned_guardrail_lifecycle, current_global
):
    fixture = owned_guardrail_lifecycle
    fixture.service.trash("registry_guardrail", fixture.id, scope=fixture.scope)
    fixture.service.restore("registry_guardrail", fixture.id, scope=fixture.scope)
    with fixture.conn.cursor() as cur:
        cur.execute(
            "UPDATE registry_changelog SET snapshot = snapshot || "
            "jsonb_build_object('project_id', %s::text, 'account_id', %s::text) "
            "WHERE entity_type = 'guardrail' AND entity_id = %s",
            (fixture.project_id, fixture.scope.account_id, fixture.id),
        )
        cur.execute(
            "UPDATE registry_guardrails SET project_id = %s WHERE id = %s",
            (None if current_global else fixture.other_project_id, fixture.id),
        )
    fixture.conn.commit()
    request = Request({"type": "http"})
    request.state.scope_context = ScopeContext(
        account_id=fixture.scope.account_id,
        allowed_project_ids=frozenset(
            {fixture.other_project_id if current_global else fixture.project_id}
        ),
    )
    if not current_global:
        with (
            patch.object(changelog, "_get_db", return_value=fixture.registry),
            pytest.raises(HTTPException) as raised,
        ):
            changelog.get_entity_history(request, "guardrail", fixture.id, limit=50)
        assert raised.value.status_code == 403
        request.state.scope_context = ScopeContext(
            account_id=fixture.scope.account_id,
            allowed_project_ids=frozenset({fixture.other_project_id}),
        )
    with patch.object(changelog, "_get_db", return_value=fixture.registry):
        history = changelog.get_entity_history(request, "guardrail", fixture.id, limit=50)
    assert [entry["action"] for entry in history] == ["restore", "trash"]
