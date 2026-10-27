"""PostgreSQL adapter for durable approval-resume state transitions."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from core.application.jobs.workflow_resume_finalization_repository import (
    WorkflowResumeFinalizationRepository,
)
from core.application.jobs.workflow_resume_state import (
    WORKFLOW_RESUME_MAX_ATTEMPTS,
    WORKFLOW_RESUME_PUBLISH_LEASE_SECONDS,
    WORKFLOW_RESUME_RETRY_DELAY_SECONDS,
    WorkflowResumeClaim,
    WorkflowResumeQueueRow,
    coerce_resume_meta,
    workflow_resume_revision,
    workflow_resume_row_from_db,
)
from core.application.management.errors import (
    ServiceConflictError,
    ServiceUnavailableError,
)


class WorkflowResumeRepositoryAdapter:
    """Persist, publish, claim, renew, and complete workflow resume intents."""

    def __init__(
        self,
        bl: Any,
        *,
        token_factory: Callable[[], str] | None = None,
    ):
        self._bl = bl
        self._finalization = WorkflowResumeFinalizationRepository(bl)
        self._token_factory = token_factory or (lambda: uuid4().hex)

    @property
    def available(self) -> bool:
        return getattr(self._bl, "conn", None) is not None

    def get_job(self, job_id: str) -> Mapping[str, Any] | None:
        getter = getattr(self._bl, "get_job", None)
        if not callable(getter):
            return None
        row = getter(str(job_id))
        return row if isinstance(row, Mapping) else None

    @property
    def conn(self) -> Any:
        conn = getattr(self._bl, "conn", None)
        if conn is None:
            raise ServiceUnavailableError("Job store unavailable — cannot manage workflow resume")
        return conn

    def mark_pending(
        self,
        *,
        thread_id: str,
        job_context: Mapping[str, Any],
        approval_id: str,
        resume_value: Mapping[str, Any],
        approved: bool,
        action: str | None,
        comment: str,
        interrupt_payload: Mapping[str, Any],
        actor_username: str,
    ) -> WorkflowResumeQueueRow:
        """Record the accepted approval decision before publishing the worker message."""

        meta = coerce_resume_meta(job_context.get("meta"))
        previous_revision = workflow_resume_revision(meta)
        revision = previous_revision + 1
        publish_token = self._token_factory()
        approval_intent = {
            "state": "pending",
            "revision": revision,
            "approval_id": str(approval_id or "").strip(),
            "resume_value": dict(resume_value),
            "approved": bool(approved),
            "action": action,
            "comment": comment,
            "interrupt_payload": dict(interrupt_payload),
            "actor_username": actor_username,
            "queued_at": datetime.now(UTC).isoformat(),
            "published_at": None,
            "lease_token": "",
            "lease_expires_at": None,
            "last_publish_error": "",
            "publish_token": publish_token,
            "publish_lease_expires_at": None,
        }

        conn = self.conn
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE jobs
                   SET status = 'resume_pending',
                       finished_at = NULL,
                       duration_ms = NULL,
                       final_report = NULL,
                       error = NULL,
                       heartbeat_at = NOW(),
                       last_error = NULL,
                       meta = jsonb_set(
                           jsonb_set(
                               COALESCE(meta, '{}'::jsonb),
                               '{workflow_resume}',
                               %s::jsonb,
                               true
                           ),
                           '{workflow_resume,publish_lease_expires_at}',
                           to_jsonb((NOW() + make_interval(secs => %s))::text),
                           true
                       )
                 WHERE thread_id = %s
                   AND status = 'awaiting_approval'
                   AND COALESCE((meta->'workflow_resume'->>'revision')::int, 0) = %s
                 RETURNING id, project_id, task_id,
                           COALESCE((meta->'workflow_resume'->>'revision')::int, %s)
                """,
                (
                    json.dumps(approval_intent, default=str),
                    WORKFLOW_RESUME_PUBLISH_LEASE_SECONDS,
                    thread_id,
                    previous_revision,
                    revision,
                ),
            )
            updated = cur.fetchone()
            if not updated or not bool(getattr(cur, "rowcount", 1)):
                conn.rollback()
                raise ServiceConflictError(
                    "Workflow resume was not accepted because the job is no longer awaiting approval"
                )
            conn.commit()
        return workflow_resume_row_from_db(
            updated,
            job_context,
            revision,
            publish_token=publish_token,
        )

    def claim_due_timers(
        self, *, now_epoch_ms: int, limit: int
    ) -> list[tuple[WorkflowResumeQueueRow, str]]:
        """Turn due ``awaiting_timer`` jobs into stored timer resume intents.

        One statement locks the due rows (``SKIP LOCKED``: concurrent
        dispatchers never claim the same job), writes the intent with a new
        revision and moves the job to ``resume_pending``. The due lookup uses
        exactly the expression and predicate of the partial index
        ``idx_jobs_workflow_wait_due``. A cancel that commits first changes the
        status, so its job is no longer selected.
        """
        conn = self.conn
        with conn.cursor() as cur:
            cur.execute(
                """
                WITH due AS (
                    SELECT id
                      FROM jobs
                     WHERE status = 'awaiting_timer'
                       AND ((meta->'workflow_wait'->>'due_epoch_ms')::bigint) <= %s
                       AND thread_id IS NOT NULL
                       AND task_id IS NOT NULL
                       AND COALESCE(meta->'workflow_wait'->>'wait_id', '') <> ''
                     ORDER BY ((meta->'workflow_wait'->>'due_epoch_ms')::bigint), id
                     LIMIT %s
                     FOR UPDATE SKIP LOCKED
                ),
                intents AS (
                    SELECT j.id,
                           COALESCE((j.meta->'workflow_resume'->>'revision')::int, 0) + 1
                               AS revision,
                           md5(random()::text || clock_timestamp()::text || j.id::text)
                               AS publish_token,
                           j.meta->'workflow_wait' AS wait
                      FROM jobs j
                      JOIN due ON due.id = j.id
                )
                UPDATE jobs j
                   SET status = 'resume_pending',
                       heartbeat_at = NOW(),
                       last_error = NULL,
                       meta = jsonb_set(
                           COALESCE(j.meta, '{}'::jsonb),
                           '{workflow_resume}',
                           jsonb_build_object(
                               'reason', 'timer',
                               'wait_id', intents.wait->>'wait_id',
                               'state', 'pending',
                               'revision', intents.revision,
                               'resume_value', jsonb_build_object(
                                   'reason', 'timer',
                                   'wait_id', intents.wait->>'wait_id',
                                   'timer_elapsed', true
                               ),
                               'approved', false,
                               'action', NULL,
                               'comment', '',
                               'interrupt_payload', intents.wait,
                               'actor_username', 'workflow_timer',
                               'queued_at', NOW()::text,
                               'published_at', NULL,
                               'lease_token', '',
                               'lease_expires_at', NULL,
                               'last_publish_error', '',
                               'publish_token', intents.publish_token,
                               'publish_lease_expires_at',
                                   (NOW() + make_interval(secs => %s))::text
                           ),
                           true
                       )
                  FROM intents
                 WHERE j.id = intents.id
                RETURNING j.id, j.project_id, j.task_id, j.thread_id,
                          intents.revision, intents.publish_token
                """,
                (int(now_epoch_ms), max(1, int(limit)), WORKFLOW_RESUME_PUBLISH_LEASE_SECONDS),
            )
            rows = cur.fetchall() or []
            conn.commit()
        return self._claimed_rows(rows)

    @staticmethod
    def _claimed_rows(rows: list[Any]) -> list[tuple[WorkflowResumeQueueRow, str]]:
        claimed: list[tuple[WorkflowResumeQueueRow, str]] = []
        for row in rows:
            values = list(row.values()) if isinstance(row, Mapping) else list(row)
            job_id, project_id, task_id, thread_id, revision, publish_token = values[:6]
            claimed.append(
                (
                    WorkflowResumeQueueRow(
                        job_id=str(job_id or ""),
                        project_id=str(project_id or ""),
                        task_id=str(task_id or ""),
                        resume_revision=int(revision or 0),
                        publish_token=str(publish_token or ""),
                    ),
                    str(thread_id or ""),
                )
            )
        return claimed

    def claim_finished_children(self, *, limit: int) -> list[tuple[WorkflowResumeQueueRow, str]]:
        """Turn parents whose child job finished into stored child resume intents.

        Parents parked ``awaiting_child`` are joined to their child job through
        ``meta.workflow_wait.child_job_id`` (partial index
        ``idx_jobs_workflow_wait_child``). A child that finished before its
        parent parked is found the same way once the parent parks. Same
        single statement, ``SKIP LOCKED`` claim and intent shape as timers.
        """
        conn = self.conn
        with conn.cursor() as cur:
            cur.execute(
                """
                WITH finished AS (
                    SELECT parent.id,
                           parent.meta->'workflow_wait'->>'child_job_id' AS child_job_id,
                           COALESCE(lower(child.status), 'missing') AS child_status
                      FROM jobs parent
                      LEFT JOIN jobs child
                        ON child.id = CASE
                            WHEN parent.meta->'workflow_wait'->>'child_job_id'
                                ~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
                            THEN (parent.meta->'workflow_wait'->>'child_job_id')::uuid
                        END
                     WHERE parent.status = 'awaiting_child'
                       AND parent.thread_id IS NOT NULL
                       AND parent.task_id IS NOT NULL
                       AND COALESCE(parent.meta->'workflow_wait'->>'wait_id', '') <> ''
                       AND (
                           child.id IS NULL
                           OR lower(child.status) IN ('completed', 'failed', 'cancelled', 'max_iterations')
                           OR (lower(child.status) = 'stuck' AND child.finished_at IS NOT NULL)
                       )
                     ORDER BY parent.id
                     LIMIT %s
                     FOR UPDATE OF parent SKIP LOCKED
                ),
                intents AS (
                    SELECT j.id,
                           finished.child_job_id,
                           finished.child_status,
                           COALESCE((j.meta->'workflow_resume'->>'revision')::int, 0) + 1
                               AS revision,
                           md5(random()::text || clock_timestamp()::text || j.id::text)
                               AS publish_token,
                           j.meta->'workflow_wait' AS wait
                      FROM jobs j
                      JOIN finished ON finished.id = j.id
                )
                UPDATE jobs j
                   SET status = 'resume_pending',
                       heartbeat_at = NOW(),
                       last_error = NULL,
                       meta = jsonb_set(
                           COALESCE(j.meta, '{}'::jsonb),
                           '{workflow_resume}',
                           jsonb_build_object(
                               'reason', 'child',
                               'wait_id', intents.wait->>'wait_id',
                               'state', 'pending',
                               'revision', intents.revision,
                               'resume_value', jsonb_build_object(
                                   'reason', 'child',
                                   'wait_id', intents.wait->>'wait_id',
                                   'child_job_id', intents.child_job_id,
                                   'child_status', intents.child_status
                               ),
                               'approved', false,
                               'action', NULL,
                               'comment', '',
                               'interrupt_payload', intents.wait,
                               'actor_username', 'workflow_child',
                               'queued_at', NOW()::text,
                               'published_at', NULL,
                               'lease_token', '',
                               'lease_expires_at', NULL,
                               'last_publish_error', '',
                               'publish_token', intents.publish_token,
                               'publish_lease_expires_at',
                                   (NOW() + make_interval(secs => %s))::text
                           ),
                           true
                       )
                  FROM intents
                 WHERE j.id = intents.id
                RETURNING j.id, j.project_id, j.task_id, j.thread_id,
                          intents.revision, intents.publish_token
                """,
                (max(1, int(limit)), WORKFLOW_RESUME_PUBLISH_LEASE_SECONDS),
            )
            rows = cur.fetchall() or []
            conn.commit()
        return self._claimed_rows(rows)

    def mark_publish_failed(
        self,
        *,
        thread_id: str,
        resume_revision: int,
        publish_token: str,
        error: str,
    ) -> bool:
        failed_at = datetime.now(UTC).isoformat()
        conn = self.conn
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE jobs
                   SET status = 'resume_pending',
                       heartbeat_at = NOW(),
                       last_error = %s,
                       meta = jsonb_set(
                           COALESCE(meta, '{}'::jsonb),
                           '{workflow_resume}',
                           COALESCE(meta->'workflow_resume', '{}'::jsonb)
                           || jsonb_build_object(
                               'state', %s::jsonb,
                               'last_publish_error', %s::jsonb,
                               'failed_at', %s::jsonb,
                               'publish_token', '',
                               'publish_lease_expires_at', NULL
                           ),
                           true
                       )
                 WHERE thread_id = %s
                   AND status = 'resume_pending'
                   AND COALESCE((meta->'workflow_resume'->>'revision')::int, 0) = %s
                   AND COALESCE(meta->'workflow_resume'->>'state', '') = 'pending'
                   AND COALESCE(meta->'workflow_resume'->>'publish_token', '') = %s
                """,
                (
                    str(error or ""),
                    json.dumps("publish_failed"),
                    json.dumps(str(error or "")),
                    json.dumps(failed_at),
                    thread_id,
                    int(resume_revision),
                    publish_token,
                ),
            )
            updated = bool(getattr(cur, "rowcount", 0))
            conn.commit()
        return updated

    def mark_published(
        self,
        *,
        thread_id: str,
        resume_revision: int,
        publish_token: str,
    ) -> bool:
        conn = self.conn
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE jobs
                   SET heartbeat_at = NOW(),
                       last_error = NULL,
                       meta = jsonb_set(
                           COALESCE(meta, '{}'::jsonb),
                           '{workflow_resume}',
                           COALESCE(meta->'workflow_resume', '{}'::jsonb)
                           || jsonb_build_object(
                               'state', %s::jsonb,
                               'published_at', NOW()::text,
                               'last_publish_error', '',
                               'publish_token', '',
                               'publish_lease_expires_at', NULL
                           ),
                           true
                       )
                 WHERE thread_id = %s
                   AND status = 'resume_pending'
                   AND COALESCE((meta->'workflow_resume'->>'revision')::int, 0) = %s
                   AND COALESCE(meta->'workflow_resume'->>'state', '') = 'pending'
                   AND COALESCE(meta->'workflow_resume'->>'publish_token', '') = %s
                """,
                (json.dumps("published"), thread_id, int(resume_revision), publish_token),
            )
            updated = bool(getattr(cur, "rowcount", 0))
            conn.commit()
        return updated

    def claim_execution(
        self,
        *,
        job_id: str,
        resume_revision: int,
        worker_id: str,
        lease_token: str,
        lease_seconds: int,
    ) -> WorkflowResumeClaim:
        conn = self.conn
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE jobs
                   SET status = 'in_progress',
                       worker_id = %s,
                       started_at = COALESCE(started_at, NOW()),
                       finished_at = NULL,
                       duration_ms = NULL,
                       error = NULL,
                       heartbeat_at = NOW(),
                       last_error = NULL,
                       -- The claimed resume consumes the projected wait; a new
                       -- pause is projected again when the run parks.
                       meta = jsonb_set(
                           jsonb_set(
                               jsonb_set(
                                   jsonb_set(COALESCE(meta, '{}'::jsonb), '{workflow_resume}',
                                       COALESCE(meta->'workflow_resume', '{}'::jsonb)
                                       || jsonb_build_object(
                                           'execution_attempts', LEAST(%s, COALESCE((meta->'workflow_resume'->>'execution_attempts')::int, 0) + 1),
                                           'exhaustion_pending', COALESCE((meta->'workflow_resume'->>'execution_attempts')::int, 0) >= %s
                                       ), true),
                                   '{workflow_resume,state}',
                                   '"running"'::jsonb,
                                   true
                               ),
                               '{workflow_resume,lease_token}',
                               %s::jsonb,
                               true
                               ),
                               '{workflow_resume,lease_expires_at}',
                               to_jsonb((NOW() + make_interval(secs => %s))::text),
                               true
                       ) - 'workflow_wait'
                 WHERE id = %s
                   AND COALESCE((meta->'workflow_resume'->>'revision')::int, 0) = %s
                   AND COALESCE(NULLIF(meta->'workflow_resume'->>'retry_not_before', '')::timestamptz, '-infinity'::timestamptz) <= NOW()
                   AND (
                       status = 'resume_pending'
                       OR (
                           status = 'in_progress'
                           AND COALESCE(meta->'workflow_resume'->>'state', '') = 'running'
                           AND COALESCE(
                               NULLIF(meta->'workflow_resume'->>'lease_expires_at', '')::timestamptz,
                               '-infinity'::timestamptz
                           ) <= NOW()
                       )
                   )
                RETURNING meta->'workflow_resume'
                """,
                (
                    worker_id,
                    WORKFLOW_RESUME_MAX_ATTEMPTS,
                    WORKFLOW_RESUME_MAX_ATTEMPTS,
                    json.dumps(lease_token),
                    max(1, int(lease_seconds)),
                    job_id,
                    int(resume_revision),
                ),
            )
            claimed = bool(getattr(cur, "rowcount", 0))
            row = cur.fetchone() if claimed else None
            raw_intent: Any = {}
            if row:
                raw_intent = next(iter(row.values())) if isinstance(row, Mapping) else row[0]
            intent = coerce_resume_meta(raw_intent)
            conn.commit()
        return WorkflowResumeClaim(
            claimed=claimed,
            lease_token=lease_token if claimed else "",
            attempts=int(intent.get("execution_attempts") or 1),
            exhausted=bool(intent.get("exhaustion_pending")),
        )

    def renew_execution(
        self,
        *,
        job_id: str,
        resume_revision: int,
        lease_token: str,
        lease_seconds: int,
    ) -> bool:
        conn = self.conn
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE jobs
                   SET heartbeat_at = NOW(),
                       meta = jsonb_set(
                           COALESCE(meta, '{}'::jsonb),
                           '{workflow_resume,lease_expires_at}',
                           to_jsonb((NOW() + make_interval(secs => %s))::text),
                           true
                       )
                 WHERE id = %s
                   AND status = 'in_progress'
                   AND COALESCE((meta->'workflow_resume'->>'revision')::int, 0) = %s
                   AND COALESCE(meta->'workflow_resume'->>'state', '') = 'running'
                   AND COALESCE(meta->'workflow_resume'->>'lease_token', '') = %s
                   AND COALESCE(
                       NULLIF(meta->'workflow_resume'->>'lease_expires_at', '')::timestamptz,
                       '-infinity'::timestamptz
                   ) > NOW()
                """,
                (
                    max(1, int(lease_seconds)),
                    job_id,
                    int(resume_revision),
                    lease_token,
                ),
            )
            renewed = bool(getattr(cur, "rowcount", 0))
            conn.commit()
        return renewed

    def complete_failed(
        self,
        *,
        job_id: str,
        resume_revision: int,
        lease_token: str,
        worker_id: str,
        error: str,
    ) -> bool:
        return self._finalization.complete_failed(
            job_id=job_id,
            resume_revision=resume_revision,
            lease_token=lease_token,
            worker_id=worker_id,
            error=error,
        )

    def mark_retryable(
        self,
        *,
        job_id: str,
        resume_revision: int,
        lease_token: str,
        error: str,
    ) -> bool:
        conn = self.conn
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE jobs
                   SET status = 'resume_pending',
                       heartbeat_at = NOW(),
                       last_error = NULLIF(%s, ''),
                       meta = jsonb_set(
                           jsonb_set(
                               jsonb_set(
                                   jsonb_set(COALESCE(meta, '{}'::jsonb), '{workflow_resume,retry_not_before}',
                                       to_jsonb((NOW() + make_interval(secs => %s))::text), true),
                                   '{workflow_resume,state}',
                                   '"publish_failed"'::jsonb,
                                   true
                               ),
                               '{workflow_resume,lease_token}',
                               '""'::jsonb,
                               true
                           ),
                           '{workflow_resume,last_publish_error}',
                           %s::jsonb,
                           true
                       )
                 WHERE id = %s
                   AND status = 'in_progress'
                   AND COALESCE((meta->'workflow_resume'->>'revision')::int, 0) = %s
                   AND COALESCE(meta->'workflow_resume'->>'state', '') = 'running'
                   AND COALESCE(meta->'workflow_resume'->>'lease_token', '') = %s
                   AND COALESCE(
                       NULLIF(meta->'workflow_resume'->>'lease_expires_at', '')::timestamptz,
                       '-infinity'::timestamptz
                   ) > NOW()
                """,
                (
                    str(error or ""),
                    WORKFLOW_RESUME_RETRY_DELAY_SECONDS,
                    json.dumps(str(error or "")),
                    job_id,
                    int(resume_revision),
                    lease_token,
                ),
            )
            updated = bool(getattr(cur, "rowcount", 0))
            conn.commit()
        return updated

    def mark_finalizing(
        self,
        *,
        job_id: str,
        resume_revision: int,
        lease_token: str,
        final_status: str,
        result: Mapping[str, Any],
        error: str | None = None,
    ) -> bool:
        return self._finalization.mark_finalizing(
            job_id=job_id,
            resume_revision=resume_revision,
            lease_token=lease_token,
            final_status=final_status,
            result=result,
            error=error,
        )

    def complete(
        self,
        *,
        job_id: str,
        resume_revision: int,
        lease_token: str = "",
        finalizer_token: str = "",
    ) -> bool:
        return self._finalization.complete(
            job_id=job_id,
            resume_revision=resume_revision,
            lease_token=lease_token,
            finalizer_token=finalizer_token,
        )

    def claim_finalizer(
        self, *, job_id: str, resume_revision: int, finalizer_token: str, lease_seconds: int
    ) -> bool:
        return self._finalization.claim_finalizer(
            job_id=job_id,
            resume_revision=resume_revision,
            finalizer_token=finalizer_token,
            lease_seconds=lease_seconds,
        )

    def mark_finalization_step_completed(
        self,
        *,
        job_id: str,
        resume_revision: int,
        finalizer_token: str,
        step: str,
    ) -> bool:
        return self._finalization.mark_finalization_step_completed(
            job_id=job_id,
            resume_revision=resume_revision,
            finalizer_token=finalizer_token,
            step=step,
        )

    def renew_finalizer(
        self, *, job_id: str, resume_revision: int, finalizer_token: str, lease_seconds: int
    ) -> bool:
        return self._finalization.renew_finalizer(
            job_id=job_id,
            resume_revision=resume_revision,
            finalizer_token=finalizer_token,
            lease_seconds=lease_seconds,
        )

    def release_finalizer(
        self,
        *,
        job_id: str,
        resume_revision: int,
        finalizer_token: str,
        error: str,
    ) -> bool:
        return self._finalization.release_finalizer(
            job_id=job_id,
            resume_revision=resume_revision,
            finalizer_token=finalizer_token,
            error=error,
        )

    def pending_rows(self, *, batch_size: int) -> list[dict[str, Any]]:
        publish_token = self._token_factory()
        conn = self.conn
        with conn.cursor() as cur:
            cur.execute(
                """
                WITH candidates AS (
                    SELECT candidate.id
                      FROM jobs AS candidate
                     WHERE candidate.thread_id IS NOT NULL
                       AND candidate.task_id IS NOT NULL
                       AND COALESCE(NULLIF(candidate.meta->'workflow_resume'->>'retry_not_before', '')::timestamptz,
                                    '-infinity'::timestamptz) <= NOW()
                       AND COALESCE(
                           (candidate.meta->'workflow_resume'->>'revision')::int,
                           0
                       ) > 0
                       AND (
                           (
                               candidate.status = 'resume_pending'
                               AND COALESCE(
                                   candidate.meta->'workflow_resume'->>'state',
                                   'pending'
                               ) IN ('pending', 'publish_failed')
                           )
                           OR (
                               candidate.status = 'in_progress'
                               AND COALESCE(
                                   candidate.meta->'workflow_resume'->>'state',
                                   ''
                               ) = 'running'
                               AND COALESCE(
                                   NULLIF(
                                       candidate.meta->'workflow_resume'->>'lease_expires_at',
                                       ''
                                   )::timestamptz,
                                   '-infinity'::timestamptz
                               ) <= NOW()
                           )
                           OR (
                               candidate.status IN (
                                   'completed',
                                   'failed',
                                   'stuck',
                                   'max_iterations',
                                   'cancelled',
                                   'awaiting_approval',
                                   'awaiting_timer',
                                   'awaiting_child'
                               )
                               AND COALESCE(
                                   candidate.meta->'workflow_resume'->>'state',
                                   ''
                               ) = 'finalizing'
                               AND (
                                   COALESCE(
                                       candidate.meta->'workflow_resume'->>'finalizer_token',
                                       ''
                                   ) = ''
                                   OR COALESCE(
                                       NULLIF(
                                           candidate.meta->'workflow_resume'
                                               ->>'finalizer_expires_at',
                                           ''
                                       )::timestamptz,
                                       '-infinity'::timestamptz
                                   ) <= NOW()
                               )
                           )
                       )
                       AND (
                           COALESCE(
                               candidate.meta->'workflow_resume'->>'publish_token',
                               ''
                           ) = ''
                           OR COALESCE(
                               NULLIF(
                                   candidate.meta->'workflow_resume'
                                       ->>'publish_lease_expires_at',
                                   ''
                               )::timestamptz,
                               '-infinity'::timestamptz
                           ) <= NOW()
                       )
                     ORDER BY candidate.heartbeat_at ASC NULLS FIRST,
                              candidate.queued_at ASC NULLS LAST,
                              candidate.id ASC
                     LIMIT %s
                     FOR UPDATE SKIP LOCKED
                )
                UPDATE jobs AS job
                   SET heartbeat_at = NOW(),
                       meta = jsonb_set(
                           jsonb_set(
                               COALESCE(job.meta, '{}'::jsonb),
                               '{workflow_resume,publish_token}',
                               %s::jsonb,
                               true
                           ),
                           '{workflow_resume,publish_lease_expires_at}',
                           to_jsonb((NOW() + make_interval(secs => %s))::text),
                           true
                       )
                  FROM candidates
                 WHERE job.id = candidates.id
                RETURNING job.id,
                          job.project_id,
                          job.task_id,
                          job.thread_id,
                          COALESCE(
                              (job.meta->'workflow_resume'->>'revision')::int,
                              0
                          ) AS resume_revision,
                          COALESCE(
                              job.meta->'workflow_resume'->>'state',
                              'pending'
                          ) AS publish_state,
                          COALESCE(
                              job.meta->'workflow_resume'->>'publish_token',
                              ''
                          ) AS publish_token
                """,
                (
                    max(1, int(batch_size or 1)),
                    json.dumps(publish_token),
                    WORKFLOW_RESUME_PUBLISH_LEASE_SECONDS,
                ),
            )
            rows = cur.fetchall() or []
            conn.commit()
        columns = (
            "id",
            "project_id",
            "task_id",
            "thread_id",
            "resume_revision",
            "publish_state",
            "publish_token",
        )
        return [
            dict(row) if isinstance(row, Mapping) else dict(zip(columns, row, strict=False))
            for row in rows
        ]

    def mark_publish_state(
        self,
        *,
        job_id: str,
        resume_revision: int,
        publish_token: str,
        expected_state: str,
        state: str,
        error: str = "",
    ) -> bool:
        published_at = datetime.now(UTC).isoformat() if not error else None
        conn = self.conn
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE jobs
                   SET heartbeat_at = NOW(),
                       last_error = NULLIF(%s, ''),
                       meta = jsonb_set(
                           COALESCE(meta, '{}'::jsonb),
                           '{workflow_resume}',
                           COALESCE(meta->'workflow_resume', '{}'::jsonb)
                           || jsonb_build_object(
                               'state', %s::jsonb,
                               'published_at', %s::jsonb,
                               'last_publish_error', %s::jsonb,
                               'publish_token', '',
                               'publish_lease_expires_at', NULL
                           ),
                           true
                       )
                 WHERE id = %s
                   AND COALESCE((meta->'workflow_resume'->>'revision')::int, 0) = %s
                   AND COALESCE(meta->'workflow_resume'->>'state', '') = %s
                   AND COALESCE(meta->'workflow_resume'->>'publish_token', '') = %s
                """,
                (
                    str(error or ""),
                    json.dumps(str(state or "").strip() or "pending"),
                    json.dumps(published_at),
                    json.dumps(str(error or "")),
                    job_id,
                    int(resume_revision),
                    str(expected_state or ""),
                    publish_token,
                ),
            )
            updated = bool(getattr(cur, "rowcount", 0))
            conn.commit()
        return updated
