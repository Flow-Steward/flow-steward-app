from __future__ import annotations

import json
import threading
import time
import uuid

import pytest

from core.application.jobs.dispatch_outbox_repository import DispatchOutboxRepository
from core.infrastructure.db import psycopg_runtime
from core.infrastructure.db.db_pool import transaction_connection
from tests._db_env import resolve_test_db_dsn


@pytest.fixture
def payload_schema(pg_conn):
    schema = f"task_payload_{uuid.uuid4().hex}"
    pg_conn.rollback()
    with pg_conn.cursor() as cur:
        cur.execute(f'CREATE SCHEMA "{schema}"')
        cur.execute(f'SET search_path TO "{schema}"')
        cur.execute(
            "CREATE TABLE jobs ("
            "id text PRIMARY KEY, status text NOT NULL, worker_id text, meta jsonb)"
        )
        cur.execute(
            "CREATE TABLE job_dispatch_outbox ("
            "job_id text PRIMARY KEY, encrypted_envelope text NOT NULL, "
            "status text NOT NULL, attempt_count integer NOT NULL DEFAULT 0, "
            "available_at timestamptz NOT NULL DEFAULT now(), published_at timestamptz, "
            "last_error text, created_at timestamptz NOT NULL DEFAULT now(), "
            "updated_at timestamptz NOT NULL DEFAULT now(), "
            "preparation_owner_token text, preparation_lease_expires_at timestamptz)"
        )
    pg_conn.commit()
    try:
        yield schema
    finally:
        pg_conn.rollback()
        with pg_conn.cursor() as cur:
            cur.execute("SET search_path TO public")
            cur.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        pg_conn.commit()


def _insert_preparing_job(conn) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO jobs (id, status, meta) VALUES (%s, 'pending', %s::jsonb)",
            ("job-1", json.dumps({"task_envelope": {"payload_complete": False}})),
        )
        cur.execute(
            "INSERT INTO job_dispatch_outbox "
            "(job_id, encrypted_envelope, status, preparation_owner_token, "
            "preparation_lease_expires_at) "
            "VALUES (%s, %s, 'preparing', %s, now() + interval '5 minutes')",
            ("job-1", "initial-envelope", "owner-1"),
        )
    conn.commit()


@pytest.mark.integration
def test_payload_and_final_dispatch_envelope_become_visible_atomically(
    pg_conn,
    payload_schema: str,
) -> None:
    _insert_preparing_job(pg_conn)
    actor = psycopg_runtime.connect(resolve_test_db_dsn())
    observer = psycopg_runtime.connect(resolve_test_db_dsn())
    actor.autocommit = True
    observer.autocommit = True
    try:
        with actor.cursor() as cur:
            cur.execute(f'SET search_path TO "{payload_schema}"')
        with observer.cursor() as cur:
            cur.execute(f'SET search_path TO "{payload_schema}"')

        with transaction_connection(actor):
            assert DispatchOutboxRepository(actor).finalize_prepared_payload(
                job_id="job-1",
                meta_patch={"task_envelope": {"payload_complete": True}},
                encrypted_envelope="final-envelope",
                preparation_owner_token="owner-1",
            )
            with observer.cursor() as cur:
                cur.execute(
                    "SELECT jobs.meta->'task_envelope'->>'payload_complete', "
                    "outbox.status, outbox.encrypted_envelope "
                    "FROM jobs JOIN job_dispatch_outbox outbox ON outbox.job_id = jobs.id "
                    "WHERE jobs.id = 'job-1'"
                )
                assert cur.fetchone() == ("false", "preparing", "initial-envelope")

        with observer.cursor() as cur:
            cur.execute(
                "SELECT jobs.meta->'task_envelope'->>'payload_complete', "
                "outbox.status, outbox.encrypted_envelope "
                "FROM jobs JOIN job_dispatch_outbox outbox ON outbox.job_id = jobs.id "
                "WHERE jobs.id = 'job-1'"
            )
            assert cur.fetchone() == ("true", "pending", "final-envelope")
    finally:
        actor.close()
        observer.close()


@pytest.mark.integration
def test_payload_finalization_conflict_rolls_back_job_metadata(
    pg_conn,
    payload_schema: str,
) -> None:
    _insert_preparing_job(pg_conn)
    with pg_conn.cursor() as cur:
        cur.execute("UPDATE job_dispatch_outbox SET status = 'failed' WHERE job_id = 'job-1'")
    pg_conn.commit()

    actor = psycopg_runtime.connect(resolve_test_db_dsn())
    actor.autocommit = True
    with actor.cursor() as cur:
        cur.execute(f'SET search_path TO "{payload_schema}"')

    try:
        _raises_callable_124_1 = DispatchOutboxRepository(actor).finalize_prepared_payload
        with (
            pytest.raises(RuntimeError, match="no longer preparing"),
            transaction_connection(actor),
        ):
            _raises_callable_124_1(
                job_id="job-1",
                meta_patch={"task_envelope": {"payload_complete": True}},
                encrypted_envelope="final-envelope",
                preparation_owner_token="owner-1",
            )
    finally:
        actor.close()

    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT meta->'task_envelope'->>'payload_complete' FROM jobs WHERE id = 'job-1'"
        )
        assert cur.fetchone() == ("false",)


@pytest.mark.integration
def test_active_preparation_heartbeat_wins_cleanup_claim_race(
    pg_conn,
    payload_schema: str,
) -> None:
    _insert_preparing_job(pg_conn)
    heartbeat_conn = psycopg_runtime.connect(resolve_test_db_dsn())
    cleanup_conn = psycopg_runtime.connect(resolve_test_db_dsn())
    heartbeat_conn.autocommit = False
    cleanup_conn.autocommit = True
    try:
        for conn in (heartbeat_conn, cleanup_conn):
            with conn.cursor() as cur:
                cur.execute(f'SET search_path TO "{payload_schema}"')
        with pg_conn.cursor() as cur:
            cur.execute(
                "UPDATE job_dispatch_outbox SET preparation_lease_expires_at = "
                "now() + interval '1 second' WHERE job_id = 'job-1'"
            )
        pg_conn.commit()

        heartbeat_repo = DispatchOutboxRepository(heartbeat_conn)
        cleanup_repo = DispatchOutboxRepository(cleanup_conn)
        cleanup_done = threading.Event()
        cleanup_result: list[dict] = []

        with transaction_connection(heartbeat_conn):
            assert heartbeat_repo.renew_preparation_lease(
                job_id="job-1",
                owner_token="owner-1",
                lease_seconds=300,
            )
            time.sleep(1.1)

            def _claim_cleanup() -> None:
                cleanup_result.extend(
                    cleanup_repo.claim_expired_preparing(limit=10, lease_seconds=60)
                )
                cleanup_done.set()

            scanner = threading.Thread(target=_claim_cleanup)
            scanner.start()
            # The scanner uses SKIP LOCKED: it must skip the heartbeat-owned row
            # immediately rather than reclaiming it from an earlier snapshot.
            assert cleanup_done.wait(1)
            assert cleanup_result == []

        scanner.join(timeout=5)
        assert cleanup_done.is_set()
        assert cleanup_result == []
    finally:
        heartbeat_conn.close()
        cleanup_conn.close()


@pytest.mark.integration
def test_stale_preparer_cannot_overwrite_cleanup_owner(
    pg_conn,
    payload_schema: str,
) -> None:
    _insert_preparing_job(pg_conn)
    with pg_conn.cursor() as cur:
        cur.execute(
            "UPDATE job_dispatch_outbox SET preparation_lease_expires_at = "
            "now() - interval '1 second' WHERE job_id = 'job-1'"
        )
    pg_conn.commit()

    repo = DispatchOutboxRepository(pg_conn)
    claimed = repo.claim_expired_preparing(limit=1, lease_seconds=60)
    assert len(claimed) == 1
    assert claimed[0]["owner_token"] != "owner-1"
    assert not repo.settle_preparation_failure(
        job_id="job-1",
        owner_token="owner-1",
        last_error="stale_submitter",
    )

    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT status, preparation_owner_token FROM job_dispatch_outbox WHERE job_id = 'job-1'"
        )
        assert cur.fetchone() == ("cleanup_pending", claimed[0]["owner_token"])


@pytest.mark.integration
def test_live_preparation_is_visible_to_idempotency_recovery(
    pg_conn,
    payload_schema: str,
) -> None:
    _insert_preparing_job(pg_conn)

    repo = DispatchOutboxRepository(pg_conn)
    assert repo.has_live_preparation(job_id="job-1")

    with pg_conn.cursor() as cur:
        cur.execute(
            "UPDATE job_dispatch_outbox SET preparation_lease_expires_at = "
            "now() - interval '1 second' WHERE job_id = 'job-1'"
        )
    pg_conn.commit()
    assert not repo.has_live_preparation(job_id="job-1")
