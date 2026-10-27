"""PostgreSQL repository for caller-transaction-owned idempotency rows."""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime
from typing import Any


def _row_to_dict(cursor, row: Any) -> dict[str, Any] | None:
    if row is None:
        return None
    if isinstance(row, dict):
        return dict(row)
    if isinstance(row, Mapping):
        return dict(row.items())
    if isinstance(row, (list, tuple)):
        columns = [str(item[0]) for item in (cursor.description or [])]
        if len(columns) == len(row):
            return {columns[index]: row[index] for index in range(len(columns))}
    raise TypeError("Unsupported idempotency row type")


class RequestIdempotencyRepository:
    """Uses the supplied pinned connection and never commits it."""

    def __init__(self, conn) -> None:
        self.conn = conn

    def insert_pending(
        self,
        *,
        scope_key: str,
        key_digest: str,
        request_id: str,
        request_fingerprint: str,
        lease_expires_at: datetime,
    ) -> dict[str, Any] | None:
        with self.conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO request_idempotency (
                    scope_key, key_digest, request_id,
                    request_fingerprint, status, tombstone_expires_at, lease_expires_at
                )
                VALUES (%s, %s, %s, %s, 'pending', now() + INTERVAL '1 day', %s)
                ON CONFLICT (scope_key, key_digest) DO NOTHING
                RETURNING scope_key, key_digest, request_id,
                          request_fingerprint, status, job_id,
                          replay_response_jsonb, result_expires_at,
                          tombstone_expires_at, lease_expires_at, updated_at
                """,
                (scope_key, key_digest, request_id, request_fingerprint, lease_expires_at),
            )
            return _row_to_dict(cur, cur.fetchone())

    def load_for_update(
        self,
        *,
        scope_key: str,
        key_digest: str,
    ) -> dict[str, Any] | None:
        with self.conn.cursor() as cur:
            cur.execute(
                """
                SELECT scope_key, key_digest, request_id,
                       request_fingerprint, status, job_id,
                       replay_response_jsonb, result_expires_at,
                       tombstone_expires_at, lease_expires_at, updated_at
                FROM request_idempotency
                WHERE scope_key = %s AND key_digest = %s
                FOR UPDATE
                """,
                (scope_key, key_digest),
            )
            return _row_to_dict(cur, cur.fetchone())

    def find_job_by_request_identity(
        self,
        *,
        scope_key: str,
        key_digest: str,
    ) -> dict[str, Any] | None:
        with self.conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, project_id, task_id, task_summary, task_source,
                       thread_id, mode, status, worker_id, meta
                FROM jobs
                WHERE (
                    (meta #>> '{idempotency,scope_key}' = %s
                     AND meta #>> '{idempotency,key_digest}' = %s)
                    OR EXISTS (
                        SELECT 1
                        FROM jsonb_array_elements(
                            COALESCE(meta->'request_idempotency_identities', '[]'::jsonb)
                        ) AS identity
                        WHERE identity->>'scope_key' = %s
                          AND identity->>'key_digest' = %s
                    )
                )
                ORDER BY queued_at ASC NULLS LAST, id ASC
                LIMIT 1
                FOR UPDATE
                """,
                (scope_key, key_digest, scope_key, key_digest),
            )
            return _row_to_dict(cur, cur.fetchone())

    def reclaim(
        self,
        *,
        scope_key: str,
        key_digest: str,
        request_id: str,
        request_fingerprint: str,
        lease_expires_at: datetime,
    ) -> dict[str, Any] | None:
        with self.conn.cursor() as cur:
            cur.execute(
                """
                UPDATE request_idempotency
                SET request_id = %s,
                    request_fingerprint = %s,
                    status = 'pending',
                    job_id = NULL,
                    replay_response_jsonb = NULL,
                    result_expires_at = NULL,
                    tombstone_expires_at = now() + INTERVAL '1 day',
                    lease_expires_at = %s,
                    last_error = '',
                    updated_at = now()
                WHERE scope_key = %s AND key_digest = %s
                  AND request_fingerprint = %s
                  AND (
                    status = 'failed'
                    OR (status = 'pending' AND lease_expires_at <= now())
                  )
                RETURNING scope_key, key_digest, request_id,
                          request_fingerprint, status, job_id,
                          replay_response_jsonb, result_expires_at,
                          tombstone_expires_at, lease_expires_at, updated_at
                """,
                (
                    request_id,
                    request_fingerprint,
                    lease_expires_at,
                    scope_key,
                    key_digest,
                    request_fingerprint,
                ),
            )
            return _row_to_dict(cur, cur.fetchone())

    def renew(
        self,
        *,
        scope_key: str,
        key_digest: str,
        request_id: str,
        lease_expires_at: datetime,
    ) -> bool:
        """Extend a live lease only for the request that currently owns it."""
        with self.conn.cursor() as cur:
            cur.execute(
                """
                UPDATE request_idempotency
                   SET lease_expires_at = %s,
                       updated_at = now()
                 WHERE scope_key = %s
                   AND key_digest = %s
                   AND request_id = %s
                   AND status = 'pending'
                   AND lease_expires_at > now()
                """,
                (lease_expires_at, scope_key, key_digest, request_id),
            )
            return getattr(cur, "rowcount", 0) == 1

    def journal(
        self,
        *,
        scope_key: str,
        key_digest: str,
        request_id: str,
        journal: dict[str, Any],
    ) -> bool:
        """Persist pre-acceptance recovery data under the current lease fence."""
        with self.conn.cursor() as cur:
            cur.execute(
                """
                UPDATE request_idempotency
                   SET replay_response_jsonb = %s::jsonb,
                       updated_at = now()
                 WHERE scope_key = %s
                   AND key_digest = %s
                   AND request_id = %s
                   AND status = 'pending'
                   AND lease_expires_at > now()
                """,
                (
                    json.dumps(journal, sort_keys=True, separators=(",", ":")),
                    scope_key,
                    key_digest,
                    request_id,
                ),
            )
            return getattr(cur, "rowcount", 0) == 1

    def accept(
        self,
        *,
        scope_key: str,
        key_digest: str,
        request_id: str,
        request_fingerprint: str,
        job_id: str | None,
        response: dict[str, Any],
        result_expires_at: datetime,
        tombstone_expires_at: datetime,
    ) -> None:
        with self.conn.cursor() as cur:
            cur.execute(
                """
                UPDATE request_idempotency
                SET request_id = %s,
                    request_fingerprint = %s,
                    status = 'accepted',
                    job_id = %s,
                    replay_response_jsonb = %s::jsonb,
                    result_expires_at = %s,
                    tombstone_expires_at = %s,
                    lease_expires_at = NULL,
                    last_error = '',
                    updated_at = now()
                WHERE scope_key = %s AND key_digest = %s
                  AND request_id = %s AND status = 'pending'
                """,
                (
                    request_id,
                    request_fingerprint,
                    job_id,
                    json.dumps(response, sort_keys=True, separators=(",", ":")),
                    result_expires_at,
                    tombstone_expires_at,
                    scope_key,
                    key_digest,
                    request_id,
                ),
            )
            if getattr(cur, "rowcount", 1) != 1:
                raise RuntimeError("Idempotency claim is no longer owned by this request")

    def fail(
        self,
        *,
        scope_key: str,
        key_digest: str,
        request_id: str,
        request_fingerprint: str,
        error_code: str,
    ) -> None:
        with self.conn.cursor() as cur:
            cur.execute(
                """
                UPDATE request_idempotency
                SET request_fingerprint = %s,
                    status = 'failed',
                    job_id = NULL,
                    replay_response_jsonb = NULL,
                    result_expires_at = NULL,
                    tombstone_expires_at = now() + INTERVAL '1 day',
                    lease_expires_at = NULL,
                    last_error = %s,
                    updated_at = now()
                WHERE scope_key = %s AND key_digest = %s
                  AND request_id = %s AND status = 'pending'
                """,
                (
                    request_fingerprint,
                    str(error_code or "").strip()[:2000],
                    scope_key,
                    key_digest,
                    request_id,
                ),
            )
            if getattr(cur, "rowcount", 1) != 1:
                raise RuntimeError("Idempotency claim is no longer owned by this request")
