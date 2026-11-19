"""The operator review queue against real PostgreSQL rows (ADR 0075, item 10).

"Awaiting review" matches a job waiting for approval itself and a job waiting
on an Invoke child that, at any depth, waits for approval; the queue's
neighbours are found by keyset, so a job that has just been decided and left
the filter still has the right previous and next job.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import pytest

from core.infrastructure.db import job_queries_store
from core.infrastructure.db.job_review_chain_store import read_awaiting_review_targets

pytestmark = pytest.mark.integration

WORKFLOW = "wf.contracts"
CHILD_WORKFLOW = "wf.recognize"
REVIEW = {"source": "job", "key": "status", "type": "choice", "value": ("awaiting_review",)}


@pytest.fixture
def conn(disposable_db):
    from core.infrastructure.db import psycopg_runtime

    connection = psycopg_runtime.connect(disposable_db.dsn)
    try:
        yield connection
    finally:
        connection.rollback()
        connection.close()


@pytest.fixture
def project(conn) -> str:
    project_id = str(uuid.uuid4())
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO projects (id, name, description) VALUES (%s, %s, '')",
            (project_id, f"review-queue-{project_id[:8]}"),
        )
    return project_id


def _job(
    conn,
    project_id: str,
    *,
    status: str,
    minutes_ago: int,
    workflow: str = WORKFLOW,
    job_id: str | None = None,
    meta: dict[str, Any] | None = None,
) -> str:
    job_id = job_id or str(uuid.uuid4())
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO jobs (
                id, project_id, task_id, thread_id, status, meta, started_at,
                selected_workflow, task_source
            )
            VALUES (
                %s, %s, %s, %s, %s, %s::jsonb, NOW() - (%s * INTERVAL '1 minute'), %s, 'manual_ui'
            )
            """,
            (
                job_id,
                project_id,
                f"task-{job_id[:8]}",
                f"thread-{job_id}",
                status,
                json.dumps(meta or {}),
                minutes_ago,
                workflow,
            ),
        )
    return job_id


def _link(conn, parent_id: str, child_id: str, **entry: Any) -> None:
    """The parent waits on the child; both sides of the Invoke link are written."""
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE jobs SET meta = COALESCE(meta, '{}'::jsonb) || jsonb_build_object(
                'workflow_wait',
                jsonb_build_object('kind', 'child', 'child_job_id', %s::text,
                                   'step_execution_id', 'exec-1'),
                'invoke_children',
                jsonb_build_object(%s::text, %s::jsonb)
            )
            WHERE id = %s
            """,
            (child_id, child_id, json.dumps({"step_execution_id": "exec-1", **entry}), parent_id),
        )
        cur.execute(
            """
            UPDATE jobs SET meta = COALESCE(meta, '{}'::jsonb)
                || jsonb_build_object('invoke_parent_job_id', %s::text)
            WHERE id = %s
            """,
            (parent_id, child_id),
        )


def _set_status(conn, job_id: str, status: str) -> None:
    with conn.cursor() as cur:
        cur.execute("UPDATE jobs SET status = %s WHERE id = %s", (status, job_id))


def _filters(project_id: str, *, depth: int = 5) -> dict[str, Any]:
    return {
        "column_exists": lambda *_args: True,
        "project_id": project_id,
        "workflow_id": WORKFLOW,
        "exclude_task_source": "workflow_test_run",
        "value_filters": [{**REVIEW, "review_chain_depth": depth}],
    }


def _ids(conn, project_id: str, **overrides: Any) -> list[str]:
    filters = {**_filters(project_id), **overrides}
    rows = job_queries_store.list_jobs_for_attention(conn, **filters)
    assert job_queries_store.count_jobs_for_attention(conn, **filters) == len(rows)
    return [str(row["id"]) for row in rows]


def _chain(conn, project_id: str) -> dict[str, str]:
    """A -> B -> C with the Approval in C; only A is a job of the section's workflow."""
    a = _job(conn, project_id, status="awaiting_child", minutes_ago=30)
    b = _job(conn, project_id, status="awaiting_child", minutes_ago=29, workflow="wf.middle")
    c = _job(conn, project_id, status="awaiting_approval", minutes_ago=28, workflow=CHILD_WORKFLOW)
    _link(conn, a, b)
    _link(conn, b, c)
    return {"a": a, "b": b, "c": c}


def test_a_parent_two_invokes_above_the_approval_waits_for_review(conn, project) -> None:
    chain = _chain(conn, project)
    plain = _job(conn, project, status="awaiting_child", minutes_ago=10)  # waits on nobody known
    done = _job(conn, project, status="completed", minutes_ago=5)

    listed = _ids(conn, project)

    assert listed == [chain["a"]]
    assert plain not in listed and done not in listed
    targets = read_awaiting_review_targets(
        conn, job_ids=[chain["a"], chain["b"], chain["c"], plain, done], max_depth=5
    )
    assert {job: target["review_job_id"] for job, target in targets.items()} == {
        chain["a"]: chain["c"],
        chain["b"]: chain["c"],
        chain["c"]: chain["c"],
    }
    assert targets[chain["a"]]["selected_workflow"] == CHILD_WORKFLOW
    assert targets[chain["a"]]["account_id"]


def test_the_depth_limit_and_a_consumed_link_end_the_chain(conn, project) -> None:
    chain = _chain(conn, project)

    assert _ids(conn, project, value_filters=[{**REVIEW, "review_chain_depth": 1}]) == []
    assert _ids(conn, project, value_filters=[{**REVIEW, "review_chain_depth": 2}]) == [chain["a"]]

    _link(conn, chain["b"], chain["c"], consumed_at="2026-11-19T10:00:00Z")
    assert _ids(conn, project) == []


def test_a_cycle_in_the_links_does_not_loop(conn, project) -> None:
    x = _job(conn, project, status="awaiting_child", minutes_ago=20)
    y = _job(conn, project, status="awaiting_child", minutes_ago=19)
    z = _job(conn, project, status="awaiting_approval", minutes_ago=18, workflow=CHILD_WORKFLOW)
    _link(conn, x, z)
    _link(conn, y, x)
    # A corrupt back link: X names Y as its parent and Y names X.
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE jobs SET meta = meta || jsonb_build_object('invoke_parent_job_id', %s::text)"
            " WHERE id = %s",
            (x, y),
        )

    assert set(_ids(conn, project, value_filters=[{**REVIEW, "review_chain_depth": 50}])) == {x, y}
    targets = read_awaiting_review_targets(conn, job_ids=[x, y], max_depth=50)
    assert {targets[x]["review_job_id"], targets[y]["review_job_id"]} == {z}


def _queue(conn, project: str) -> list[str]:
    """Five documents waiting for review, newest first."""
    return [
        _job(conn, project, status="awaiting_approval", minutes_ago=minutes)
        for minutes in (1, 2, 3, 4, 5)
    ]


def _around(conn, project: str, job_id: str) -> dict[str, Any] | None:
    return job_queries_store.job_list_neighbours(conn, job_id=job_id, **_filters(project))


def test_position_and_neighbours_follow_the_list_order(conn, project) -> None:
    jobs = _queue(conn, project)
    assert _ids(conn, project) == jobs

    first = _around(conn, project, jobs[0])
    middle = _around(conn, project, jobs[2])
    last = _around(conn, project, jobs[4])

    assert first == {
        "job_id": jobs[0],
        "in_list": True,
        "position": 1,
        "total": 5,
        "previous_job_id": None,
        "next_job_id": jobs[1],
    }
    assert (middle["position"], middle["previous_job_id"], middle["next_job_id"]) == (
        3,
        jobs[1],
        jobs[3],
    )
    assert (last["position"], last["previous_job_id"], last["next_job_id"]) == (5, jobs[3], None)


def test_a_decided_job_keeps_its_neighbours_without_skips_or_repeats(conn, project) -> None:
    jobs = _queue(conn, project)

    seen: list[str] = []
    current = jobs[0]
    while current:
        seen.append(current)
        _set_status(conn, current, "in_progress")  # decided: leaves "awaiting review"
        after = _around(conn, project, current)
        assert after is not None and after["in_list"] is False and after["position"] is None
        current = after["next_job_id"]

    assert seen == jobs
    assert _ids(conn, project) == []


def test_a_decided_job_in_the_middle_points_at_both_sides(conn, project) -> None:
    jobs = _queue(conn, project)
    _set_status(conn, jobs[2], "in_progress")

    after = _around(conn, project, jobs[2])

    assert after == {
        "job_id": jobs[2],
        "in_list": False,
        "position": None,
        "total": 4,
        "previous_job_id": jobs[1],
        "next_job_id": jobs[3],
    }


def test_a_job_of_another_workflow_has_no_place_in_the_list(conn, project) -> None:
    other = _job(conn, project, status="awaiting_approval", minutes_ago=1, workflow="wf.other")

    assert _around(conn, project, other) is None
    assert _around(conn, project, "not-a-uuid") is None
