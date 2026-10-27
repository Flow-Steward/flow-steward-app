"""Standalone control-plane scans and enqueue helpers.

This module is transport-agnostic and can run inside the dedicated
control-plane process without depending on the app transport module.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import socket
import subprocess
from datetime import UTC, datetime
from typing import Any

from core.application.orchestration import interrupted_step_runtime as _interrupted_step_runtime
from core.infrastructure.config.runtime_config import CONFIG
from core.infrastructure.runtime import state_reader_runtime
from core.infrastructure.runtime.control.config import ControlConfig
from core.infrastructure.runtime.control.scans_health import (
    _as_utc_datetime as _as_utc_datetime,
)
from core.infrastructure.runtime.control.scans_health import (
    _classify_watchdog_recovery as _classify_watchdog_recovery,
)
from core.infrastructure.runtime.control.scans_health import (
    _coerce_project_lease_rows as _coerce_project_lease_rows,
)
from core.infrastructure.runtime.control.scans_health import (
    _lease_heartbeat_age_seconds as _lease_heartbeat_age_seconds,
)
from core.infrastructure.runtime.control.scans_health import (
    _lease_is_live as _lease_is_live,
)
from core.infrastructure.runtime.control.scans_health import (
    _lease_timestamp_age_seconds as _lease_timestamp_age_seconds,
)
from core.infrastructure.runtime.control.scans_health import (
    classify_runtime_health,
)
from core.infrastructure.runtime.control.scans_rollups import (
    dashboard_rollup_scan as _dashboard_rollup_scan_impl,
)
from core.infrastructure.runtime.control.scans_rollups import (
    security_posture_scan as _security_posture_scan_impl,
)
from core.infrastructure.runtime.control.scans_scheduled_jobs import (
    _enqueue_scheduled_job as _scheduled_enqueue_scheduled_job,
)
from core.infrastructure.runtime.control.scans_scheduled_jobs import (
    _enqueue_task_standalone as _scheduled_enqueue_task_standalone,
)
from core.infrastructure.runtime.control.scans_scheduled_jobs import (
    _record_scheduled_job_outcome,
    _scheduled_job_next_run_at,
)
from core.infrastructure.runtime.control.scans_scheduled_jobs import (
    _run_scheduled_job_once as _scheduled_run_scheduled_job_once,
)
from core.infrastructure.runtime.control.scans_scheduled_jobs import (
    scheduled_jobs_scan as _scheduled_jobs_scan_impl,
)
from core.infrastructure.runtime.subprocess_lifecycle import reap_stale_worker_subprocesses

logger = logging.getLogger(__name__)

_RETRYABLE_INTERRUPTED_STEP_STATUS = "retryable_interrupted_step"


def get_thread_state_values(thread_id: str) -> dict[str, Any]:
    return state_reader_runtime.get_committed_checkpoint_values(
        thread_id,
        config=CONFIG,
        logger=logger,
    )


def _to_json_obj(value):
    if value is None:
        return None
    if isinstance(value, (dict, list)):
        return value
    if isinstance(value, str):
        try:
            return json.loads(value)
        except Exception:
            return value
    return value


def _parse_worker_pid(worker_id: str | None) -> int | None:
    if not worker_id:
        return None
    match = re.search(r"-(\d+)$", str(worker_id).strip())
    if not match:
        return None
    try:
        return int(match.group(1))
    except Exception:
        return None


def _parse_worker_host(worker_id: str | None) -> str:
    """Host part of a worker id, excluding its incarnation and pid."""
    normalized = str(worker_id or "").strip()
    incarnation_match = re.search(r"^(.*)-[0-9a-f]{12}-\d+$", normalized)
    if incarnation_match:
        return incarnation_match.group(1).strip()
    match = re.search(r"^(.*)-\d+$", normalized)
    return match.group(1).strip() if match else ""


def signalable_worker_pid(worker_id: str | None) -> int | None:
    """Return the pid only when it names a process this host can signal.

    A worker id is ``<hostname>-<incarnation>-<pid>`` (older rows can still use
    ``<hostname>-<pid>``), and the pid is meaningful only inside the process
    namespace that recorded it. A containerised worker is normally pid 1 of its
    own container, so signalling that number from the API
    container hits the API's own init instead — the documented stuck-job
    recovery action would kill the platform it was invoked on.

    Two independent guards, either of which alone prevents that: the recorded
    host must be this host, and pid 1 is never signalled, because in a
    container that is always the caller's own init.
    """
    pid = _parse_worker_pid(worker_id)
    if pid is None or pid <= 1:
        return None
    recorded_host = _parse_worker_host(worker_id)
    if not recorded_host or recorded_host != socket.gethostname().strip():
        return None
    return pid


def _pid_alive(pid: int | None) -> bool:
    if not pid or pid <= 0:
        return False
    proc_root = "/proc"
    if os.path.isdir(proc_root):
        return os.path.exists(f"{proc_root}/{pid}")
    try:
        probe = subprocess.run(
            ["ps", "-p", str(pid), "-o", "pid="],
            capture_output=True,
            check=False,
            text=True,
        )
        return probe.returncode == 0 and bool(str(probe.stdout or "").strip())
    except Exception:
        return False


def _active_project_leases_for_project(bl, project_id: str) -> list[dict]:
    normalized_project_id = str(project_id or "").strip()
    if not bl or not normalized_project_id:
        return []
    lease_lister = getattr(bl, "list_project_execution_leases", None)
    if callable(lease_lister):
        try:
            rows = lease_lister(active_only=True, project_id=normalized_project_id)
            if isinstance(rows, list):
                return [dict(row) for row in rows if isinstance(row, dict)]
        except Exception:
            pass
    lease_getter = getattr(bl, "get_project_execution_lease", None)
    if callable(lease_getter):
        try:
            row = lease_getter(normalized_project_id, active_only=True)
        except Exception:
            row = None
        if isinstance(row, dict):
            return [dict(row)]
    return []


def _project_concurrency_cap(bl, project_id: str) -> int:
    normalized_project_id = str(project_id or "").strip()
    if not bl or not normalized_project_id or not hasattr(bl, "get_project_policy"):
        return 1
    try:
        policy = bl.get_project_policy(normalized_project_id)
    except Exception:
        return 1
    if not isinstance(policy, dict):
        return 1
    try:
        return max(1, int(policy.get("concurrency_cap") or 1))
    except (TypeError, ValueError):
        return 1


def _index_active_project_leases(bl) -> dict[str, list[dict]]:
    index: dict[str, list[dict]] = {}
    if not bl or not hasattr(bl, "list_project_execution_leases"):
        return index
    try:
        rows = bl.list_project_execution_leases(active_only=True)
    except Exception:
        return index
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        project_id = str(row.get("project_id") or "").strip()
        if not project_id:
            continue
        index.setdefault(project_id, []).append(dict(row))
    return index


def _count_live_active_project_leases(
    lease_index: dict[str, list[dict]],
    now: datetime,
    *,
    orphan_grace_seconds: int,
) -> int:
    count = 0
    for rows in (lease_index or {}).values():
        for row in rows or []:
            if not isinstance(row, dict):
                continue
            if not _lease_is_live(row, now, orphan_grace_seconds):
                continue
            active_job_id = str(row.get("active_job_id") or "").strip() or None
            if active_job_id is not None:
                count += 1
    return count


def _live_worker_ids(lease_rows: Any, now: datetime, orphan_grace_seconds: int) -> set[str]:
    return {
        str(row.get("worker_instance_id") or "").strip()
        for row in _coerce_project_lease_rows(lease_rows)
        if _lease_is_live(row, now, orphan_grace_seconds)
        and str(row.get("worker_instance_id") or "").strip()
    }


def _classify_runtime_health(
    job: dict,
    now: datetime,
    *,
    project_leases: Any,
    project_concurrency_cap: int = 1,
    global_live_active_lease_count: int = 0,
    maintenance_activated_at: datetime | None = None,
    cfg: ControlConfig,
) -> dict | None:
    return classify_runtime_health(
        job,
        now,
        project_leases=project_leases,
        project_concurrency_cap=project_concurrency_cap,
        global_live_active_lease_count=global_live_active_lease_count,
        maintenance_activated_at=maintenance_activated_at,
        cfg=cfg,
        parse_worker_pid=_parse_worker_pid,
        pid_alive=_pid_alive,
    )


def _update_job_status_with_reason(
    bl,
    job_id: str,
    *,
    status: str,
    reason: str,
    heartbeat_now: bool = False,
) -> None:
    bl.update_job(job_id, status=status)
    try:
        bl.update_job_runtime(
            job_id,
            heartbeat_now=heartbeat_now,
            last_error=str(reason or "")[:4000],
        )
    except Exception as exc:
        logger.debug("_update_job_status_with_reason runtime update failed: %s", exc)


def _append_runtime_journal(
    bl,
    job_id: str,
    *,
    event: str,
    reason: str,
    detail: dict | None = None,
    max_entries: int = 200,
) -> dict:
    job = bl.get_job(job_id)
    if not job:
        return {}
    meta = job.get("meta") or {}
    if isinstance(meta, str):
        try:
            meta = json.loads(meta)
        except Exception:
            meta = {}
    if not isinstance(meta, dict):
        meta = {}

    journal = meta.get("runtime_journal")
    if not isinstance(journal, list):
        journal = []
    entry = {
        "timestamp": datetime.now(UTC).isoformat(),
        "event": str(event or "runtime_event"),
        "reason": str(reason or "").strip(),
        "detail": detail if isinstance(detail, dict) else {},
    }
    journal.append(entry)
    if len(journal) > max_entries:
        journal = journal[-max_entries:]
    meta["runtime_journal"] = journal
    bl.update_job(job_id, meta=meta)

    with contextlib.suppress(Exception):
        bl.log_event(
            job_id=job_id,
            phase="watchdog",
            agent="watchdog",
            event_type=entry["event"],
            title=entry["reason"] or entry["event"],
            detail=entry.get("detail") or {},
            severity="warn",
        )
    return entry


def _list_watchdog_recovery_candidates(bl, *, limit: int = 500) -> list[dict]:
    getter = getattr(bl, "list_jobs_for_attention", None)
    if callable(getter):
        try:
            rows = getter(limit=max(50, min(int(limit or 500), 2000)))
            if isinstance(rows, list):
                return [dict(row) for row in rows if isinstance(row, dict)]
        except Exception:
            return []
    return []


def _sanitize_retryable_interrupted_step_snapshot(raw: Any) -> dict | None:
    return _interrupted_step_runtime.sanitize_interrupted_step_snapshot(raw)


def _extract_runtime_step_hint(runtime_state: dict) -> dict:
    return _interrupted_step_runtime.extract_runtime_step_hint(runtime_state)


def _detect_retryable_interrupted_step_snapshot(bl, job: dict) -> dict | None:
    return _interrupted_step_runtime.detect_interrupted_step_snapshot(
        bl,
        job,
        get_thread_state_values_fn=get_thread_state_values,
        to_json_obj=_to_json_obj,
        sanitize_snapshot_fn=_sanitize_retryable_interrupted_step_snapshot,
        extract_step_hint_fn=_extract_runtime_step_hint,
    )


def get_committed_pause(thread_id: str) -> Any:
    return state_reader_runtime.get_committed_pause(thread_id, config=CONFIG, logger=logger)


def _restore_pause_projection(bl, job_id: str, job: dict) -> str | None:
    """Re-project a pause the checkpoint holds but the job row never got.

    A worker can die after LangGraph committed the interrupt and before the
    job row was parked. Such a run is waiting, not stuck: the status and
    ``workflow_wait`` are rebuilt from the checkpoint, idempotently.
    """
    from core.application.jobs.workflow_wait_projection import (
        pause_projection,
        workflow_wait_meta,
    )

    thread_id = str((job or {}).get("thread_id") or "").strip()
    if not thread_id:
        return None
    projection = pause_projection(get_committed_pause(thread_id))
    if projection is None:
        return None
    from core.application.jobs.terminal_persistence import TerminalPersistenceConflict
    from core.application.jobs.workflow_pause_persistence import job_pause_transaction

    status, wait = projection
    try:
        with job_pause_transaction(
            bl, job_id=job_id, expected_worker_id=str(job.get("worker_id") or "")
        ):
            if wait is not None:
                bl.merge_job_meta(
                    job_id, {"workflow_wait": workflow_wait_meta(wait, thread_id=thread_id)}
                )
            bl.update_job(job_id, status=status)
    except TerminalPersistenceConflict:
        return None
    return status


def workflow_timer_scan(bl, cfg: ControlConfig | None = None) -> dict:
    """Wake runs whose authored Wait is due (every second, at most 100 per pass)."""
    del cfg
    from core.application.jobs.workflow_timer_dispatcher import dispatch_due_timers
    from core.infrastructure.db.worker import WorkflowResumeRepositoryAdapter
    from core.infrastructure.runtime.worker.queue_publisher import enqueue_workflow_resume

    return dispatch_due_timers(WorkflowResumeRepositoryAdapter(bl), enqueue_workflow_resume)


def workflow_child_scan(bl, cfg: ControlConfig | None = None) -> dict:
    """Wake parents whose child workflow job finished (every second, at most 100)."""
    del cfg
    from core.application.jobs.workflow_timer_dispatcher import dispatch_finished_children
    from core.infrastructure.db.worker import WorkflowResumeRepositoryAdapter
    from core.infrastructure.runtime.worker.queue_publisher import enqueue_workflow_resume

    return dispatch_finished_children(WorkflowResumeRepositoryAdapter(bl), enqueue_workflow_resume)


def invoke_child_cancel_scan(bl, cfg: ControlConfig | None = None) -> dict:
    """Apply recorded cancel requests to children of cancelled parents."""
    del cfg
    from core.application.jobs.invoke_child_cancellation import apply_child_cancels

    return apply_child_cancels(bl)


def dispatch_outbox_scan(bl, cfg: ControlConfig | None = None) -> dict:
    """Relay due durable job dispatches independently of optional watchdog policy."""
    from core.application.jobs.artifact_cleanup_recovery import (
        reconcile_job_artifact_purge_markers,
        recover_expired_preparing_submissions,
        resolve_artifact_storage_adapter_from_env,
    )
    from core.application.jobs.task_enqueue_runtime import relay_pending_dispatch_outbox
    from core.infrastructure.runtime.worker.queue_publisher import enqueue_task

    storage = resolve_artifact_storage_adapter_from_env()
    preparing_recovery = recover_expired_preparing_submissions(
        bl,
        storage=storage,
    )
    result = relay_pending_dispatch_outbox(
        bl=bl,
        enqueue_task_fn=enqueue_task,
        logger=logger,
    )
    try:
        artifact_cleanup_reconciliation = reconcile_job_artifact_purge_markers(
            bl,
            storage=storage,
            budget=getattr(cfg, "artifact_cleanup_reconciliation_budget", None),
        )
    except Exception as exc:
        logger.warning("artifact cleanup reconciliation failed: %s", exc)
        artifact_cleanup_reconciliation = {
            "scanned": 0,
            "reconciled": 0,
            "preserved": 0,
            "failed": 1,
        }
    return {
        **result,
        "preparing_recovery": preparing_recovery,
        "artifact_cleanup_reconciliation": artifact_cleanup_reconciliation,
    }


def watchdog_scan(
    bl,
    cfg: ControlConfig,
    *,
    maintenance_activated_at: datetime | None = None,
) -> dict:
    """Scan active jobs for stuck/orphaned conditions, optionally apply marks."""
    now = datetime.now(UTC)
    try:
        candidates = [dict(r) for r in (bl.get_active_jobs() or [])]
    except Exception:
        candidates = []
    candidate_by_id = {
        str(item.get("id") or "").strip(): item
        for item in candidates
        if isinstance(item, dict) and str(item.get("id") or "").strip()
    }
    lease_by_project = _index_active_project_leases(bl)
    global_live_active_lease_count = _count_live_active_project_leases(
        lease_by_project,
        now,
        orphan_grace_seconds=cfg.watchdog_orphan_grace_seconds,
    )
    concurrency_cap_by_project: dict[str, int] = {}

    def _concurrency_cap(project_id: str) -> int:
        normalized_project_id = str(project_id or "").strip()
        if not normalized_project_id:
            return 1
        cached = concurrency_cap_by_project.get(normalized_project_id)
        if cached is not None:
            return cached
        resolved = _project_concurrency_cap(bl, normalized_project_id)
        concurrency_cap_by_project[normalized_project_id] = resolved
        return resolved

    detections: list[dict] = []
    applied: list[dict] = []
    recoveries: list[dict] = []
    recoveries_applied: list[dict] = []
    reaped_worker_subprocesses = 0

    for job in candidates:
        job_id = str(job.get("id") or "").strip()
        project_id = str(job.get("project_id") or "").strip()
        project_leases = lease_by_project.get(project_id)
        worker_id = str(job.get("worker_id") or "").strip()
        live_worker_ids = _live_worker_ids(
            project_leases,
            now,
            cfg.watchdog_orphan_grace_seconds,
        )
        stale_worker_ids = {worker_id} if worker_id and worker_id not in live_worker_ids else set()
        if job_id:
            with contextlib.suppress(Exception):
                reaped_worker_subprocesses += reap_stale_worker_subprocesses(
                    bl,
                    job_id=job_id,
                    grace_seconds=cfg.watchdog_orphan_grace_seconds,
                    now=now,
                    stale_worker_ids=stale_worker_ids,
                )
        row = _classify_runtime_health(
            job,
            now,
            project_leases=project_leases,
            project_concurrency_cap=_concurrency_cap(project_id),
            global_live_active_lease_count=global_live_active_lease_count,
            maintenance_activated_at=maintenance_activated_at,
            cfg=cfg,
        )
        if not row:
            continue
        detections.append(row)
        if not cfg.watchdog_auto_apply:
            continue
        job_id = str(row["job_id"])
        try:
            latest = bl.get_job(job_id) if hasattr(bl, "get_job") else None
            merged = None
            if isinstance(latest, dict):
                merged = dict(candidate_by_id.get(job_id) or {})
                merged.update(latest)
                latest_project_id = str(merged.get("project_id") or "").strip()
                latest_lease = None
                if latest_project_id:
                    latest_lease = _active_project_leases_for_project(bl, latest_project_id)
                    if not latest_lease:
                        latest_lease = lease_by_project.get(latest_project_id)
                refreshed = _classify_runtime_health(
                    merged,
                    datetime.now(UTC),
                    project_leases=latest_lease,
                    project_concurrency_cap=_concurrency_cap(latest_project_id),
                    global_live_active_lease_count=global_live_active_lease_count,
                    maintenance_activated_at=maintenance_activated_at,
                    cfg=cfg,
                )
                if not refreshed:
                    continue
                row = refreshed

            target_status = str(row.get("target_status") or "").strip().lower()
            if (
                target_status in {"stuck", "orphaned"}
                and str(row.get("current_status") or "").strip().lower() == "in_progress"
            ):
                source_job = merged if isinstance(merged, dict) else (latest or job)
                restored = _restore_pause_projection(bl, job_id, source_job)
                if restored is not None:
                    journal = _append_runtime_journal(
                        bl,
                        job_id,
                        event="watchdog_pause_projection_restored",
                        reason=f"{restored}: checkpoint is paused",
                        max_entries=cfg.watchdog_max_journal_entries,
                        detail={"from_status": row["current_status"], "status": restored},
                    )
                    applied.append({"job_id": job_id, "status": restored, "journal": journal})
                    continue
            interrupted_snapshot = None
            if (
                target_status in {"stuck", "orphaned"}
                and str(row.get("current_status") or "").strip().lower() == "in_progress"
            ):
                if isinstance(merged, dict):
                    source_job = merged
                elif isinstance(latest, dict):
                    source_job = latest
                else:
                    source_job = job
                interrupted_snapshot = _detect_retryable_interrupted_step_snapshot(
                    bl,
                    source_job,
                )
                if interrupted_snapshot:
                    target_status = _RETRYABLE_INTERRUPTED_STEP_STATUS
                    row["target_status"] = target_status
                    row["classification"] = f"{row.get('classification')}_interrupted_step"
                    row["reason"] = (
                        "Interrupted in-flight step detected after worker interruption. "
                        "Manual Retry Step is required."
                    )
                    latest_meta = _to_json_obj((latest or {}).get("meta"))
                    if not isinstance(latest_meta, dict):
                        latest_meta = {}
                    latest_meta["retryable_interrupted_step"] = interrupted_snapshot
                    bl.update_job(job_id, meta=latest_meta)

            _update_job_status_with_reason(
                bl,
                job_id,
                status=target_status,
                reason=str(row.get("reason") or "")[:2000],
                heartbeat_now=False,
            )
            journal = _append_runtime_journal(
                bl,
                job_id,
                event="watchdog_mark",
                reason=f"{target_status}: {row['classification']}",
                max_entries=cfg.watchdog_max_journal_entries,
                detail={
                    "from_status": row["current_status"],
                    "worker_id": row.get("worker_id"),
                    "worker_pid": row.get("worker_pid"),
                    "worker_alive": row.get("worker_alive"),
                    "heartbeat_age_sec": row.get("heartbeat_age_sec"),
                    "started_age_sec": row.get("started_age_sec"),
                    "lease_active_job_id": row.get("lease_active_job_id"),
                    "lease_heartbeat_age_sec": row.get("lease_heartbeat_age_sec"),
                    "reason": row.get("reason"),
                    "retryable_interrupted_step": interrupted_snapshot or {},
                },
            )
            applied.append({"job_id": job_id, "status": target_status, "journal": journal})
        except Exception as apply_err:
            row["apply_error"] = str(apply_err)

    for job in _list_watchdog_recovery_candidates(bl):
        if not isinstance(job, dict):
            continue
        project_id = str(job.get("project_id") or "").strip()
        recovery = _classify_watchdog_recovery(
            job,
            now,
            project_leases=lease_by_project.get(project_id),
            cfg=cfg,
        )
        if not recovery:
            continue
        recoveries.append(recovery)
        if not cfg.watchdog_auto_apply:
            continue
        try:
            job_id = str(recovery.get("job_id") or "").strip()
            if not job_id:
                continue
            latest = bl.get_job(job_id) if hasattr(bl, "get_job") else None
            if not isinstance(latest, dict):
                continue
            merged = dict(job)
            merged.update(latest)
            latest_project_id = str(merged.get("project_id") or project_id or "").strip()
            latest_lease = None
            if latest_project_id:
                latest_lease = _active_project_leases_for_project(bl, latest_project_id)
                if not latest_lease:
                    latest_lease = lease_by_project.get(latest_project_id)
            refreshed = _classify_watchdog_recovery(
                merged,
                datetime.now(UTC),
                project_leases=latest_lease,
                cfg=cfg,
            )
            if not refreshed:
                continue
            recovery = refreshed
            _update_job_status_with_reason(
                bl,
                job_id,
                status="in_progress",
                reason=str(recovery.get("reason") or "Recovered by watchdog")[:2000],
                heartbeat_now=True,
            )
            with contextlib.suppress(Exception):
                bl.update_job_runtime(job_id, heartbeat_now=True, clear_last_error=True)
            journal = _append_runtime_journal(
                bl,
                job_id,
                event="watchdog_recover",
                reason="in_progress: false-positive watchdog mark cleared",
                max_entries=cfg.watchdog_max_journal_entries,
                detail={
                    "from_status": recovery.get("from_status"),
                    "reasons": recovery.get("reasons") or [],
                    "heartbeat_age_sec": recovery.get("heartbeat_age_sec"),
                    "lease_active_job_id": recovery.get("lease_active_job_id"),
                    "lease_heartbeat_age_sec": recovery.get("lease_heartbeat_age_sec"),
                    "lease_is_live": recovery.get("lease_is_live"),
                },
            )
            recoveries_applied.append(
                {"job_id": job_id, "status": "in_progress", "journal": journal}
            )
        except Exception as recovery_err:
            recovery["apply_error"] = str(recovery_err)

    return {
        "scanned_at": now.isoformat(),
        "thresholds": {
            "stuck_seconds": cfg.watchdog_stuck_seconds,
            "orphan_grace_seconds": cfg.watchdog_orphan_grace_seconds,
            "pickup_grace_seconds": cfg.watchdog_pickup_grace_seconds,
        },
        "active_count": len(candidates),
        "detection_count": len(detections),
        "detections": detections,
        "applied_count": len(applied),
        "applied": applied,
        "recovery_count": len(recoveries),
        "recoveries": recoveries,
        "recovery_applied_count": len(recoveries_applied),
        "recoveries_applied": recoveries_applied,
        "reaped_worker_subprocess_count": reaped_worker_subprocesses,
    }


def _enqueue_task_standalone(
    bl,
    *,
    task_id: str,
    task_summary: str,
    task_source: str,
    project_id: str,
    project_name: str,
    project_config: dict,
    workflow_id: str,
    mode: str | None = None,
    test_mode: bool | None = None,
    extra_state: dict | None = None,
    meta: dict | None = None,
    pre_publish_fn=None,
) -> str | int:
    return _scheduled_enqueue_task_standalone(
        bl,
        task_id=task_id,
        task_summary=task_summary,
        task_source=task_source,
        project_id=project_id,
        project_name=project_name,
        project_config=project_config,
        _workflow_id=workflow_id,
        mode=mode,
        test_mode=test_mode,
        extra_state=extra_state,
        meta=meta,
        pre_publish_fn=pre_publish_fn,
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
    return _scheduled_enqueue_scheduled_job(
        bl,
        task_id=task_id,
        task_summary=task_summary,
        task_source=task_source,
        project_id=project_id,
        project_name=project_name,
        project_config=project_config,
        workflow_id=workflow_id,
        scheduled_job_id=scheduled_job_id,
        account_id=account_id,
        lock_key=lock_key,
        run_type=run_type,
        scheduled_for=scheduled_for,
        mode=mode,
        test_mode=test_mode,
        extra_state=extra_state,
        meta=meta,
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
) -> dict:
    return _scheduled_run_scheduled_job_once(
        bl,
        scheduled_job,
        cfg,
        run_type=run_type,
        scheduled_for=scheduled_for,
        advance_schedule=advance_schedule,
        now_utc=now_utc,
        _as_utc_datetime_fn=_as_utc_datetime,
        _enqueue_scheduled_job_fn=_enqueue_scheduled_job,
        _record_scheduled_job_outcome_fn=_record_scheduled_job_outcome,
        _scheduled_job_next_run_at_fn=_scheduled_job_next_run_at,
    )


def scheduled_jobs_scan(bl, cfg: ControlConfig) -> dict:
    return _scheduled_jobs_scan_impl(
        bl,
        cfg,
        _as_utc_datetime_fn=_as_utc_datetime,
        _run_scheduled_job_once_fn=_run_scheduled_job_once,
        _scheduled_job_next_run_at_fn=_scheduled_job_next_run_at,
        _record_scheduled_job_outcome_fn=_record_scheduled_job_outcome,
    )


def dashboard_rollup_scan(bl, cfg: ControlConfig) -> dict:
    return _dashboard_rollup_scan_impl(bl, cfg)


def external_effect_maintenance_scan(
    bl,
    cfg: ControlConfig,
    *,
    artifact_plane: Any,
) -> dict[str, int]:
    """Expire stale leases and reconcile one globally bounded output-marker batch."""
    del cfg
    from core.application.extensions import external_effects
    from core.infrastructure.db import external_effect_store

    account_ids = external_effect_store.list_external_effect_maintenance_accounts(bl, limit=100)
    resolved = 0
    failed_accounts = 0
    for account_id in account_ids:
        try:
            external_effects.cleanup_stale_external_effects(bl, account_id=account_id)
            resolved += external_effects.reconcile_external_effect_outputs(
                bl,
                account_id=account_id,
                artifact_plane=artifact_plane,
                limit=100,
            )
        except Exception:
            failed_accounts += 1
            logger.exception(
                "external-effect maintenance failed for account_id=%s",
                account_id,
            )
    return {
        "scanned_accounts": len(account_ids),
        "failed_accounts": failed_accounts,
        "resolved": resolved,
    }


def workflow_knowledge_sync_scan(
    changelog_store,
    event_store,
    cfg: ControlConfig,
) -> dict[str, Any]:
    """Enqueue a bounded workflow-knowledge refresh for stateless workers.

    AC17-R149: this used to stamp last_attempt_at before enqueuing. The iteration is
    the worker's -- it stamps the attempt with its claim when it takes the request and
    the success when it finishes -- so the stamp here only ever described a request,
    and read as an unfinished iteration: until a worker claimed it; after an in-flight
    worker's claim marker was overwritten by it; and, for a whole sync interval, after
    a control-plane restart inside the cadence bucket of the last success, because that
    request's idempotency key already existed and the outbox dropped it. `config apply`
    recreates the control plane, so readiness answered 503 and the change rolled back.
    A request that cannot be enqueued is still recorded, as a failure.
    """

    required_ports = (
        "record_workflow_knowledge_sync_loop_failure",
        "list_recent_registry_changelog",
    )
    missing = [
        name for name in required_ports if not callable(getattr(changelog_store, name, None))
    ]
    if missing:
        raise RuntimeError(
            "required workflow knowledge sync status repository port is unavailable: "
            + ", ".join(missing)
        )
    from core.application.jobs.workflow_knowledge_sync_requests import (
        enqueue_workflow_knowledge_sync_batch,
    )

    try:
        return enqueue_workflow_knowledge_sync_batch(
            changelog_store=changelog_store,
            event_store=event_store,
            changelog_limit=cfg.workflow_knowledge_sync_changelog_limit,
            cadence_seconds=cfg.workflow_knowledge_sync_interval,
        )
    except Exception as exc:
        reason = str(exc or "workflow knowledge sync iteration failed")[:512]
        try:
            changelog_store.record_workflow_knowledge_sync_loop_failure(reason)
        except Exception:
            logger.exception("workflow_knowledge_sync_scan: failed to persist iteration failure")
        raise


# ---------------------------------------------------------------------------
# Security posture scan
# ---------------------------------------------------------------------------
def security_posture_scan(bl, cfg: ControlConfig) -> dict:
    return _security_posture_scan_impl(bl, cfg)
