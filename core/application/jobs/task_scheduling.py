"""Atomic scheduled-job creation, locking, and publication."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from core.application.jobs.submission_admission import admit_public_submission
from core.application.jobs.task_dispatch_common import (
    _encrypt_dispatch_envelope,
    _finalize_prepared_dispatch_payload,
    _initialize_queue_delivery_meta,
    _job_lifecycle_transaction,
    _journal_replay_snapshot_reservation,
    _persist_requested_workflow,
    _ScheduledLockConflict,
    _ScheduledLockSchemaUnavailable,
    _terminalize_pre_publish_failure,
    initialize_public_job_progress,
    prepare_enqueue_inputs,
)
from core.application.jobs.task_dispatch_publish import (
    _activate_scheduled_lock,
    _dispatch_created_notification,
    _publish_initial_task_message,
    _record_initial_publish_attempt,
    _record_initial_publish_confirmation,
    _release_scheduled_lock,
)
from core.infrastructure.db.scheduled_job_lock_store import (
    ScheduledJobExecutionLockSchemaUnavailableError,
    default_pre_publish_lock_expiry,
)


def _enqueue_scheduled_job_unadmitted(
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
    scheduled_job_id: str,
    account_id: str,
    workflow_id: str,
    lock_key: str,
    run_type: str,
    scheduled_for: datetime | None,
    scheduler_instance_id: str,
    notification_dispatch_fn=None,
    notification_account_id: str | None = None,
    mode: str | None = None,
    artifact_plane=None,
    replay_snapshot: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Create a pending job and scheduled lock atomically before publishing."""
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
    job_id: Any | None = None
    lock_row: dict[str, Any] | None = None
    normalized_lock_key = str(lock_key or "").strip()
    preparation_owner_token = uuid.uuid4().hex if replay_snapshot is not None else ""
    preparation_lease_seconds = 300

    dispatch_envelope = {
        "project_name": str(project_name or ""),
        "project_config": dict(project_config or {}),
        "extra_state": dict(effective_extra_state or {}),
    }
    try:
        with _job_lifecycle_transaction(bl) as transaction_conn:
            from core.application.jobs.job_submission_repository import JobSubmissionRepository

            job_id = JobSubmissionRepository(bl, transaction_conn).create(
                encrypted_envelope=_encrypt_dispatch_envelope(dispatch_envelope),
                outbox_status="preparing" if replay_snapshot is not None else "pending",
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
                workflow_id=workflow_id,
            )
            try:
                lock_row = bl.acquire_scheduled_job_execution_lock(
                    scheduled_job_id=scheduled_job_id,
                    account_id=account_id,
                    project_id=project_id,
                    workflow_id=workflow_id,
                    lock_key=normalized_lock_key,
                    job_id=job_id,
                    run_type=run_type,
                    status="pre_publish",
                    scheduled_for=scheduled_for,
                    scheduler_instance_id=scheduler_instance_id,
                    expires_at=default_pre_publish_lock_expiry(),
                )
            except ScheduledJobExecutionLockSchemaUnavailableError as exc:
                raise _ScheduledLockSchemaUnavailable(str(exc)) from exc
            if not lock_row:
                raise _ScheduledLockConflict
    except _ScheduledLockSchemaUnavailable as exc:
        return {
            "status": "failed",
            "reason": "scheduled_job_lock_schema_unavailable",
            "job_id": None,
            "lock_id": None,
            "error": str(exc),
        }
    except _ScheduledLockConflict:
        blockers = (
            bl.list_active_scheduled_job_execution_locks(
                account_id=account_id,
                project_id=project_id,
                lock_key=normalized_lock_key,
                limit=1,
            )
            if hasattr(bl, "list_active_scheduled_job_execution_locks")
            else []
        )
        blocker = blockers[0] if blockers else {}
        return {
            "status": "conflict",
            "reason": "overlap_active",
            "job_id": None,
            "lock_id": None,
            "blocking_job_id": blocker.get("job_id"),
        }

    if replay_snapshot is not None:
        replay_reservation: dict[str, str] | None = None
        try:
            if artifact_plane is None:
                raise RuntimeError("Artifact storage is required for workflow replay snapshots")
            from core.application.jobs.job_invocation_artifacts import (
                capture_replay_snapshot,
                mint_replay_file_handles,
                reserve_replay_snapshot_artifact,
            )

            final_snapshot = dict(replay_snapshot)
            final_files = effective_extra_state.get("files")
            if isinstance(final_files, list):
                final_snapshot["files"] = mint_replay_file_handles(
                    artifact_plane=artifact_plane,
                    project_id=str(project_id),
                    job_id=str(job_id),
                    files=list(final_files),
                )
            final_snapshot["_workflow_secret_ref_fingerprints"] = list(
                effective_extra_state.get("_workflow_secret_ref_fingerprints") or []
            )
            task_envelope = final_snapshot.get("task_envelope")
            if isinstance(task_envelope, dict):
                final_envelope = dict(task_envelope)
                final_envelope["files"] = list(final_snapshot.get("files") or [])
                final_snapshot["task_envelope"] = final_envelope
            replay_reservation = reserve_replay_snapshot_artifact()
            _journal_replay_snapshot_reservation(
                bl=bl,
                job_id=job_id,
                reservation=replay_reservation,
            )
            pointer = capture_replay_snapshot(
                artifact_plane=artifact_plane,
                project_id=str(project_id),
                job_id=str(job_id),
                artifact_id=replay_reservation["artifact_id"],
                snapshot=final_snapshot,
            )
            _finalize_prepared_dispatch_payload(
                bl=bl,
                job_id=job_id,
                project_name=project_name,
                project_config=dict(project_config or {}),
                effective_extra_state=effective_extra_state,
                meta_patch={"replay_snapshot": pointer},
                preparation_owner_token=preparation_owner_token,
            )
        except Exception:
            if replay_reservation is not None and artifact_plane is not None:
                try:
                    artifact_plane.delete_artifact(replay_reservation["artifact_id"])
                except Exception:
                    logger.exception("%s: could not clean failed replay artifact", log_prefix)
            _release_scheduled_lock(
                bl,
                lock_row,
                job_id=job_id,
                project_id=project_id,
                reason="replay_snapshot_failed",
                logger=logger,
                log_prefix=log_prefix,
            )
            _terminalize_pre_publish_failure(
                bl=bl,
                job_id=job_id,
                preparation_owner_token=preparation_owner_token,
            )
            raise

    try:
        attempted_meta = _record_initial_publish_attempt(
            bl=bl,
            job_id=job_id,
            fallback_meta=effective_meta,
            queue_name=queue_name,
            project_config=project_config,
        )
    except Exception:
        _release_scheduled_lock(
            bl,
            lock_row,
            job_id=job_id,
            project_id=project_id,
            reason="publish_fence_lost",
            logger=logger,
            log_prefix=log_prefix,
        )
        raise

    try:
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
            wrap_transport_error=False,
        )
    except Exception:
        _release_scheduled_lock(
            bl,
            lock_row,
            job_id=job_id,
            project_id=project_id,
            reason="publish_failed",
            logger=logger,
            log_prefix=log_prefix,
        )
        raise

    _record_initial_publish_confirmation(
        bl=bl,
        job_id=job_id,
        attempted_meta=attempted_meta,
        queue_name=queue_name,
        project_config=project_config,
        logger=logger,
        log_prefix=log_prefix,
        suppress_marker_errors=False,
    )
    _activate_scheduled_lock(
        bl=bl,
        lock_row=lock_row,
        job_id=job_id,
        logger=logger,
        log_prefix=log_prefix,
    )

    logger.info(
        "%s: enqueued scheduled task_id=%s job_id=%s project_id=%s queue=%s",
        log_prefix,
        task_id,
        str(job_id or ""),
        project_id,
        queue_name,
    )
    if effective_test_mode:
        logger.info(
            "%s: test_mode enabled for scheduled job_id=%s — skipping external notifications",
            log_prefix,
            str(job_id or ""),
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

    return {
        "status": "enqueued",
        "job_id": str(job_id or ""),
        "lock_id": str((lock_row or {}).get("id") or "").strip() or None,
        "queue_name": queue_name,
    }


def enqueue_scheduled_job(**kwargs: Any) -> dict[str, Any]:
    """Create a scheduled job through the canonical new-job admission gate."""
    with admit_public_submission(
        task_source=str(kwargs.get("task_source") or "schedule"),
        meta=kwargs.get("meta") if isinstance(kwargs.get("meta"), dict) else None,
    ):
        return _enqueue_scheduled_job_unadmitted(**kwargs)
