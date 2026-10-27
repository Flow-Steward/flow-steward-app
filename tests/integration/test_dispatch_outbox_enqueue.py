"""Real-PostgreSQL enqueue contract for the job dispatch outbox.

Remediation epic 2026-07-29, WS-03 — covers `AUDIT-007` (scheduled `Run now`)
and `AUDIT-020` (immediate submission), which share one root cause:

    enqueue_failed:could not determine data type of parameter $5

`DispatchOutboxRepository.create` built its lease expiry with a bare parameter
inside `CASE WHEN %s IS NULL`. PostgreSQL cannot infer a type for a parameter
whose only use is an `IS NULL` test, so the INSERT was rejected at parse time —
before any row was written. Every submission path funnels through this one
statement, so no durable job could be created through the UI, REST, MCP, or the
CLI.

These tests run against a disposable canonical PostgreSQL. A mock cursor cannot
catch this class of defect at all: the failure lives in the server's parser, so
the statement has to reach a real server to be proved either way.
"""

from __future__ import annotations

import uuid

import pytest

from core.application.jobs.dispatch_outbox_repository import DispatchOutboxRepository

pytestmark = pytest.mark.integration

# The exact statement shipped before the fix. Kept verbatim so the test proves
# the defect still fails, rather than asserting the fix in isolation.
PRE_FIX_INSERT = """
INSERT INTO job_dispatch_outbox
    (job_id, encrypted_envelope, status, available_at,
     preparation_owner_token, preparation_lease_expires_at)
VALUES (
    %s, %s, %s, now() + interval '30 seconds', %s,
    CASE WHEN %s IS NULL THEN NULL
         ELSE now() + (%s * interval '1 second') END
)
"""


def _make_job(conn) -> str:
    """Create the minimum project+job rows an outbox envelope can reference."""
    job_id = str(uuid.uuid4())
    project_id = str(uuid.uuid4())
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO projects (id, name, description) VALUES (%s, %s, '')",
            (project_id, f"dispatch-outbox-test-{job_id[:8]}"),
        )
        cur.execute(
            """
            INSERT INTO jobs (id, project_id, task_id, status)
            VALUES (%s, %s, %s, 'pending')
            """,
            (job_id, project_id, f"task-{job_id[:8]}"),
        )
    return job_id


@pytest.fixture
def conn(disposable_db):
    import psycopg

    connection = psycopg.connect(disposable_db.dsn)
    connection.autocommit = True
    try:
        yield connection
    finally:
        connection.close()


def test_pre_fix_statement_still_fails_to_type_its_parameter(conn):
    """Pins the defect: without a cast, PostgreSQL rejects the INSERT outright."""
    job_id = _make_job(conn)

    # The concrete psycopg error class varies by driver build; the SQLSTATE
    # message is what pins the defect.
    with (
        pytest.raises(Exception, match="determine data type of parameter") as excinfo,
        conn.cursor() as cur,
    ):
        cur.execute(PRE_FIX_INSERT, (job_id, "envelope", "pending", None, None, 300))

    message = str(excinfo.value).lower()
    assert "determine data type of parameter" in message, message
    assert "$5" in message, message


def test_create_enqueues_a_pending_envelope(conn):
    """AUDIT-020: immediate submission must produce a durable outbox row."""
    job_id = _make_job(conn)

    DispatchOutboxRepository(conn).create(job_id=job_id, encrypted_envelope="envelope-immediate")

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT status, encrypted_envelope, preparation_owner_token,
                   preparation_lease_expires_at, available_at > now()
              FROM job_dispatch_outbox WHERE job_id = %s
            """,
            (job_id,),
        )
        row = cur.fetchone()

    assert row is not None, "no dispatch envelope was created"
    status, envelope, owner_token, lease_expires_at, available_in_future = row
    assert status == "pending"
    assert envelope == "envelope-immediate"
    assert owner_token is None
    assert lease_expires_at is None, "a pending envelope must not hold a preparation lease"
    assert available_in_future, "the envelope must not be claimable before its delay elapses"


def test_create_enqueues_a_preparing_envelope_with_a_live_lease(conn):
    """AUDIT-007: the scheduled path takes the other CASE branch."""
    job_id = _make_job(conn)

    DispatchOutboxRepository(conn).create(
        job_id=job_id,
        encrypted_envelope="envelope-scheduled",
        status="preparing",
        preparation_owner_token="owner-token-1",
        preparation_lease_seconds=300,
    )

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT status, preparation_owner_token, preparation_lease_expires_at > now()
              FROM job_dispatch_outbox WHERE job_id = %s
            """,
            (job_id,),
        )
        row = cur.fetchone()

    assert row == ("preparing", "owner-token-1", True)


@pytest.mark.parametrize(
    ("requested_seconds", "expected_between"),
    [
        (5, (29, 31)),  # clamped up to the 30s floor
        (300, (299, 301)),
        (99_999, (3599, 3601)),  # clamped down to the 1h ceiling
    ],
)
def test_preparation_lease_seconds_are_clamped(conn, requested_seconds, expected_between):
    """The lease bounds must survive the cast that fixes the parameter typing."""
    job_id = _make_job(conn)

    DispatchOutboxRepository(conn).create(
        job_id=job_id,
        encrypted_envelope="envelope",
        status="preparing",
        preparation_owner_token="owner-token-2",
        preparation_lease_seconds=requested_seconds,
    )

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT EXTRACT(EPOCH FROM (preparation_lease_expires_at - now()))
              FROM job_dispatch_outbox WHERE job_id = %s
            """,
            (job_id,),
        )
        remaining = float(cur.fetchone()[0])

    low, high = expected_between
    assert low <= remaining <= high, f"lease was {remaining}s, expected between {low} and {high}"


def test_preparing_without_an_owner_token_is_refused(conn):
    """Fail before the INSERT rather than persisting an unownable preparation."""
    job_id = _make_job(conn)

    _raises_callable_181_1 = DispatchOutboxRepository(conn).create
    with pytest.raises(ValueError, match="owner token"):
        _raises_callable_181_1(job_id=job_id, encrypted_envelope="envelope", status="preparing")

    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM job_dispatch_outbox WHERE job_id = %s", (job_id,))
        assert cur.fetchone()[0] == 0


def test_unsupported_initial_status_is_refused(conn):
    job_id = _make_job(conn)

    _raises_callable_194_1 = DispatchOutboxRepository(conn).create
    with pytest.raises(ValueError, match="Unsupported initial job dispatch outbox status"):
        _raises_callable_194_1(job_id=job_id, encrypted_envelope="envelope", status="published")


def test_created_envelope_is_claimable_once_its_delay_elapses(conn):
    """End-to-end: an enqueued envelope reaches the dispatcher's claim path."""
    job_id = _make_job(conn)
    repo = DispatchOutboxRepository(conn)
    repo.create(job_id=job_id, encrypted_envelope="envelope-claimable")

    assert repo.claim_pending() == [], "envelope claimed before its 30s delay elapsed"

    with conn.cursor() as cur:
        cur.execute(
            "UPDATE job_dispatch_outbox SET available_at = now() - interval '1 second' "
            "WHERE job_id = %s",
            (job_id,),
        )

    assert job_id in repo.claim_pending()
