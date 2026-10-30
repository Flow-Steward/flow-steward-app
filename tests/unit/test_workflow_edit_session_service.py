"""Edit session compare and "keep my version" against the canonical schema (Task 6).

The browser keeps the session; the server compares a copy and writes it only
on the author's explicit choice, through the one definition writer, guarded by
the revision the author reviewed.
"""

from __future__ import annotations

import copy
import json
from contextlib import contextmanager

import pytest

from core.application.auth.config import DEFAULT_ACCOUNT_ID
from core.application.registry.db_store_workflows_mixin import WorkflowRevisionConflictError
from core.application.workflows.workflow_change_authorization import ChangeReferenceAccess
from core.application.workflows.workflow_definition_service import WorkflowDefinitionChangeError
from core.application.workflows.workflow_edit_session_contract import authoring_content
from core.application.workflows.workflow_edit_session_service import (
    apply_workflow_content,
    compare_workflow_session,
)
from tests._workflow_definitions import linear_graph
from tests.unit.test_workflow_history_backend import (
    NEW_SECRET_REF,
    _changelog_count,
    _history,
    _inputs,
    _save,
    _save_settings_only,
    _step_ids,
)
from tests.unit.test_workflow_hook_binding_postgres import hooked as hooked


def _content(store, workflow_id: str) -> dict:
    """What the builder holds: the public authoring content of the stored workflow."""
    return json.loads(json.dumps(authoring_content(store.get_workflow(workflow_id)), default=str))


def _stored_state(store, conn, workflow_id: str) -> tuple:
    """The stored workflow and its history length, to prove nothing was written."""
    return store.get_workflow(workflow_id), _changelog_count(conn, workflow_id)


def _paths(comparison: dict) -> set[str]:
    return {change["path"] for change in comparison["changes"]}


def _apply(store, workflow_id: str, content: dict, expected: int, **kwargs):
    return apply_workflow_content(
        store,
        workflow_id,
        content=content,
        expected_revision=expected,
        author=kwargs.pop("author", "author-b"),
        **kwargs,
    )


def _renamed(content: dict, step_id: str, name: str) -> dict:
    changed = copy.deepcopy(content)
    for step in changed["steps"]:
        if step["step_id"] == step_id:
            step["display_name"] = name
    return changed


def test_compare_is_three_way_and_read_only(audit_db) -> None:
    store, conn, workflow_id, _project_id = audit_db
    _save(store, workflow_id, ("a", "b"))
    base = _content(store, workflow_id)
    # Someone else saves revision 2: a new description.
    _save_settings_only(store, workflow_id, description="theirs")
    # My local copy renames step a.
    local = _renamed(base, "a", "Mine")
    before = _stored_state(store, conn, workflow_id)

    result = compare_workflow_session(store, workflow_id, base_content=base, local_content=local)

    assert result["current_revision"] == 2
    assert result["current_content"]["description"] == "theirs"
    assert _paths(result["base_to_local"]) == {'steps["a"].display_name'}
    assert _paths(result["base_to_server"]) == {"description"}
    assert _paths(result["local_to_server"]) == {'steps["a"].display_name', "description"}
    assert result["base_to_local"]["summary"] == {
        "total": 1,
        "added": 0,
        "removed": 0,
        "changed": 1,
    }
    assert _stored_state(store, conn, workflow_id) == before


def test_save_mine_uses_reviewed_revision(audit_db) -> None:
    store, conn, workflow_id, _project_id = audit_db
    _save(store, workflow_id, ("a", "b"), enabled=True)
    base = _content(store, workflow_id)
    _save_settings_only(store, workflow_id, description="theirs")
    local = _renamed(base, "a", "Mine")
    reviewed = compare_workflow_session(store, workflow_id, base_content=base, local_content=local)
    history_before = _changelog_count(conn, workflow_id)

    result = _apply(store, workflow_id, local, reviewed["current_revision"])

    stored = store.get_workflow(workflow_id)
    assert result.revision == 3 == stored["revision"]
    assert result.workflow["revision"] == 3
    assert _changelog_count(conn, workflow_id) == history_before + 1
    assert _history(store, workflow_id)[0]["author"] == "author-b"
    # The author's copy replaces what the other save changed; enabled stays current.
    assert stored["description"] == base.get("description")
    assert [step["display_name"] for step in stored["steps"] if step["step_id"] == "a"] == ["Mine"]
    assert stored["enabled"] is True
    assert _step_ids(stored) == [step["step_id"] for step in local["steps"]]


def test_new_server_revision_invalidates_overwrite(audit_db) -> None:
    store, conn, workflow_id, _project_id = audit_db
    _save(store, workflow_id, ("a",))
    base = _content(store, workflow_id)
    _save_settings_only(store, workflow_id, description="13")
    reviewed = compare_workflow_session(
        store, workflow_id, base_content=base, local_content=_renamed(base, "a", "Mine")
    )
    _save_settings_only(store, workflow_id, description="14")
    before = _stored_state(store, conn, workflow_id)

    mine = _renamed(base, "a", "Mine")
    with pytest.raises(WorkflowRevisionConflictError):
        _apply(store, workflow_id, mine, reviewed["current_revision"])

    assert _stored_state(store, conn, workflow_id) == before
    assert store.get_workflow(workflow_id)["revision"] == 3


def test_conflicting_session_keeps_the_server_revision(audit_db) -> None:
    """Two sessions start at revision 1; the first writes 2, the second gets a conflict."""
    store, conn, workflow_id, _project_id = audit_db
    _save(store, workflow_id, ("a",))
    base = _content(store, workflow_id)
    first = _apply(store, workflow_id, _renamed(base, "a", "First"), 1, author="author-a")
    assert first.revision == 2

    second = _renamed(base, "a", "Second")
    with pytest.raises(WorkflowRevisionConflictError):
        _apply(store, workflow_id, second, 1)

    stored = store.get_workflow(workflow_id)
    assert stored["revision"] == 2
    assert [step["display_name"] for step in stored["steps"] if step["step_id"] == "a"] == ["First"]
    assert _changelog_count(conn, workflow_id) == 2


def test_dry_run_judges_without_writing(audit_db) -> None:
    store, conn, workflow_id, _project_id = audit_db
    _save(store, workflow_id, ("a",))
    before = _stored_state(store, conn, workflow_id)

    result = _apply(
        store, workflow_id, _renamed(_content(store, workflow_id), "a", "Mine"), 1, dry_run=True
    )

    assert result.applied is False
    assert result.revision == 1
    assert _stored_state(store, conn, workflow_id) == before


def _unrouted(content: dict) -> dict:
    changed = copy.deepcopy(content)
    changed["settings"].pop("execution_graph", None)
    return changed


def test_enabled_workflow_refuses_an_incomplete_copy(audit_db) -> None:
    store, conn, workflow_id, _project_id = audit_db
    _save(store, workflow_id, ("a",), enabled=True)
    before = _stored_state(store, conn, workflow_id)

    unrouted = _unrouted(_content(store, workflow_id))
    with pytest.raises(WorkflowDefinitionChangeError):
        _apply(store, workflow_id, unrouted, 1)

    assert _stored_state(store, conn, workflow_id) == before


def test_disabled_workflow_keeps_an_incomplete_copy_and_reports_it(audit_db) -> None:
    store, _conn, workflow_id, _project_id = audit_db
    _save(store, workflow_id, ("a",))

    result = _apply(store, workflow_id, _unrouted(_content(store, workflow_id)), 1)

    assert result.revision == 2
    assert result.issues
    assert result.blocking_issues == []
    assert store.get_workflow(workflow_id)["enabled"] is False


@pytest.mark.parametrize(
    ("change", "code"),
    [
        (lambda c: c.update(enabled=True), "workflow.edit_session.field_unknown"),
        (lambda c: c.update(workflow_id="other"), "workflow.edit_session.field_unknown"),
        (lambda c: c.update(phases=[{"phase_id": "p"}]), "workflow.edit_session.field_unknown"),
        (lambda c: c.update(source_install_id="x"), "workflow.edit_session.field_unknown"),
        (
            lambda c: c["settings"].update(canvas_annotations=[]),
            "workflow.edit_session.service_owned",
        ),
        (
            lambda c: c["settings"].update(
                _inputs({"name": "login", "type": "secret", "default_secret_ref": NEW_SECRET_REF})
            ),
            "workflow.edit_session.authority_field",
        ),
        (
            lambda c: c["settings"].update(
                _inputs({"name": "f", "type": "file", "sample_file": {"artifact_id": "x"}})
            ),
            "workflow.edit_session.authority_field",
        ),
        (lambda c: c.update(steps=None), "workflow.edit_session.field_invalid"),
        (
            lambda c: c["steps"].append({"step_kind": "agent"}),
            "workflow.edit_session.node_ids_invalid",
        ),
        (
            lambda c: c["settings"].update(execution_graph={"schema_version": 0, "edges": []}),
            "workflow.edit_session.graph_invalid",
        ),
    ],
)
def test_content_outside_the_authoring_contract_is_refused_without_any_write(
    audit_db, change, code
) -> None:
    store, conn, workflow_id, _project_id = audit_db
    _save(store, workflow_id, ("a",))
    content = _content(store, workflow_id)
    change(content)
    before = _stored_state(store, conn, workflow_id)

    with pytest.raises(WorkflowDefinitionChangeError) as raised:
        _apply(store, workflow_id, content, 1)

    assert code in {issue["code"] for issue in raised.value.issues}
    assert _stored_state(store, conn, workflow_id) == before


@pytest.mark.parametrize(
    ("change", "code"),
    [
        (
            lambda c: c["steps"].insert(0, {"step_id": "loop", "step_kind": "for_each"}),
            "workflow.step.for_each_inline_removed",
        ),
        (
            lambda c: c["groups"].append({"group_id": "g", "kind": "loop", "loop": {}}),
            "workflow.group.loop_removed",
        ),
    ],
)
def test_removed_formats_are_refused(audit_db, change, code) -> None:
    store, _conn, workflow_id, _project_id = audit_db
    _save(store, workflow_id, ("a",))
    content = _content(store, workflow_id)
    change(content)

    with pytest.raises(WorkflowDefinitionChangeError) as raised:
        _apply(store, workflow_id, content, 1)

    assert code in {issue["code"] for issue in raised.value.issues}
    assert store.get_workflow(workflow_id)["revision"] == 1


def test_current_secret_and_sample_file_references_stay(audit_db) -> None:
    store, _conn, workflow_id, _project_id = audit_db
    _save(
        store,
        workflow_id,
        ("a",),
        settings=_inputs(
            {"name": "login", "type": "secret", "default_secret_ref": NEW_SECRET_REF},
            {"name": "sample", "type": "file", "sample_file": {"artifact_id": "file-now"}},
        ),
    )
    content = _content(store, workflow_id)
    assert NEW_SECRET_REF not in json.dumps(content)

    _apply(store, workflow_id, _renamed(content, "a", "Mine"), 1)

    rows = {
        row["name"]: row
        for row in store.get_workflow(workflow_id)["settings"]["callable"]["expected_inputs"]
    }
    assert rows["login"]["default_secret_ref"] == NEW_SECRET_REF
    assert rows["sample"]["sample_file"]["artifact_id"] == "file-now"


def test_canvas_notes_and_provenance_stay_current(audit_db) -> None:
    store, conn, workflow_id, _project_id = audit_db
    _save(store, workflow_id, ("a",))
    notes = [{"id": "note-1", "text": "current", "x": 0, "y": 0}]
    store.save_workflow_canvas_annotations(
        workflow_id,
        account_id=str(DEFAULT_ACCOUNT_ID),
        annotations=notes,
        expected_revision=1,
        expected_annotations_revision=0,
    )
    conn.execute(
        "UPDATE registry_workflows SET availability_status='unavailable',"
        " new_runs_blocked=true WHERE workflow_id=%s",
        (workflow_id,),
    )
    content = _content(store, workflow_id)
    assert "canvas_annotations" not in content["settings"]

    _apply(store, workflow_id, _renamed(content, "a", "Mine"), 1)

    stored = store.get_workflow(workflow_id)
    assert stored["settings"]["canvas_annotations"] == notes
    assert stored["settings"]["canvas_annotations_revision"] == 1
    assert stored["availability_status"] == "unavailable"
    assert stored["new_runs_blocked"] is True


def test_a_reference_the_author_may_not_use_is_refused(audit_db) -> None:
    store, _conn, workflow_id, _project_id = audit_db
    _save(store, workflow_id, ("a",))
    content = _content(store, workflow_id)
    content["steps"].insert(
        0,
        {
            "step_id": "call",
            "step_kind": "invoke_workflow",
            "override": {"invoke_workflow": {"workflow_id": f"{workflow_id}_other"}},
        },
    )
    content["settings"]["execution_graph"] = linear_graph(["call", "a", "done"])
    denied = ChangeReferenceAccess(
        agent=lambda _id: False,
        workflow=lambda _id: False,
        connection=lambda _id: False,
        install=lambda _id: False,
    )

    with pytest.raises(WorkflowDefinitionChangeError) as raised:
        _apply(store, workflow_id, content, 1, reference_access=denied)

    assert raised.value.issues[0]["path"] == "steps.call.override.invoke_workflow.workflow_id"
    assert store.get_workflow(workflow_id)["revision"] == 1


def test_a_projection_anchor_is_never_replaced(audit_db) -> None:
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

    content = _content(store, workflow_id)
    with pytest.raises(WorkflowDefinitionChangeError) as raised:
        _apply(store, workflow_id, content, 1)

    assert raised.value.issues[0]["code"] == "workflow.history.projection_reference"
    assert store.get_workflow(workflow_id)["revision"] == 1


def test_a_copy_that_breaks_an_enabled_caller_is_refused(hooked) -> None:
    from core.application.workflows.workflow_consumer_guard import WorkflowConsumerConflictError
    from tests.unit.test_workflow_hook_binding_postgres import request

    ctx = hooked
    store = ctx["store"]
    ctx["service"].save(request(ctx))
    ctx["conn"].execute(
        "UPDATE registry_workflows SET enabled = true WHERE workflow_id = %s", (ctx["owner_id"],)
    )
    current = store.get_workflow(ctx["child_id"])
    content = json.loads(json.dumps(authoring_content(current), default=str))
    content["settings"]["callable"]["expected_outputs"] = []

    with pytest.raises(WorkflowConsumerConflictError):
        _apply(store, ctx["child_id"], content, int(current["revision"]))

    assert store.get_workflow(ctx["child_id"])["revision"] == current["revision"]


def test_apply_ack_matches_its_committed_revision(audit_db) -> None:
    """A write right after the commit does not replace the answer's workflow."""
    from core.application.registry.db_store import RegistryDBStore

    store, _conn, workflow_id, _project_id = audit_db
    _save(store, workflow_id, ("a",))
    content = _renamed(_content(store, workflow_id), "a", "Mine")
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
        # Only after the outermost transaction committed: another writer saves.
        if state["depth"] == 0 and not state["fired"]:
            state["fired"] = True
            _save_settings_only(other, workflow_id, description="concurrent")

    store._transaction = transaction_then_concurrent_save
    try:
        result = _apply(store, workflow_id, content, 1)
    finally:
        store._transaction = original

    assert state["fired"] is True
    assert result.revision == 2
    assert result.workflow["revision"] == 2
    assert result.workflow.get("description") != "concurrent"
    assert store.get_workflow(workflow_id)["revision"] == 3
