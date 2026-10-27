"""Child approval propagation against a real PostgreSQL job lineage.

The ancestry is read from committed job rows only: the child's
``invoke_parent_job_id`` and the parent's ``invoke_children`` link written in
the creating transaction. Each waiting ancestor gets one projection; a cancel
request on the link or a decided approval stops the walk.
"""

from __future__ import annotations

import json
import uuid
from types import SimpleNamespace

import pytest

from core.application.jobs.invoke_child_link_repository import (
    link_child_in_transaction,
    mark_child,
)
from core.application.workflows.approval_waiting_propagation import (
    pending_child_approval_event,
    propagate_approval_waiting,
)

pytestmark = pytest.mark.integration


@pytest.fixture
def db(disposable_db):
    import psycopg

    from core.infrastructure.db.platform_stores import PlatformWorkerStore

    conn = psycopg.connect(disposable_db.dsn)
    logger = PlatformWorkerStore(disposable_db.dsn)
    try:
        yield SimpleNamespace(conn=conn, logger=logger)
    finally:
        conn.close()
        closer = getattr(logger, "close", None)
        if callable(closer):
            closer()


def _project(conn) -> str:
    project_id = str(uuid.uuid4())
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO projects (id, name, description) VALUES (%s, %s, '')",
            (project_id, f"approval-propagation-{project_id[:8]}"),
        )
    conn.commit()
    return project_id


def _job(conn, project_id: str, *, status: str, meta: dict) -> str:
    job_id = str(uuid.uuid4())
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO jobs (id, project_id, task_id, thread_id, status, meta, started_at)
            VALUES (%s, %s, %s, %s, %s, %s::jsonb, NOW())
            """,
            (
                job_id,
                project_id,
                f"task-{job_id[:8]}",
                f"thread-{job_id}",
                status,
                json.dumps(meta),
            ),
        )
    conn.commit()
    return job_id


def _set_meta(conn, job_id: str, patch: dict) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE jobs SET meta = COALESCE(meta, '{}'::jsonb) || %s::jsonb WHERE id = %s",
            (json.dumps(patch), job_id),
        )
    conn.commit()


def _link(conn, parent_id: str, child_id: str, execution: str) -> None:
    link_child_in_transaction(
        conn,
        parent_job_id=parent_id,
        child_job_id=child_id,
        entry={
            "invocation_key": f"{parent_id}:{execution}",
            "step_id": "call_child",
            "step_execution_id": execution,
        },
    )
    conn.commit()


def _park_on_child(conn, parent_id: str, child_id: str, execution: str) -> None:
    _set_meta(
        conn,
        parent_id,
        {
            "workflow_wait": {
                "kind": "child",
                "wait_id": f"wait-{execution}",
                "child_job_id": child_id,
                "step_id": "call_child",
                "step_execution_id": execution,
                "group_id": None,
            }
        },
    )
    with conn.cursor() as cur:
        cur.execute("UPDATE jobs SET status = 'awaiting_child' WHERE id = %s", (parent_id,))
    conn.commit()


def _chain(db) -> SimpleNamespace:
    project = _project(db.conn)
    root = _job(db.conn, project, status="in_progress", meta={"selected_workflow_id": "wf-root"})
    parent = _job(
        db.conn,
        project,
        status="in_progress",
        meta={"selected_workflow_id": "wf-parent", "invoke_parent_job_id": root},
    )
    _link(db.conn, root, parent, "root-exec")
    child = _job(
        db.conn,
        project,
        status="awaiting_approval",
        meta={
            "selected_workflow_id": "wf-child",
            "invoke_parent_job_id": parent,
            "workflow_wait": {
                "kind": "approval",
                "wait_id": "w-1",
                "approval_id": "exec-approval",
                "step_id": "approve",
                "step_execution_id": "child-exec",
            },
        },
    )
    _link(db.conn, parent, child, "parent-exec")
    _park_on_child(db.conn, root, parent, "root-exec")
    _park_on_child(db.conn, parent, child, "parent-exec")
    return SimpleNamespace(root=root, parent=parent, child=child)


def _origin(child: str) -> dict:
    return {
        "job_id": child,
        "workflow_id": "wf-child",
        "event_id": "origin",
        "payload": {
            "wait_id": "w-1",
            "approval_id": "gate-1",
            "reason": "authored",
            "step_execution_id": "child-exec",
        },
    }


def test_every_waiting_ancestor_gets_one_projection_from_the_host_lineage(db):
    chain = _chain(db)
    events: list[dict] = []

    propagate_approval_waiting(
        db.logger, _origin(chain.child), evaluate=lambda event: events.append(event) or []
    )

    assert [(event["job_id"], event["workflow_id"]) for event in events] == [
        (chain.parent, "wf-parent"),
        (chain.root, "wf-root"),
    ]
    assert events[0]["payload"]["child_job_id"] == chain.child
    assert events[1]["payload"]["child_job_id"] == chain.parent
    assert {event["payload"]["approval_job_id"] for event in events} == {chain.child}
    assert events[0]["payload"]["step_execution_id"] == "parent-exec"
    assert events[1]["payload"]["step_execution_id"] == "root-exec"
    assert events[0]["event_id"] == f"wf-event:{chain.parent}:job.approval_waiting:w-1:propagated"


def test_a_cancel_request_on_the_link_stops_the_walk(db):
    chain = _chain(db)
    mark_child(
        db.conn,
        parent_job_id=chain.root,
        child_job_id=chain.parent,
        fields={"cancel_requested_at": "2026-10-12T00:00:00+00:00"},
    )
    db.conn.commit()
    events: list[dict] = []

    propagate_approval_waiting(
        db.logger, _origin(chain.child), evaluate=lambda event: events.append(event) or []
    )

    assert [event["job_id"] for event in events] == [chain.parent]


def test_a_decided_approval_propagates_nothing(db):
    chain = _chain(db)
    with db.conn.cursor() as cur:
        cur.execute("UPDATE jobs SET status = 'in_progress' WHERE id = %s", (chain.child,))
    db.conn.commit()
    events: list[dict] = []

    propagate_approval_waiting(
        db.logger, _origin(chain.child), evaluate=lambda event: events.append(event) or []
    )

    assert events == []


def test_the_root_parking_late_finds_the_pending_grandchild_approval(db):
    chain = _chain(db)

    origin = pending_child_approval_event(db.logger, chain.root)

    assert origin is not None
    assert origin["job_id"] == chain.child
    assert origin["payload"]["wait_id"] == "w-1"
