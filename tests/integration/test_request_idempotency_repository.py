from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta

import pytest

from core.application.idempotency.repository import RequestIdempotencyRepository


@pytest.mark.integration
def test_pending_recovery_journal_is_fenced_in_postgres(pg_conn) -> None:
    with pg_conn.cursor() as cur:
        cur.execute(
            """
            CREATE TEMPORARY TABLE request_idempotency (
                scope_key text NOT NULL,
                key_digest text NOT NULL,
                request_id text NOT NULL,
                status text NOT NULL,
                job_id text,
                last_error text DEFAULT '' NOT NULL,
                request_fingerprint text NOT NULL,
                replay_response_jsonb jsonb,
                result_expires_at timestamptz,
                tombstone_expires_at timestamptz,
                lease_expires_at timestamptz,
                created_at timestamptz DEFAULT now() NOT NULL,
                updated_at timestamptz DEFAULT now() NOT NULL,
                PRIMARY KEY (scope_key, key_digest)
            )
            """
        )
    suffix = uuid.uuid4().hex
    repository = RequestIdempotencyRepository(pg_conn)
    scope_key = f"request:v1:{suffix}"
    key_digest = f"sha256:{suffix}"
    request_id = f"request-{suffix}"
    inserted = repository.insert_pending(
        scope_key=scope_key,
        key_digest=key_digest,
        request_id=request_id,
        request_fingerprint=f"fingerprint:{suffix}",
        lease_expires_at=datetime.now(UTC) + timedelta(minutes=2),
    )
    assert inserted is not None

    assert repository.journal(
        scope_key=scope_key,
        key_digest=key_digest,
        request_id=request_id,
        journal={"pending_artifact_ids": ["art_reserved"]},
    )
    assert not repository.journal(
        scope_key=scope_key,
        key_digest=key_digest,
        request_id="different-owner",
        journal={"pending_artifact_ids": ["art_attacker"]},
    )
    row = repository.load_for_update(scope_key=scope_key, key_digest=key_digest)
    assert row is not None
    assert row["replay_response_jsonb"] == {"pending_artifact_ids": ["art_reserved"]}


@pytest.mark.integration
def test_find_durable_job_uses_existing_jobs_columns_in_postgres(pg_conn) -> None:
    """The durable-job lookup must not depend on a nonexistent jobs.created_at."""
    with pg_conn.cursor() as cur:
        cur.execute(
            """
            CREATE TEMPORARY TABLE jobs (
                id text NOT NULL,
                project_id text NOT NULL,
                task_id text NOT NULL,
                task_summary text,
                task_source text,
                thread_id text,
                mode text,
                status text NOT NULL,
                worker_id text,
                meta jsonb,
                queued_at timestamptz
            )
            """
        )
        cur.execute(
            """
            INSERT INTO jobs (
                id, project_id, task_id, task_summary, task_source,
                thread_id, mode, status, worker_id, meta, queued_at
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, now())
            """,
            (
                "job-1",
                "project-1",
                "task-1",
                "Review invoice",
                "manual",
                "thread-1",
                None,
                "pending",
                None,
                json.dumps(
                    {
                        "idempotency": {
                            "scope_key": "request:v1:scope",
                            "key_digest": "sha256:key",
                        }
                    }
                ),
            ),
        )

        row = RequestIdempotencyRepository(pg_conn).find_job_by_request_identity(
            scope_key="request:v1:scope",
            key_digest="sha256:key",
        )

    assert row is not None
    assert row["id"] == "job-1"
    assert row["thread_id"] == "thread-1"
