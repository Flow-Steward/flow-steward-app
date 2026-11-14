"""Event waits against a real PostgreSQL (ADR 0071).

A delivered event wakes only a run of the same account and project whose wait
names its source, the exact key and the type; each event wakes one wait, each
wait wakes once; an event that arrives before the run parks waits in the inbox;
duplicates are refused; the deadline is claimed only when no event came; a
cancelled run is never woken.
"""

from __future__ import annotations

import json
import threading
import uuid
from types import SimpleNamespace

import pytest

from core.application.jobs.workflow_event_wait_repository import (
    WaitEventDelivery,
    WorkflowEventWaitRepository,
)

pytestmark = pytest.mark.integration

DEFAULT_ACCOUNT = "00000000-0000-0000-0000-000000000002"
NOW_MS = 1_760_263_200_000
SOURCE = "acme.crawler"


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


def _project(conn) -> str:
    project_id = str(uuid.uuid4())
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO projects (id, name, description) VALUES (%s, %s, '')",
            (project_id, f"event-wait-{project_id[:8]}"),
        )
    conn.commit()
    return project_id


def _parked(
    conn,
    project_id: str,
    *,
    key: str = "run-42",
    source: str = SOURCE,
    types: tuple[str, ...] = ("run_completed", "run_failed"),
    due_epoch_ms: int = NOW_MS + 3_600_000,
    status: str = "awaiting_event",
) -> str:
    job_id = str(uuid.uuid4())
    wait = {
        "wait_id": f"w-{job_id[:8]}",
        "kind": "event",
        "step_id": "wait_for_crawl",
        "step_execution_id": str(uuid.uuid4()),
        "attempt": 1,
        "source": source,
        "event_types": list(types),
        "correlation_key": key,
        "due_epoch_ms": due_epoch_ms,
        "checkpoint": {"thread_id": f"thread-{job_id}"},
    }
    with conn.cursor() as cur:
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


def _repository(conn) -> WorkflowEventWaitRepository:
    return WorkflowEventWaitRepository(SimpleNamespace(conn=conn))


def _deliver(conn, project_id: str, *items, source: str = SOURCE, account=DEFAULT_ACCOUNT):
    return _repository(conn).deliver(
        account_id=account,
        project_id=project_id,
        source=source,
        extension_install_id="install-1",
        items=[
            WaitEventDelivery(correlation_key=key, event_type=kind, payload=payload)
            for key, kind, payload in items
        ],
    )


def test_a_delivered_event_wakes_its_wait_with_the_payload(connect):
    conn = connect()
    project = _project(conn)
    job = _parked(conn, project)

    assert _deliver(conn, project, ("run-42", "run_completed", {"rows": 120})) == [True]
    claimed = _repository(conn).claim_delivered_events(limit=100)

    assert [row.job_id for row, _thread in claimed] == [job]
    status, meta = _job(conn, job)
    intent = meta["workflow_resume"]
    assert status == "resume_pending"
    assert intent["reason"] == "event"
    assert intent["state"] == "pending"
    assert intent["revision"] == 1
    assert intent["resume_value"]["wait_id"] == meta["workflow_wait"]["wait_id"]
    assert intent["resume_value"]["event_type"] == "run_completed"
    assert intent["resume_value"]["correlation_key"] == "run-42"
    assert intent["resume_value"]["payload"] == {"rows": 120}
    # Claimed once: a second pass finds nothing.
    assert _repository(conn).claim_delivered_events(limit=100) == []


def test_only_the_same_project_source_exact_key_and_listed_type_match(connect):
    conn = connect()
    project, other_project = _project(conn), _project(conn)
    waiting = _parked(conn, project, key="run-42")

    _deliver(conn, other_project, ("run-42", "run_completed", {}))
    _deliver(conn, project, ("run-42", "run_completed", {}), source="acme.other")
    _deliver(conn, project, ("RUN-42", "run_completed", {}), ("run-4", "run_completed", {}))
    _deliver(conn, project, ("run-42", "run_started", {}))

    assert _repository(conn).claim_delivered_events(limit=100) == []
    assert _job(conn, waiting)[0] == "awaiting_event"

    _deliver(conn, project, ("run-42", "run_failed", {"error": "blocked"}))
    claimed = _repository(conn).claim_delivered_events(limit=100)
    assert [row.job_id for row, _thread in claimed] == [waiting]


def test_an_event_that_arrives_before_the_run_parks_is_kept_for_it(connect):
    conn = connect()
    project = _project(conn)
    _deliver(conn, project, ("run-7", "run_completed", {"early": True}))
    assert _repository(conn).claim_delivered_events(limit=100) == []

    job = _parked(conn, project, key="run-7")
    claimed = _repository(conn).claim_delivered_events(limit=100)

    assert [row.job_id for row, _thread in claimed] == [job]
    assert _job(conn, job)[1]["workflow_resume"]["resume_value"]["payload"] == {"early": True}


def test_a_duplicate_delivery_is_refused_before_and_after_it_was_consumed(connect):
    conn = connect()
    project = _project(conn)
    item = ("run-42", "run_completed", {"n": 1})

    assert _deliver(conn, project, item) == [True]
    assert _deliver(conn, project, item) == [False]
    _parked(conn, project)
    assert len(_repository(conn).claim_delivered_events(limit=100)) == 1
    assert _deliver(conn, project, item) == [False]

    # A second run parked on the same key and type is not woken by the old event.
    second = _parked(conn, project)
    assert _repository(conn).claim_delivered_events(limit=100) == []
    assert _job(conn, second)[0] == "awaiting_event"


def test_one_event_wakes_one_of_two_waits_on_the_same_key(connect):
    conn = connect()
    project = _project(conn)
    first, second = _parked(conn, project), _parked(conn, project)
    _deliver(conn, project, ("run-42", "run_completed", {}))

    claimed = _repository(conn).claim_delivered_events(limit=100)

    assert len(claimed) == 1
    statuses = sorted(_job(conn, job)[0] for job in (first, second))
    assert statuses == ["awaiting_event", "resume_pending"]


def test_a_cancelled_run_is_never_woken_and_its_event_stays(connect):
    conn = connect()
    project = _project(conn)
    job = _parked(conn, project, status="cancelled")
    _deliver(conn, project, ("run-42", "run_completed", {}))

    assert _repository(conn).claim_delivered_events(limit=100) == []
    assert _job(conn, job)[0] == "cancelled"


def test_the_deadline_is_claimed_only_without_an_event(connect):
    conn = connect()
    project = _project(conn)
    due = _parked(conn, project, key="late", due_epoch_ms=NOW_MS - 1)
    later = _parked(conn, project, key="later", due_epoch_ms=NOW_MS + 60_000)

    claimed = _repository(conn).claim_timed_out_event_waits(now_epoch_ms=NOW_MS, limit=100)

    assert [row.job_id for row, _thread in claimed] == [due]
    status, meta = _job(conn, due)
    assert status == "resume_pending"
    assert meta["workflow_resume"]["resume_value"] == {
        "reason": "event",
        "wait_id": meta["workflow_wait"]["wait_id"],
        "timed_out": True,
    }
    assert _job(conn, later)[0] == "awaiting_event"
    # A late event finds no waiting run and stays unconsumed.
    _deliver(conn, project, ("late", "run_completed", {}))
    assert _repository(conn).claim_delivered_events(limit=100) == []


def test_concurrent_dispatchers_never_claim_the_same_wait(connect):
    setup = connect()
    project = _project(setup)
    jobs = {_parked(setup, project, key=f"run-{index}") for index in range(20)}
    _deliver(setup, project, *[(f"run-{index}", "run_completed", {}) for index in range(20)])
    results: list[list] = [[], []]
    barrier = threading.Barrier(2)

    def _claim(slot: int) -> None:
        conn = connect()
        barrier.wait()
        results[slot] = _repository(conn).claim_delivered_events(limit=100)

    threads = [threading.Thread(target=_claim, args=(slot,)) for slot in (0, 1)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    claimed = [row.job_id for result in results for row, _thread in result]
    assert len(claimed) == len(set(claimed))
    leftover = _repository(setup).claim_delivered_events(limit=100)
    assert set(claimed) | {row.job_id for row, _t in leftover} == jobs


def test_expired_events_are_deleted(connect):
    conn = connect()
    project = _project(conn)
    _deliver(conn, project, ("old", "done", {}), ("new", "done", {}))
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE workflow_wait_events SET received_at = NOW() - interval '8 days'"
            " WHERE project_id = %s AND correlation_key = 'old'",
            (project,),
        )
    conn.commit()

    assert _repository(conn).delete_expired_events(retention_days=7, limit=100) >= 1
    with conn.cursor() as cur:
        cur.execute(
            "SELECT correlation_key FROM workflow_wait_events WHERE project_id = %s", (project,)
        )
        keys = [row[0] for row in cur.fetchall()]
    conn.commit()
    assert keys == ["new"]
