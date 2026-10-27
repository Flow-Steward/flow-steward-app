"""Initial queue publication and delivery-state transitions."""

from __future__ import annotations

from typing import Any

from core.application.jobs.queue_delivery_runtime import (
    queue_dead_letter_queue_name,
    record_queue_delivery_failure,
    record_queue_delivery_publish_attempted,
    record_queue_delivery_publish_not_sent,
    record_queue_delivery_publisher_confirmed,
)
from core.application.jobs.task_dispatch_common import (
    PublishFailureDisposition,
    QueueTransportUnavailableError,
    _apply_publish_failure_outbox_disposition,
    _is_queue_transport_unavailable,
    _iter_exception_chain,
    _mark_dispatch_outbox,
    _persist_queue_delivery_transition,
)


def _log_publish_failure(
    *,
    bl,
    job_id: Any,
    project_id: Any,
    queue_name: str,
    publish_err: Exception,
    logger,
    log_prefix: str,
    job_meta_override: dict[str, Any] | None = None,
) -> PublishFailureDisposition:
    err_text = f"Queue publish failed: {publish_err}"
    try:
        try:
            source_hint, category_hint = ("system", "transient")
            if hasattr(bl, "infer_exception_hints"):
                try:
                    source_hint, category_hint = bl.infer_exception_hints(
                        publish_err,
                        context={
                            "stage": "enqueue_publish",
                            "project_id": project_id,
                            "queue_name": queue_name,
                        },
                    )
                except Exception:
                    source_hint, category_hint = ("system", "transient")
            normalized = bl.normalize_exception_error(
                publish_err,
                source_hint=source_hint,
                category_hint=category_hint,
                context={
                    "stage": "enqueue_publish",
                    "project_id": project_id,
                    "queue_name": queue_name,
                },
            )
            bl.log_job_error(
                job_id,
                source=normalized.get("source", "system"),
                category=normalized.get("category", "transient"),
                provider_name=normalized.get("provider_name"),
                http_status=normalized.get("http_status"),
                provider_error_code=normalized.get("provider_error_code"),
                provider_error_type=normalized.get("provider_error_type"),
                request_id=normalized.get("request_id"),
                retryable=normalized.get("retryable"),
                error_message=normalized.get("error_message", str(publish_err)),
                response_excerpt=normalized.get("response_excerpt"),
                traceback_text=normalized.get("traceback_text"),
                context_jsonb=normalized.get("context_jsonb") or {},
            )
        except Exception as struct_err:
            logger.debug("%s: structured publish error logging failed: %s", log_prefix, struct_err)
        bl.log_event(
            job_id=job_id,
            phase="enqueue",
            agent="orchestrator",
            event_type="queue_publish_failed",
            title="Task was not published to RabbitMQ",
            detail={
                "project_id": project_id,
                "queue_name": queue_name,
                "error": str(publish_err),
            },
            severity="error",
        )
        job = bl.get_job(str(job_id)) if hasattr(bl, "get_job") else None
        job_meta = {}
        if isinstance(job_meta_override, dict):
            job_meta = dict(job_meta_override)
        elif isinstance(job, dict):
            job_meta = dict((job or {}).get("meta") or {})
        current_delivery = job_meta.get("queue_delivery") if isinstance(job_meta, dict) else {}
        dispatch_state = (
            str(
                (current_delivery or {}).get("dispatch_state")
                if isinstance(current_delivery, dict)
                else ""
            )
            .strip()
            .lower()
        )
        expected_dispatch_state = dispatch_state or "not_started"
        known_not_sent = any(
            str(getattr(item, "queue_dispatch_outcome", "")).strip().lower() == "not_sent"
            for item in _iter_exception_chain(publish_err)
        )
        if known_not_sent:
            pre_dispatch_meta, _pre_dispatch_state = record_queue_delivery_publish_not_sent(
                job_meta,
                queue_name=queue_name,
                dead_letter_queue_name=queue_dead_letter_queue_name(queue_name),
                project_policy=None,
            )
            updated_meta, _state, _disposition = record_queue_delivery_failure(
                pre_dispatch_meta,
                queue_name=queue_name,
                dead_letter_queue_name=queue_dead_letter_queue_name(queue_name),
                project_policy=None,
                failure_class="queue_publish_not_sent",
                failure_code="queue_publish_not_sent",
                failure_message=err_text,
            )
            dispatch_state = "not_started"
        else:
            updated_meta, _state, _disposition = record_queue_delivery_failure(
                job_meta,
                queue_name=queue_name,
                dead_letter_queue_name=queue_dead_letter_queue_name(queue_name),
                project_policy=None,
                failure_class="queue_publish_failed",
                failure_code="queue_publish_failed",
                failure_message=err_text,
            )
        idempotency = job_meta.get("idempotency") if isinstance(job_meta, dict) else None
        can_recover_idempotently = isinstance(idempotency, dict) and all(
            str(idempotency.get(key) or "").strip()
            for key in ("scope_key", "key_digest", "request_fingerprint")
        )
        recoverable_delivery_state = str(_state.get("state") or "").strip().lower()
        if (
            can_recover_idempotently
            and dispatch_state == "not_started"
            and recoverable_delivery_state in {"queued", "retrying_delivery"}
        ):
            # A broker failure occurs before the worker can start the graph.
            # Only a durable idempotency identity has a caller-driven recovery
            # path that can safely re-publish this exact pending job.
            persisted = _persist_queue_delivery_transition(
                bl,
                job_id,
                updated_meta,
                status="pending",
                expected_dispatch_states={expected_dispatch_state},
            )
            if persisted is False:
                logger.info(
                    "%s: worker claimed job %s before publish-failure state persisted",
                    log_prefix,
                    job_id,
                )
                return "superseded"
            return "recoverable"

        # Non-idempotent callers have no path that can re-publish this job;
        # neither do idempotent jobs whose delivery retry policy has already
        # dead-lettered the pre-execution dispatch. Preserve the terminal
        # failure rather than stranding a permanently pending row.
        persisted = _persist_queue_delivery_transition(
            bl,
            job_id,
            updated_meta,
            expected_dispatch_states={expected_dispatch_state},
        )
        if persisted is False:
            logger.info(
                "%s: worker claimed job %s before terminal publish-failure state persisted",
                log_prefix,
                job_id,
            )
            return "superseded"
        from core.application.jobs.terminal_persistence import (
            TerminalPersistenceConflict,
            TerminalPersistenceService,
        )

        terminal_status = (
            "triage_needed" if recoverable_delivery_state == "dead_lettered" else "failed"
        )
        try:
            TerminalPersistenceService(bl).complete_failure(
                job_id=str(job_id),
                status=terminal_status,
                error_message=err_text,
                error_code="queue_publish_failed",
                category="queue",
                retryable=True,
                require_pending_unowned=True,
            )
        except TerminalPersistenceConflict:
            return "superseded"
        return "terminal_failed"
    except Exception as db_err:
        logger.warning(
            "%s: could not mark job %s failed after publish error: %s",
            log_prefix,
            job_id,
            db_err,
        )
        return "persistence_unknown"


def _dispatch_created_notification(
    *,
    bl,
    project_id: Any,
    job_id: Any,
    task_id: str,
    task_summary: str,
    task_source: str,
    queue_name: str,
    workflow_id: str | None,
    notification_dispatch_fn,
    notification_account_id: str | None,
    logger,
    log_prefix: str,
) -> None:
    if not notification_dispatch_fn:
        return
    try:
        kwargs = {
            "project_id": project_id,
            "event_type": "job.created",
            "severity": "info",
            "workflow_id": workflow_id,
            "job_id": job_id,
            "payload": {
                "task_id": task_id,
                "task_summary": task_summary,
                "task_source": task_source,
                "queue_name": queue_name,
            },
        }
        if notification_account_id:
            kwargs["account_id"] = notification_account_id
        notification_dispatch_fn(bl, **kwargs)
    except Exception as notify_err:
        logger.debug(
            "%s: notification dispatch skipped for job %s: %s",
            log_prefix,
            job_id,
            notify_err,
        )


def _record_initial_publish_attempt(
    *,
    bl: Any,
    job_id: Any,
    fallback_meta: dict[str, Any],
    queue_name: str,
    project_config: dict | None,
) -> dict[str, Any]:
    job = bl.get_job(str(job_id)) if hasattr(bl, "get_job") else None
    meta = (
        dict(job.get("meta") or {})
        if isinstance(job, dict) and isinstance(job.get("meta"), dict)
        else fallback_meta
    )
    attempted_meta, _attempted_state = record_queue_delivery_publish_attempted(
        meta,
        queue_name=queue_name,
        dead_letter_queue_name=queue_dead_letter_queue_name(queue_name),
        project_policy=(project_config or {}).get("project_policy")
        if isinstance((project_config or {}).get("project_policy"), dict)
        else None,
    )
    if not _persist_queue_delivery_transition(
        bl,
        job_id,
        attempted_meta,
        expected_dispatch_states={"not_started"},
    ):
        raise RuntimeError("Queue publish fence could not be acquired")
    return attempted_meta


def _publish_initial_task_message(
    *,
    bl: Any,
    job_id: Any,
    project_id: Any,
    task_id: str,
    thread_id: str,
    task_summary: str,
    task_source: str,
    project_name: str,
    project_config: dict | None,
    effective_extra_state: dict[str, Any],
    queue_name: str,
    attempted_meta: dict[str, Any],
    enqueue_task_fn,
    logger: Any,
    log_prefix: str,
    wrap_transport_error: bool = True,
) -> None:
    try:
        enqueue_task_fn(
            project_id=project_id,
            job_id=job_id,
            task_id=task_id,
            thread_id=thread_id,
            task_summary=task_summary,
            task_source=task_source,
            project_name=project_name,
            project_config=project_config or {},
            extra_state=effective_extra_state or None,
        )
    except Exception as publish_err:
        disposition = _log_publish_failure(
            bl=bl,
            job_id=job_id,
            project_id=project_id,
            queue_name=queue_name,
            publish_err=publish_err,
            logger=logger,
            log_prefix=log_prefix,
            job_meta_override=attempted_meta,
        )
        _apply_publish_failure_outbox_disposition(
            bl,
            str(job_id),
            disposition=disposition,
            error=str(publish_err),
        )
        if wrap_transport_error and _is_queue_transport_unavailable(publish_err):
            raise QueueTransportUnavailableError(
                "RabbitMQ task queue is unavailable; the job was not enqueued.",
                job_id=str(job_id),
                recoverable=disposition in {"recoverable", "persistence_unknown"},
            ) from publish_err
        raise


def _record_initial_publish_confirmation(
    *,
    bl: Any,
    job_id: Any,
    attempted_meta: dict[str, Any],
    queue_name: str,
    project_config: dict | None,
    logger: Any,
    log_prefix: str,
    suppress_marker_errors: bool = True,
) -> None:
    try:
        confirmed_meta, _confirmed_state = record_queue_delivery_publisher_confirmed(
            attempted_meta,
            queue_name=queue_name,
            dead_letter_queue_name=queue_dead_letter_queue_name(queue_name),
            project_policy=(project_config or {}).get("project_policy")
            if isinstance((project_config or {}).get("project_policy"), dict)
            else None,
        )
        persisted = _persist_queue_delivery_transition(
            bl,
            job_id,
            confirmed_meta,
            expected_dispatch_states={"attempted"},
        )
        if persisted is False:
            logger.info(
                "%s: worker claim retained for job %s instead of late publisher confirmation",
                log_prefix,
                job_id,
            )
        _mark_dispatch_outbox(bl, str(job_id), status="published")
    except Exception as marker_err:
        if not suppress_marker_errors:
            raise
        logger.error(
            "%s: publisher confirmation marker was not persisted for job %s: %s",
            log_prefix,
            job_id,
            marker_err,
        )


def _release_scheduled_lock(
    bl: Any,
    lock_row: dict[str, Any] | None,
    *,
    job_id: Any,
    project_id: Any,
    reason: str,
    logger: Any,
    log_prefix: str,
) -> None:
    lock_id = str((lock_row or {}).get("id") or "").strip()
    if not lock_id:
        return
    release = getattr(bl, "release_scheduled_job_execution_lock_by_id", None)
    if not callable(release):
        raise RuntimeError("Scheduled execution lock release capability is unavailable")
    context = {
        "scheduled_job_id": str((lock_row or {}).get("scheduled_job_id") or "").strip(),
        "job_id": str(job_id or "").strip(),
        "account_id": str((lock_row or {}).get("account_id") or "").strip(),
        "project_id": str((lock_row or {}).get("project_id") or project_id or "").strip(),
        "lock_id": lock_id,
        "reason": str(reason or "").strip()[:256],
    }
    try:
        released = release(
            lock_id=lock_id,
            release_reason=reason,
        )
        if not released:
            raise RuntimeError("Scheduled execution lock was not released")
        bl.log_event(
            job_id=context["job_id"],
            phase="enqueue",
            agent="scheduler",
            event_type="scheduled_execution_lock_released",
            title="Scheduled execution lock released",
            detail=context,
            severity="info",
        )
    except Exception as exc:
        logger.error(
            "%s: scheduled execution lock release failed lock_id=%s job_id=%s project_id=%s: %s",
            log_prefix,
            lock_id,
            context["job_id"],
            context["project_id"],
            exc,
        )
        try:
            bl.log_event(
                job_id=context["job_id"],
                phase="enqueue",
                agent="scheduler",
                event_type="scheduled_execution_lock_release_failed",
                title="Scheduled execution lock release failed",
                detail={**context, "error_type": exc.__class__.__name__},
                severity="error",
            )
        except Exception as event_exc:
            logger.error(
                "%s: lock release failure event was not persisted: %s", log_prefix, event_exc
            )
        raise


def _activate_scheduled_lock(
    *, bl: Any, lock_row: dict[str, Any] | None, job_id: Any, logger: Any, log_prefix: str
) -> None:
    lock_id = str((lock_row or {}).get("id") or "").strip()
    if not lock_id or not hasattr(bl, "activate_scheduled_job_execution_lock"):
        return
    try:
        activated = bl.activate_scheduled_job_execution_lock(
            lock_id=lock_id,
            status="active",
            expires_at=None,
        )
        if not activated:
            logger.error(
                "%s: could not mark scheduled execution lock active; "
                "lock_id=%s job_id=%s remains pre_publish until reconciler promotes or releases it",
                log_prefix,
                lock_id,
                str(job_id or ""),
            )
    except Exception as activation_err:
        logger.error(
            "%s: scheduled execution lock activation failed lock_id=%s job_id=%s err=%s",
            log_prefix,
            lock_id,
            str(job_id or ""),
            activation_err,
        )
