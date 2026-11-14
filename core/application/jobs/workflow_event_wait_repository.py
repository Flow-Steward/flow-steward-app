"""PostgreSQL adapter for event waits: the delivered-event inbox and its claims (ADR 0071).

An extension's ``resume_waits`` items are stored in ``workflow_wait_events``;
nothing is resumed in the extension's request. The control process then
matches stored events to jobs parked ``awaiting_event`` and, in one
transaction, marks each matched event consumed by exactly one job and stores
that job's ``event`` resume intent (``resume_pending``), the same intent shape
as timer and child wakes. Deadlines are claimed the same way with
``timed_out: true``. Publication reuses the workflow resume queue.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from core.application.jobs.workflow_resume_repository import WorkflowResumeRepositoryAdapter
from core.application.jobs.workflow_resume_state import (
    RESUME_REASON_EVENT,
    WORKFLOW_RESUME_PUBLISH_LEASE_SECONDS,
    WorkflowResumeQueueRow,
)


@dataclass(frozen=True)
class WaitEventDelivery:
    """One validated ``resume_waits`` item, ready to store."""

    correlation_key: str
    event_type: str
    payload: Mapping[str, Any]


@dataclass(frozen=True)
class EventWaitCandidate:
    """A stored event and a parked job it may wake, with what the wake needs."""

    event_id: str
    job_id: str
    wait_id: str
    event: Mapping[str, Any] = field(default_factory=dict)
    wait: Mapping[str, Any] = field(default_factory=dict)
    revision: int = 0


def match_events_to_waits(candidates: Sequence[EventWaitCandidate]) -> list[EventWaitCandidate]:
    """Pair events and waits one to one, in the order the candidates come.

    The candidates come oldest event first. Each job takes the first event it
    can, each event goes to the first job that can take it; nothing is shared,
    so an event wakes at most one wait and a wait is woken at most once.
    """
    used_events: set[str] = set()
    used_jobs: set[str] = set()
    matched: list[EventWaitCandidate] = []
    for candidate in candidates:
        if candidate.event_id in used_events or candidate.job_id in used_jobs:
            continue
        used_events.add(candidate.event_id)
        used_jobs.add(candidate.job_id)
        matched.append(candidate)
    return matched


def _values(row: Any) -> list[Any]:
    return list(row.values()) if isinstance(row, Mapping) else list(row)


def _candidate(values: list[Any]) -> EventWaitCandidate:
    event_id, job_id, wait_id, event_type, key, payload, received_at, wait, revision = values[:9]
    return EventWaitCandidate(
        event_id=str(event_id),
        job_id=str(job_id),
        wait_id=str(wait_id or ""),
        event={
            "event_type": event_type,
            "correlation_key": key,
            "payload": _json_object(payload),
            "received_at": received_at,
        },
        wait=_json_object(wait),
        revision=int(revision or 0),
    )


def _json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, (str, bytes, bytearray)) and value:
        loaded = json.loads(value)
        return dict(loaded) if isinstance(loaded, Mapping) else {}
    return {}


class WorkflowEventWaitRepository(WorkflowResumeRepositoryAdapter):
    """Store delivered events, match them to parked runs, claim deadlines."""

    def deliver(
        self,
        *,
        account_id: str,
        project_id: str,
        source: str,
        extension_install_id: str,
        items: Sequence[WaitEventDelivery],
    ) -> list[bool]:
        """Store each item; ``False`` for an item already delivered (a duplicate)."""
        stored: list[bool] = []
        conn = self.conn
        try:
            with conn.cursor() as cur:
                for item in items:
                    cur.execute(
                        """
                        INSERT INTO workflow_wait_events
                            (account_id, project_id, source, extension_install_id,
                             correlation_key, event_type, payload)
                        VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb)
                        ON CONFLICT ON CONSTRAINT workflow_wait_events_delivery_key DO NOTHING
                        RETURNING id
                        """,
                        (
                            account_id,
                            project_id,
                            source,
                            extension_install_id,
                            item.correlation_key,
                            item.event_type,
                            json.dumps(dict(item.payload), default=str),
                        ),
                    )
                    stored.append(cur.fetchone() is not None)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        return stored

    def claim_delivered_events(self, *, limit: int) -> list[tuple[WorkflowResumeQueueRow, str]]:
        """Wake parked runs whose event was delivered: one event, one wait, once.

        One transaction locks unconsumed events and the ``awaiting_event`` jobs
        of the same account and project whose wait names the event's source,
        exact correlation key and type (``SKIP LOCKED`` on both: concurrent
        dispatchers never take the same row). Each pair consumes the event and
        stores the job's ``event`` intent; a job that left ``awaiting_event``
        (cancelled) or an event consumed meanwhile is skipped.
        """
        conn = self.conn
        claimed: list[tuple[WorkflowResumeQueueRow, str]] = []
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT e.id, j.id, j.meta->'workflow_wait'->>'wait_id',
                           e.event_type, e.correlation_key, e.payload, e.received_at::text,
                           j.meta->'workflow_wait',
                           COALESCE((j.meta->'workflow_resume'->>'revision')::int, 0)
                      FROM workflow_wait_events e
                      JOIN jobs j
                        ON j.status = 'awaiting_event'
                       AND j.project_id = e.project_id
                       AND j.account_id = e.account_id
                       AND (j.meta->'workflow_wait'->>'correlation_key') = e.correlation_key
                       AND (j.meta->'workflow_wait'->>'source') = e.source
                       AND jsonb_typeof(j.meta->'workflow_wait'->'event_types') = 'array'
                       AND (j.meta->'workflow_wait'->'event_types') ? e.event_type
                       AND j.thread_id IS NOT NULL
                       AND j.task_id IS NOT NULL
                       AND COALESCE(j.meta->'workflow_wait'->>'wait_id', '') <> ''
                     WHERE e.consumed_at IS NULL
                     ORDER BY e.received_at, e.id, j.started_at, j.id
                     LIMIT %s
                     FOR UPDATE OF e, j SKIP LOCKED
                    """,
                    (max(1, int(limit)),),
                )
                candidates = [_candidate(_values(row)) for row in cur.fetchall() or []]
                for pair in match_events_to_waits(candidates):
                    row = self._wake_with_event(cur, pair)
                    if row is not None:
                        claimed.append(row)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        return claimed

    def _wake_with_event(
        self, cur: Any, pair: EventWaitCandidate
    ) -> tuple[WorkflowResumeQueueRow, str] | None:
        revision = pair.revision + 1
        publish_token = uuid4().hex
        resume_value = {
            "reason": RESUME_REASON_EVENT,
            "wait_id": pair.wait_id,
            "event_id": pair.event_id,
            "event_type": str(pair.event.get("event_type") or ""),
            "correlation_key": str(pair.event.get("correlation_key") or ""),
            "payload": dict(pair.event.get("payload") or {}),
            "received_at": str(pair.event.get("received_at") or ""),
        }
        intent = _event_intent(
            wait_id=pair.wait_id,
            revision=revision,
            resume_value=resume_value,
            interrupt_payload=pair.wait,
            actor="workflow_event",
            publish_token=publish_token,
        )
        cur.execute(
            """
            UPDATE jobs
               SET status = 'resume_pending',
                   heartbeat_at = NOW(),
                   last_error = NULL,
                   meta = jsonb_set(
                       jsonb_set(COALESCE(meta, '{}'::jsonb), '{workflow_resume}', %s::jsonb, true),
                       '{workflow_resume,publish_lease_expires_at}',
                       to_jsonb((NOW() + make_interval(secs => %s))::text),
                       true
                   )
             WHERE id = %s
               AND status = 'awaiting_event'
               AND COALESCE((meta->'workflow_resume'->>'revision')::int, 0) = %s
            RETURNING id, project_id, task_id, thread_id
            """,
            (
                json.dumps(intent, default=str),
                WORKFLOW_RESUME_PUBLISH_LEASE_SECONDS,
                pair.job_id,
                pair.revision,
            ),
        )
        updated = cur.fetchone()
        if updated is None:
            return None
        cur.execute(
            """
            UPDATE workflow_wait_events
               SET consumed_at = NOW(), consumed_job_id = %s, consumed_wait_id = %s
             WHERE id = %s AND consumed_at IS NULL
            """,
            (pair.job_id, pair.wait_id, pair.event_id),
        )
        job_id, project_id, task_id, thread_id = _values(updated)[:4]
        return (
            WorkflowResumeQueueRow(
                job_id=str(job_id or ""),
                project_id=str(project_id or ""),
                task_id=str(task_id or ""),
                resume_revision=revision,
                publish_token=publish_token,
            ),
            str(thread_id or ""),
        )

    def claim_timed_out_event_waits(
        self, *, now_epoch_ms: int, limit: int
    ) -> list[tuple[WorkflowResumeQueueRow, str]]:
        """Turn ``awaiting_event`` jobs past their deadline into timeout intents.

        The lookup uses the expression and predicate of the partial index
        ``idx_jobs_workflow_wait_event_due``; run after event matching, so an
        event delivered before the deadline wins.
        """
        conn = self.conn
        with conn.cursor() as cur:
            cur.execute(
                """
                WITH due AS (
                    SELECT id
                      FROM jobs
                     WHERE status = 'awaiting_event'
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
                               'reason', 'event',
                               'wait_id', intents.wait->>'wait_id',
                               'state', 'pending',
                               'revision', intents.revision,
                               'resume_value', jsonb_build_object(
                                   'reason', 'event',
                                   'wait_id', intents.wait->>'wait_id',
                                   'timed_out', true
                               ),
                               'approved', false,
                               'action', NULL,
                               'comment', '',
                               'interrupt_payload', intents.wait,
                               'actor_username', 'workflow_event_timeout',
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

    def delete_expired_events(self, *, retention_days: int, limit: int) -> int:
        """Delete delivered events older than the retention, consumed or not."""
        conn = self.conn
        with conn.cursor() as cur:
            cur.execute(
                """
                DELETE FROM workflow_wait_events
                 WHERE id IN (
                     SELECT id
                       FROM workflow_wait_events
                      WHERE received_at < NOW() - make_interval(days => %s)
                      ORDER BY received_at
                      LIMIT %s
                 )
                """,
                (max(1, int(retention_days)), max(1, int(limit))),
            )
            deleted = int(getattr(cur, "rowcount", 0) or 0)
            conn.commit()
        return deleted


def _event_intent(
    *,
    wait_id: str,
    revision: int,
    resume_value: Mapping[str, Any],
    interrupt_payload: Mapping[str, Any],
    actor: str,
    publish_token: str,
) -> dict[str, Any]:
    """The stored ``workflow_resume`` intent: the shape of timer and child wakes."""
    return {
        "reason": RESUME_REASON_EVENT,
        "wait_id": wait_id,
        "state": "pending",
        "revision": revision,
        "resume_value": dict(resume_value),
        "approved": False,
        "action": None,
        "comment": "",
        "interrupt_payload": dict(interrupt_payload),
        "actor_username": actor,
        "queued_at": datetime.now(UTC).isoformat(),
        "published_at": None,
        "lease_token": "",
        "lease_expires_at": None,
        "last_publish_error": "",
        "publish_token": publish_token,
        "publish_lease_expires_at": None,
    }


__all__ = [
    "EventWaitCandidate",
    "WaitEventDelivery",
    "WorkflowEventWaitRepository",
    "match_events_to_waits",
]
