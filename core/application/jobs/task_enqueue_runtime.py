"""Stable façade for job submission, dispatch, recovery, and scheduling."""

from __future__ import annotations

import uuid
from typing import Any

from core.application.auth.secrets import decrypt_secret as decrypt_secret
from core.application.jobs.dispatch_outbox_repository import DispatchOutboxRepository
from core.application.jobs.job_invocation_artifacts import (
    capture_replay_snapshot,
    mint_replay_file_handles,
    reserve_replay_snapshot_artifact,
)
from core.application.jobs.preparation_lease import (
    PreparationLeaseHeartbeat,
    PreparationLeaseLostError,
)
from core.application.jobs.submission_admission import admit_public_submission
from core.application.jobs.task_dispatch_common import (
    QueueTransportUnavailableError as QueueTransportUnavailableError,
)
from core.application.jobs.task_dispatch_common import (
    _apply_publish_failure_outbox_disposition as _apply_publish_failure_outbox_disposition,
)
from core.application.jobs.task_dispatch_common import (
    _dispatch_outbox_repository,
    _encrypt_dispatch_envelope,
    _finalize_prepared_dispatch_payload,
    _initialize_queue_delivery_meta,
    _job_lifecycle_transaction,
    _journal_replay_snapshot_reservation,
    _persist_requested_workflow,
    _preparation_heartbeat_connection_provider,
    _replace_dispatch_outbox_envelope,
    _run_pre_publish_hook,
    _terminalize_pre_publish_failure,
    initialize_public_job_progress,
    prepare_enqueue_inputs,
)
from core.application.jobs.task_dispatch_common import (
    _is_queue_transport_unavailable as _is_queue_transport_unavailable,
)
from core.application.jobs.task_dispatch_common import (
    _persist_queue_delivery_transition as _persist_queue_delivery_transition,
)
from core.application.jobs.task_dispatch_publish import (
    _dispatch_created_notification,
    _publish_initial_task_message,
    _record_initial_publish_attempt,
    _record_initial_publish_confirmation,
)
from core.application.jobs.task_dispatch_publish import (
    _log_publish_failure as _log_publish_failure,
)
from core.application.jobs.task_dispatch_recovery import (
    redeliver_pre_execution_job as redeliver_pre_execution_job,
)
from core.application.jobs.task_dispatch_recovery import (
    relay_pending_dispatch_outbox as relay_pending_dispatch_outbox,
)
from core.application.jobs.task_scheduling import (
    enqueue_scheduled_job as enqueue_scheduled_job,
)

_PREPARED_EXECUTION_STATE_KEYS = ("files", "inputs", "task_envelope")


def _final_replay_snapshot(
    candidate: dict[str, Any],
    *,
    artifact_plane: Any,
    project_id: str,
    job_id: str,
    effective_extra_state: dict[str, Any],
) -> dict[str, Any]:
    snapshot = dict(candidate)
    final_inputs = effective_extra_state.get("inputs")
    if isinstance(final_inputs, dict):
        snapshot["inputs"] = dict(final_inputs)
    final_files = effective_extra_state.get("files")
    if isinstance(final_files, list):
        snapshot["files"] = mint_replay_file_handles(
            artifact_plane=artifact_plane,
            project_id=project_id,
            job_id=job_id,
            files=list(final_files),
        )
    snapshot["_workflow_secret_ref_fingerprints"] = list(
        effective_extra_state.get("_workflow_secret_ref_fingerprints") or []
    )
    task_envelope = effective_extra_state.get("task_envelope")
    if isinstance(task_envelope, dict):
        final_envelope = dict(task_envelope)
        final_envelope["inputs"] = dict(snapshot.get("inputs") or {})
        final_envelope["files"] = list(snapshot.get("files") or [])
        snapshot["task_envelope"] = final_envelope
    return snapshot


def _pointer_only_meta_patch(
    pre_publish_patch: dict[str, Any],
    *,
    replay_snapshot: dict[str, Any] | None,
) -> dict[str, Any]:
    if replay_snapshot is None:
        return dict(pre_publish_patch)
    return {
        str(key): value
        for key, value in pre_publish_patch.items()
        if key not in {"execution_envelope", "files", "inputs", "task_envelope"}
    }


def _enqueue_job_unadmitted(
    *,
    bl,
    task_id: str,
    task_summary: str,
    task_source: str,
    project_id: Any,
    project_name: str,
    project_config: dict | None,
    test_mode: bool | None,
    extra_state: dict | None,
    meta: dict | None,
    enqueue_task_fn,
    build_project_config_snapshot,
    logger,
    log_prefix: str,
    pre_publish_fn=None,
    preparation_additional_heartbeat=None,
    fail_on_pre_publish_error: bool = False,
    notification_dispatch_fn=None,
    notification_account_id: str | None = None,
    mode: str | None = None,
    artifact_plane=None,
    replay_snapshot: dict[str, Any] | None = None,
    job_created_hook=None,
) -> int:
    """Create a pending job row, publish it, and perform non-fatal side effects.

    ``job_created_hook(conn, job_id)`` runs inside the creating transaction,
    before anything is published; raising rolls the new job back.
    """
    normalized_mode = str(mode or "").strip() or None
    effective_meta, effective_extra_state, effective_test_mode = prepare_enqueue_inputs(
        project_config=project_config,
        test_mode=test_mode,
        extra_state=extra_state,
        meta=meta,
        build_project_config_snapshot=build_project_config_snapshot,
    )
    thread_id = uuid.uuid4().hex
    queue_name = f"platform.project.{project_id}"
    _initialize_queue_delivery_meta(
        effective_meta=effective_meta,
        queue_name=queue_name,
        project_config=project_config,
    )
    dispatch_envelope = {
        "project_name": str(project_name or ""),
        "project_config": dict(project_config or {}),
        "extra_state": dict(effective_extra_state or {}),
    }
    dispatch_outbox_ciphertext = _encrypt_dispatch_envelope(dispatch_envelope)
    needs_preparation = bool(pre_publish_fn or replay_snapshot is not None)
    preparation_owner_token = uuid.uuid4().hex if needs_preparation else ""
    preparation_lease_seconds = 300
    with _job_lifecycle_transaction(bl) as transaction_conn:
        from core.application.jobs.job_submission_repository import JobSubmissionRepository

        job_id = JobSubmissionRepository(bl, transaction_conn).create(
            encrypted_envelope=dispatch_outbox_ciphertext,
            outbox_status="preparing" if needs_preparation else "pending",
            preparation_owner_token=preparation_owner_token or None,
            preparation_lease_seconds=preparation_lease_seconds,
            project_id=project_id,
            task_id=task_id,
            task_summary=task_summary,
            task_source=task_source,
            thread_id=thread_id,
            mode=normalized_mode,
            complexity=None,
            status="pending",
            queue_name=queue_name,
            selected_workflow_key=str(effective_extra_state.get("workflow_id") or "").strip()
            or None,
            meta=effective_meta or None,
        )
        if callable(job_created_hook):
            job_created_hook(transaction_conn, str(job_id))
        _persist_requested_workflow(
            bl=bl,
            job_id=job_id,
            effective_extra_state=effective_extra_state,
            logger=logger,
        )
        initialize_public_job_progress(
            bl,
            job_id=job_id,
            project_id=project_id,
            workflow_id=str(effective_extra_state.get("workflow_id") or ""),
        )

    # Commit the job, initial incomplete envelope, and outbox before any
    # object-storage side effect. Attachment cleanup reservations written by
    # the hook are then independently durable if the publisher process dies.
    replay_reservation: dict[str, str] | None = None
    try:
        if needs_preparation:
            with PreparationLeaseHeartbeat(
                bl,
                job_id=str(job_id),
                owner_token=preparation_owner_token,
                lease_seconds=preparation_lease_seconds,
                connection_provider=_preparation_heartbeat_connection_provider(bl),
                renew_preparation=lambda connection=None: (
                    DispatchOutboxRepository(connection).renew_preparation_lease(
                        job_id=str(job_id),
                        owner_token=preparation_owner_token,
                        lease_seconds=preparation_lease_seconds,
                    )
                    if connection is not None
                    else _dispatch_outbox_repository(bl).renew_preparation_lease(
                        job_id=str(job_id),
                        owner_token=preparation_owner_token,
                        lease_seconds=preparation_lease_seconds,
                    )
                ),
                additional_heartbeat=preparation_additional_heartbeat,
            ) as preparation_lease:
                pre_publish_patch = _run_pre_publish_hook(
                    job_id=job_id,
                    bl=bl,
                    pre_publish_fn=pre_publish_fn,
                    logger=logger,
                    log_prefix=log_prefix,
                    fail_on_pre_publish_error=fail_on_pre_publish_error,
                    preparation_heartbeat=preparation_lease.heartbeat,
                )
        else:
            pre_publish_patch = {}
        if pre_publish_patch:
            # Attachment hooks also return durable job metadata. Only prepared
            # payload fields belong in graph state; archive summaries do not.
            effective_extra_state.update(
                {
                    key: pre_publish_patch[key]
                    for key in _PREPARED_EXECUTION_STATE_KEYS
                    if key in pre_publish_patch
                }
            )
        final_meta_patch = _pointer_only_meta_patch(
            pre_publish_patch,
            replay_snapshot=replay_snapshot,
        )
        if replay_snapshot is not None:
            if artifact_plane is None:
                raise RuntimeError("Artifact storage is required for workflow replay snapshots")
            replay_reservation = reserve_replay_snapshot_artifact()
            _journal_replay_snapshot_reservation(
                bl=bl,
                job_id=job_id,
                reservation=replay_reservation,
            )
            final_meta_patch["replay_snapshot"] = capture_replay_snapshot(
                artifact_plane=artifact_plane,
                project_id=str(project_id),
                job_id=str(job_id),
                artifact_id=replay_reservation["artifact_id"],
                snapshot=_final_replay_snapshot(
                    replay_snapshot,
                    artifact_plane=artifact_plane,
                    project_id=str(project_id),
                    job_id=str(job_id),
                    effective_extra_state=effective_extra_state,
                ),
            )
        if needs_preparation:
            _finalize_prepared_dispatch_payload(
                bl=bl,
                job_id=job_id,
                project_name=project_name,
                project_config=dict(project_config or {}),
                effective_extra_state=effective_extra_state,
                meta_patch=final_meta_patch,
                preparation_owner_token=preparation_owner_token,
            )
        else:
            _replace_dispatch_outbox_envelope(
                bl=bl,
                job_id=job_id,
                project_name=project_name,
                project_config=dict(project_config or {}),
                effective_extra_state=effective_extra_state,
            )
    except PreparationLeaseLostError:
        # Ownership has transferred (or is eligible to transfer) to cleanup.
        # A stale submitter must not mutate the job or another owner's outbox row.
        raise
    except Exception:
        if replay_reservation is not None and artifact_plane is not None:
            try:
                artifact_plane.delete_artifact(replay_reservation["artifact_id"])
            except Exception:
                logger.exception("%s: could not clean failed replay artifact", log_prefix)
        try:
            _terminalize_pre_publish_failure(
                bl=bl,
                job_id=job_id,
                preparation_owner_token=preparation_owner_token,
            )
        except Exception:
            # If persistence itself is unavailable, leave the preparation row
            # recoverable instead of independently mutating half of the state.
            logger.exception("%s: could not terminalize failed payload preparation", log_prefix)
        raise

    attempted_meta = _record_initial_publish_attempt(
        bl=bl,
        job_id=job_id,
        fallback_meta=effective_meta,
        queue_name=queue_name,
        project_config=project_config,
    )
    _publish_initial_task_message(
        bl=bl,
        job_id=job_id,
        project_id=project_id,
        task_id=task_id,
        thread_id=thread_id,
        task_summary=task_summary,
        task_source=task_source,
        project_name=project_name,
        project_config=project_config,
        effective_extra_state=effective_extra_state,
        queue_name=queue_name,
        attempted_meta=attempted_meta,
        enqueue_task_fn=enqueue_task_fn,
        logger=logger,
        log_prefix=log_prefix,
    )
    _record_initial_publish_confirmation(
        bl=bl,
        job_id=job_id,
        attempted_meta=attempted_meta,
        queue_name=queue_name,
        project_config=project_config,
        logger=logger,
        log_prefix=log_prefix,
    )

    logger.info(
        "Enqueued task_id=%s job_id=%s project_id=%s queue=%s",
        task_id,
        job_id,
        project_id,
        queue_name,
    )
    if effective_test_mode:
        logger.info(
            "%s: test_mode enabled for job_id=%s — skipping external notifications",
            log_prefix,
            job_id,
        )
    else:
        _dispatch_created_notification(
            bl=bl,
            project_id=project_id,
            job_id=job_id,
            task_id=task_id,
            task_summary=task_summary,
            task_source=task_source,
            queue_name=queue_name,
            workflow_id=str(effective_extra_state.get("workflow_id") or "").strip() or None,
            notification_dispatch_fn=notification_dispatch_fn,
            notification_account_id=notification_account_id,
            logger=logger,
            log_prefix=log_prefix,
        )
    return job_id


def enqueue_job(**kwargs) -> int:
    """Enqueue a job after the one public-submission lifecycle boundary."""
    from core.application.workflows.workflow_start_runtime import safe_workflow_job_metadata

    task_source = str(kwargs.get("task_source") or "")
    raw_meta = kwargs.get("meta")
    meta = safe_workflow_job_metadata(raw_meta) if isinstance(raw_meta, dict) else None
    if meta is not None:
        kwargs = {**kwargs, "meta": meta}
    with admit_public_submission(task_source=task_source, meta=meta):
        return _enqueue_job_unadmitted(**kwargs)


def enqueue_task(
    *,
    config: dict[str, Any],
    create_logger_fn,
    task_id: str,
    task_summary: str,
    task_source: str,
    project_id: Any,
    project_name: str,
    project_config: dict | None = None,
    mode: str | None = None,
    test_mode: bool | None = None,
    extra_state: dict | None = None,
    meta: dict | None = None,
    enqueue_task_fn,
    build_project_config_snapshot,
    project_account_id_fn,
    logger,
    log_prefix: str,
    pre_publish_fn=None,
    preparation_additional_heartbeat=None,
    fail_on_pre_publish_error: bool = False,
    artifact_plane=None,
    replay_snapshot: dict[str, Any] | None = None,
) -> int:
    dsn = config.get("fs_observability_dsn", "") or config.get("fs_db_dsn", "")
    if not dsn:
        raise RuntimeError("FS_OBSERVABILITY_DSN/FS_DB_DSN not configured")

    bl = create_logger_fn(dsn)

    from core.application.notifications.dispatch import dispatch_project_notification_event

    return enqueue_job(
        bl=bl,
        task_id=task_id,
        task_summary=task_summary,
        task_source=task_source,
        project_id=project_id,
        project_name=project_name,
        project_config=project_config,
        mode=mode,
        test_mode=test_mode,
        extra_state=extra_state,
        meta=meta,
        enqueue_task_fn=enqueue_task_fn,
        build_project_config_snapshot=build_project_config_snapshot,
        logger=logger,
        log_prefix=log_prefix,
        pre_publish_fn=pre_publish_fn,
        preparation_additional_heartbeat=preparation_additional_heartbeat,
        fail_on_pre_publish_error=fail_on_pre_publish_error,
        artifact_plane=artifact_plane,
        replay_snapshot=replay_snapshot,
        notification_dispatch_fn=dispatch_project_notification_event,
        notification_account_id=project_account_id_fn(bl, project_id),
    )


def build_enqueue_task_handler(
    *,
    get_config,
    create_logger_fn,
    enqueue_task_fn,
    build_project_config_snapshot,
    project_account_id_fn,
    logger,
    log_prefix: str = "_enqueue_task",
    artifact_plane=None,
):
    def _enqueue(**kwargs) -> int:
        return enqueue_task(
            config=get_config(),
            create_logger_fn=create_logger_fn,
            enqueue_task_fn=enqueue_task_fn,
            build_project_config_snapshot=build_project_config_snapshot,
            project_account_id_fn=project_account_id_fn,
            logger=logger,
            log_prefix=log_prefix,
            artifact_plane=artifact_plane() if callable(artifact_plane) else artifact_plane,
            **kwargs,
        )

    return _enqueue
