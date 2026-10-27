from __future__ import annotations

import uuid

import pytest

from core.application.jobs.job_lifecycle_repository import (
    update_job_queue_delivery_meta,
)
from core.application.jobs.terminal_persistence import (
    TerminalPersistenceConflict,
    TerminalPersistenceService,
)
from core.infrastructure.db import psycopg_runtime
from tests._db_env import resolve_test_db_dsn


@pytest.mark.integration
def test_publisher_terminal_fence_preserves_worker_claim_on_postgres(pg_conn) -> None:
    """A worker claim committed between publisher phases must win."""
    schema = f"publisher_fence_{uuid.uuid4().hex}"
    dsn = resolve_test_db_dsn()
    pg_conn.rollback()
    worker_conn = psycopg_runtime.connect(dsn)
    publisher_conn = psycopg_runtime.connect(dsn)
    try:
        with pg_conn.cursor() as cur:
            cur.execute(f'CREATE SCHEMA "{schema}"')
            cur.execute(
                f'CREATE TABLE "{schema}".jobs ('
                "id text PRIMARY KEY, status text NOT NULL, worker_id text, "
                "queue_metadata_persisted boolean NOT NULL DEFAULT false)"
            )
            cur.execute(
                f'INSERT INTO "{schema}".jobs (id, status) VALUES (%s, %s)',
                ("job-1", "pending"),
            )
        pg_conn.commit()

        for conn in (publisher_conn, worker_conn):
            conn.autocommit = False
            with conn.cursor() as cur:
                cur.execute(f'SET search_path TO "{schema}"')
            conn.commit()

        with publisher_conn.cursor() as cur:
            cur.execute(
                "UPDATE jobs SET queue_metadata_persisted = true WHERE id = %s",
                ("job-1",),
            )
        publisher_conn.commit()

        with worker_conn.cursor() as cur:
            cur.execute(
                "UPDATE jobs SET status = 'in_progress', worker_id = %s WHERE id = %s",
                ("worker-1", "job-1"),
            )
        worker_conn.commit()

        with pytest.raises(TerminalPersistenceConflict, match="no longer pending and unowned"):
            TerminalPersistenceService._assert_terminal_write_allowed(
                publisher_conn,
                job_id="job-1",
                expected_worker_id=None,
                require_pending_unowned=True,
            )
        publisher_conn.rollback()

        with publisher_conn.cursor() as cur:
            cur.execute(
                "SELECT status, worker_id, queue_metadata_persisted FROM jobs WHERE id = %s",
                ("job-1",),
            )
            row = cur.fetchone()
        assert row == ("in_progress", "worker-1", True)
    finally:
        publisher_conn.rollback()
        worker_conn.rollback()
        publisher_conn.close()
        worker_conn.close()
        pg_conn.rollback()
        with pg_conn.cursor() as cur:
            cur.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        pg_conn.commit()


@pytest.mark.integration
@pytest.mark.parametrize("desired_dispatch_state", ["attempted", "not_started"])
def test_publish_failure_cas_allows_explicit_transition_from_attempted_on_postgres(
    pg_conn,
    desired_dispatch_state: str,
) -> None:
    schema = f"publisher_failure_cas_{uuid.uuid4().hex}"
    pg_conn.rollback()
    try:
        with pg_conn.cursor() as cur:
            cur.execute(f'CREATE SCHEMA "{schema}"')
            cur.execute(f'SET search_path TO "{schema}"')
            cur.execute(
                "CREATE TABLE jobs ("
                "id text PRIMARY KEY, status text NOT NULL, worker_id text, meta jsonb)"
            )
            cur.execute(
                "INSERT INTO jobs (id, status, meta) VALUES (%s, %s, %s::jsonb)",
                (
                    "job-1",
                    "pending",
                    '{"queue_delivery":{"state":"queued","dispatch_state":"attempted"}}',
                ),
            )
        pg_conn.commit()

        assert update_job_queue_delivery_meta(
            conn=pg_conn,
            job_id="job-1",
            queue_delivery={
                "state": "retrying_delivery",
                "dispatch_state": desired_dispatch_state,
                "delivery_succeeded": False,
            },
            runtime_journal=[{"event": "queue_delivery_failed"}],
            expected_dispatch_states={"attempted"},
        )
        pg_conn.commit()

        with pg_conn.cursor() as cur:
            cur.execute(
                "SELECT status, worker_id, meta->'queue_delivery'->>'state', "
                "meta->'queue_delivery'->>'dispatch_state' "
                "FROM jobs WHERE id = %s",
                ("job-1",),
            )
            row = cur.fetchone()
        assert row == ("pending", None, "retrying_delivery", desired_dispatch_state)
    finally:
        pg_conn.rollback()
        with pg_conn.cursor() as cur:
            cur.execute("SET search_path TO public")
            cur.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        pg_conn.commit()
