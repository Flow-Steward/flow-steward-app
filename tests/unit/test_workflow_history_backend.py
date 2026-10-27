"""Workflow history against the canonical schema: full snapshots, stable numbers, safe restore."""

from __future__ import annotations

import json
from copy import deepcopy

import pytest

from core.application.auth.config import DEFAULT_ACCOUNT_ID
from core.application.registry.changelog_projection import redact_changelog_diff
from core.application.registry.db_store_revisions_mixin import WorkflowHistoryComparisonError
from core.application.registry.db_store_workflows_mixin import WorkflowRevisionConflictError
from core.application.workflows.workflow_change_authorization import ChangeReferenceAccess
from core.application.workflows.workflow_definition_service import (
    WorkflowDefinitionChangeError,
    apply_workflow_changes,
    definition_from_workflow,
    restore_workflow_snapshot,
)
from core.application.workflows.workflow_definition_validation import (
    validate_workflow_definition,
)
from core.application.workflows.workflow_history import (
    HISTORY_SCHEMA_KEY,
    SNAPSHOT_KIND_LOCAL,
    SNAPSHOT_KIND_PROJECTION,
    diff_workflow_snapshots,
)
from tests._workflow_definitions import edge, linear_graph, node
from tests.unit.test_workflow_definition_service import build_group_for_each_collect
from tests.unit.test_workflow_hook_binding_postgres import hooked as hooked


def _steps(*step_ids: str) -> list[dict]:
    steps = [{"step_id": step_id, "step_kind": "transform"} for step_id in step_ids]
    return [*steps, {"step_id": "done", "step_kind": "stop_success"}]


def _save(store, workflow_id: str, step_ids=("work",), *, expected=None, **fields) -> int:
    steps = _steps(*step_ids)
    settings = {
        **dict(fields.pop("settings", {}) or {}),
        "execution_graph": linear_graph([step["step_id"] for step in steps]),
    }
    return store.save_workflow(
        {
            "workflow_id": workflow_id,
            "display_name": fields.pop("display_name", workflow_id),
            "enabled": fields.pop("enabled", False),
            "settings": settings,
            "steps": steps,
            "groups": [],
            "for_each": [],
            **fields,
        },
        expected_revision=expected,
    )


def _save_settings_only(store, workflow_id: str, **changes) -> int:
    """What PUT /api/workflows/{id} sends: the row without its definition."""
    current = store.get_workflow(workflow_id)
    payload = {
        key: value
        for key, value in current.items()
        if key not in {"steps", "groups", "for_each", "id", "created_at", "updated_at"}
    }
    payload.update(changes)
    return store.save_workflow(payload, expected_revision=int(current["revision"]))


def _history(store, workflow_id: str) -> list[dict]:
    return store.get_entity_history("workflow", workflow_id, limit=200)


def _entry(store, workflow_id: str, revision: int) -> dict:
    entry = store.get_entity_revision("workflow", workflow_id, revision)
    assert entry is not None, revision
    return entry


def _restore(store, workflow_id: str, entry: dict, **kwargs):
    current = store.get_workflow(workflow_id)
    return restore_workflow_snapshot(
        store,
        workflow_id,
        snapshot=entry["snapshot"],
        expected_revision=kwargs.pop("expected_revision", int(current["revision"])),
        author=kwargs.pop("author", "tester"),
        **kwargs,
    )


def _step_ids(workflow: dict) -> list[str]:
    return [step["step_id"] for step in workflow["steps"]]


def _changelog_count(conn, workflow_id: str) -> int:
    return conn.execute(
        "SELECT count(*) FROM registry_changelog WHERE entity_type='workflow' AND entity_id=%s",
        (workflow_id,),
    ).fetchone()[0]


# --- Task 1: full snapshots -------------------------------------------------


def test_settings_only_save_records_the_stored_definition_and_its_revision(audit_db) -> None:
    store, _conn, workflow_id, _project_id = audit_db
    _save(store, workflow_id, ("a", "b"))
    revision = _save_settings_only(store, workflow_id, description="described")

    newest = _history(store, workflow_id)[0]
    snapshot = newest["snapshot"]
    assert revision == 2
    assert newest["revision"] == 2
    assert snapshot["revision"] == 2
    assert snapshot[HISTORY_SCHEMA_KEY] == 1
    assert newest["snapshot_kind"] == SNAPSHOT_KIND_LOCAL
    assert newest["restorable"] is True
    assert newest["restore_unavailable_reason"] is None
    assert _step_ids(snapshot) == ["a", "b", "done"]
    assert snapshot["groups"] == []
    assert snapshot["for_each"] == []
    assert snapshot["settings"]["execution_graph"]["entry"] == {"kind": "step", "id": "a"}
    assert snapshot["description"] == "described"


def test_restoring_a_settings_only_entry_brings_back_its_steps(audit_db) -> None:
    store, _conn, workflow_id, _project_id = audit_db
    _save(store, workflow_id, ("a", "b"))
    _save_settings_only(store, workflow_id, description="kept")
    _save(store, workflow_id, ("c",), expected=2, description="later")

    result = _restore(store, workflow_id, _entry(store, workflow_id, 2))

    restored = store.get_workflow(workflow_id)
    assert result.revision == 4 == restored["revision"]
    assert _step_ids(restored) == ["a", "b", "done"]
    assert restored["description"] == "kept"
    newest = _history(store, workflow_id)[0]
    assert newest["revision"] == 4
    assert newest["author"] == "tester"


def test_settings_only_save_diff_shows_only_the_setting(audit_db) -> None:
    store, _conn, workflow_id, _project_id = audit_db
    _save(store, workflow_id, ("a", "b"))
    _save_settings_only(store, workflow_id, description="described")

    diff = store.diff_entity_history(
        "workflow",
        workflow_id,
        from_entry_id=str(_entry(store, workflow_id, 1)["id"]),
        to_entry_id=str(_entry(store, workflow_id, 2)["id"]),
    )

    assert [(c["path"], c["change"]) for c in diff["changes"]] == [("description", "changed")]


def test_group_for_each_definition_survives_a_restore_round_trip(audit_db) -> None:
    store, _conn, workflow_id, _project_id = audit_db
    store.save_workflow(
        {
            "workflow_id": workflow_id,
            "display_name": workflow_id,
            "enabled": False,
            "settings": {},
            "steps": [],
            "groups": [],
            "for_each": [],
        }
    )
    apply_workflow_changes(
        store,
        workflow_id,
        expected_revision=1,
        operations=build_group_for_each_collect(),
        author="audit",
    )
    built = store.get_workflow(workflow_id)
    _save(store, workflow_id, ("other",), expected=2)

    _restore(store, workflow_id, _entry(store, workflow_id, 2))

    restored = store.get_workflow(workflow_id)
    assert definition_from_workflow(restored) == definition_from_workflow(built)


def test_builder_metadata_backfill_adds_one_revision_and_is_idempotent(audit_db) -> None:
    store, conn, workflow_id, _project_id = audit_db
    _save(store, workflow_id)
    before = _changelog_count(conn, workflow_id)

    assert store.set_entity_builder_metadata(
        "workflow", workflow_id, {"purpose": "Sync orders"}, author="system", only_if_empty=True
    )
    assert not store.set_entity_builder_metadata(
        "workflow", workflow_id, {"purpose": "Sync orders"}, author="system", only_if_empty=True
    )

    stored = store.get_workflow(workflow_id)
    assert stored["revision"] == 2
    assert _changelog_count(conn, workflow_id) == before + 1
    newest = _history(store, workflow_id)[0]
    assert newest["revision"] == 2
    assert newest["snapshot"]["builder_metadata_jsonb"]["purpose"] == "Sync orders"
    assert _step_ids(newest["snapshot"]) == ["work", "done"]


def test_builder_metadata_backfill_never_overwrites_a_concurrent_author_save(audit_db) -> None:
    store, _conn, workflow_id, _project_id = audit_db
    _save(store, workflow_id)
    stale = store.get_workflow(workflow_id)
    _save_settings_only(store, workflow_id, description="author")

    assert not store._save_workflow_builder_metadata(stale, {"purpose": "seed"}, author="system")
    assert store.get_workflow(workflow_id)["description"] == "author"


# --- Task 2: stable numbers -------------------------------------------------


def test_revision_number_names_the_same_entry_after_saves_toggle_and_cleanup(audit_db) -> None:
    store, conn, workflow_id, _project_id = audit_db
    _save(store, workflow_id, ("a",))
    _save(store, workflow_id, ("b",), expected=1)
    first = _entry(store, workflow_id, 2)["id"]
    store.toggle_workflow(workflow_id)
    _save(store, workflow_id, ("c",), expected=3, enabled=True)
    assert _entry(store, workflow_id, 2)["id"] == first
    assert store.get_entity_revision("workflow", workflow_id, 3) is None  # toggle: no content

    conn.execute(
        "DELETE FROM registry_changelog WHERE id = %s", (_entry(store, workflow_id, 1)["id"],)
    )

    assert store.get_entity_revision("workflow", workflow_id, 1) is None
    assert _entry(store, workflow_id, 2)["id"] == first
    assert _step_ids(_entry(store, workflow_id, 4)["snapshot"]) == ["c", "done"]


def _insert_legacy_entry(conn, workflow_id: str, snapshot: dict) -> str:
    return str(
        conn.execute(
            "INSERT INTO registry_changelog (entity_type, entity_id, action, snapshot, author)"
            " VALUES ('workflow', %s, 'update', %s::jsonb, 'legacy') RETURNING id",
            (workflow_id, json.dumps(snapshot)),
        ).fetchone()[0]
    )


def test_legacy_entries_have_no_trusted_number(audit_db) -> None:
    store, conn, workflow_id, _project_id = audit_db
    _save(store, workflow_id)
    full = {**store.get_workflow(workflow_id), "revision": 1}
    full.pop(HISTORY_SCHEMA_KEY, None)
    _insert_legacy_entry(conn, workflow_id, json.loads(json.dumps(full, default=str)))
    _insert_legacy_entry(conn, workflow_id, {"workflow_id": workflow_id, "revision": 1})

    legacy = [entry for entry in _history(store, workflow_id) if entry["author"] == "legacy"]
    assert [entry["revision"] for entry in legacy] == [None, None]
    by_restorable = sorted(entry["restorable"] for entry in legacy)
    assert by_restorable == [False, True]
    assert _entry(store, workflow_id, 1)["author"] != "legacy"


def test_diff_reads_named_entries_older_than_the_list_window(audit_db) -> None:
    store, conn, workflow_id, _project_id = audit_db
    _save(store, workflow_id, ("a",))
    oldest = str(_entry(store, workflow_id, 1)["id"])
    conn.execute(
        "INSERT INTO registry_changelog (entity_type, entity_id, action, snapshot, author)"
        " SELECT 'workflow', %s, 'update', '{}'::jsonb, 'filler' FROM generate_series(1, 205)",
        (workflow_id,),
    )
    _save(store, workflow_id, ("b",), expected=1)

    diff = store.diff_entity_history(
        "workflow",
        workflow_id,
        from_entry_id=oldest,
        to_entry_id=str(_entry(store, workflow_id, 2)["id"]),
    )

    assert {change["path"] for change in diff["changes"]} >= {'steps["a"].step_kind'}


def test_another_workflows_entry_is_not_found(audit_db) -> None:
    store, conn, workflow_id, _project_id = audit_db
    other = f"{workflow_id}_other"
    _save(store, workflow_id)
    _save(store, other)
    try:
        foreign = str(_entry(store, other, 1)["id"])
        with pytest.raises(KeyError):
            store.diff_entity_history(
                "workflow", workflow_id, from_entry_id=foreign, to_entry_id=foreign
            )
        assert (
            store.get_changelog_entry(foreign, entity_type="workflow", entity_id=workflow_id)
            is None
        )
    finally:
        conn.execute("DELETE FROM registry_workflows WHERE workflow_id = %s", (other,))


def test_incomplete_or_projection_entries_are_not_compared(audit_db) -> None:
    store, conn, workflow_id, _project_id = audit_db
    _save(store, workflow_id)
    legacy = _insert_legacy_entry(conn, workflow_id, {"workflow_id": workflow_id})

    first = str(_entry(store, workflow_id, 1)["id"])
    with pytest.raises(WorkflowHistoryComparisonError):
        store.diff_entity_history("workflow", workflow_id, from_entry_id=legacy, to_entry_id=first)


# --- Task 3: restore --------------------------------------------------------


def test_restore_keeps_enabled_provenance_and_availability_current(audit_db) -> None:
    store, conn, workflow_id, _project_id = audit_db
    # The old revision was enabled; restoring it must not switch the workflow on.
    _save(store, workflow_id, ("a",), enabled=True, display_name="Old name")
    _save(store, workflow_id, ("b",), expected=1, enabled=False, display_name="New name")
    conn.execute(
        "UPDATE registry_workflows SET availability_status='unavailable',"
        " availability_reason_code='extension_missing', new_runs_blocked=true,"
        " visible_in_catalog=false WHERE workflow_id=%s",
        (workflow_id,),
    )

    _restore(store, workflow_id, _entry(store, workflow_id, 1))

    restored = store.get_workflow(workflow_id)
    assert _step_ids(restored) == ["a", "done"]
    assert restored["display_name"] == "Old name"
    assert restored["enabled"] is False
    assert restored["availability_status"] == "unavailable"
    assert restored["availability_reason_code"] == "extension_missing"
    assert restored["new_runs_blocked"] is True
    assert restored["visible_in_catalog"] is False
    assert str(restored["account_id"]) == str(DEFAULT_ACCOUNT_ID)


#: Stand-ins for stored secret reference keys, not credentials.
NEW_SECRET_REF = "ref-new"  # pragma: allowlist secret
OLD_SECRET_REF = "ref-old"  # pragma: allowlist secret


def _inputs(*rows: dict) -> dict:
    return {"callable": {"enabled": False, "expected_inputs": list(rows)}}


@pytest.mark.parametrize(
    ("current_inputs", "expected"),
    [
        # Replaced since: the current reference is kept.
        (
            [
                {"name": "login", "type": "secret", "default_secret_ref": NEW_SECRET_REF},
                {"name": "sample", "type": "file", "sample_file": {"artifact_id": "file-new"}},
            ],
            {"login": NEW_SECRET_REF, "sample": "file-new"},
        ),
        # Cleared since: nothing comes back.
        (
            [{"name": "login", "type": "secret"}, {"name": "sample", "type": "file"}],
            {"login": None, "sample": None},
        ),
        # Field removed since: nothing comes back.
        ([], {"login": None, "sample": None}),
        # Type changed since: the old reference does not come back with the old type.
        (
            [{"name": "login", "type": "string"}, {"name": "sample", "type": "string"}],
            {"login": None, "sample": None},
        ),
    ],
)
def test_restore_never_brings_back_old_secret_or_sample_file_references(
    audit_db, current_inputs, expected
) -> None:
    store, _conn, workflow_id, _project_id = audit_db
    old = _inputs(
        {"name": "login", "type": "secret", "default_secret_ref": OLD_SECRET_REF},
        {"name": "sample", "type": "file", "sample_file": {"artifact_id": "file-old"}},
    )
    _save(store, workflow_id, ("a",), settings=old)
    _save(store, workflow_id, ("b",), expected=1, settings=_inputs(*current_inputs))

    _restore(store, workflow_id, _entry(store, workflow_id, 1))

    rows = {
        row["name"]: row
        for row in store.get_workflow(workflow_id)["settings"]["callable"]["expected_inputs"]
    }
    assert rows["login"]["type"] == "secret"
    assert rows["sample"]["type"] == "file"
    assert rows["login"].get("default_secret_ref") == expected["login"]
    assert (rows["sample"].get("sample_file") or {}).get("artifact_id") == expected["sample"]
    assert OLD_SECRET_REF not in json.dumps(store.get_workflow(workflow_id)["settings"])


def test_incomplete_entry_is_refused_without_any_write(audit_db) -> None:
    store, conn, workflow_id, _project_id = audit_db
    _save(store, workflow_id)
    legacy = _insert_legacy_entry(conn, workflow_id, {"workflow_id": workflow_id, "steps": []})
    before = (_changelog_count(conn, workflow_id), store.get_workflow(workflow_id)["revision"])

    entry = store.get_changelog_entry(legacy, entity_type="workflow", entity_id=workflow_id)
    assert entry["restorable"] is False
    with pytest.raises(WorkflowDefinitionChangeError) as raised:
        _restore(store, workflow_id, entry)

    assert raised.value.issues[0]["code"] == "workflow.history.snapshot_incomplete"
    after = (_changelog_count(conn, workflow_id), store.get_workflow(workflow_id)["revision"])
    assert after == before


def test_complete_legacy_entry_is_restorable_by_its_entry_id(audit_db) -> None:
    store, conn, workflow_id, _project_id = audit_db
    _save(store, workflow_id, ("a",))
    full = json.loads(json.dumps(store.get_workflow(workflow_id), default=str))
    full.pop(HISTORY_SCHEMA_KEY, None)
    legacy = _insert_legacy_entry(conn, workflow_id, full)
    _save(store, workflow_id, ("b",), expected=1)

    entry = store.get_changelog_entry(legacy, entity_type="workflow", entity_id=workflow_id)
    _restore(store, workflow_id, entry)

    assert _step_ids(store.get_workflow(workflow_id)) == ["a", "done"]


def test_stale_expected_revision_is_a_conflict_without_any_write(audit_db) -> None:
    store, conn, workflow_id, _project_id = audit_db
    _save(store, workflow_id, ("a",))
    _save(store, workflow_id, ("b",), expected=1)
    before = _changelog_count(conn, workflow_id)

    entry = _entry(store, workflow_id, 1)
    with pytest.raises(WorkflowRevisionConflictError):
        _restore(store, workflow_id, entry, expected_revision=1)

    assert _changelog_count(conn, workflow_id) == before
    assert _step_ids(store.get_workflow(workflow_id)) == ["b", "done"]


def _unrouted_entry(store, conn, workflow_id: str) -> dict:
    """A full entry whose graph routes nothing: a draft, not a runnable definition."""
    snapshot = json.loads(json.dumps(store.get_workflow(workflow_id), default=str))
    snapshot.pop(HISTORY_SCHEMA_KEY, None)
    snapshot["settings"].pop("execution_graph", None)
    legacy = _insert_legacy_entry(conn, workflow_id, snapshot)
    return store.get_changelog_entry(legacy, entity_type="workflow", entity_id=workflow_id)


def test_disabled_workflow_restores_an_incomplete_definition_and_reports_it(audit_db) -> None:
    store, conn, workflow_id, _project_id = audit_db
    _save(store, workflow_id, ("a",))
    entry = _unrouted_entry(store, conn, workflow_id)

    result = _restore(store, workflow_id, entry)

    assert result.issues
    assert result.blocking_issues == []
    restored = store.get_workflow(workflow_id)
    assert restored["revision"] == 2
    assert restored["enabled"] is False
    assert restored["settings"]["execution_graph"] == {
        "schema_version": 1,
        "entry": None,
        "edges": [],
    }


def test_enabled_workflow_refuses_an_incomplete_definition(audit_db) -> None:
    store, conn, workflow_id, _project_id = audit_db
    _save(store, workflow_id, ("a",), enabled=True)
    entry = _unrouted_entry(store, conn, workflow_id)
    before = _changelog_count(conn, workflow_id)

    with pytest.raises(WorkflowDefinitionChangeError):
        _restore(store, workflow_id, entry)

    assert _changelog_count(conn, workflow_id) == before
    assert store.get_workflow(workflow_id)["revision"] == 1


@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize(
    ("change", "code"),
    [
        (lambda s: s.update(phases=[{"phase_id": "p"}]), "workflow.history.phases_removed"),
        (
            lambda s: s["steps"].insert(0, {"step_id": "loop", "step_kind": "for_each"}),
            "workflow.step.for_each_inline_removed",
        ),
        (
            lambda s: s["groups"].append({"group_id": "g", "kind": "loop", "loop": {}}),
            "workflow.group.loop_removed",
        ),
    ],
)
def test_removed_formats_are_refused_in_both_states(audit_db, enabled, change, code) -> None:
    store, conn, workflow_id, _project_id = audit_db
    _save(store, workflow_id, ("a",), enabled=enabled)
    snapshot = json.loads(json.dumps(store.get_workflow(workflow_id), default=str))
    change(snapshot)
    legacy = _insert_legacy_entry(conn, workflow_id, snapshot)
    entry = store.get_changelog_entry(legacy, entity_type="workflow", entity_id=workflow_id)
    assert entry["restorable"] is False

    with pytest.raises(WorkflowDefinitionChangeError) as raised:
        _restore(store, workflow_id, entry)

    assert code in {issue["code"] for issue in raised.value.issues}
    assert store.get_workflow(workflow_id)["revision"] == 1


def test_restore_keeps_canvas_notes_current(audit_db) -> None:
    store, _conn, workflow_id, _project_id = audit_db
    _save(store, workflow_id, ("a",))
    _save(store, workflow_id, ("b",), expected=1)
    notes = [{"id": "note-1", "text": "current", "x": 0, "y": 0}]
    store.save_workflow_canvas_annotations(
        workflow_id,
        account_id=str(DEFAULT_ACCOUNT_ID),
        annotations=notes,
        expected_revision=2,
        expected_annotations_revision=0,
    )

    _restore(store, workflow_id, _entry(store, workflow_id, 1))

    settings = store.get_workflow(workflow_id)["settings"]
    assert settings["canvas_annotations"] == notes
    assert settings["canvas_annotations_revision"] == 1


def test_restore_refuses_a_reference_the_workflow_may_no_longer_use(audit_db) -> None:
    store, _conn, workflow_id, _project_id = audit_db
    call = {
        "step_id": "call",
        "step_kind": "invoke_workflow",
        "override": {"invoke_workflow": {"workflow_id": f"{workflow_id}_missing_child"}},
    }
    store.save_workflow(
        {
            "workflow_id": workflow_id,
            "display_name": workflow_id,
            "enabled": False,
            "settings": {"execution_graph": linear_graph(["call"])},
            "steps": [call],
            "groups": [],
            "for_each": [],
        }
    )
    _save(store, workflow_id, ("b",), expected=1)
    denied = ChangeReferenceAccess(
        agent=lambda _id: False,
        workflow=lambda _id: False,
        connection=lambda _id: False,
        install=lambda _id: False,
    )

    entry = _entry(store, workflow_id, 1)
    with pytest.raises(WorkflowDefinitionChangeError) as raised:
        _restore(store, workflow_id, entry, reference_access=denied)

    assert raised.value.issues[0]["path"] == "steps.call.override.invoke_workflow.workflow_id"
    assert store.get_workflow(workflow_id)["revision"] == 2


def test_a_soft_deleted_workflow_is_not_restored(audit_db) -> None:
    store, conn, workflow_id, _project_id = audit_db
    _save(store, workflow_id, ("a",))
    entry = _entry(store, workflow_id, 1)
    conn.execute(
        "UPDATE registry_workflows SET deleted_at = now() WHERE workflow_id = %s", (workflow_id,)
    )

    with pytest.raises(LookupError):
        restore_workflow_snapshot(
            store, workflow_id, snapshot=entry["snapshot"], expected_revision=1, author="t"
        )


def test_projection_anchor_history_references_the_projection_and_is_never_restored(
    audit_db,
) -> None:
    store, _conn, workflow_id, _project_id = audit_db
    store.save_workflow(
        {
            "workflow_id": workflow_id,
            "display_name": "Anchor",
            "enabled": False,
            "projection_mode": "derived",
            "settings": {},
            "steps": [],
            "groups": [],
            "for_each": [],
        }
    )
    _save_settings_only(store, workflow_id, description="described")

    newest = _history(store, workflow_id)[0]
    assert newest["snapshot_kind"] == SNAPSHOT_KIND_PROJECTION
    assert newest["revision"] == 2
    assert newest["restorable"] is False
    assert "steps" not in newest["snapshot"]
    first = _entry(store, workflow_id, 1)
    with pytest.raises(WorkflowDefinitionChangeError) as raised:
        _restore(store, workflow_id, first)
    assert raised.value.issues[0]["code"] == "workflow.history.projection_reference"
    assert store.get_workflow(workflow_id)["revision"] == 2


def test_restoring_a_child_that_breaks_its_enabled_caller_is_refused(hooked) -> None:
    from core.application.workflows.workflow_consumer_guard import (
        WorkflowConsumerConflictError,
    )
    from tests.unit.test_workflow_hook_binding_postgres import request

    ctx = hooked
    store = ctx["store"]
    child = store.get_workflow(ctx["child_id"])
    narrow = json.loads(json.dumps(child["settings"]))
    narrow["callable"]["expected_outputs"] = []
    store.save_workflow({**child, "settings": narrow}, expected_revision=int(child["revision"]))
    narrow_revision = int(child["revision"]) + 1
    store.save_workflow(
        {**store.get_workflow(ctx["child_id"]), "settings": child["settings"]},
        expected_revision=narrow_revision,
    )
    ctx["service"].save(request(ctx))
    ctx["conn"].execute(
        "UPDATE registry_workflows SET enabled = true WHERE workflow_id = %s", (ctx["owner_id"],)
    )
    current = store.get_workflow(ctx["child_id"])

    narrow_entry = _entry(store, ctx["child_id"], narrow_revision)
    with pytest.raises(WorkflowConsumerConflictError):
        _restore(store, ctx["child_id"], narrow_entry)

    assert store.get_workflow(ctx["child_id"])["revision"] == current["revision"]


# --- Task 4: diff by business id ---------------------------------------------


def _snap(*steps: dict, edges: list | None = None, **fields) -> dict:
    return {
        "workflow_id": "wf",
        "history_schema_version": 1,
        "revision": 1,
        "settings": {
            "execution_graph": {
                "schema_version": 1,
                "entry": node(steps[0]["step_id"]) if steps else None,
                "edges": list(edges or []),
            }
        },
        "steps": [dict(step) for step in steps],
        "groups": fields.pop("groups", []),
        "for_each": fields.pop("for_each", []),
        **fields,
    }


def _step(step_id: str, **fields) -> dict:
    return {"step_id": step_id, "step_kind": "transform", **fields}


def _paths(changes: list[dict]) -> list[tuple[str, str]]:
    return [(change["path"], change["change"]) for change in changes]


_GRAPH_EDGES = "settings.execution_graph.edges["
ROOT = ["root", ""]


def _edge_path(*identity) -> str:
    """The diff path of the edge whose ``edge_identity`` is ``identity``."""
    key = json.dumps(list(identity), ensure_ascii=False, separators=(",", ":"))
    return f"{_GRAPH_EDGES}{json.dumps(key, ensure_ascii=False)}]"


def _edge_identity(path: str) -> list:
    """The ``edge_identity`` components an edge path names, read back exactly."""
    assert path.startswith(_GRAPH_EDGES), path
    key, _end = json.JSONDecoder().raw_decode(path, len(_GRAPH_EDGES))
    return json.loads(key)


def test_inserting_a_step_first_is_one_addition() -> None:
    older = _snap(_step("b"), _step("c"))
    newer = _snap(_step("a"), _step("b"), _step("c"))
    newer["settings"]["execution_graph"]["entry"] = node("b")

    changes = diff_workflow_snapshots(older, newer)

    assert {path for path, kind in _paths(changes) if kind != "added"} == set()
    assert {path.split(".")[0] for path, _kind in _paths(changes)} == {'steps["a"]'}


def test_a_parameter_change_is_addressed_by_step_id() -> None:
    older = _snap(_step("a", override={"url": "https://old"}), _step("b"))
    newer = _snap(_step("a", override={"url": "https://new"}), _step("b"))

    assert _paths(diff_workflow_snapshots(older, newer)) == [('steps["a"].override.url', "changed")]


def test_rename_removal_and_membership_move_are_addressed_by_id() -> None:
    older = _snap(_step("a", display_name="A"), _step("b"), _step("gone"))
    newer = _snap(_step("a", display_name="Renamed"), _step("b", lane_id="lane-1"))

    assert set(_paths(diff_workflow_snapshots(older, newer))) == {
        ('steps["a"].display_name', "changed"),
        ('steps["b"].lane_id', "changed"),
        ('steps["gone"].step_kind', "removed"),
    }


def test_a_reorder_is_one_order_change_not_a_replacement() -> None:
    older = _snap(_step("a"), _step("b"))
    newer = _snap(_step("b"), _step("a"))
    newer["settings"]["execution_graph"]["entry"] = node("a")

    assert _paths(diff_workflow_snapshots(older, newer)) == [("steps.order", "changed")]


def _edges_snap(edges: list, **fields) -> dict:
    return _snap(_step("a"), _step("b"), _step("c"), _step("d"), edges=edges, **fields)


def test_reordered_edges_are_not_a_change() -> None:
    first, second = edge("a", node("b")), edge("b", node("c"))

    assert diff_workflow_snapshots(_edges_snap([first, second]), _edges_snap([second, first])) == []


def test_an_edge_inserted_in_the_middle_is_one_addition() -> None:
    first, last = edge("a", node("b")), edge("c", node("d"))
    added = edge("b", node("c"))

    paths = _paths(
        diff_workflow_snapshots(_edges_snap([first, last]), _edges_snap([first, added, last]))
    )

    assert paths
    assert all(kind == "added" for _path, kind in paths)
    assert all(_edge_identity(path) == [ROOT, "step", "b", "success", None] for path, _ in paths)


def test_a_new_target_changes_the_same_edge() -> None:
    older = _edges_snap([edge("a", node("b"))])
    newer = _edges_snap([edge("a", node("c"), rework=False)])

    assert _paths(diff_workflow_snapshots(older, newer)) == [
        (_edge_path(ROOT, "step", "a", "success", None) + ".target.id", "changed")
    ]


def test_a_new_case_id_is_another_edge() -> None:
    older = _edges_snap([edge("a", node("b"), "case", case_id="eu")])
    newer = _edges_snap([edge("a", node("b"), "case", case_id="us")])

    kinds = {
        (json.dumps(_edge_identity(path)), kind)
        for path, kind in _paths(diff_workflow_snapshots(older, newer))
    }

    assert kinds == {
        (json.dumps([ROOT, "step", "a", "case", "eu"]), "removed"),
        (json.dumps([ROOT, "step", "a", "case", "us"]), "added"),
    }


def test_edge_keys_carry_the_group_and_for_each_lane_scope() -> None:
    groups = [
        {
            "group_id": "g",
            "body": {"kind": "lanes", "lanes": [{"lane_id": "l1", "entry": node("in_lane")}]},
        }
    ]
    for_each = [
        {
            "for_each_id": "fe",
            "over": "state.items",
            "body": {"kind": "sequence", "entry": node("per_item")},
        }
    ]
    steps = [
        _step("in_lane", group_id="g", lane_id="l1"),
        _step("lane_next", group_id="g", lane_id="l1"),
        _step("per_item", for_each_id="fe"),
        _step("per_item_next", for_each_id="fe"),
    ]
    newer = _snap(
        *steps,
        groups=groups,
        for_each=for_each,
        edges=[edge("in_lane", node("lane_next")), edge("per_item", node("per_item_next"))],
    )
    older = _snap(*steps, groups=groups, for_each=for_each)

    keys = {
        json.dumps(_edge_identity(path))
        for path, _kind in _paths(diff_workflow_snapshots(older, newer))
    }

    assert keys == {
        json.dumps([["group", "g", "l1"], "step", "in_lane", "success", None]),
        json.dumps([["for_each", "fe", None], "step", "per_item", "success", None]),
    }


def test_an_unfinished_graph_without_entry_still_compares_its_edges() -> None:
    older = _edges_snap([edge("a", node("b"))])
    newer = _edges_snap([edge("a", node("c"))])
    for snapshot in (older, newer):
        snapshot["settings"]["execution_graph"]["entry"] = None

    assert _paths(diff_workflow_snapshots(older, newer)) == [
        (_edge_path(ROOT, "step", "a", "success", None) + ".target.id", "changed")
    ]


def _scoped_snap(
    step: str = "s",
    group: str = "g",
    lane: str = "l",
    for_each: str = "fe",
    case: str = "c",
    **fields,
) -> dict:
    """A valid graph with an edge in every scope: root, a Group lane, a For each body."""
    return _snap(
        {
            "step_id": step,
            "step_kind": "switch",
            "override": {
                "switch": {"selector": "state.kind", "cases": [{"case_id": case, "when": "yes"}]}
            },
        },
        _step("x", group_id=group, lane_id=lane),
        _step("y", for_each_id=for_each),
        {"step_id": "done", "step_kind": "stop_success"},
        groups=[
            {
                "group_id": group,
                "body": {"kind": "lanes", "lanes": [{"lane_id": lane, "entry": node("x")}]},
            }
        ],
        for_each=[
            {
                "for_each_id": for_each,
                "over": "state.items",
                "item_name": "item",
                "body": {"kind": "sequence", "entry": node("y")},
            }
        ],
        edges=[
            edge(step, node(group, "group"), "case", case_id=case),
            edge(step, node("done"), "default"),
            edge(group, node(for_each, "for_each"), source_kind="group"),
            edge(for_each, node("done"), source_kind="for_each"),
            edge("x", {"kind": "group_exit", "group_id": group, "lane_id": lane}),
            edge("y", {"kind": "for_each_exit", "for_each_id": for_each, "lane_id": None}),
        ],
        **fields,
    )


_DELIMITED_IDS = ["a:b", "a/b", "a%2Fb", 'a"b', "a\\b", "ä✓/:"]


@pytest.mark.parametrize("value", _DELIMITED_IDS)
@pytest.mark.parametrize("position", ["step", "group", "lane", "for_each", "case"])
def test_edge_identity_paths_preserve_component_boundaries(position: str, value: str) -> None:
    snapshot = _scoped_snap(**{position: value})
    ids = {"step": "s", "group": "g", "lane": "l", "for_each": "fe", "case": "c", position: value}
    assert validate_workflow_definition(definition_from_workflow(snapshot)) == []
    without_edges = deepcopy(snapshot)
    without_edges["settings"]["execution_graph"]["edges"] = []

    added = _paths(diff_workflow_snapshots(without_edges, snapshot))
    identities = {json.dumps(_edge_identity(path)) for path, _kind in added}

    assert {kind for _path, kind in added} == {"added"}
    assert identities == {
        json.dumps(identity)
        for identity in (
            [ROOT, "step", ids["step"], "case", ids["case"]],
            [ROOT, "step", ids["step"], "default", None],
            [ROOT, "group", ids["group"], "success", None],
            [ROOT, "for_each", ids["for_each"], "success", None],
            [["group", ids["group"], ids["lane"]], "step", "x", "success", None],
            [["for_each", ids["for_each"], None], "step", "y", "success", None],
        )
    }
    # The same edge keeps its path across versions: a reorder plus an
    # unrelated change is only that change.
    newer = deepcopy(snapshot)
    newer["description"] = "changed"
    newer["settings"]["execution_graph"]["edges"].reverse()
    assert _paths(diff_workflow_snapshots(snapshot, newer)) == [("description", "added")]


def test_delimiters_inside_ids_do_not_merge_distinct_edges() -> None:
    """``a`` → case ``success`` and ``a:case`` → ``success`` are two edges, not one."""
    older = _snap(
        {
            "step_id": "a",
            "step_kind": "switch",
            "override": {
                "switch": {"selector": "state.kind", "cases": [{"case_id": "success", "when": "y"}]}
            },
        },
        _step("a:case"),
        {"step_id": "done", "step_kind": "stop_success"},
        edges=[
            edge("a", node("a:case"), "case", case_id="success"),
            edge("a", node("done"), "default"),
            edge("a:case", node("done")),
        ],
    )
    assert validate_workflow_definition(definition_from_workflow(older)) == []
    newer = deepcopy(older)
    newer["settings"]["execution_graph"]["edges"][2]["rework"] = True

    assert _paths(diff_workflow_snapshots(older, newer)) == [
        (_edge_path(ROOT, "step", "a:case", "success", None) + ".rework", "changed")
    ]


def test_diff_accepts_distinct_edge_identities_with_delimiters(audit_db) -> None:
    store, _conn, workflow_id, _project_id = audit_db
    payload = {
        "workflow_id": workflow_id,
        "enabled": False,
        "settings": {
            "execution_graph": {
                "schema_version": 1,
                "entry": node("a"),
                "edges": [
                    edge("a", node("a:case"), "case", case_id="success"),
                    edge("a", node("done"), "default"),
                    edge("a:case", node("done")),
                ],
            }
        },
        "steps": [
            {
                "step_id": "a",
                "step_kind": "switch",
                "override": {
                    "switch": {
                        "selector": "state.kind",
                        "cases": [{"case_id": "success", "when": "yes"}],
                    }
                },
            },
            {"step_id": "a:case", "step_kind": "transform"},
            {"step_id": "done", "step_kind": "stop_success"},
        ],
        "groups": [],
        "for_each": [],
    }
    assert validate_workflow_definition(definition_from_workflow(payload)) == []
    store.save_workflow(payload)
    current = store.get_workflow(workflow_id)
    assert [step["step_id"] for step in current["steps"]] == ["a", "a:case", "done"]
    store.save_workflow({**current, "description": "changed"}, expected_revision=1)
    history = store.get_entity_history("workflow", workflow_id)

    diff = store.diff_entity_history(
        "workflow",
        workflow_id,
        from_entry_id=str(history[1]["id"]),
        to_entry_id=str(history[0]["id"]),
    )

    assert [change["path"] for change in diff["changes"]] == ["description"]


@pytest.mark.parametrize(
    "edges",
    [
        [{"source": {"kind": "step"}, "outcome": {"name": "success"}, "target": node("b")}],
        [edge("a", node("b")), edge("a", node("c"))],
        [edge("ghost", node("b"))],
    ],
    ids=["malformed", "duplicate", "unknown-source"],
)
def test_edges_that_cannot_be_identified_are_a_comparison_limit(edges) -> None:
    from core.application.workflows.workflow_history import WorkflowHistoryComparisonError

    older, newer = _edges_snap([]), _edges_snap(edges)
    with pytest.raises(WorkflowHistoryComparisonError):
        diff_workflow_snapshots(older, newer)


def test_groups_and_for_each_are_matched_by_id() -> None:
    older = _snap(
        _step("a"),
        groups=[{"group_id": "g1", "display_name": "One"}],
        for_each=[{"for_each_id": "fe", "over": "state.items"}],
    )
    newer = _snap(
        _step("a"),
        groups=[
            {"group_id": "g0", "display_name": "Zero"},
            {"group_id": "g1", "display_name": "One"},
        ],
        for_each=[{"for_each_id": "fe", "over": "state.orders"}],
    )

    paths = set(_paths(diff_workflow_snapshots(older, newer)))

    assert ('for_each["fe"].over', "changed") in paths
    assert ('groups["g0"].display_name', "added") in paths
    assert not any(path.startswith('groups["g1"]') for path, _kind in paths)


def test_ids_with_dots_and_quotes_stay_unambiguous() -> None:
    older = _snap(_step('a.b"c', display_name="x"), _step("display"))
    newer = _snap(_step('a.b"c', display_name="y"), _step("display"))

    assert _paths(diff_workflow_snapshots(older, newer)) == [
        ('steps["a.b\\"c"].display_name', "changed")
    ]


def test_bookkeeping_and_current_fields_are_not_content() -> None:
    older = _snap(_step("a"), id="row-1", revision=1, enabled=False, new_runs_blocked=False)
    newer = _snap(_step("a"), id="row-2", revision=7, enabled=True, new_runs_blocked=True)
    newer["created_at"] = "2026-10-20"
    newer["settings"]["canvas_annotations"] = [{"id": "n"}]

    assert diff_workflow_snapshots(older, newer) == []


def test_secret_values_in_step_paths_are_redacted() -> None:
    older = _snap(_step("a.b", override={"headers": {"password": "old-pass"}}))
    newer = _snap(_step("a.b", override={"headers": {"password": "new-pass"}}))
    payload = {"changes": diff_workflow_snapshots(older, newer)}

    projected = json.dumps(redact_changelog_diff(payload))

    assert "old-pass" not in projected
    assert "new-pass" not in projected
