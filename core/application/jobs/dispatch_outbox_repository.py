"""PostgreSQL repository for durable pre-execution dispatch envelopes."""

from __future__ import annotations

import json
from typing import Any

from core.infrastructure.db import psycopg_runtime


class DispatchOutboxRepository:
    """Own the canonical job-dispatch outbox persistence contract."""

    def __init__(self, db: Any) -> None:
        self.db = db

    def create(
        self,
        *,
        job_id: str,
        encrypted_envelope: str,
        status: str = "pending",
        preparation_owner_token: str | None = None,
        preparation_lease_seconds: int = 300,
    ) -> None:
        if status not in {"pending", "preparing"}:
            raise ValueError("Unsupported initial job dispatch outbox status")
        owner_token = str(preparation_owner_token or "").strip() or None
        if status == "preparing" and owner_token is None:
            raise ValueError("Preparing dispatch requires an owner token")
        with self.db.cursor() as cur:
            cur.execute(
                """
                INSERT INTO job_dispatch_outbox
                    (job_id, encrypted_envelope, status, available_at,
                     preparation_owner_token, preparation_lease_expires_at)
                VALUES (
                    %s, %s, %s, now() + interval '30 seconds', %s::text,
                    CASE WHEN %s::text IS NULL THEN NULL
                         ELSE now() + (%s::int * interval '1 second') END
                )
                """,
                (
                    job_id,
                    encrypted_envelope,
                    status,
                    owner_token,
                    owner_token,
                    max(30, min(int(preparation_lease_seconds), 3600)),
                ),
            )

    def renew_preparation_lease(
        self, *, job_id: str, owner_token: str, lease_seconds: int = 300
    ) -> bool:
        """Renew a live preparation lease without allowing expired-owner resurrection."""
        with self.db.cursor() as cur:
            cur.execute(
                """
                UPDATE job_dispatch_outbox
                   SET preparation_lease_expires_at =
                           now() + (%s * interval '1 second'),
                       updated_at = now()
                 WHERE job_id = %s
                   AND status = 'preparing'
                   AND preparation_owner_token = %s
                   AND preparation_lease_expires_at > now()
                RETURNING job_id
                """,
                (
                    max(30, min(int(lease_seconds), 3600)),
                    job_id,
                    str(owner_token or "").strip(),
                ),
            )
            return cur.fetchone() is not None

    def finalize_prepared_payload(
        self,
        *,
        job_id: str,
        meta_patch: dict[str, Any],
        encrypted_envelope: str,
        preparation_owner_token: str,
    ) -> bool:
        """Atomically expose complete payload metadata and its final envelope.

        The caller owns the transaction boundary. Locking the outbox row fences
        this transition against stale-preparation cleanup.
        """
        with self.db.cursor() as cur:
            cur.execute(
                """
                SELECT outbox.job_id
                  FROM job_dispatch_outbox AS outbox
                  JOIN jobs ON jobs.id = outbox.job_id
                 WHERE outbox.job_id = %s
                   AND outbox.status = 'preparing'
                   AND outbox.preparation_owner_token = %s
                   AND outbox.preparation_lease_expires_at > now()
                   AND jobs.status = 'pending'
                   AND NULLIF(jobs.worker_id, '') IS NULL
                 FOR UPDATE OF outbox, jobs
                """,
                (job_id, str(preparation_owner_token or "").strip()),
            )
            if cur.fetchone() is None:
                raise RuntimeError("Durable dispatch payload is no longer preparing")
            cur.execute(
                """
                UPDATE jobs
                   SET meta = (COALESCE(meta, '{}'::jsonb) || %s::jsonb)
                              - 'attachment_persistence_artifacts'
                 WHERE id = %s
                   AND status = 'pending'
                   AND NULLIF(worker_id, '') IS NULL
                RETURNING id
                """,
                (json.dumps(meta_patch, default=str), job_id),
            )
            if cur.fetchone() is None:
                raise RuntimeError("Durable dispatch payload job could not be finalized")
            cur.execute(
                """
                UPDATE job_dispatch_outbox
                   SET encrypted_envelope = %s,
                       status = 'pending',
                       preparation_owner_token = NULL,
                       preparation_lease_expires_at = NULL,
                       available_at = now() + interval '30 seconds',
                       last_error = NULL,
                       updated_at = now()
                 WHERE job_id = %s
                   AND status = 'preparing'
                   AND preparation_owner_token = %s
                RETURNING job_id
                """,
                (
                    encrypted_envelope,
                    job_id,
                    str(preparation_owner_token or "").strip(),
                ),
            )
            if cur.fetchone() is None:
                raise RuntimeError("Durable dispatch payload is no longer preparing")
        return True

    def claim_expired_preparing(
        self, *, limit: int = 100, lease_seconds: int = 60
    ) -> list[dict[str, Any]]:
        """Lease stale preparations for artifact cleanup and terminalization."""
        with self.db.cursor() as cur:
            cur.execute(
                """
                WITH candidates AS (
                    SELECT outbox.job_id
                      FROM job_dispatch_outbox AS outbox
                      JOIN jobs ON jobs.id = outbox.job_id
                     WHERE jobs.status = 'pending'
                       AND NULLIF(jobs.worker_id, '') IS NULL
                       AND (
                            (outbox.status = 'preparing'
                             AND outbox.preparation_lease_expires_at <= now())
                            OR (outbox.status = 'cleanup_pending'
                                AND outbox.available_at <= now())
                       )
                     ORDER BY outbox.created_at
                     FOR UPDATE OF outbox SKIP LOCKED
                     LIMIT %s
                )
                UPDATE job_dispatch_outbox AS outbox
                   SET status = 'cleanup_pending',
                       preparation_owner_token = gen_random_uuid()::text,
                       preparation_lease_expires_at =
                           now() + (%s * interval '1 second'),
                       available_at = now() + (%s * interval '1 second'),
                       attempt_count = attempt_count + 1,
                       updated_at = now()
                  FROM candidates, jobs
                 WHERE outbox.job_id = candidates.job_id
                   AND jobs.id = outbox.job_id
                RETURNING outbox.job_id, jobs.meta, outbox.preparation_owner_token
                """,
                (
                    max(1, min(int(limit), 500)),
                    max(5, min(int(lease_seconds), 300)),
                    max(5, min(int(lease_seconds), 300)),
                ),
            )
            return [
                {
                    "job_id": str(row[0]),
                    "meta": row[1] if len(row) > 1 else {},
                    "owner_token": str(row[2]) if len(row) > 2 else "",
                }
                for row in (cur.fetchall() or [])
            ]

    def verify_cleanup_owner(self, *, job_id: str, owner_token: str) -> bool:
        with self.db.cursor() as cur:
            cur.execute(
                """
                SELECT job_id
                  FROM job_dispatch_outbox
                 WHERE job_id = %s
                   AND status = 'cleanup_pending'
                   AND preparation_owner_token = %s
                   AND preparation_lease_expires_at > now()
                 FOR UPDATE
                """,
                (job_id, str(owner_token or "").strip()),
            )
            return cur.fetchone() is not None

    def verify_preparation_owner(self, *, job_id: str, owner_token: str) -> bool:
        with self.db.cursor() as cur:
            cur.execute(
                """
                SELECT job_id
                  FROM job_dispatch_outbox
                 WHERE job_id = %s
                   AND status = 'preparing'
                   AND preparation_owner_token = %s
                   AND preparation_lease_expires_at > now()
                 FOR UPDATE
                """,
                (job_id, str(owner_token or "").strip()),
            )
            return cur.fetchone() is not None

    def has_live_preparation(self, *, job_id: str) -> bool:
        """Return whether an incomplete submission still has a live owner."""
        with self.db.cursor() as cur:
            cur.execute(
                """
                SELECT job_id
                  FROM job_dispatch_outbox
                 WHERE job_id = %s
                   AND status = 'preparing'
                   AND NULLIF(preparation_owner_token, '') IS NOT NULL
                   AND preparation_lease_expires_at > now()
                """,
                (job_id,),
            )
            return cur.fetchone() is not None

    def settle_preparation_failure(self, *, job_id: str, owner_token: str, last_error: str) -> bool:
        with self.db.cursor() as cur:
            cur.execute(
                """
                UPDATE job_dispatch_outbox
                   SET status = 'failed',
                       preparation_owner_token = NULL,
                       preparation_lease_expires_at = NULL,
                       last_error = %s,
                       updated_at = now()
                 WHERE job_id = %s
                   AND status = 'preparing'
                   AND preparation_owner_token = %s
                   AND preparation_lease_expires_at > now()
                RETURNING job_id
                """,
                (last_error, job_id, str(owner_token or "").strip()),
            )
            return cur.fetchone() is not None

    def settle_cleanup(
        self,
        *,
        job_id: str,
        owner_token: str,
        status: str,
        last_error: str,
    ) -> bool:
        if status not in {"failed", "superseded"}:
            raise ValueError("Unsupported preparation cleanup settlement")
        with self.db.cursor() as cur:
            cur.execute(
                """
                UPDATE job_dispatch_outbox
                   SET status = %s,
                       preparation_owner_token = NULL,
                       preparation_lease_expires_at = NULL,
                       last_error = %s,
                       updated_at = now()
                 WHERE job_id = %s
                   AND status = 'cleanup_pending'
                   AND preparation_owner_token = %s
                   AND preparation_lease_expires_at > now()
                RETURNING job_id
                """,
                (status, last_error, job_id, str(owner_token or "").strip()),
            )
            return cur.fetchone() is not None

    def get(self, *, job_id: str) -> dict[str, Any] | None:
        with self.db.cursor(cursor_factory=psycopg_runtime.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT job_id, encrypted_envelope, status, attempt_count,
                       available_at, published_at, last_error, created_at, updated_at
                  FROM job_dispatch_outbox
                 WHERE job_id = %s
                 LIMIT 1
                """,
                (job_id,),
            )
            row = cur.fetchone()
        return dict(row) if row else None

    def replace_envelope(self, *, job_id: str, encrypted_envelope: str) -> bool:
        with self.db.cursor() as cur:
            cur.execute(
                """
                UPDATE job_dispatch_outbox
                   SET encrypted_envelope = %s,
                       updated_at = now()
                 WHERE job_id = %s
                   AND status = 'pending'
                   AND published_at IS NULL
                """,
                (encrypted_envelope, job_id),
            )
            return bool(cur.rowcount)

    def mark(self, *, job_id: str, status: str, last_error: str | None = None) -> bool:
        if status not in {
            "pending",
            "published",
            "failed",
            "superseded",
            "cleanup_pending",
        }:
            raise ValueError("Unsupported job dispatch outbox status")
        with self.db.cursor() as cur:
            cur.execute(
                """
                UPDATE job_dispatch_outbox
                   SET status = %s,
                       attempt_count = attempt_count + 1,
                       published_at = CASE WHEN %s = 'published' THEN now() ELSE published_at END,
                       last_error = %s,
                       updated_at = now()
                 WHERE job_id = %s
                   AND (status <> 'published' OR %s = 'published')
                """,
                (status, status, last_error, job_id, status),
            )
            return bool(cur.rowcount)

    def settle_after_execution_advanced(self, *, job_id: str) -> bool:
        """Settle stale recovery work after execution or delivery has moved on."""
        with self.db.cursor() as cur:
            cur.execute(
                """
                UPDATE job_dispatch_outbox AS outbox
                   SET status = 'superseded',
                       last_error = 'worker_claimed',
                       updated_at = now()
                  FROM jobs
                 WHERE outbox.job_id = jobs.id
                   AND outbox.job_id = %s
                   AND outbox.status = 'pending'
                   AND (
                        NULLIF(jobs.worker_id, '') IS NOT NULL
                        OR jobs.status <> 'pending'
                        OR COALESCE(
                            jobs.meta->'queue_delivery'->>'delivery_succeeded',
                            'false'
                        ) = 'true'
                   )
                RETURNING outbox.job_id
                """,
                (job_id,),
            )
            return cur.fetchone() is not None

    def sweep_advanced_pending(self, *, limit: int = 100) -> int:
        """Settle pending outbox rows whose jobs already advanced."""
        with self.db.cursor() as cur:
            cur.execute(
                """
                WITH candidates AS (
                    SELECT outbox.job_id
                      FROM job_dispatch_outbox AS outbox
                      JOIN jobs ON jobs.id = outbox.job_id
                     WHERE outbox.status = 'pending'
                       AND (
                            NULLIF(jobs.worker_id, '') IS NOT NULL
                            OR jobs.status <> 'pending'
                            OR COALESCE(
                                jobs.meta->'queue_delivery'->>'delivery_succeeded',
                                'false'
                            ) = 'true'
                       )
                     ORDER BY outbox.updated_at, outbox.created_at
                     FOR UPDATE OF outbox SKIP LOCKED
                     LIMIT %s
                ), updated AS (
                    UPDATE job_dispatch_outbox AS outbox
                       SET status = 'superseded',
                           last_error = 'execution_advanced',
                           updated_at = now()
                      FROM candidates
                     WHERE outbox.job_id = candidates.job_id
                    RETURNING outbox.job_id
                )
                SELECT COUNT(*) FROM updated
                """,
                (max(1, min(int(limit), 500)),),
            )
            row = cur.fetchone()
            return int((row or (0,))[0] or 0)

    def claim_pending(self, *, limit: int = 100, lease_seconds: int = 30) -> list[str]:
        """Lease recoverable envelopes, fenced to jobs that have not started."""
        with self.db.cursor() as cur:
            cur.execute(
                """
                WITH candidates AS (
                    SELECT outbox.job_id
                      FROM job_dispatch_outbox AS outbox
                      JOIN jobs ON jobs.id = outbox.job_id
                     WHERE outbox.status = 'pending'
                       AND outbox.available_at <= now()
                       AND jobs.status = 'pending'
                       AND NULLIF(jobs.worker_id, '') IS NULL
                       AND COALESCE(jobs.meta->'queue_delivery'->>'delivery_succeeded', 'false')
                           <> 'true'
                       AND COALESCE(
                           jobs.meta->'task_envelope'->>'payload_complete',
                           'true'
                       ) = 'true'
                       AND COALESCE(
                           jobs.meta->'queue_delivery'->>'dispatch_state',
                           'not_started'
                       ) IN ('not_started', 'attempted')
                     ORDER BY outbox.available_at, outbox.created_at
                     FOR UPDATE OF outbox SKIP LOCKED
                     LIMIT %s
                )
                UPDATE job_dispatch_outbox AS outbox
                   SET available_at = now() + (%s * interval '1 second'),
                       attempt_count = attempt_count + 1,
                       updated_at = now()
                  FROM candidates
                 WHERE outbox.job_id = candidates.job_id
                RETURNING outbox.job_id
                """,
                (max(1, min(int(limit), 500)), max(5, min(int(lease_seconds), 300))),
            )
            return [str(row[0]) for row in (cur.fetchall() or [])]


__all__ = ["DispatchOutboxRepository"]
