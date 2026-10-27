# mypy: ignore-errors
"""Scheduled jobs scan helpers extracted from core.infrastructure.runtime.control.scans.

This module keeps scheduled-job orchestration logic isolated so control/scans.py
can focus on watchdog/dashboard/security scans.
"""

from __future__ import annotations

import logging
import os
import socket
import uuid
from datetime import UTC, datetime
from functools import lru_cache
from typing import Any

from core.application.jobs import task_enqueue_runtime as _task_enqueue_runtime
from core.application.jobs.scheduled_job_concurrency import (
    DEFAULT_SCHEDULED_JOB_OVERLAP_REASON,
    resolve_scheduled_job_concurrency_key,
)
from core.application.jobs.scheduled_job_outcomes import update_scheduled_job_outcome
from core.application.orchestration.input_sanitization import sanitize_external_input
from core.application.workflows.workflow_start_runtime import (
    workflow_run_summary,
    workflow_run_summary_metadata,
)
from core.infrastructure.config.runtime_env import runtime_env as _runtime_env
from core.infrastructure.runtime.control._cron import cron_next_run_at
from core.infrastructure.runtime.control.config import ControlConfig
from core.infrastructure.runtime.control.scans_scheduled_jobs_common import (
    _as_utc_datetime,
    _build_project_config_snapshot,
    _json_safe,
)

logger = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def _job_invocation_artifact_plane() -> Any:
    from core.application.extensions.artifact_dataset_plane_v2 import ArtifactDatasetPlane
    from core.application.extensions.artifact_storage import create_storage_adapter
    from core.infrastructure.runtime.artifact_storage_factory import build_artifact_storage

    return build_artifact_storage(
        environ=dict(_runtime_env),
        create_storage_adapter=create_storage_adapter,
        artifact_plane_factory=ArtifactDatasetPlane,
    )


def _normalize_cron_expr(value: str) -> str:
    return " ".join(str(value or "").strip().lower().split())


def _materialize_scheduled_job_launch(
    workflow_id: str, caller_inputs: dict[str, Any]
) -> tuple[dict[str, Any], list[str]]:
    """Resolve current defaults and secret authority at the execution boundary."""
    from core.application.registry.workflow_contract_service import WorkflowInterfaceService
    from core.application.registry.workflow_secret_defaults import workflow_secret_launch_snapshot

    interface_service = WorkflowInterfaceService()
    interface = interface_service.interface_for_workflow_id(workflow_id)
    validation = interface_service.validate_inputs(interface, caller_inputs, channel="schedule")
    if not validation.ok:
        detail = "; ".join(
            str(item.get("message") or item.get("code") or "").strip()
            for item in validation.errors[:5]
            if isinstance(item, dict)
        )
        raise ValueError(detail or "Workflow schedule inputs are invalid")
    return dict(validation.inputs), workflow_secret_launch_snapshot(
        interface,
        inputs=validation.inputs,
    )


def _scheduled_job_next_run_at(
    cron_expr: str,
    *,
    scheduled_for: datetime,
    now_utc: datetime,
    schedule_tz: str | None = None,
) -> datetime:
    reference_at = scheduled_for if scheduled_for >= now_utc else now_utc
    return cron_next_run_at(cron_expr, reference_at, schedule_tz=schedule_tz)


def _scheduler_instance_id() -> str:
    explicit = (
        str(_runtime_env.get("SCHEDULED_JOB_SCHEDULER_ID", "")).strip()
        or str(_runtime_env.get("CONTROL_PLANE_INSTANCE_ID", "")).strip()
        or str(_runtime_env.get("HOSTNAME", "")).strip()
    )
    if explicit:
        return explicit
    return f"{socket.gethostname()}-{os.getpid()}"


def _record_scheduled_job_outcome(
    bl,
    *,
    scheduled_job: dict,
    run_type: str,
    status: str,
    reason: str = "",
    job_id: str | None = None,
    scheduled_for: datetime | None = None,
    next_run_at: datetime | None = None,
    mark_error: bool = False,
    detail: dict | None = None,
) -> None:
    project_id = str(scheduled_job.get("project_id") or "").strip()
    scheduled_job_id = str(scheduled_job.get("id") or "").strip()
    account_id = str(scheduled_job.get("account_id") or "").strip()
    workflow_id = str(scheduled_job.get("workflow_id") or "").strip()
    detail_payload = dict(detail or {})
    if not scheduled_job_id or not project_id or not workflow_id:
        return

    if account_id and hasattr(bl, "create_scheduled_job_run"):
        try:
            bl.create_scheduled_job_run(
                scheduled_job_id=scheduled_job_id,
                account_id=account_id,
                project_id=project_id,
                workflow_id=workflow_id,
                run_type=run_type,
                status=status,
                reason=reason,
                job_id=job_id,
                scheduled_for=scheduled_for,
                detail=detail_payload,
            )
        except TypeError:
            bl.create_scheduled_job_run(
                scheduled_job_id=scheduled_job_id,
                project_id=project_id,
                workflow_id=workflow_id,
                run_type=run_type,
                status=status,
                reason=reason,
                job_id=job_id,
                scheduled_for=scheduled_for,
                detail=detail_payload,
            )
        except Exception as exc:
            logger.debug(
                "Could not persist scheduled job run row for id=%s: %s", scheduled_job_id, exc
            )

    if hasattr(bl, "update_scheduled_job"):
        if mark_error:
            scheduled_status = "error"
        elif bool(scheduled_job.get("enabled", True)):
            scheduled_status = "active"
        else:
            scheduled_status = "paused"
        update_kwargs: dict[str, Any] = {
            "last_run_at": datetime.now(UTC),
            "last_run_status": status,
            "last_skip_reason": reason if status == "skipped" else "",
            "last_error": reason if status == "failed" else "",
            "status": scheduled_status,
        }
        if job_id:
            update_kwargs["last_job_id"] = str(job_id)
        if next_run_at is not None:
            update_kwargs["next_run_at"] = next_run_at
        try:
            update_scheduled_job_outcome(
                bl,
                project_id=project_id,
                scheduled_job_id=scheduled_job_id,
                workflow_id=workflow_id,
                account_id=account_id,
                changes=update_kwargs,
            )
        except Exception as exc:
            logger.debug("Could not update scheduled job row for id=%s: %s", scheduled_job_id, exc)


def _enqueue_task_standalone(
    bl,
    *,
    task_id: str,
    task_summary: str,
    task_source: str,
    project_id: str,
    project_name: str,
    project_config: dict,
    _workflow_id: str,
    mode: str | None = None,
    test_mode: bool | None = None,
    extra_state: dict | None = None,
    meta: dict | None = None,
    pre_publish_fn=None,
) -> int:
    from core.application.notifications.dispatch import dispatch_project_notification_event
    from core.infrastructure.runtime.worker.queue_publisher import enqueue_task

    return _task_enqueue_runtime.enqueue_job(
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
        enqueue_task_fn=enqueue_task,
        build_project_config_snapshot=_build_project_config_snapshot,
        logger=logger,
        log_prefix="_enqueue_task_standalone",
        pre_publish_fn=pre_publish_fn,
        fail_on_pre_publish_error=False,
        notification_dispatch_fn=dispatch_project_notification_event,
    )


def _enqueue_scheduled_job(
    bl,
    *,
    task_id: str,
    task_summary: str,
    task_source: str,
    project_id: str,
    project_name: str,
    project_config: dict,
    workflow_id: str,
    scheduled_job_id: str,
    account_id: str,
    lock_key: str,
    run_type: str,
    scheduled_for: datetime | None,
    mode: str | None = None,
    test_mode: bool | None = None,
    extra_state: dict | None = None,
    meta: dict | None = None,
    replay_snapshot: dict[str, Any] | None = None,
) -> dict[str, Any]:
    from core.application.notifications.dispatch import dispatch_project_notification_event
    from core.infrastructure.runtime.worker.queue_publisher import enqueue_task

    return _task_enqueue_runtime.enqueue_scheduled_job(
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
        enqueue_task_fn=enqueue_task,
        build_project_config_snapshot=_build_project_config_snapshot,
        logger=logger,
        log_prefix="_enqueue_scheduled_job",
        scheduled_job_id=scheduled_job_id,
        account_id=account_id,
        workflow_id=workflow_id,
        lock_key=lock_key,
        run_type=run_type,
        scheduled_for=scheduled_for,
        scheduler_instance_id=_scheduler_instance_id(),
        notification_dispatch_fn=dispatch_project_notification_event,
        notification_account_id=account_id,
        artifact_plane=_job_invocation_artifact_plane(),
        replay_snapshot=replay_snapshot,
    )


def _run_scheduled_job_once(
    bl,
    scheduled_job: dict,
    cfg: ControlConfig,
    *,
    run_type: str = "scheduled",
    scheduled_for: datetime | None = None,
    advance_schedule: bool = True,
    now_utc: datetime | None = None,
    _as_utc_datetime_fn=None,
    _enqueue_scheduled_job_fn=None,
    _record_scheduled_job_outcome_fn=None,
    _scheduled_job_next_run_at_fn=None,
    _get_project_run_config_fn=None,
    _materialize_scheduled_job_launch_fn=None,
) -> dict:
    _ = cfg
    as_utc_datetime_fn = _as_utc_datetime_fn or _as_utc_datetime
    record_scheduled_job_outcome_fn = (
        _record_scheduled_job_outcome_fn or _record_scheduled_job_outcome
    )
    scheduled_job_next_run_at_fn = _scheduled_job_next_run_at_fn or _scheduled_job_next_run_at
    enqueue_scheduled_job_fn = _enqueue_scheduled_job_fn or _enqueue_scheduled_job
    get_project_run_config_fn = getattr(bl, "get_project_run_config", None)
    if not callable(get_project_run_config_fn):
        get_project_run_config_fn = _get_project_run_config_fn
    current_now_candidate = as_utc_datetime_fn(now_utc, fallback=datetime.now(UTC))
    current_now_utc = (
        current_now_candidate if isinstance(current_now_candidate, datetime) else datetime.now(UTC)
    )
    scheduled_for_candidate = as_utc_datetime_fn(
        scheduled_for or scheduled_job.get("next_run_at"),
        fallback=current_now_utc,
    )
    scheduled_for_utc = (
        scheduled_for_candidate
        if isinstance(scheduled_for_candidate, datetime)
        else current_now_utc
    )
    scheduled_job_id = str(scheduled_job.get("id") or "").strip()
    project_id = str(scheduled_job.get("project_id") or "").strip()
    workflow_id = str(scheduled_job.get("workflow_id") or "").strip()
    cron_expr = _normalize_cron_expr(scheduled_job.get("cron_expr") or "")
    if not scheduled_job_id or not project_id or not workflow_id:
        return {"status": "failed", "reason": "invalid_scheduled_job_payload"}

    admission_allowed = bool(scheduled_job.get("admission_allowed"))
    try:
        admission_revision = int(scheduled_job.get("admission_workflow_revision"))
        current_revision = int(scheduled_job.get("current_workflow_revision"))
    except (TypeError, ValueError):
        admission_revision = 0
        current_revision = 0
    if not admission_allowed or admission_revision < 1 or admission_revision != current_revision:
        reason = (
            "workflow_admission_stale"
            if admission_allowed and admission_revision != current_revision
            else str(scheduled_job.get("admission_reason") or "workflow_admission_missing")
        )
        next_run_at = (
            as_utc_datetime_fn(scheduled_job.get("next_run_at"), fallback=None)
            if scheduled_job.get("next_run_at")
            else None
        )
        record_scheduled_job_outcome_fn(
            bl,
            scheduled_job=scheduled_job,
            run_type=run_type,
            status="skipped",
            reason=reason,
            scheduled_for=scheduled_for_utc,
            next_run_at=next_run_at,
            mark_error=False,
            detail={
                "workflow_id": workflow_id,
                "admission_workflow_revision": admission_revision or None,
                "current_workflow_revision": current_revision or None,
            },
        )
        return {"status": "skipped", "reason": reason, "next_run_at": next_run_at}

    next_run_at: datetime | None
    if advance_schedule:
        schedule_tz = (
            str(scheduled_job.get("schedule_timezone") or "").strip()
            or str(scheduled_job.get("project_schedule_timezone") or "").strip()
            or None
        )
        try:
            try:
                next_run_at = scheduled_job_next_run_at_fn(
                    cron_expr,
                    scheduled_for=scheduled_for_utc,
                    now_utc=current_now_utc,
                    schedule_tz=schedule_tz,
                )
            except TypeError:
                next_run_at = scheduled_job_next_run_at_fn(
                    cron_expr,
                    scheduled_for=scheduled_for_utc,
                    now_utc=current_now_utc,
                )
        except Exception as exc:
            reason = f"invalid_cron_expr:{exc}"
            record_scheduled_job_outcome_fn(
                bl,
                scheduled_job=scheduled_job,
                run_type=run_type,
                status="failed",
                reason=reason,
                scheduled_for=scheduled_for_utc,
                next_run_at=None,
                mark_error=True,
            )
            return {"status": "failed", "reason": reason}
    else:
        next_run_at = (
            as_utc_datetime_fn(scheduled_job.get("next_run_at"), fallback=None)
            if scheduled_job.get("next_run_at")
            else None
        )

    if not callable(get_project_run_config_fn):
        reason = "project_run_config_unavailable"
        record_scheduled_job_outcome_fn(
            bl,
            scheduled_job=scheduled_job,
            run_type=run_type,
            status="failed",
            reason=reason,
            scheduled_for=scheduled_for_utc,
            next_run_at=next_run_at,
            mark_error=True,
        )
        return {"status": "failed", "reason": reason, "next_run_at": next_run_at}

    run_cfg = get_project_run_config_fn(project_id) or {}
    if not isinstance(run_cfg, dict) or not run_cfg:
        reason = "project_run_config_not_found"
        record_scheduled_job_outcome_fn(
            bl,
            scheduled_job=scheduled_job,
            run_type=run_type,
            status="failed",
            reason=reason,
            scheduled_for=scheduled_for_utc,
            next_run_at=next_run_at,
            mark_error=True,
        )
        return {"status": "failed", "reason": reason, "next_run_at": next_run_at}

    account_id = str(scheduled_job.get("account_id") or run_cfg.get("account_id") or "").strip()
    if not account_id:
        reason = "scheduled_job_account_unavailable"
        record_scheduled_job_outcome_fn(
            bl,
            scheduled_job=scheduled_job,
            run_type=run_type,
            status="failed",
            reason=reason,
            scheduled_for=scheduled_for_utc,
            next_run_at=next_run_at,
            mark_error=True,
        )
        return {"status": "failed", "reason": reason, "next_run_at": next_run_at}

    effective_summary = sanitize_external_input(workflow_run_summary(workflow_id))

    caller_inputs = (
        dict(scheduled_job.get("inputs") or {})
        if isinstance(scheduled_job.get("inputs"), dict)
        else {}
    )
    materialize_scheduled_job_launch_fn = (
        _materialize_scheduled_job_launch_fn or _materialize_scheduled_job_launch
    )
    try:
        workflow_inputs, secret_ref_snapshot = materialize_scheduled_job_launch_fn(
            workflow_id, caller_inputs
        )
    except Exception:
        reason = "workflow_input_validation_failed"
        record_scheduled_job_outcome_fn(
            bl,
            scheduled_job=scheduled_job,
            run_type=run_type,
            status="failed",
            reason=reason,
            scheduled_for=scheduled_for_utc,
            next_run_at=next_run_at,
            mark_error=True,
        )
        return {"status": "failed", "reason": reason, "next_run_at": next_run_at}

    try:
        lock_key = resolve_scheduled_job_concurrency_key(
            account_id=account_id,
            project_id=project_id,
            scheduled_job_id=scheduled_job_id,
        )
    except ValueError:
        reason = "invalid_schedule_lock_identity"
        record_scheduled_job_outcome_fn(
            bl,
            scheduled_job=scheduled_job,
            run_type=run_type,
            status="failed",
            reason=reason,
            scheduled_for=scheduled_for_utc,
            next_run_at=next_run_at,
            mark_error=True,
        )
        return {"status": "failed", "reason": reason, "next_run_at": next_run_at}

    task_id = f"scheduled-{scheduled_job_id}-{uuid.uuid4().hex[:12]}"
    extra_state = {
        "workflow_id": workflow_id,
        "workflow_selection_source": "schedule_binding",
        "scheduled_job_id": scheduled_job_id,
        "scheduled_job_account_id": account_id,
        "scheduled_run_type": run_type,
        "_workflow_secret_ref_fingerprints": secret_ref_snapshot,
    }
    meta = {
        **workflow_run_summary_metadata(workflow_id),
        "requested_workflow_id": workflow_id,
        "workflow_selection_source": "schedule_binding",
        "scheduled_job_id": scheduled_job_id,
        "scheduled_job_account_id": account_id,
        "scheduled_run_type": run_type,
        "scheduled_for": scheduled_for_utc.isoformat(),
        "scheduled_cron_expr": cron_expr,
    }

    extra_state["inputs"] = workflow_inputs

    try:
        safe_run_cfg = _json_safe(run_cfg)
        if not isinstance(safe_run_cfg, dict):
            safe_run_cfg = {}
        safe_extra_state = _json_safe(extra_state)
        if not isinstance(safe_extra_state, dict):
            safe_extra_state = {}
        safe_meta = _json_safe(meta)
        if not isinstance(safe_meta, dict):
            safe_meta = {}

        from core.application.workflows.workflow_start_runtime import enqueue_workflow_start

        inputs = workflow_inputs

        enqueue_result: dict[str, Any] = {}

        def _enqueue_scheduled_with_canonical_state(**kwargs: Any) -> str:
            local_extra_state = (
                dict(kwargs.get("extra_state") or {})
                if isinstance(kwargs.get("extra_state"), dict)
                else {}
            )
            local_meta = (
                dict(kwargs.get("meta") or {}) if isinstance(kwargs.get("meta"), dict) else {}
            )
            result = enqueue_scheduled_job_fn(
                bl,
                task_id=str(kwargs.get("task_id") or "").strip(),
                task_summary=str(kwargs.get("task_summary") or "").strip(),
                task_source=str(kwargs.get("task_source") or "").strip(),
                project_id=str(kwargs.get("project_id") or "").strip(),
                project_name=str(kwargs.get("project_name") or "").strip(),
                project_config=(
                    dict(kwargs.get("project_config") or {})
                    if isinstance(kwargs.get("project_config"), dict)
                    else {}
                ),
                mode=kwargs.get("mode"),
                test_mode=kwargs.get("test_mode"),
                workflow_id=workflow_id,
                scheduled_job_id=scheduled_job_id,
                account_id=account_id,
                lock_key=lock_key,
                run_type=run_type,
                scheduled_for=scheduled_for_utc,
                extra_state=local_extra_state,
                meta=local_meta,
                replay_snapshot=kwargs.get("replay_snapshot"),
            )
            if isinstance(result, dict):
                enqueue_result.update(result)
            return str((enqueue_result or {}).get("job_id") or "")

        enqueue_workflow_start(
            enqueue_task_fn=_enqueue_scheduled_with_canonical_state,
            task_id=task_id,
            task_summary=effective_summary,
            task_source="schedule",
            project_id=project_id,
            project_name=str(safe_run_cfg.get("project_name") or ""),
            project_config=safe_run_cfg,
            selected_workflow_id=workflow_id,
            workflow_selection_source="schedule_binding",
            entry_path="schedule",
            input_contract="direct_inputs",
            source_system="schedule",
            requested_workflow_id=workflow_id,
            source_event_id=task_id,
            source_object_id=scheduled_job_id,
            mode="",
            test_mode=False,
            inputs=inputs,
            extra_state=safe_extra_state,
            workflow_secret_ref_fingerprints=secret_ref_snapshot,
            job_meta=safe_meta,
        )
    except Exception as exc:
        logger.exception(
            "scheduled job enqueue failed: scheduled_job_id=%s project_id=%s workflow_id=%s",
            scheduled_job_id,
            project_id,
            workflow_id,
        )
        reason = f"enqueue_failed:{exc}"
        record_scheduled_job_outcome_fn(
            bl,
            scheduled_job=scheduled_job,
            run_type=run_type,
            status="failed",
            reason=reason,
            scheduled_for=scheduled_for_utc,
            next_run_at=next_run_at,
            mark_error=False,
        )
        return {"status": "failed", "reason": reason, "next_run_at": next_run_at}

    enqueue_status = str((enqueue_result or {}).get("status") or "").strip().lower()
    if enqueue_status == "conflict":
        overlap_job_id = str((enqueue_result or {}).get("blocking_job_id") or "").strip() or None
        overlap_reason = DEFAULT_SCHEDULED_JOB_OVERLAP_REASON
        record_scheduled_job_outcome_fn(
            bl,
            scheduled_job=scheduled_job,
            run_type=run_type,
            status="skipped",
            reason=overlap_reason,
            job_id=overlap_job_id,
            scheduled_for=scheduled_for_utc,
            next_run_at=next_run_at,
            detail={
                "blocking_job_id": overlap_job_id,
            },
        )
        return {
            "status": "skipped",
            "reason": overlap_reason,
            "job_id": overlap_job_id,
            "next_run_at": next_run_at,
        }

    job_id = str((enqueue_result or {}).get("job_id") or "").strip()
    if enqueue_status != "enqueued" or not job_id:
        reason = str((enqueue_result or {}).get("reason") or "enqueue_failed:unknown").strip()
        record_scheduled_job_outcome_fn(
            bl,
            scheduled_job=scheduled_job,
            run_type=run_type,
            status="failed",
            reason=reason,
            scheduled_for=scheduled_for_utc,
            next_run_at=next_run_at,
            mark_error=False,
        )
        return {"status": "failed", "reason": reason, "next_run_at": next_run_at}

    record_scheduled_job_outcome_fn(
        bl,
        scheduled_job=scheduled_job,
        run_type=run_type,
        status="enqueued",
        reason="",
        job_id=job_id,
        scheduled_for=scheduled_for_utc,
        next_run_at=next_run_at,
        mark_error=False,
    )
    return {"status": "enqueued", "job_id": job_id, "next_run_at": next_run_at}


def scheduled_jobs_scan(
    bl,
    cfg: ControlConfig,
    *,
    now_utc: datetime | None = None,
    _as_utc_datetime_fn=None,
    _run_scheduled_job_once_fn=None,
    _scheduled_job_next_run_at_fn=None,
    _record_scheduled_job_outcome_fn=None,
) -> dict:
    as_utc_datetime_fn = _as_utc_datetime_fn or _as_utc_datetime
    run_scheduled_job_once_fn = _run_scheduled_job_once_fn or _run_scheduled_job_once
    scheduled_job_next_run_at_fn = _scheduled_job_next_run_at_fn or _scheduled_job_next_run_at
    record_scheduled_job_outcome_fn = (
        _record_scheduled_job_outcome_fn or _record_scheduled_job_outcome
    )

    summary: dict[str, Any] = {
        "checked": 0,
        "enqueued": 0,
        "skipped": 0,
        "failed": 0,
        "details": [],
    }
    if not cfg.scheduled_jobs_enabled:
        summary["disabled"] = True
        return summary
    if not hasattr(bl, "list_due_scheduled_jobs"):
        summary["unavailable"] = True
        return summary

    scan_now_candidate = as_utc_datetime_fn(now_utc, fallback=datetime.now(UTC))
    scan_now_utc = (
        scan_now_candidate if isinstance(scan_now_candidate, datetime) else datetime.now(UTC)
    )
    if hasattr(bl, "reconcile_scheduled_job_execution_locks"):
        try:
            summary["lock_reconcile"] = bl.reconcile_scheduled_job_execution_locks(now=scan_now_utc)
        except Exception as exc:
            logger.debug("Scheduled job lock reconciliation skipped: %s", exc)
            summary["lock_reconcile"] = {"status": "failed", "error": str(exc)}
    due_jobs = (
        bl.list_due_scheduled_jobs(now=scan_now_utc, limit=cfg.scheduled_jobs_batch_limit) or []
    )
    for row in due_jobs:
        scheduled_job = row if isinstance(row, dict) else {}
        summary["checked"] += 1
        scheduled_for_candidate = as_utc_datetime_fn(
            scheduled_job.get("next_run_at"),
            fallback=scan_now_utc,
        )
        scheduled_for = (
            scheduled_for_candidate
            if isinstance(scheduled_for_candidate, datetime)
            else scan_now_utc
        )
        result: dict

        claim_fn = getattr(bl, "claim_due_scheduled_job", None)
        project_schedule_tz = (
            str(scheduled_job.get("schedule_timezone") or "").strip()
            or str(scheduled_job.get("project_schedule_timezone") or "").strip()
            or None
        )
        if callable(claim_fn):
            try:
                try:
                    claim_next_run_at = scheduled_job_next_run_at_fn(
                        str(scheduled_job.get("cron_expr") or ""),
                        scheduled_for=scheduled_for,
                        now_utc=scan_now_utc,
                        schedule_tz=project_schedule_tz,
                    )
                except TypeError:
                    claim_next_run_at = scheduled_job_next_run_at_fn(
                        str(scheduled_job.get("cron_expr") or ""),
                        scheduled_for=scheduled_for,
                        now_utc=scan_now_utc,
                    )
            except Exception as exc:
                reason = f"invalid_cron_expr:{exc}"
                claimed = claim_fn(
                    project_id=str(scheduled_job.get("project_id") or "").strip(),
                    scheduled_job_id=str(scheduled_job.get("id") or "").strip(),
                    expected_next_run_at=scheduled_for,
                    next_run_at=None,
                    now=scan_now_utc,
                )
                if claimed is None:
                    result = {"status": "skipped", "reason": "claim_lost_or_already_processed"}
                elif isinstance(claimed, dict):
                    record_scheduled_job_outcome_fn(
                        bl,
                        scheduled_job=claimed,
                        run_type="scheduled",
                        status="failed",
                        reason=reason,
                        scheduled_for=scheduled_for,
                        next_run_at=None,
                        mark_error=True,
                    )
                    result = {"status": "failed", "reason": reason}
                else:
                    record_scheduled_job_outcome_fn(
                        bl,
                        scheduled_job=scheduled_job,
                        run_type="scheduled",
                        status="failed",
                        reason=reason,
                        scheduled_for=scheduled_for,
                        next_run_at=None,
                        mark_error=True,
                    )
                    result = {"status": "failed", "reason": reason}
            else:
                claimed = claim_fn(
                    project_id=str(scheduled_job.get("project_id") or "").strip(),
                    scheduled_job_id=str(scheduled_job.get("id") or "").strip(),
                    expected_next_run_at=scheduled_for,
                    next_run_at=claim_next_run_at,
                    now=scan_now_utc,
                )
                if claimed is None:
                    result = {"status": "skipped", "reason": "claim_lost_or_already_processed"}
                elif isinstance(claimed, dict):
                    result = run_scheduled_job_once_fn(
                        bl,
                        claimed,
                        cfg,
                        run_type="scheduled",
                        scheduled_for=scheduled_for,
                        advance_schedule=False,
                        now_utc=scan_now_utc,
                    )
                else:
                    result = run_scheduled_job_once_fn(
                        bl,
                        scheduled_job,
                        cfg,
                        run_type="scheduled",
                        scheduled_for=scheduled_for,
                        advance_schedule=True,
                        now_utc=scan_now_utc,
                    )
        else:
            result = run_scheduled_job_once_fn(
                bl,
                scheduled_job,
                cfg,
                run_type="scheduled",
                scheduled_for=scheduled_for,
                advance_schedule=True,
                now_utc=scan_now_utc,
            )

        status = str(result.get("status") or "failed").strip().lower()
        detail = {
            "scheduled_job_id": str(scheduled_job.get("id") or "").strip(),
            "project_id": str(scheduled_job.get("project_id") or "").strip(),
            "workflow_id": str(scheduled_job.get("workflow_id") or "").strip(),
            "status": status,
            "reason": str(result.get("reason") or ""),
            "job_id": str(result.get("job_id") or "").strip() or None,
        }
        summary["details"].append(detail)
        if status == "enqueued":
            summary["enqueued"] += 1
        elif status == "skipped":
            summary["skipped"] += 1
        else:
            summary["failed"] += 1
    return summary
