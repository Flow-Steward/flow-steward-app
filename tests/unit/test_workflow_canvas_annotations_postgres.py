"""Presentation writes preserve run identity and serialize against definition writers."""

import pytest

from core.application.workflows.workflow_canvas_annotation_service import (
    WorkflowCanvasAnnotationService,
)
from tests._workflow_definitions import linear_workflow
from tests.unit.test_workflow_canvas_annotations import NOTE, Plane
from tests.unit.test_workflow_definition_snapshot_repository import (
    _job,
    _pin,
)


def _save(store, workflow_id):
    store.save_workflow(
        linear_workflow(
            {"step_id": "work", "step_kind": "transform"},
            workflow_id=workflow_id,
            enabled=False,
            scope_kind="account",
            account_id="00000000-0000-0000-0000-000000000002",
        )
    )


def _annotation_save(store, workflow_id, *, annotations_revision=0, text="Documentation"):
    source = store.get_workflow(workflow_id)
    return WorkflowCanvasAnnotationService(store, Plane()).save_note(
        workflow_id=workflow_id,
        account_id=str(source["account_id"]),
        annotation={**NOTE, "text": text},
        expected_revision=source["revision"],
        expected_annotations_revision=annotations_revision,
    )


def test_note_save_keeps_runtime_revision_snapshot_and_definition_history(audit_db):
    store, conn, workflow_id, project_id = audit_db
    _save(store, workflow_id)
    before = _pin(conn, _job(conn, project_id), workflow_id)
    history_before = conn.execute(
        "SELECT count(*) FROM registry_changelog WHERE entity_type = 'workflow' AND entity_id = %s",
        (workflow_id,),
    ).fetchone()[0]
    result = _annotation_save(store, workflow_id)
    assert result["revision"] == 1
    assert result["annotations_revision"] == 1
    after = _pin(conn, _job(conn, project_id), workflow_id)
    assert after == before
    assert (
        conn.execute(
            "SELECT count(*) FROM registry_changelog WHERE entity_type = 'workflow' AND entity_id = %s",
            (workflow_id,),
        ).fetchone()[0]
        == history_before
    )


def test_full_definition_save_preserves_a_newer_note_saved_at_same_runtime_revision(audit_db):
    store, _, workflow_id, _ = audit_db
    _save(store, workflow_id)
    stale = store.get_workflow(workflow_id)
    _annotation_save(store, workflow_id)
    stale["description"] = "New description"
    store.save_workflow(stale, author="audit", expected_revision=1)
    actual = store.get_workflow(workflow_id)
    assert actual["description"] == "New description"
    assert actual["revision"] == 2
    assert actual["settings"]["canvas_annotations"] == [{**NOTE, "text": "Documentation"}]
    assert actual["settings"]["canvas_annotations_revision"] == 1


def test_competing_annotation_writer_is_rejected_without_lost_update(audit_db):
    store, _, workflow_id, _ = audit_db
    _save(store, workflow_id)
    _annotation_save(store, workflow_id)
    with pytest.raises(RuntimeError, match="revision"):
        _annotation_save(store, workflow_id, text="Stale")
    assert (
        store.get_workflow(workflow_id)["settings"]["canvas_annotations"][0]["text"]
        == "Documentation"
    )


def test_caller_supplied_extension_ownership_cannot_replace_existing_notes(audit_db):
    store, conn, workflow_id, _ = audit_db
    _save(store, workflow_id)
    _annotation_save(store, workflow_id)
    payload = store.get_workflow(workflow_id)
    install_id = workflow_id + "_install"
    conn.execute(
        "INSERT INTO extension_installs (extension_id, account_id, install_id) VALUES (%s, %s, %s)",
        ("fixture.notes-owner", payload["account_id"], install_id),
    )
    try:
        # A real install id meets the FK but does not grant the public writer
        # permission to replace presentation state through a definition payload.
        payload["source_install_id"] = install_id
        payload["settings"]["canvas_annotations"] = []
        payload["settings"]["canvas_annotations_revision"] = 0
        store.save_workflow(payload, expected_revision=1)
        actual = store.get_workflow(workflow_id)
        assert actual["settings"]["canvas_annotations_revision"] == 1
        assert actual["settings"]["canvas_annotations"] == [{**NOTE, "text": "Documentation"}]
    finally:
        conn.execute(
            "UPDATE registry_workflows SET source_install_id = NULL WHERE workflow_id = %s",
            (workflow_id,),
        )
        conn.execute("DELETE FROM extension_installs WHERE install_id = %s", (install_id,))


def test_clone_initializes_independent_presentation_revision(audit_db):
    store, conn, workflow_id, _ = audit_db
    _save(store, workflow_id)
    _annotation_save(store, workflow_id)
    child_id = workflow_id + "_clone"
    try:
        store.clone_workflow(
            workflow_id,
            child_id,
            account_id="00000000-0000-0000-0000-000000000002",
            scope_kind="account",
        )
        clone = store.get_workflow(child_id)
        assert clone["settings"]["canvas_annotations_revision"] == 0
        assert clone["settings"]["canvas_annotations"] == [{**NOTE, "text": "Documentation"}]
        assert store.get_workflow(workflow_id)["settings"]["canvas_annotations_revision"] == 1
    finally:
        conn.execute("DELETE FROM registry_workflows WHERE workflow_id = %s", (child_id,))
