"""PostgreSQL terminal persistence for leased workflow resume intents."""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from core.application.jobs.workflow_resume_state import (
    WORKFLOW_RESUME_FINALIZATION_STEPS,
    WORKFLOW_RESUME_MAX_ATTEMPTS,
    WORKFLOW_RESUME_RETRY_DELAY_SECONDS,
    coerce_resume_meta,
)
from core.application.management.errors import (
    ServiceUnavailableError,
)


class WorkflowResumeFinalizationRepository:
    """Persist terminal outcomes and bounded finalizer progress under the same job lease."""

    def __init__(self, bl: Any):
        self._bl = bl

    @property
    def conn(self) -> Any:
        conn = getattr(self._bl, "conn", None)
        if conn is None:
            raise ServiceUnavailableError("Job store unavailable — cannot manage workflow resume")
        return conn

    def complete_failed(
        self,
        *,
        job_id: str,
        resume_revision: int,
        lease_token: str,
        worker_id: str,
        error: str,
    ) -> bool:
        """End an exhausted resume and request child cancellation under its live fence."""
        return self._complete_failure_under_resume_lease(
            job_id=job_id,
            resume_revision=resume_revision,
            lease_token=lease_token,
            worker_id=worker_id,
            status="failed",
            error_code="resume_failed",
            error=error or "Workflow resume attempts exhausted",
            result={"status": "failed", "error_code": "resume_failed", "error": error},
        )

    def _complete_failure_under_resume_lease(
        self,
        *,
        job_id: str,
        resume_revision: int,
        lease_token: str,
        status: str,
        error_code: str | None,
        error: str,
        result: Mapping[str, Any],
        worker_id: str | None = None,
    ) -> bool:
        from core.application.jobs.terminal_persistence import TerminalPersistenceService
        from core.infrastructure.db.db_pool import transaction_connection

        with transaction_connection(self.conn) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT meta->'workflow_resume' AS resume, worker_id FROM jobs
                         WHERE id = %s AND status = 'in_progress'
                           AND (%s::text IS NULL OR worker_id = %s)
                           AND (meta->'workflow_resume'->>'revision')::int = %s
                           AND meta->'workflow_resume'->>'state' = 'running'
                           AND meta->'workflow_resume'->>'lease_token' = %s
                           AND (meta->'workflow_resume'->>'lease_expires_at')::timestamptz > NOW()
                         FOR UPDATE""",
                    (job_id, worker_id, worker_id, resume_revision, lease_token),
                )
                row = cur.fetchone()
            if not row:
                return False
            intent = coerce_resume_meta(row.get("resume") if isinstance(row, Mapping) else row[0])
            owner = row.get("worker_id") if isinstance(row, Mapping) else row[1]
            intent.update(
                state="finalizing",
                lease_token="",
                lease_expires_at=None,
                error=error,
                final_status=status,
                exhaustion_pending=False,
                finalization_steps=dict.fromkeys(WORKFLOW_RESUME_FINALIZATION_STEPS, "pending"),
                finalization_attempts=0,
                finalizer_token="",
                finalizer_expires_at=None,
                result=dict(result),
            )
            TerminalPersistenceService(self._bl).complete_failure(
                job_id=job_id,
                status=status,
                error_code=error_code,
                error_message=error,
                category="cancellation" if status == "cancelled" else "execution",
                expected_worker_id=str(owner or ""),
                cancel_children=True,
                meta_patch={"workflow_resume": intent},
            )
        return True

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
        if final_status == "cancelled":
            # Budget/approval aborts use the same terminal persistence and
            # durable child cancellation as operator cancellation. Finalizer
            # side effects still run after the atomic terminal transaction.
            return self._complete_failure_under_resume_lease(
                job_id=job_id,
                resume_revision=resume_revision,
                lease_token=lease_token,
                status="cancelled",
                error_code=str(result.get("error_code") or "") or None,
                error=error or "Workflow aborted by user",
                result=result,
            )
        finalizing_at = datetime.now(UTC).isoformat()
        workflow_outputs = result.get("workflow_outputs")
        meta_patch = (
            {"workflow_outputs": dict(workflow_outputs)}
            if isinstance(workflow_outputs, Mapping)
            else {}
        )
        workflow_wait = result.get("workflow_wait")
        if isinstance(workflow_wait, Mapping):
            # A run that parks again mirrors its new pause; the old one was
            # consumed by the claim, so nothing stale survives finalization.
            meta_patch["workflow_wait"] = dict(workflow_wait)
        approval = result.get("approval")
        if final_status == "awaiting_approval" and isinstance(approval, Mapping):
            approval_id = str(approval.get("approval_id") or "").strip()
            if approval_id:
                # Test Run polling reads this durable mirror. Commit it with
                # the new pause so the UI never submits the previous gate ID.
                meta_patch["state_approval_id"] = approval_id
                meta_patch["approval_context"] = str(approval.get("message") or "")
        conn = self.conn
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE jobs
                   SET status = %s,
                       finished_at = CASE
                           WHEN %s IN ('awaiting_approval', 'awaiting_timer', 'awaiting_child')
                           THEN NULL
                           ELSE NOW()
                       END,
                       duration_ms = CASE
                           WHEN %s IN ('awaiting_approval', 'awaiting_timer', 'awaiting_child')
                           THEN NULL
                           ELSE EXTRACT(EPOCH FROM (NOW() - started_at))::INTEGER * 1000
                       END,
                       final_report = %s,
                       error = NULLIF(%s, ''),
                       heartbeat_at = NOW(),
                       last_error = NULLIF(%s, ''),
                       meta = jsonb_set(
                           COALESCE(meta, '{}'::jsonb) || %s::jsonb,
                           '{workflow_resume}',
                           COALESCE(meta->'workflow_resume', '{}'::jsonb)
                           || jsonb_build_object(
                               'state', 'finalizing',
                               'finalization_steps', %s::jsonb,
                               'final_status', %s::jsonb,
                               'result', %s::jsonb,
                               'error', %s::jsonb,
                               'finalizing_at', %s::jsonb,
                               'lease_token', '',
                               'lease_expires_at', NULL,
                               'finalizer_token', '',
                               'finalizer_expires_at', NULL
                           ),
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
                    str(final_status or ""),
                    str(final_status or ""),
                    str(final_status or ""),
                    dict(result).get("final_report"),
                    str(error or ""),
                    str(error or ""),
                    json.dumps(meta_patch, default=str),
                    json.dumps(dict.fromkeys(WORKFLOW_RESUME_FINALIZATION_STEPS, "pending")),
                    json.dumps(str(final_status or "")),
                    json.dumps(dict(result), default=str),
                    json.dumps(str(error or "")),
                    json.dumps(finalizing_at),
                    job_id,
                    int(resume_revision),
                    lease_token,
                ),
            )
            updated = bool(getattr(cur, "rowcount", 0))
            conn.commit()
        return updated

    def complete(
        self,
        *,
        job_id: str,
        resume_revision: int,
        lease_token: str = "",
        finalizer_token: str = "",
    ) -> bool:
        completed_steps = json.dumps(dict.fromkeys(WORKFLOW_RESUME_FINALIZATION_STEPS, "completed"))
        conn = self.conn
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE jobs
                   SET heartbeat_at = NOW(),
                       meta = jsonb_set(
                           COALESCE(meta, '{}'::jsonb),
                           '{workflow_resume}',
                           COALESCE(meta->'workflow_resume', '{}'::jsonb)
                           || jsonb_build_object(
                               'state', 'completed',
                               'lease_token', '',
                               'lease_expires_at', NULL,
                               'finalizer_token', '',
                               'finalizer_expires_at', NULL
                           ),
                           true
                       )
                 WHERE id = %s
                   AND COALESCE((meta->'workflow_resume'->>'revision')::int, 0) = %s
                   AND (
                       (
                           COALESCE(meta->'workflow_resume'->>'state', '') = 'running'
                           AND NULLIF(%s, '') IS NOT NULL
                           AND COALESCE(meta->'workflow_resume'->>'lease_token', '') = %s
                           AND COALESCE(
                               NULLIF(meta->'workflow_resume'->>'lease_expires_at', '')::timestamptz,
                               '-infinity'::timestamptz
                           ) > NOW()
                       )
                       OR (
                           COALESCE(meta->'workflow_resume'->>'state', '') = 'finalizing'
                           AND NULLIF(%s, '') IS NOT NULL
                           AND COALESCE(meta->'workflow_resume'->>'finalizer_token', '') = %s
                           AND COALESCE(
                               NULLIF(meta->'workflow_resume'->>'finalizer_expires_at', '')::timestamptz,
                               '-infinity'::timestamptz
                           ) > NOW()
                           AND COALESCE(
                               meta->'workflow_resume'->'finalization_steps',
                               '{}'::jsonb
                           ) = %s::jsonb
                       )
                   )
                """,
                (
                    job_id,
                    int(resume_revision),
                    lease_token,
                    lease_token,
                    finalizer_token,
                    finalizer_token,
                    completed_steps,
                ),
            )
            completed = bool(getattr(cur, "rowcount", 0))
            conn.commit()
        return completed

    def claim_finalizer(
        self, *, job_id: str, resume_revision: int, finalizer_token: str, lease_seconds: int
    ) -> bool:
        with self.conn.cursor() as cur:
            cur.execute(
                """
                UPDATE jobs
                   SET meta = jsonb_set(
                           jsonb_set(jsonb_set(COALESCE(meta, '{}'::jsonb), '{workflow_resume,finalization_attempts}',
                               to_jsonb(COALESCE((meta->'workflow_resume'->>'finalization_attempts')::int, 0) + 1), true),
                               '{workflow_resume,finalizer_token}', %s::jsonb, true),
                           '{workflow_resume,finalizer_expires_at}',
                           to_jsonb((NOW() + make_interval(secs => %s))::text), true)
                 WHERE id = %s
                   AND COALESCE((meta->'workflow_resume'->>'revision')::int, 0) = %s
                   AND COALESCE(meta->'workflow_resume'->>'state', '') = 'finalizing'
                   AND COALESCE(NULLIF(meta->'workflow_resume'->>'retry_not_before', '')::timestamptz, '-infinity'::timestamptz) <= NOW()
                   AND (COALESCE(meta->'workflow_resume'->>'finalizer_token', '') = ''
                        OR COALESCE(NULLIF(meta->'workflow_resume'->>'finalizer_expires_at', '')::timestamptz,
                                    '-infinity'::timestamptz) <= NOW())
                """,
                (
                    json.dumps(finalizer_token),
                    max(1, int(lease_seconds)),
                    job_id,
                    int(resume_revision),
                ),
            )
            claimed = bool(getattr(cur, "rowcount", 0))
            self.conn.commit()
        return claimed

    def mark_finalization_step_completed(
        self,
        *,
        job_id: str,
        resume_revision: int,
        finalizer_token: str,
        step: str,
    ) -> bool:
        """Persist one idempotent terminal effect behind the finalizer fence."""

        if step not in WORKFLOW_RESUME_FINALIZATION_STEPS:
            raise ValueError(f"Unsupported approval-resume finalization step: {step}")
        with self.conn.cursor() as cur:
            cur.execute(
                """
                UPDATE jobs
                   SET heartbeat_at = NOW(),
                       meta = jsonb_set(
                           jsonb_set(
                               jsonb_set(
                                   COALESCE(meta, '{}'::jsonb),
                                   '{workflow_resume}',
                                   COALESCE(meta->'workflow_resume', '{}'::jsonb),
                                   true
                               ),
                               '{workflow_resume,finalization_steps}',
                               COALESCE(
                                   meta->'workflow_resume'->'finalization_steps',
                                   '{}'::jsonb
                               ),
                               true
                           ),
                           ARRAY['workflow_resume', 'finalization_steps', %s]::text[],
                           '"completed"'::jsonb,
                           true
                       )
                 WHERE id = %s
                   AND COALESCE((meta->'workflow_resume'->>'revision')::int, 0) = %s
                   AND COALESCE(meta->'workflow_resume'->>'state', '') = 'finalizing'
                   AND COALESCE(meta->'workflow_resume'->>'finalizer_token', '') = %s
                   AND COALESCE(
                       NULLIF(meta->'workflow_resume'->>'finalizer_expires_at', '')::timestamptz,
                       '-infinity'::timestamptz
                   ) > NOW()
                """,
                (step, job_id, int(resume_revision), finalizer_token),
            )
            updated = bool(getattr(cur, "rowcount", 0))
            self.conn.commit()
        return updated

    def renew_finalizer(
        self, *, job_id: str, resume_revision: int, finalizer_token: str, lease_seconds: int
    ) -> bool:
        """Renew finalizer ownership only while the same fenced token is active."""

        with self.conn.cursor() as cur:
            cur.execute(
                """
                UPDATE jobs
                   SET heartbeat_at = NOW(),
                       meta = jsonb_set(
                           COALESCE(meta, '{}'::jsonb),
                           '{workflow_resume,finalizer_expires_at}',
                           to_jsonb((NOW() + make_interval(secs => %s))::text),
                           true
                       )
                 WHERE id = %s
                   AND COALESCE((meta->'workflow_resume'->>'revision')::int, 0) = %s
                   AND COALESCE(meta->'workflow_resume'->>'state', '') = 'finalizing'
                   AND COALESCE(meta->'workflow_resume'->>'finalizer_token', '') = %s
                   AND COALESCE(
                       NULLIF(meta->'workflow_resume'->>'finalizer_expires_at', '')::timestamptz,
                       '-infinity'::timestamptz
                   ) > NOW()
                """,
                (
                    max(1, int(lease_seconds)),
                    job_id,
                    int(resume_revision),
                    finalizer_token,
                ),
            )
            renewed = bool(getattr(cur, "rowcount", 0))
            self.conn.commit()
        return renewed

    def release_finalizer(
        self,
        *,
        job_id: str,
        resume_revision: int,
        finalizer_token: str,
        error: str,
    ) -> bool:
        """Delay finalizer retries, preserving terminal status when its budget ends."""

        with self.conn.cursor() as cur:
            cur.execute(
                """
                UPDATE jobs
                   SET heartbeat_at = NOW(),
                       last_error = NULLIF(%s, ''),
                       meta = jsonb_set(COALESCE(meta, '{}'::jsonb), '{workflow_resume}',
                           COALESCE(meta->'workflow_resume', '{}'::jsonb) || jsonb_build_object(
                               'state', CASE WHEN COALESCE((meta->'workflow_resume'->>'finalization_attempts')::int, 0) >= %s
                                             THEN 'completed' ELSE 'finalizing' END,
                               'finalization_error', %s::text,
                               'finalization_exhausted', COALESCE((meta->'workflow_resume'->>'finalization_attempts')::int, 0) >= %s,
                               'retry_not_before', (NOW() + make_interval(secs => %s))::text,
                               'finalizer_token', '', 'finalizer_expires_at', NULL), true)
                 WHERE id = %s
                   AND COALESCE((meta->'workflow_resume'->>'revision')::int, 0) = %s
                   AND COALESCE(meta->'workflow_resume'->>'state', '') = 'finalizing'
                   AND COALESCE(meta->'workflow_resume'->>'finalizer_token', '') = %s
                   AND COALESCE(
                       NULLIF(meta->'workflow_resume'->>'finalizer_expires_at', '')::timestamptz,
                       '-infinity'::timestamptz
                   ) > NOW()
                """,
                (
                    str(error or ""),
                    WORKFLOW_RESUME_MAX_ATTEMPTS,
                    str(error or ""),
                    WORKFLOW_RESUME_MAX_ATTEMPTS,
                    WORKFLOW_RESUME_RETRY_DELAY_SECONDS,
                    job_id,
                    int(resume_revision),
                    finalizer_token,
                ),
            )
            updated = bool(getattr(cur, "rowcount", 0))
            self.conn.commit()
        return updated
