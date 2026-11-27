# ruff: noqa: F403, F405
# mypy: ignore-errors
"""Failure handling of a manual task enqueue.

When ``enqueue_manual_task`` cannot hand a task to the queue, the staged
attachment artifacts are rolled back (unless a created job can still recover
them), the idempotency claim is marked failed, and the original error is
translated into the service error the caller sees.
"""

from __future__ import annotations

import logging

from .command_services_common import *  # NOSONAR

logger = logging.getLogger(__name__)


class ProjectCommandServiceTaskFailureMixin:
    def _enqueue_failure_error(
        self,
        exc: Exception,
        *,
        bl: Any,
        plane: Any,
        attachment_rows: list[dict[str, Any]],
        attachment_metadata: list[dict[str, Any]],
        idempotency_claim: Any,
        created_job_id: str,
        task_id: str,
        selected_workflow_id: Any,
    ) -> ServiceError | None:
        """Undo a failed enqueue and return the error to raise from *exc*.

        ``None`` means *exc* is already a service error and is re-raised as is.
        Call it from the ``except`` block that caught *exc*.
        """
        preserve_recoverable_artifacts = False
        if idempotency_claim is not None and idempotency_claim.claimed and created_job_id:
            created_job = bl.get_job(created_job_id) if hasattr(bl, "get_job") else None
            created_meta = (created_job or {}).get("meta") if isinstance(created_job, dict) else {}
            if isinstance(created_meta, str):
                created_meta = json.loads(created_meta)
            created_delivery = (
                created_meta.get("queue_delivery") if isinstance(created_meta, dict) else {}
            )
            delivery_state = str((created_delivery or {}).get("state") or "").strip().lower()
            preserve_recoverable_artifacts = (
                str((created_job or {}).get("status") or "").strip().lower() == "pending"
                and delivery_state in {"queued", "retrying_delivery"}
                and not bool((created_delivery or {}).get("delivery_succeeded"))
            )
        delete_artifact = getattr(plane, "delete_artifact", None)
        if callable(delete_artifact) and not preserve_recoverable_artifacts:
            for attachment in attachment_metadata:
                artifact_id = str(attachment.get("artifact_id") or "").strip()
                if artifact_id:
                    try:
                        if not delete_artifact(artifact_id):
                            logger.error(
                                "Staged attachment rollback did not complete artifact_id=%s",
                                artifact_id,
                            )
                    except Exception:
                        logger.exception(
                            "Failed to roll back staged attachment artifact_id=%s",
                            artifact_id,
                        )
        if idempotency_claim is not None and idempotency_claim.claimed:
            from core.application.idempotency import (
                RequestIdempotencyRepository,
                RequestIdempotencyService,
            )
            from core.infrastructure.db.db_pool import transaction_connection

            try:
                with transaction_connection(bl.conn) as conn:
                    RequestIdempotencyService(RequestIdempotencyRepository(conn)).fail(
                        claim=idempotency_claim,
                        error_code="task_enqueue_failed",
                    )
            except Exception as idempotency_error:
                # The primary failure (including an ownership-fencing 409) is
                # authoritative. A reclaimed lease must not be masked by a
                # best-effort transition of the former owner's reservation.
                logger.warning(
                    "Could not mark failed idempotency claim after enqueue failure: %s",
                    safe_operator_exception(idempotency_error),
                )
        if isinstance(exc, ServiceError):
            return None
        from core.application.jobs.task_enqueue_runtime import QueueTransportUnavailableError

        if isinstance(exc, QueueTransportUnavailableError):
            return ServiceUnavailableError("RabbitMQ task queue is unavailable")
        if (not_runnable := _workflow_not_runnable_error(exc)) is not None:
            return not_runnable
        if attachment_rows and _is_attachment_storage_misconfigured(exc):
            return ServiceUnavailableError(
                "Attachment storage is unavailable (FS_SECRET_KEY / cryptography required)"
            )
        if _is_artifact_storage_misconfigured(exc):
            return ServiceUnavailableError(artifact_storage_misconfigured_message())
        logger.exception(
            "Task enqueue failed task_id=%s workflow_id=%s",
            task_id,
            selected_workflow_id,
        )
        return ServiceError("Failed to enqueue task")


__all__ = [name for name in globals() if not name.startswith("__")]
