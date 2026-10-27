"""Due Wait timers against a real PostgreSQL (T19, T20).

The claim is one statement: it locks due ``awaiting_timer`` rows with
``SKIP LOCKED``, stores the timer resume intent with a new revision and moves
the job to ``resume_pending``. These tests prove what a mock cursor cannot:
the due lookup only takes due rows, two dispatchers never claim the same job,
a cancel that commits first wins, and the planner can use the partial index.
"""

from __future__ import annotations

import json
import threading
import uuid
from types import SimpleNamespace

import pytest

from core.application.jobs.workflow_resume_repository import (
    WorkflowResumeRepositoryAdapter,
)

pytestmark = pytest.mark.integration

NOW_MS = 1_760_263_200_000


@pytest.fixture
def connect(disposable_db):
    import psycopg

    opened = []

    def _connect():
        connection = psycopg.connect(disposable_db.dsn)
        opened.append(connection)
        return connection

    try:
        yield _connect
    finally:
        for connection in opened:
            connection.close()


def _waiting_job(conn, *, due_epoch_ms: int, status: str = "awaiting_timer") -> str:
    job_id = str(uuid.uuid4())
    project_id = str(uuid.uuid4())
    wait = {
        "wait_id": f"w-{job_id[:8]}",
        "kind": "timer",
        "step_id": "pause",
        "step_execution_id": str(uuid.uuid4()),
        "attempt": 1,
        "due_epoch_ms": due_epoch_ms,
        "checkpoint": {"thread_id": f"thread-{job_id}"},
    }
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO projects (id, name, description) VALUES (%s, %s, '')",
            (project_id, f"timer-test-{job_id[:8]}"),
        )
        cur.execute(
            """
            INSERT INTO jobs (id, project_id, task_id, thread_id, status, meta)
            VALUES (%s, %s, %s, %s, %s, %s::jsonb)
            """,
            (
                job_id,
                project_id,
                f"task-{job_id[:8]}",
                f"thread-{job_id}",
                status,
                json.dumps({"workflow_wait": wait}),
            ),
        )
    conn.commit()
    return job_id


def _job(conn, job_id: str) -> tuple[str, dict]:
    with conn.cursor() as cur:
        cur.execute("SELECT status, meta FROM jobs WHERE id = %s", (job_id,))
        status, meta = cur.fetchone()
    conn.commit()
    return status, meta


def _repository(conn) -> WorkflowResumeRepositoryAdapter:
    return WorkflowResumeRepositoryAdapter(SimpleNamespace(conn=conn))


def test_only_due_timers_are_claimed_into_stored_timer_intents(connect):
    conn = connect()
    due = _waiting_job(conn, due_epoch_ms=NOW_MS - 1)
    exactly_due = _waiting_job(conn, due_epoch_ms=NOW_MS)
    later = _waiting_job(conn, due_epoch_ms=NOW_MS + 60_000)

    claimed = _repository(conn).claim_due_timers(now_epoch_ms=NOW_MS, limit=100)

    assert {row.job_id for row, _thread in claimed} == {due, exactly_due}
    status, meta = _job(conn, due)
    intent = meta["workflow_resume"]
    assert status == "resume_pending"
    assert intent["reason"] == "timer"
    assert intent["state"] == "pending"
    assert intent["revision"] == 1
    assert intent["resume_value"] == {
        "reason": "timer",
        "wait_id": meta["workflow_wait"]["wait_id"],
        "timer_elapsed": True,
    }
    assert "approved" not in intent["resume_value"]
    assert _job(conn, later)[0] == "awaiting_timer"


def test_a_due_timer_is_claimed_once_across_dispatchers(connect):
    setup = connect()
    job_ids = {_waiting_job(setup, due_epoch_ms=NOW_MS - 1) for _ in range(20)}
    results: list[list] = [[], []]
    barrier = threading.Barrier(2)

    def dispatcher(index: int) -> None:
        conn = connect()
        barrier.wait()
        results[index] = _repository(conn).claim_due_timers(now_epoch_ms=NOW_MS, limit=100)

    threads = [threading.Thread(target=dispatcher, args=(i,)) for i in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    first = [row.job_id for row, _ in results[0]]
    second = [row.job_id for row, _ in results[1]]
    assert not set(first) & set(second)
    assert set(first) | set(second) == job_ids
    # A claimed job is no longer awaiting a timer: a later pass takes nothing.
    assert _repository(setup).claim_due_timers(now_epoch_ms=NOW_MS, limit=100) == []


def test_a_cancelled_run_is_not_woken(connect):
    conn = connect()
    job_id = _waiting_job(conn, due_epoch_ms=NOW_MS - 1)
    with conn.cursor() as cur:
        cur.execute("UPDATE jobs SET status = 'cancelled' WHERE id = %s", (job_id,))
    conn.commit()

    assert _repository(conn).claim_due_timers(now_epoch_ms=NOW_MS, limit=100) == []
    status, meta = _job(conn, job_id)
    assert status == "cancelled"
    assert "workflow_resume" not in meta


def test_a_new_wake_takes_the_next_revision(connect):
    conn = connect()
    job_id = _waiting_job(conn, due_epoch_ms=NOW_MS - 1)
    with conn.cursor() as cur:
        cur.execute(
            """UPDATE jobs SET meta = meta || '{"workflow_resume": {"revision": 4, "state": "completed"}}'::jsonb
               WHERE id = %s""",
            (job_id,),
        )
    conn.commit()

    ((row, _thread),) = _repository(conn).claim_due_timers(now_epoch_ms=NOW_MS, limit=100)

    assert row.resume_revision == 5


def test_the_due_lookup_can_use_the_partial_index(connect):
    conn = connect()
    with conn.cursor() as cur:
        cur.execute("SET enable_seqscan = off")
        cur.execute(
            """
            EXPLAIN SELECT id FROM jobs
             WHERE status = 'awaiting_timer'
               AND ((meta->'workflow_wait'->>'due_epoch_ms')::bigint) <= %s
             ORDER BY ((meta->'workflow_wait'->>'due_epoch_ms')::bigint), id
             LIMIT 100
            """,
            (NOW_MS,),
        )
        plan = "\n".join(str(row[0]) for row in cur.fetchall())
    conn.rollback()
    assert "idx_jobs_workflow_wait_due" in plan
