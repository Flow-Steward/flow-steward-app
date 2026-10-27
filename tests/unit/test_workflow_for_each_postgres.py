"""Group → For each definitions round-trip through the canonical PostgreSQL schema."""

from __future__ import annotations

import psycopg
import pytest

from core.application.workflows.workflow_definition_service import (
    apply_workflow_changes,
    definition_from_workflow,
)
from core.application.workflows.workflow_definition_validation import validate_workflow_definition
from tests.unit.test_workflow_definition_service import build_group_for_each_collect


def _create_empty(store, workflow_id: str) -> None:
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


def test_group_for_each_definition_roundtrip(audit_db) -> None:
    store, _conn, workflow_id, _project_id = audit_db
    _create_empty(store, workflow_id)
    result = apply_workflow_changes(
        store,
        workflow_id,
        expected_revision=1,
        operations=build_group_for_each_collect(),
        author="audit",
    )
    assert result.issues == []

    stored = store.get_workflow(workflow_id)
    definition = definition_from_workflow(stored)
    assert validate_workflow_definition(definition) == []
    assert definition["groups"][0]["body"] == {
        "kind": "sequence",
        "entry": {"kind": "step", "id": "prepare"},
    }
    (item,) = definition["for_each"]
    assert (item["for_each_id"], item["group_id"], item["over"]) == ("fe", "g", "state.orders")
    assert (item["item_name"], item["index_name"]) == ("order", "order_index")
    assert item["body"] == {"kind": "sequence", "entry": {"kind": "step", "id": "check"}}
    owners = {s["step_id"]: (s["group_id"], s["for_each_id"]) for s in definition["steps"]}
    assert owners == {
        "prepare": ("g", None),
        "check": (None, "fe"),
        "keep": (None, "fe"),
        "skip": (None, "fe"),
        "done": (None, None),
    }
    graph = definition["execution_graph"]
    assert graph["entry"] == {"kind": "group", "id": "g"}
    assert {
        "source": {"kind": "for_each", "id": "fe"},
        "outcome": {"name": "success"},
        "target": {"kind": "group_exit", "group_id": "g", "lane_id": None},
        "rework": False,
    } in graph["edges"]


def test_the_schema_refuses_two_owners_and_a_foreign_for_each(audit_db) -> None:
    store, conn, workflow_id, _project_id = audit_db
    _create_empty(store, workflow_id)
    apply_workflow_changes(
        store,
        workflow_id,
        expected_revision=1,
        operations=build_group_for_each_collect(),
        author="audit",
    )
    with pytest.raises(psycopg.errors.CheckViolation), conn.transaction():
        conn.execute(
            """UPDATE registry_workflow_steps s
                      SET group_id = (SELECT id FROM registry_workflow_groups
                                       WHERE workflow_id = %s AND group_id = 'g')
                    WHERE s.owner_workflow_id = %s AND s.step_id = 'keep'""",
            (workflow_id, workflow_id),
        )
    other = workflow_id + "_other"
    _create_empty(store, other)
    try:
        with pytest.raises(psycopg.errors.ForeignKeyViolation), conn.transaction():
            # A step of another workflow cannot name this workflow's For each row.
            conn.execute(
                """INSERT INTO registry_workflow_steps (owner_workflow_id, step_id, for_each_id)
                       SELECT %s, 'intruder', id FROM registry_workflow_for_each
                        WHERE workflow_id = %s AND for_each_id = 'fe'""",
                (other, workflow_id),
            )
    finally:
        conn.execute("DELETE FROM registry_workflows WHERE workflow_id = %s", (other,))


def test_deleting_the_group_cascades_to_its_for_each(audit_db) -> None:
    store, conn, workflow_id, _project_id = audit_db
    _create_empty(store, workflow_id)
    apply_workflow_changes(
        store,
        workflow_id,
        expected_revision=1,
        operations=build_group_for_each_collect(),
        author="audit",
    )
    apply_workflow_changes(
        store,
        workflow_id,
        expected_revision=2,
        operations=[{"op": "delete_group", "group_id": "g", "with_contents": True}],
        author="audit",
    )
    remaining = conn.execute(
        "SELECT count(*) FROM registry_workflow_for_each WHERE workflow_id = %s", (workflow_id,)
    ).fetchone()[0]
    assert remaining == 0
    assert [s["step_id"] for s in store.get_workflow(workflow_id)["steps"]] == ["done"]


def test_a_save_without_for_each_is_refused(audit_db) -> None:
    store, _conn, workflow_id, _project_id = audit_db
    with pytest.raises(ValueError, match="'steps', 'groups' and 'for_each' together"):
        store.save_workflow(
            {"workflow_id": workflow_id, "display_name": "x", "steps": [], "groups": []}
        )
