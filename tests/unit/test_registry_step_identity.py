from __future__ import annotations

import os
import uuid
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import psycopg
import pytest
from psycopg.conninfo import conninfo_to_dict

from core.application.registry.db_store import RegistryDBStore
from core.application.registry.db_store_workflows_mixin import WorkflowRevisionConflictError
from tests._xdist_isolation import audit_database_name


@pytest.fixture
def workflow_fixture():
    # Writes are allowed only against lint.sh's own disposable, canonical audit
    # database. Never fall back to a developer or product database.
    assert os.environ.get("FS_AUDIT_ISOLATED") == "1"
    dsn = os.environ["FS_DB_DSN"]
    info = conninfo_to_dict(dsn)
    assert (info.get("host"), info.get("dbname"), info.get("user")) == (
        "pg-audit",
        audit_database_name(),
        "audit",
    )
    workflow_id = f"unit_step_identity_{uuid.uuid4().hex}"
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO registry_workflows (workflow_key, display_name, enabled) VALUES (%s,%s,false)",
            (workflow_id, "Disposable step identity regression"),
        )
        try:
            yield RegistryDBStore(dsn), conn, workflow_id
        finally:
            conn.execute("DELETE FROM registry_workflows WHERE workflow_id = %s", (workflow_id,))


def _step(step_id: str, **extra) -> dict:
    return {"step_kind": "set_variable", "step_id": step_id, "display_name": "Before", **extra}


def _loop(group_id: str, entry: str) -> dict:
    return {
        "group_id": group_id,
        "body": {"kind": "sequence", "entry": {"kind": "step", "id": entry}},
    }


def _definition(workflow_id: str, steps: list, groups: list | None = None) -> dict:
    return {
        "workflow_id": workflow_id,
        "display_name": workflow_id,
        "enabled": False,
        "steps": steps,
        "groups": groups or [],
        "for_each": [],
    }


def _revision(conn, workflow_id: str) -> int:
    return conn.execute(
        "SELECT revision FROM registry_workflows WHERE workflow_id=%s", (workflow_id,)
    ).fetchone()[0]


def _step_rows(conn, workflow_id: str) -> dict[str, tuple]:
    rows = conn.execute(
        """SELECT s.step_id, s.id::text, g.group_id, s.lane_id
             FROM registry_workflow_steps s
             LEFT JOIN registry_workflow_groups g ON g.id = s.group_id
            WHERE s.owner_workflow_id = %s""",
        (workflow_id,),
    ).fetchall()
    return {row[0]: tuple(row[1:]) for row in rows}


def test_resave_keeps_step_row_uuid_and_loads_business_group_ids(workflow_fixture):
    db, conn, workflow_id = workflow_fixture
    db.save_workflow(
        _definition(workflow_id, [_step("a"), _step("l1", group_id="g")], [_loop("g", "l1")])
    )
    first = _step_rows(conn, workflow_id)
    db.save_workflow(
        _definition(
            workflow_id,
            [_step("a", display_name="After"), _step("l1", group_id="g")],
            [_loop("g", "l1")],
        )
    )
    second = _step_rows(conn, workflow_id)
    assert first["a"][0] == second["a"][0]
    loaded = db.get_workflow(workflow_id)
    steps = {step["step_id"]: step for step in loaded["steps"]}
    assert steps["l1"]["group_id"] == "g"
    assert steps["a"]["group_id"] is None
    assert steps["a"]["display_name"] == "After"
    assert [group["group_id"] for group in loaded["groups"]] == ["g"]


def test_step_moved_out_of_a_removed_group_survives(workflow_fixture):
    db, conn, workflow_id = workflow_fixture
    db.save_workflow(_definition(workflow_id, [_step("l1", group_id="g")], [_loop("g", "l1")]))
    row_id = _step_rows(conn, workflow_id)["l1"][0]
    db.save_workflow(_definition(workflow_id, [_step("l1")], []))
    assert _step_rows(conn, workflow_id)["l1"] == (row_id, None, None)
    groups = conn.execute(
        "SELECT count(*) FROM registry_workflow_groups WHERE workflow_id=%s", (workflow_id,)
    ).fetchone()[0]
    assert groups == 0


def test_duplicate_step_id_is_refused_without_a_partial_write(workflow_fixture):
    db, conn, workflow_id = workflow_fixture
    db.save_workflow(_definition(workflow_id, [_step("a")]))
    before = _revision(conn, workflow_id)
    duplicate = _definition(workflow_id, [_step("b"), _step("b")])
    with pytest.raises(ValueError, match="used twice"):
        db.save_workflow(duplicate)
    assert set(_step_rows(conn, workflow_id)) == {"a"}
    assert _revision(conn, workflow_id) == before


def test_phase_payload_is_refused(workflow_fixture):
    db, _conn, workflow_id = workflow_fixture
    with pytest.raises(ValueError, match="phase-based"):
        db.save_workflow({"workflow_id": workflow_id, "display_name": "x", "phases": []})


def test_lane_requires_a_group_and_a_group_belongs_to_its_workflow(workflow_fixture):
    _db, conn, workflow_id = workflow_fixture
    with pytest.raises(psycopg.errors.CheckViolation):
        conn.execute(
            "INSERT INTO registry_workflow_steps (owner_workflow_id, step_id, lane_id)"
            " VALUES (%s, 'x', 'left')",
            (workflow_id,),
        )
    other_id = f"unit_step_identity_other_{uuid.uuid4().hex}"
    conn.execute(
        "INSERT INTO registry_workflows (workflow_key, display_name, enabled) VALUES (%s,%s,false)",
        (other_id, "other"),
    )
    try:
        foreign_group = conn.execute(
            """INSERT INTO registry_workflow_groups (workflow_id, group_id, body_jsonb)
               VALUES (%s, 'g', '{}') RETURNING id""",
            (other_id,),
        ).fetchone()[0]
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            conn.execute(
                "INSERT INTO registry_workflow_steps (owner_workflow_id, step_id, group_id)"
                " VALUES (%s, 'y', %s)",
                (workflow_id, foreign_group),
            )
    finally:
        conn.execute("DELETE FROM registry_workflows WHERE workflow_id = %s", (other_id,))


def test_concurrent_saves_with_one_revision_have_one_winner(workflow_fixture):
    db, conn, workflow_id = workflow_fixture
    db.save_workflow(_definition(workflow_id, [_step("a")]))
    revision = _revision(conn, workflow_id)
    barrier = Barrier(2)

    def write(index: int) -> bool:
        writer = RegistryDBStore(db.dsn)
        barrier.wait(timeout=10)
        try:
            writer.save_workflow(
                _definition(workflow_id, [_step("a"), _step(f"winner_{index}")]),
                expected_revision=revision,
            )
        except WorkflowRevisionConflictError:
            return False
        return True

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(write, range(2)))
    assert sorted(results) == [False, True]
    assert len(_step_rows(conn, workflow_id)) == 2
    assert _revision(conn, workflow_id) == revision + 1
