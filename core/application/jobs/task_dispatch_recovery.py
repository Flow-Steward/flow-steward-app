"""Recovery and relay of durable pre-execution dispatches."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from core.application.jobs.queue_delivery_runtime import (
    queue_dead_letter_queue_name,
    record_queue_delivery_publish_attempted,
    record_queue_delivery_publisher_confirmed,
)
from core.application.jobs.task_dispatch_common import (
    _MAX_UNREADABLE_DISPATCH_ATTEMPTS,
    DispatchEnvelopeUnreadableError,
    QueueTransportUnavailableError,
    _apply_publish_failure_outbox_disposition,
    _dispatch_outbox_repository,
    _job_lifecycle_transaction,
    _mark_dispatch_outbox,
    _persist_queue_delivery_transition,
    _read_dispatch_envelope,
    _settle_dispatch_outbox_if_execution_advanced,
    _terminalize_unreadable_dispatch,
)
from core.application.jobs.task_dispatch_publish import _log_publish_failure


@dataclass(frozen=True)
class _RedeliveryCandidate:
    job: dict[str, Any]
    meta: dict[str, Any]
    dispatch_state: str
    queue_name: str
    policy: dict[str, Any] | None


def _load_redelivery_candidate(
    *, bl: Any, job_id: str, outbox_claimed: bool
) -> _RedeliveryCandidate | None:
    job = bl.get_job(str(job_id)) if hasattr(bl, "get_job") else None
    if not isinstance(job, dict):
        return None
    status = str(job.get("status") or "").strip().lower()
    worker_id = str(job.get("worker_id") or "").strip()
    meta = dict(job.get("meta") or {}) if isinstance(job.get("meta"), dict) else {}
    raw_delivery = meta.get("queue_delivery")
    delivery = dict(raw_delivery) if isinstance(raw_delivery, dict) else {}
    delivery_state = str(delivery.get("state") or "").strip().lower()
    dispatch_state = str(delivery.get("dispatch_state") or "").strip().lower()
    allowed_dispatch_states = {"not_started", "attempted"} if outbox_claimed else {"not_started"}
    if (
        status != "pending"
        or bool(worker_id)
        or delivery_state not in {"queued", "retrying_delivery"}
        or dispatch_state not in allowed_dispatch_states
    ):
        if outbox_claimed:
            _settle_dispatch_outbox_if_execution_advanced(bl, str(job_id))
        return None
    policy = meta.get("project_policy_snapshot")
    return _RedeliveryCandidate(
        job=job,
        meta=meta,
        dispatch_state=dispatch_state,
        queue_name=str(delivery.get("queue_name") or f"platform.project.{job.get('project_id')}"),
        policy=policy if isinstance(policy, dict) else None,
    )


def _load_redelivery_envelope(
    *, bl: Any, job_id: str, outbox_claimed: bool, logger: Any, log_prefix: str
) -> dict[str, Any] | None:
    try:
        envelope = _read_dispatch_envelope(bl, str(job_id))
    except DispatchEnvelopeUnreadableError as exc:
        if outbox_claimed and exc.attempt_count >= _MAX_UNREADABLE_DISPATCH_ATTEMPTS:
            if _terminalize_unreadable_dispatch(bl, str(job_id)):
                logger.error(
                    "%s: terminalized unreadable durable dispatch envelope job_id=%s",
                    log_prefix,
                    job_id,
                )
            else:
                logger.info(
                    "%s: superseded unreadable dispatch recovery after worker claim job_id=%s",
                    log_prefix,
                    job_id,
                )
        return None
    if not isinstance(envelope.get("project_config"), dict) or not isinstance(
        envelope.get("extra_state"), dict
    ):
        return None
    return envelope


def _publish_redelivery_candidate(
    *,
    bl: Any,
    job_id: str,
    candidate: _RedeliveryCandidate,
    envelope: dict[str, Any],
    attempted_meta: dict[str, Any],
    enqueue_task_fn,
    logger: Any,
    log_prefix: str,
    outbox_claimed: bool,
) -> tuple[bool, Exception | None, bool]:
    job = candidate.job
    try:
        enqueue_task_fn(
            project_id=job.get("project_id"),
            job_id=str(job_id),
            task_id=str(job.get("task_id") or ""),
            thread_id=str(job.get("thread_id") or ""),
            task_summary=str(job.get("task_summary") or ""),
            task_source=str(job.get("task_source") or "webhook"),
            project_name=str(envelope.get("project_name") or ""),
            project_config=dict(envelope["project_config"]),
            extra_state=dict(envelope["extra_state"]),
        )
    except Exception as exc:
        disposition = _log_publish_failure(
            bl=bl,
            job_id=str(job_id),
            project_id=job.get("project_id"),
            queue_name=candidate.queue_name,
            publish_err=exc,
            logger=logger,
            log_prefix=log_prefix,
            job_meta_override=attempted_meta,
        )
        _apply_publish_failure_outbox_disposition(
            bl, str(job_id), disposition=disposition, error=str(exc)
        )
        return False, exc, disposition in {"recoverable", "persistence_unknown"}
    confirmed_meta, _confirmed_state = record_queue_delivery_publisher_confirmed(
        attempted_meta,
        queue_name=candidate.queue_name,
        dead_letter_queue_name=queue_dead_letter_queue_name(candidate.queue_name),
        project_policy=candidate.policy,
    )
    if not _persist_queue_delivery_transition(
        bl,
        str(job_id),
        confirmed_meta,
        expected_dispatch_states={"attempted"},
    ):
        if outbox_claimed:
            _settle_dispatch_outbox_if_execution_advanced(bl, str(job_id))
        return False, None, False
    _mark_dispatch_outbox(bl, str(job_id), status="published")
    return True, None, False


def redeliver_pre_execution_job(
    *,
    bl,
    job_id: str,
    enqueue_task_fn,
    logger,
    log_prefix: str = "idempotency_recovery",
    outbox_claimed: bool = False,
) -> bool:
    """Publish the same durable job only when its first publish was known not sent.

    The conditional delivery transition is the fence: a worker claim or a
    terminal/cancelled row wins over recovery and no new message is emitted.
    """
    publish_error: Exception | None = None
    published = False
    with _job_lifecycle_transaction(bl):
        candidate = _load_redelivery_candidate(
            bl=bl, job_id=str(job_id), outbox_claimed=outbox_claimed
        )
        if candidate is None:
            return False
        dispatch_envelope = _load_redelivery_envelope(
            bl=bl,
            job_id=str(job_id),
            outbox_claimed=outbox_claimed,
            logger=logger,
            log_prefix=log_prefix,
        )
        if dispatch_envelope is None:
            return False
        attempted_meta = candidate.meta
        if candidate.dispatch_state == "not_started":
            attempted_meta, _attempted_state = record_queue_delivery_publish_attempted(
                candidate.meta,
                queue_name=candidate.queue_name,
                dead_letter_queue_name=queue_dead_letter_queue_name(candidate.queue_name),
                project_policy=candidate.policy,
            )
        # This CAS is also the final worker fence. The surrounding transaction
        # retains the row lock until broker publish and confirmation persistence
        # finish, so a worker claim can only win before or after this operation.
        if not _persist_queue_delivery_transition(
            bl,
            str(job_id),
            attempted_meta,
            expected_dispatch_states={candidate.dispatch_state},
        ):
            if outbox_claimed:
                _settle_dispatch_outbox_if_execution_advanced(bl, str(job_id))
            return False

        published, publish_error, publish_recoverable = _publish_redelivery_candidate(
            bl=bl,
            job_id=str(job_id),
            candidate=candidate,
            envelope=dispatch_envelope,
            attempted_meta=attempted_meta,
            enqueue_task_fn=enqueue_task_fn,
            logger=logger,
            log_prefix=log_prefix,
            outbox_claimed=outbox_claimed,
        )

    if publish_error is not None:
        raise QueueTransportUnavailableError(
            "RabbitMQ task queue is unavailable; the job was not enqueued.",
            job_id=str(job_id),
            recoverable=publish_recoverable,
        ) from publish_error
    return published


def relay_pending_dispatch_outbox(
    *,
    bl: Any,
    enqueue_task_fn,
    logger,
    limit: int = 100,
    lease_seconds: int = 30,
) -> dict[str, Any]:
    """Lease and publish durable pre-execution envelopes left by crashed publishers."""
    repository = _dispatch_outbox_repository(bl)
    swept = repository.sweep_advanced_pending(limit=limit)
    swept_superseded = int(swept) if isinstance(swept, int) and not isinstance(swept, bool) else 0
    job_ids = repository.claim_pending(limit=limit, lease_seconds=lease_seconds) or []
    published = 0
    superseded = swept_superseded
    failed = 0
    for job_id in job_ids:
        try:
            if redeliver_pre_execution_job(
                bl=bl,
                job_id=str(job_id),
                enqueue_task_fn=enqueue_task_fn,
                logger=logger,
                log_prefix="dispatch_outbox_relay",
                outbox_claimed=True,
            ):
                published += 1
            else:
                outbox = repository.get(job_id=str(job_id))
                if isinstance(outbox, dict) and outbox.get("status") == "superseded":
                    superseded += 1
                else:
                    failed += 1
        except Exception as exc:
            failed += 1
            logger.warning("Dispatch outbox relay failed job_id=%s: %s", job_id, exc)
    return {
        "claimed": len(job_ids),
        "published": published,
        "superseded": superseded,
        "failed": failed,
    }
