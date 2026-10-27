"""Reusable runtime helpers for creating and publishing queued jobs."""

from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any, Literal

from core.application.auth.secrets import decrypt_secret, encrypt_secret
from core.application.jobs.dispatch_outbox_repository import DispatchOutboxRepository
from core.application.jobs.preparation_lease import (
    PreparationLeaseLostError,
)
from core.application.jobs.queue_delivery_runtime import (
    build_initial_queue_delivery_state,
    queue_dead_letter_queue_name,
)


class QueueTransportUnavailableError(RuntimeError):
    """Raised when RabbitMQ is unavailable or publish confirmation fails."""

    def __init__(
        self,
        message: str,
        *,
        job_id: str = "",
        recoverable: bool = False,
    ) -> None:
        super().__init__(message)
        self.job_id = str(job_id or "").strip()
        self.recoverable = bool(recoverable)


class DispatchEnvelopeUnreadableError(RuntimeError):
    def __init__(self, *, attempt_count: int) -> None:
        super().__init__("durable dispatch envelope is unreadable")
        self.attempt_count = max(0, int(attempt_count))


_MAX_UNREADABLE_DISPATCH_ATTEMPTS = 3
PublishFailureDisposition = Literal[
    "recoverable", "terminal_failed", "superseded", "persistence_unknown"
]


@contextmanager
def _job_lifecycle_transaction(bl: Any):
    """Use the logger's explicit lifecycle unit-of-work when available."""
    unit_of_work_factory = getattr(bl, "job_lifecycle_unit_of_work", None)
    if callable(unit_of_work_factory):
        unit_of_work = unit_of_work_factory()
    else:
        if getattr(bl, "conn", None) is None:
            yield None
            return
        from core.application.jobs.job_lifecycle_uow import JobLifecycleUnitOfWork
        from core.infrastructure.db.db_pool import transaction_connection

        unit_of_work = JobLifecycleUnitOfWork(lambda: transaction_connection(bl.conn))
    with unit_of_work.transaction() as transaction_conn:
        yield transaction_conn


def _journal_replay_snapshot_reservation(
    *, bl: Any, job_id: Any, reservation: dict[str, str]
) -> None:
    """Persist an exact cleanup target before replay artifact creation."""
    with _job_lifecycle_transaction(bl):
        job = bl.get_job(str(job_id))
        if not isinstance(job, dict):
            raise RuntimeError("Created job could not be reloaded for replay artifact journal")
        current_meta = job.get("meta") or {}
        if isinstance(current_meta, str):
            current_meta = json.loads(current_meta)
        if not isinstance(current_meta, dict):
            raise RuntimeError("Created job metadata is invalid for replay artifact journal")
        artifact_id = str(reservation.get("artifact_id") or "").strip()
        pending = current_meta.get("attachment_persistence_artifacts")
        pending = list(pending) if isinstance(pending, list) else []
        pending = [
            dict(item)
            for item in pending
            if isinstance(item, dict) and str(item.get("artifact_id") or "").strip() != artifact_id
        ]
        pending.append(dict(reservation))
        current_meta["attachment_persistence_artifacts"] = pending
        bl.update_job(str(job_id), meta=current_meta)


class _ScheduledLockConflict(RuntimeError):
    pass


class _ScheduledLockSchemaUnavailable(RuntimeError):
    pass


def initialize_public_job_progress(
    bl: Any, *, job_id: Any, project_id: Any, workflow_id: str
) -> None:
    """Create the mandatory initial public snapshot for every durable job."""
    bl.record_public_progress_transition(
        job_id=str(job_id),
        project_id=str(project_id),
        workflow_id=str(workflow_id or "").strip() or "unknown",
        job_status="pending",
        occurred_at=datetime.now(UTC),
    )


def _encrypt_dispatch_envelope(payload: dict[str, Any]) -> str:
    return encrypt_secret(json.dumps(payload, default=str, separators=(",", ":")))


def _dispatch_outbox_repository(bl: Any) -> DispatchOutboxRepository:
    from core.infrastructure.db.db_pool import active_transaction_connection

    connection = active_transaction_connection(bl.conn) or bl.conn
    return DispatchOutboxRepository(connection)


def _preparation_heartbeat_connection_provider(bl: Any):
    pool = getattr(bl, "__dict__", {}).get("_pool")
    if pool is None:
        return None
    from core.infrastructure.db.db_pool import pooled_connection

    return lambda: pooled_connection(pool, autocommit=True)


def _read_dispatch_envelope(bl: Any, job_id: str) -> dict[str, Any]:
    row = _dispatch_outbox_repository(bl).get(job_id=str(job_id))
    if not isinstance(row, dict) or str(row.get("status") or "pending") == "failed":
        raise DispatchEnvelopeUnreadableError(attempt_count=0)
    attempt_count = int(row.get("attempt_count") or 0)
    ciphertext = str(row.get("encrypted_envelope") or "").strip()
    if not ciphertext:
        raise DispatchEnvelopeUnreadableError(attempt_count=attempt_count)
    try:
        decoded = json.loads(decrypt_secret(ciphertext))
    except Exception as exc:
        raise DispatchEnvelopeUnreadableError(attempt_count=attempt_count) from exc
    if not isinstance(decoded, dict):
        raise DispatchEnvelopeUnreadableError(attempt_count=attempt_count)
    if not isinstance(decoded.get("project_config"), dict) or not isinstance(
        decoded.get("extra_state"), dict
    ):
        raise DispatchEnvelopeUnreadableError(attempt_count=attempt_count)
    return dict(decoded)


def _terminalize_unreadable_dispatch(bl: Any, job_id: str) -> bool:
    from core.application.jobs.terminal_persistence import (
        TerminalPersistenceConflict,
        TerminalPersistenceService,
    )

    try:
        TerminalPersistenceService(bl).complete_failure(
            job_id=job_id,
            status="failed",
            error_message="Durable queue dispatch data could not be read.",
            error_code="dispatch_envelope_unreadable",
            category="queue",
            retryable=False,
            require_pending_unowned=True,
        )
    except TerminalPersistenceConflict:
        if _settle_dispatch_outbox_if_execution_advanced(bl, str(job_id)):
            return False
        raise
    _mark_dispatch_outbox(
        bl,
        job_id,
        status="failed",
        error="dispatch_envelope_unreadable",
    )
    return True


def _mark_dispatch_outbox(bl: Any, job_id: str, *, status: str, error: str | None = None) -> None:
    _dispatch_outbox_repository(bl).mark(job_id=str(job_id), status=status, last_error=error)


def _settle_dispatch_outbox_if_execution_advanced(bl: Any, job_id: str) -> bool:
    return _dispatch_outbox_repository(bl).settle_after_execution_advanced(job_id=str(job_id))


def _apply_publish_failure_outbox_disposition(
    bl: Any,
    job_id: str,
    *,
    disposition: PublishFailureDisposition,
    error: str,
) -> str:
    if disposition == "superseded":
        _settle_dispatch_outbox_if_execution_advanced(bl, str(job_id))
    elif disposition == "persistence_unknown":
        # The outbox is already pending. Do not risk replacing recoverable
        # state after an unconfirmed/failed persistence operation.
        pass
    else:
        status = "pending" if disposition == "recoverable" else "failed"
        _mark_dispatch_outbox(bl, str(job_id), status=status, error=error)
    return disposition


def _persist_queue_delivery_transition(
    bl,
    job_id: Any,
    meta: dict[str, Any],
    *,
    status: str | None = None,
    expected_dispatch_states: set[str],
) -> bool:
    """Atomically apply a publisher transition without overwriting a worker claim."""
    queue_delivery = meta.get("queue_delivery") if isinstance(meta, dict) else None
    if not isinstance(queue_delivery, dict):
        raise RuntimeError("atomic queue-delivery transition is missing canonical state")
    updater = getattr(bl, "update_queue_delivery_meta", None)
    if not callable(updater):
        raise RuntimeError("atomic queue-delivery gateway is unavailable")
    return bool(
        updater(
            job_id,
            queue_delivery=queue_delivery,
            runtime_journal=meta.get("runtime_journal"),
            status=status,
            expected_dispatch_states=expected_dispatch_states,
        )
    )


def _is_chained_error(value: object | None) -> bool:
    return value is not None and hasattr(value, "__traceback__")


def _iter_exception_chain(exc: Exception) -> list[object]:
    chain: list[object] = []
    seen: set[int] = set()
    current: object | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        chain.append(current)
        next_exc = getattr(current, "__cause__", None) or getattr(current, "__context__", None)
        current = next_exc if _is_chained_error(next_exc) else None
    return chain


def _is_queue_transport_unavailable(exc: Exception) -> bool:
    for item in _iter_exception_chain(exc):
        if str(getattr(item, "queue_dispatch_outcome", "")).strip().lower() == "not_sent":
            return True
        message = str(item or "").strip().lower()
        if "cannot connect to rabbitmq" in message:
            return True
        if "publish not confirmed" in message:
            return True
        if "rabbitmq" in message and "unavailable" in message:
            return True
    return False


def prepare_enqueue_inputs(
    *,
    project_config: dict | None,
    test_mode: bool | None,
    extra_state: dict | None,
    meta: dict | None,
    build_project_config_snapshot,
) -> tuple[dict[str, Any], dict[str, Any], bool]:
    from core.infrastructure.runtime.test_mode import parse_test_mode

    effective_meta = dict(meta or {})
    effective_extra_state = dict(extra_state or {})
    effective_test_mode = (
        parse_test_mode(test_mode, False)
        or parse_test_mode(effective_meta.get("test_mode"), False)
        or parse_test_mode(effective_extra_state.get("test_mode"), False)
    )
    if effective_test_mode:
        effective_extra_state["test_mode"] = True
        effective_meta["test_mode"] = True

    _copy_lineage_values(effective_meta, effective_extra_state)
    _copy_workflow_selection_source(effective_meta, effective_extra_state)
    _copy_project_config_snapshots(
        project_config,
        effective_meta=effective_meta,
        build_project_config_snapshot=build_project_config_snapshot,
    )
    _apply_project_policy_defaults(
        project_config,
        effective_meta=effective_meta,
        effective_extra_state=effective_extra_state,
    )
    return effective_meta, effective_extra_state, effective_test_mode


def _copy_lineage_values(
    effective_meta: dict[str, Any], effective_extra_state: dict[str, Any]
) -> None:
    for lineage_key in ("parent_workflow_id",):
        if (
            not str(effective_meta.get(lineage_key) or "").strip()
            and str(effective_extra_state.get(lineage_key) or "").strip()
        ):
            effective_meta[lineage_key] = str(effective_extra_state.get(lineage_key)).strip()


def _copy_workflow_selection_source(
    effective_meta: dict[str, Any],
    effective_extra_state: dict[str, Any],
) -> None:
    workflow_selection_source = str(effective_meta.get("workflow_selection_source") or "").strip()
    if (
        workflow_selection_source
        and not str(effective_extra_state.get("workflow_selection_source") or "").strip()
    ):
        effective_extra_state["workflow_selection_source"] = workflow_selection_source


def _copy_project_config_snapshots(
    project_config: dict | None,
    *,
    effective_meta: dict[str, Any],
    build_project_config_snapshot,
) -> None:
    if not isinstance(project_config, dict):
        return
    policy_snapshot = project_config.get("project_policy")
    config_snapshot = build_project_config_snapshot(project_config)
    if isinstance(policy_snapshot, dict) and policy_snapshot:
        effective_meta["project_policy_snapshot"] = dict(policy_snapshot)
    if config_snapshot:
        effective_meta["project_config_snapshot"] = config_snapshot


def _initialize_queue_delivery_meta(
    *,
    effective_meta: dict[str, Any],
    queue_name: str,
    project_config: dict | None,
) -> None:
    if "queue_delivery" in effective_meta:
        return
    project_policy = (project_config or {}).get("project_policy")
    effective_meta["queue_delivery"] = build_initial_queue_delivery_state(
        queue_name=queue_name,
        dead_letter_queue_name=queue_dead_letter_queue_name(queue_name),
        project_policy=project_policy if isinstance(project_policy, dict) else None,
    )


def _apply_project_policy_defaults(
    project_config: dict | None,
    *,
    effective_meta: dict[str, Any],
    effective_extra_state: dict[str, Any],
) -> None:
    configured_project_policy = (project_config or {}).get("project_policy")
    if isinstance(configured_project_policy, dict) and configured_project_policy:
        effective_extra_state["project_policy"] = dict(configured_project_policy)
        effective_meta["project_policy_snapshot"] = dict(configured_project_policy)


def _persist_requested_workflow(
    *,
    bl,
    job_id: Any,
    effective_extra_state: dict[str, Any],
    logger,
) -> None:
    if not effective_extra_state.get("workflow_id"):
        return
    try:
        bl.update_job_runtime(
            job_id,
            selected_workflow=str(effective_extra_state.get("workflow_id")),
            heartbeat_now=True,
        )
    except Exception as exc:
        logger.debug("Could not persist requested workflow for job %s: %s", job_id, exc)


def _run_pre_publish_hook(
    *,
    job_id: Any,
    bl,
    pre_publish_fn,
    logger,
    log_prefix: str,
    fail_on_pre_publish_error: bool,
    preparation_heartbeat,
) -> dict[str, Any]:
    """Return durable metadata together with finalized attachment payload fields.

    Enqueue copies only files, inputs, and task_envelope into execution state;
    the complete return value remains available for durable metadata finalization.
    """
    if not pre_publish_fn:
        return {}
    try:
        patch = pre_publish_fn(job_id, bl, preparation_heartbeat)
        return dict(patch) if isinstance(patch, dict) else {}
    except Exception as pre_err:
        logger.warning("%s: pre_publish_fn failed: %s", log_prefix, pre_err)
        if fail_on_pre_publish_error:
            raise
    return {}


def _replace_dispatch_outbox_envelope(
    *,
    bl: Any,
    job_id: Any,
    project_name: str,
    project_config: dict[str, Any],
    effective_extra_state: dict[str, Any],
) -> None:
    ciphertext = _encrypt_dispatch_envelope(
        {
            "project_name": str(project_name or ""),
            "project_config": dict(project_config or {}),
            "extra_state": dict(effective_extra_state or {}),
        }
    )
    if not _dispatch_outbox_repository(bl).replace_envelope(
        job_id=str(job_id), encrypted_envelope=ciphertext
    ):
        raise RuntimeError("Durable dispatch outbox envelope could not be finalized")


def _finalize_prepared_dispatch_payload(
    *,
    bl: Any,
    job_id: Any,
    project_name: str,
    project_config: dict[str, Any],
    effective_extra_state: dict[str, Any],
    meta_patch: dict[str, Any],
    preparation_owner_token: str,
) -> None:
    ciphertext = _encrypt_dispatch_envelope(
        {
            "project_name": str(project_name or ""),
            "project_config": dict(project_config or {}),
            "extra_state": dict(effective_extra_state or {}),
        }
    )
    with _job_lifecycle_transaction(bl):
        if not _dispatch_outbox_repository(bl).finalize_prepared_payload(
            job_id=str(job_id),
            meta_patch=dict(meta_patch or {}),
            encrypted_envelope=ciphertext,
            preparation_owner_token=preparation_owner_token,
        ):
            raise RuntimeError("Durable dispatch payload could not be finalized")


def _terminalize_pre_publish_failure(*, bl: Any, job_id: Any, preparation_owner_token: str) -> None:
    from core.application.jobs.terminal_persistence import TerminalPersistenceService

    with _job_lifecycle_transaction(bl):
        repository = _dispatch_outbox_repository(bl)
        if not repository.verify_preparation_owner(
            job_id=str(job_id), owner_token=preparation_owner_token
        ):
            raise PreparationLeaseLostError("Submission preparation lease was lost")
        TerminalPersistenceService(bl).complete_failure(
            job_id=str(job_id),
            status="failed",
            error_message="Submission payload could not be prepared",
            error_code="pre_publish_setup_failed",
            category="submission",
            require_pending_unowned=True,
        )
        if not repository.settle_preparation_failure(
            job_id=str(job_id),
            owner_token=preparation_owner_token,
            last_error="pre_publish_setup_failed",
        ):
            raise PreparationLeaseLostError("Submission preparation lease was lost")
