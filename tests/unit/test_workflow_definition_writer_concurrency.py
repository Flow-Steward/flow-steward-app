"""Secondary workflow writers must not overwrite a concurrently edited definition."""

from contextlib import contextmanager
from copy import deepcopy
from functools import partial
from unittest.mock import MagicMock

import pytest

from core.application.management.command_services_project_workflow_bindings_mixin import (
    ProjectCommandServiceWorkflowBindingsMixin,
)
from core.application.registry.db_store import RegistryDBStore
from core.application.registry.db_store_workflows_mixin import (
    WorkflowCreateConflictError,
    WorkflowRevisionConflictError,
)
from core.entrypoints.admin import workflows_routes_write as writes
from core.entrypoints.api import workflow_entrypoints_registry as entrypoints


def test_provider_trigger_save_uses_loaded_revision_and_rejects_concurrent_edit(monkeypatch):
    original = {
        "workflow_id": "wf",
        "revision": 4,
        "steps": [{"step_id": "old"}],
        "groups": [],
        "settings": {"execution_graph": {"entry": {"kind": "step", "id": "old"}}},
    }
    persisted = deepcopy(original)
    persisted.update(revision=5, steps=[{"step_id": "new"}])

    def save(payload, *, author, expected_revision=None):
        if expected_revision is not None and expected_revision != persisted["revision"]:
            raise WorkflowRevisionConflictError("wf", expected_revision)
        persisted.update(payload)

    db = MagicMock()
    db.save_workflow.side_effect = save
    monkeypatch.setattr(entrypoints, "_workflow_registry_db", lambda: db)
    monkeypatch.setattr(entrypoints.RegistryManager, "instance", lambda: MagicMock())
    with pytest.raises(WorkflowRevisionConflictError):
        entrypoints._save_workflow_manifest(original, author="test")
    assert persisted["steps"] == [{"step_id": "new"}]


def test_revision_restore_propagates_expected_current_revision_to_save():
    db = RegistryDBStore("postgres://unused")
    db._normalize_restore_payload = MagicMock(return_value={"workflow_id": "wf"})
    db.save_workflow = MagicMock(return_value=8)
    db.get_workflow = MagicMock(return_value={"workflow_id": "wf", "revision": 8})
    db.apply_entity_snapshot("workflow", "wf", {"workflow_id": "wf"}, expected_revision=7)
    assert db.save_workflow.call_args.kwargs["expected_revision"] == 7


def test_enable_validation_runs_while_workflow_row_is_locked(monkeypatch):
    workflow = {"workflow_id": "wf", "revision": 4, "enabled": False}
    db = MagicMock()
    db.get_workflow.return_value = workflow
    scope = MagicMock(account_id="account")
    locked = False
    cursor = MagicMock()
    cursor.fetchone.return_value = workflow

    def execute(sql, *args):
        nonlocal locked
        if "FOR UPDATE" in str(sql):
            locked = True

    cursor.execute.side_effect = execute

    @contextmanager
    def transaction():
        nonlocal locked
        try:
            yield cursor
        finally:
            locked = False

    db._transaction = transaction

    def validate(*args, **kwargs):
        assert locked, "A concurrent edit can invalidate the workflow before enable"
        return {"valid": True, "errors": [], "warnings": []}

    monkeypatch.setattr(writes, "_get_db", lambda: db)
    monkeypatch.setattr(writes, "_get_registry", MagicMock())
    monkeypatch.setattr(writes, "_resolve_workflow_scope_target", lambda *args: (workflow, scope))
    monkeypatch.setattr(writes, "_assert_workflow_mutation_allowed", lambda *args: None)
    monkeypatch.setattr(writes, "_get_workflow_validation", validate)
    writes.toggle_workflow("wf", MagicMock())


def test_extension_anchor_refresh_compares_revision_instead_of_overwriting_settings():
    store = MagicMock()
    store.get_workflow.return_value = {
        "workflow_id": "ext.install.wf",
        "revision": 7,
        "display_name": "Old",
        "settings": {"execution_graph": {"entry": None}},
    }
    service = ProjectCommandServiceWorkflowBindingsMixin()
    service._list_workflow_choices = MagicMock(
        return_value=[{"workflow_id": "ext.install.wf", "display_name": "New"}]
    )
    service._ensure_extension_workflow_anchors(
        bl=MagicMock(db_store=store),
        workflow_ids=["ext.install.wf"],
        account_id="account",
        actor_id="actor",
        transaction_conn=None,
    )
    assert store.save_workflow.call_args.kwargs["expected_revision"] == 7


def test_clone_cannot_upsert_over_a_concurrent_create():
    db = RegistryDBStore("postgres://unused")
    db.get_workflow = MagicMock(
        side_effect=[{"workflow_id": "source", "steps": [], "groups": []}, None]
    )
    db.save_workflow = MagicMock()
    db.clone_workflow("source", "target", account_id="account", scope_kind="account")
    assert db.save_workflow.call_args.kwargs.get("create_only") is True


@pytest.mark.parametrize("concurrent_create", [False, True])
def test_admin_create_cannot_upsert_over_a_concurrent_create(monkeypatch, concurrent_create):
    db = MagicMock()
    db.get_workflow.return_value = None
    scope = MagicMock(account_id="account", actor_name="admin", project_id=None)
    portability = MagicMock()
    portability.public_create_payload.side_effect = lambda value: value
    body = writes.WorkflowSave(
        workflow_id="wf_cas", display_name="Concurrent create", enabled=False
    )
    monkeypatch.setattr(writes, "_get_db", lambda: db)
    monkeypatch.setattr(writes, "_request_scope", lambda request: scope)
    monkeypatch.setattr(writes, "require_default_registry_account", lambda scope: None)
    monkeypatch.setattr(writes, "_get_workflow_portability_service", lambda db: portability)
    monkeypatch.setattr(writes, "_enforce_project_scope", lambda *args: None)
    monkeypatch.setattr(
        writes, "_bind_workflow_to_project_if_requested", lambda *args, **kwargs: None
    )
    monkeypatch.setattr(
        writes,
        "_get_workflow_validation",
        lambda *args, **kwargs: {"valid": True, "errors": [], "warnings": []},
    )
    monkeypatch.setattr(writes, "_reload_registry_after_workflow_mutation", lambda **kwargs: [])
    if concurrent_create:
        db.save_workflow.side_effect = WorkflowCreateConflictError("wf_cas")
        http_request = MagicMock()
        with pytest.raises(writes.HTTPException) as error:
            writes.create_workflow(body, http_request)
        assert error.value.status_code == 409
    else:
        writes.create_workflow(body, MagicMock())
    assert db.save_workflow.call_args.kwargs.get("create_only") is True


def test_generic_registry_restore_refuses_a_workflow():
    # A workflow is restored by its definition service, which locks the live row:
    # a soft-deleted workflow is not found there and cannot be resurrected.
    db = RegistryDBStore("postgres://unused")
    db.apply_entity_snapshot = MagicMock(return_value={"workflow_id": "trashed"})
    with pytest.raises(ValueError, match="restore_workflow_snapshot"):
        db.restore_entity_revision("workflow", "trashed", entry_id="old")
    db.apply_entity_snapshot.assert_not_called()


def test_expected_revision_cannot_insert_a_missing_workflow(audit_db):
    store, conn, workflow_id, _project_id = audit_db
    with pytest.raises(WorkflowRevisionConflictError):
        store.save_workflow({"workflow_id": workflow_id, "enabled": False}, expected_revision=1)
    assert (
        conn.execute(
            "SELECT 1 FROM registry_workflows WHERE workflow_id=%s", (workflow_id,)
        ).fetchone()
        is None
    )


def test_expected_revision_cannot_resurrect_a_soft_deleted_workflow(audit_db):
    store, conn, workflow_id, _project_id = audit_db
    store.save_workflow({"workflow_id": workflow_id, "enabled": False})
    row = conn.execute(
        "UPDATE registry_workflows SET deleted_at=NOW() WHERE workflow_id=%s RETURNING revision",
        (workflow_id,),
    ).fetchone()
    with pytest.raises(WorkflowRevisionConflictError):
        store.save_workflow(
            {"workflow_id": workflow_id, "enabled": False}, expected_revision=row[0]
        )
    assert (
        conn.execute(
            "SELECT deleted_at IS NOT NULL FROM registry_workflows WHERE workflow_id=%s",
            (workflow_id,),
        ).fetchone()[0]
        is True
    )


@pytest.mark.parametrize("mode", ["production", "development"])
@pytest.mark.parametrize("target", ["new_hook", "retained_hook", "no_hook"])
def test_public_history_restore_cannot_introduce_new_live_hook_identity(monkeypatch, mode, target):
    from fastapi import HTTPException

    from core.application.management.scope import ScopeContext
    from core.application.workflows.workflow_portability import WorkflowPortabilityService
    from core.entrypoints.admin import workflows_routes_history as history
    from core.entrypoints.admin.workflows_models import RestoreRevisionRequest
    from tests._workflow_definitions import linear_workflow
    from tests.unit.test_workflow_hook_contract import declaration

    monkeypatch.setenv("ENVIRONMENT", mode)
    monkeypatch.setenv("FS_STACK_ENVIRONMENT", mode)
    hook = {
        "step_id": "before",
        "step_kind": "invoke_workflow",
        "override": {
            "invoke_workflow": {
                "hook": declaration(
                    input_schema={"type": "object", "properties": {}},
                    input_mapping={},
                    mutable_fields=[],
                    additional_outputs={},
                )
            }
        },
    }
    current = linear_workflow(
        *([hook] if target == "retained_hook" else []), workflow_id="wf", revision=7, enabled=False
    )
    snapshot = linear_workflow(
        *([hook] if target != "no_hook" else []), workflow_id="wf", revision=2, enabled=False
    )
    # Stored step rows carry explicit empty placement, including the generated Stop.
    for workflow in (current, snapshot):
        for step in workflow["steps"]:
            for placement in ("group_id", "for_each_id", "lane_id"):
                step.setdefault(placement, None)
    before = deepcopy(current)
    entry = {
        "id": 11,
        "action": "update",
        "entity_type": "workflow",
        "entity_id": "wf",
        "snapshot": snapshot,
    }
    db = RegistryDBStore("postgres://unused")
    db.get_workflow = lambda _id: deepcopy(current)
    db.get_changelog_entry = lambda *_args, **_kwargs: deepcopy(entry)
    cursor = MagicMock()
    cursor.fetchone.side_effect = lambda: {"revision": current["revision"]}
    cursor.fetchall.return_value = []
    locked = False

    @contextmanager
    def transaction():
        nonlocal locked
        assert not locked
        locked = True
        try:
            yield cursor
        finally:
            locked = False

    db._transaction = MagicMock(side_effect=transaction)
    writes_seen = []

    def save(payload, *, author, expected_revision):
        assert locked, "History restore must save while the current workflow row is locked"
        assert expected_revision == 7 == current["revision"]
        assert author == "admin"
        writes_seen.append(deepcopy(payload))
        current.update(deepcopy(payload))
        current["revision"] = 8
        return 8

    db.save_workflow = MagicMock(side_effect=save)
    actual_restore = db.restore_entity_revision
    db.restore_entity_revision = MagicMock(wraps=actual_restore)
    scope = ScopeContext(account_id="account", actor_id="admin", project_id=None)
    monkeypatch.setattr(history, "_get_db", lambda: db)
    monkeypatch.setattr(
        history, "_resolve_workflow_scope_target", lambda *_a, **_k: (deepcopy(current), scope)
    )
    monkeypatch.setattr(history, "_assert_workflow_mutation_allowed", lambda _row: None)
    monkeypatch.setattr(
        history,
        "_get_workflow_portability_service",
        lambda store: WorkflowPortabilityService(store),
    )
    monkeypatch.setattr(
        history,
        "_get_workflow_validation",
        lambda *_a: {"valid": True, "errors": [], "warnings": []},
    )
    monkeypatch.setattr(history, "_get_registry", MagicMock)
    body = RestoreRevisionRequest(entry_id="11", expected_revision=7)
    if mode == "production" and target == "new_hook":
        http_request = MagicMock()
        with pytest.raises(HTTPException) as raised:
            history.restore_workflow_revision("wf", body, http_request)
        assert raised.value.status_code == 403
        assert raised.value.detail["error"] == "workflow.hook.declaration_requires_development"
        assert any(
            issue.get("code") == "workflow.hook.declaration_requires_development"
            and issue.get("severity") == "error"
            and issue.get("path")
            for issue in raised.value.detail["errors"]
        )
        db.restore_entity_revision.assert_not_called()
        db._transaction.assert_not_called()
        db.save_workflow.assert_not_called()
        assert writes_seen == []
        assert current == before
    else:
        result = history.restore_workflow_revision("wf", body, MagicMock())
        assert result["workflow_id"] == "wf"
        assert len(writes_seen) == 1
        assert current["steps"] == snapshot["steps"]
        assert current["revision"] == 8
        db._transaction.assert_called_once_with()
        cursor.execute.assert_any_call(
            "SELECT revision FROM registry_workflows"
            " WHERE workflow_id = %s AND deleted_at IS NULL FOR UPDATE",
            ("wf",),
        )
        db.save_workflow.assert_called_once()
        assert db.save_workflow.call_args.kwargs == {"author": "admin", "expected_revision": 7}
        assert result["revision"] == 8
        assert result["snapshot"]["revision"] == 8
        assert result["snapshot"]["steps"] == snapshot["steps"]
        assert current["settings"]["execution_graph"] == snapshot["settings"]["execution_graph"]
        db.restore_entity_revision.assert_not_called()
        assert not locked


@pytest.mark.parametrize("adapter", ["workflow", "changelog"])
def test_public_restore_refuses_a_failed_declaration_snapshot_read(monkeypatch, adapter):
    from contextlib import nullcontext

    from fastapi import HTTPException

    from core.application.management.scope import ScopeContext
    from core.application.workflows.workflow_portability import WorkflowPortabilityService
    from core.entrypoints.admin import changelog
    from core.entrypoints.admin import workflows_routes_history as history
    from core.entrypoints.admin.workflows_models import RestoreRevisionRequest
    from tests._workflow_definitions import linear_workflow
    from tests.unit.test_workflow_hook_contract import declaration

    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("FS_STACK_ENVIRONMENT", "production")
    current = linear_workflow(workflow_id="wf", revision=7, enabled=False)
    snapshot = linear_workflow(
        {
            "step_id": "before",
            "step_kind": "invoke_workflow",
            "override": {"invoke_workflow": {"hook": declaration()}},
        },
        workflow_id="wf",
        revision=2,
        enabled=False,
    )
    before = deepcopy(current)
    entry = {
        "id": 11,
        "action": "update",
        "entity_type": "workflow",
        "entity_id": "wf",
        "snapshot": snapshot,
    }
    fault = {"failures": 0, "later_reads": 0}
    writes_seen, history_writes = [], []
    db = RegistryDBStore("postgres://unused")
    db.get_workflow = lambda _id, **_kwargs: deepcopy(current)

    def get_changelog_entry(*_args, **_kwargs):
        if not fault["failures"]:
            fault["failures"] += 1
            raise ConnectionError("one-shot declaration snapshot read failure")
        fault["later_reads"] += 1
        return deepcopy(entry)

    def save(payload, *, author, expected_revision):
        assert expected_revision == 7
        writes_seen.append(deepcopy(payload))
        history_writes.append({"author": author, "snapshot": deepcopy(payload)})
        current.update(deepcopy(payload))
        current["revision"] = 8
        return 8

    db.get_changelog_entry = MagicMock(side_effect=get_changelog_entry)
    db.save_workflow = MagicMock(side_effect=save)
    db._transaction = MagicMock(side_effect=lambda: nullcontext(MagicMock()))
    db.restore_entity_revision = MagicMock(wraps=db.restore_entity_revision)
    scope = ScopeContext(account_id="account", actor_id="admin", project_id=None)
    monkeypatch.setattr(history, "_get_db", lambda: db)
    monkeypatch.setattr(
        history, "_resolve_workflow_scope_target", lambda *_a, **_k: (deepcopy(current), scope)
    )
    monkeypatch.setattr(history, "_assert_workflow_mutation_allowed", lambda _row: None)
    monkeypatch.setattr(
        history,
        "_get_workflow_portability_service",
        lambda store: WorkflowPortabilityService(store),
    )
    monkeypatch.setattr(
        history,
        "_get_workflow_validation",
        lambda *_a: {"valid": True, "errors": [], "warnings": []},
    )
    monkeypatch.setattr(history, "_get_registry", MagicMock())
    if adapter == "changelog":
        monkeypatch.setattr(changelog, "_require_admin_request", lambda _request: None)
        monkeypatch.setattr(changelog, "_get_db", lambda: db)
        monkeypatch.setattr(changelog, "_request_scope", lambda _request: scope)
        monkeypatch.setattr(
            changelog, "_lookup_entity_history_snapshot", lambda *_a, **_k: deepcopy(snapshot)
        )
        monkeypatch.setattr(changelog, "_assert_entity_scope", lambda *_a, **_k: None)
        monkeypatch.setattr(
            changelog,
            "_get_workflow_portability_service",
            lambda store: WorkflowPortabilityService(store),
        )
        monkeypatch.setattr(
            changelog,
            "_validate_restored_entity",
            lambda *_a, **_k: {"valid": True, "errors": [], "warnings": []},
        )
        monkeypatch.setattr(changelog, "_get_registry", MagicMock())
    if adapter == "workflow":
        restore_revision = partial(
            history.restore_workflow_revision,
            "wf",
            RestoreRevisionRequest(entry_id="11", expected_revision=7),
            MagicMock(),
        )
    else:
        restore_revision = partial(
            changelog.restore_entity_revision,
            MagicMock(),
            "workflow",
            "wf",
            changelog.RestoreRevisionRequest(entry_id="11", expected_revision=7),
        )
    with pytest.raises(HTTPException) as raised:
        restore_revision()
    assert raised.value.status_code == 503
    assert fault["failures"] == 1
    assert db.get_changelog_entry("11", entity_type="workflow", entity_id="wf") == entry
    assert fault["later_reads"] == 1
    db.restore_entity_revision.assert_not_called()
    db._transaction.assert_not_called()
    db.save_workflow.assert_not_called()
    assert current == before
    assert writes_seen == []
    assert history_writes == []


@pytest.mark.parametrize("adapter", ["workflow", "changelog"])
def test_public_restore_binds_retained_hook_authority_to_expected_revision(
    audit_db, monkeypatch, adapter
):
    from fastapi import HTTPException

    from core.application.management.scope import ScopeContext
    from core.application.workflows.workflow_definition_service import apply_workflow_changes
    from core.application.workflows.workflow_portability import WorkflowPortabilityService
    from core.entrypoints.admin import changelog
    from core.entrypoints.admin import workflows_routes_history as history
    from core.entrypoints.admin.workflows_models import RestoreRevisionRequest
    from tests._workflow_definitions import linear_workflow
    from tests.unit.test_workflow_hook_contract import declaration

    db, _conn, workflow_id, _project_id = audit_db
    # Trusted historical fixture, not public Production declaration authoring.
    db.save_workflow(
        linear_workflow(
            {
                "step_id": "before",
                "step_kind": "invoke_workflow",
                "override": {"invoke_workflow": {"hook": declaration()}},
            },
            workflow_id=workflow_id,
            enabled=False,
        ),
        author="test_historical_hook_setup",
    )
    current = db.get_workflow(workflow_id)
    revision = int(current["revision"])
    entries_before = db.get_entity_history(entity_type="workflow", entity_id=workflow_id, limit=200)
    entry_id = str(entries_before[0]["id"])
    snapshot = deepcopy(
        db.get_changelog_entry(entry_id, entity_type="workflow", entity_id=workflow_id)["snapshot"]
    )
    scope = ScopeContext(
        account_id=str(current["account_id"]),
        project_id=None,
        actor_id="test_admin",
        principal_type="user",
    )
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("FS_STACK_ENVIRONMENT", "production")
    policy = WorkflowPortabilityService(db)
    authoritative_read = policy._authoritative_guard_record
    interleaved = []

    def read_then_delete(method_name, ref, **filters):
        row = authoritative_read(method_name, ref, **filters)
        if method_name == "get_workflow" and not interleaved:
            assert row["revision"] == revision
            assert any(step["step_id"] == "before" for step in row["steps"])
            # Deterministic scheduling point AFTER the guard copied r, BEFORE
            # it judges retained identities. The normal common writer performs
            # an authorized Production deletion, committing r+1 through real CAS.
            result = apply_workflow_changes(
                db,
                workflow_id,
                expected_revision=revision,
                operations=[
                    {"op": "delete_step", "step_id": "before"},
                    {"op": "set_entry", "target": {"kind": "step", "id": "done"}},
                ],
                author="test_concurrent_personal_delete",
            )
            assert result.applied
            assert result.revision == revision + 1
            deleted = db.get_workflow(workflow_id)
            assert int(deleted["revision"]) == revision + 1
            assert [step["step_id"] for step in deleted["steps"]] == ["done"]
            interleaved.append(deepcopy(deleted))
        return row

    monkeypatch.setattr(policy, "_authoritative_guard_record", read_then_delete)
    restore = MagicMock(wraps=db.restore_entity_revision)
    monkeypatch.setattr(db, "restore_entity_revision", restore)
    monkeypatch.setattr(history, "_get_db", lambda: db)
    monkeypatch.setattr(
        history, "_resolve_workflow_scope_target", lambda *_a, **_k: (deepcopy(current), scope)
    )
    monkeypatch.setattr(history, "_assert_workflow_mutation_allowed", lambda _row: None)
    monkeypatch.setattr(history, "_get_workflow_portability_service", lambda _db: policy)
    monkeypatch.setattr(
        history,
        "_get_workflow_validation",
        lambda *_a: {"valid": True, "errors": [], "warnings": []},
    )
    monkeypatch.setattr(history, "_get_registry", MagicMock())
    if adapter == "changelog":
        monkeypatch.setattr(changelog, "_require_admin_request", lambda _request: None)
        monkeypatch.setattr(changelog, "_get_db", lambda: db)
        monkeypatch.setattr(changelog, "_request_scope", lambda _request: scope)
        monkeypatch.setattr(
            changelog, "_lookup_entity_history_snapshot", lambda *_a, **_k: deepcopy(snapshot)
        )
        monkeypatch.setattr(changelog, "_assert_entity_scope", lambda *_a, **_k: None)
        monkeypatch.setattr(changelog, "_get_workflow_portability_service", lambda _db: policy)
        monkeypatch.setattr(
            changelog,
            "_validate_restored_entity",
            lambda *_a, **_k: {"valid": True, "errors": [], "warnings": []},
        )
        monkeypatch.setattr(changelog, "_get_registry", MagicMock())

    # Predict the successor revision: old policy uses r as its retention
    # authority while raw restore accepts the caller's independent r+1.
    if adapter == "workflow":
        restore_revision = partial(
            history.restore_workflow_revision,
            workflow_id,
            RestoreRevisionRequest(entry_id=entry_id, expected_revision=revision + 1),
            MagicMock(),
        )
    else:
        restore_revision = partial(
            changelog.restore_entity_revision,
            MagicMock(),
            "workflow",
            workflow_id,
            changelog.RestoreRevisionRequest(entry_id=entry_id, expected_revision=revision + 1),
        )
    with pytest.raises(HTTPException) as raised:
        restore_revision()
    assert raised.value.status_code == 409
    assert (
        raised.value.detail.get("code", raised.value.detail.get("error"))
        == "workflow_revision_conflict"
    )
    assert len(interleaved) == 1
    restore.assert_not_called()
    after = db.get_workflow(workflow_id)
    assert after == interleaved[0]
    assert int(after["revision"]) == revision + 1
    assert [step["step_id"] for step in after["steps"]] == ["done"]
    entries_after = db.get_entity_history(entity_type="workflow", entity_id=workflow_id, limit=200)
    assert len(entries_after) == len(entries_before) + 1
    assert not any(entry["action"] == "restore" for entry in entries_after)


@pytest.mark.parametrize("writer", ["restore", "edit_session"])
def test_whole_definition_write_answers_with_its_own_revision(audit_db, writer):
    """The answer is read in the write's transaction, not after a later writer committed."""
    from core.application.workflows.workflow_definition_service import restore_workflow_snapshot
    from tests.unit.test_workflow_history_backend import _entry, _save, _save_settings_only

    store, _conn, workflow_id, _project_id = audit_db
    _save(store, workflow_id, ("a",))
    _save(store, workflow_id, ("b",), expected=1)
    snapshot = _entry(store, workflow_id, 1)["snapshot"]
    other = RegistryDBStore(store.dsn)
    original = store._transaction
    state = {"depth": 0, "fired": False}

    @contextmanager
    def transaction_then_concurrent_save():
        state["depth"] += 1
        try:
            with original() as cur:
                yield cur
        finally:
            state["depth"] -= 1
        if state["depth"] == 0 and not state["fired"]:
            state["fired"] = True
            _save_settings_only(other, workflow_id, description="concurrent")

    store._transaction = transaction_then_concurrent_save
    try:
        if writer == "restore":
            result = restore_workflow_snapshot(
                store, workflow_id, snapshot=snapshot, expected_revision=2, author="t"
            )
        else:
            from core.application.workflows.workflow_definition_service import (
                replace_workflow_content,
            )
            from core.application.workflows.workflow_edit_session_contract import (
                authoring_content,
            )

            result = replace_workflow_content(
                store,
                workflow_id,
                content=authoring_content(snapshot),
                expected_revision=2,
                author="t",
            )
    finally:
        store._transaction = original

    assert state["fired"] is True
    assert result.revision == 3
    assert result.workflow["revision"] == 3
    assert result.workflow.get("description") != "concurrent"
    assert store.get_workflow(workflow_id)["revision"] == 4
