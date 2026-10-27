"""Classify runtime health and recoverability from jobs and worker leases."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from core.application.jobs.workflow_wait_projection import DURABLE_WAIT_STATUSES
from core.infrastructure.runtime.control.config import ControlConfig


def _as_utc_datetime(value: Any, fallback: datetime | None = None) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    text = str(value or "").strip()
    if text:
        try:
            if text.endswith("Z"):
                text = text[:-1] + "+00:00"
            parsed = datetime.fromisoformat(text)
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
        except Exception:
            pass
    if fallback is not None:
        return fallback if fallback.tzinfo else fallback.replace(tzinfo=UTC)
    return None


def _lease_heartbeat_age_seconds(lease_row: dict | None, now: datetime) -> int | None:
    if not isinstance(lease_row, dict):
        return None
    heartbeat_at = lease_row.get("heartbeat_at")
    if not isinstance(heartbeat_at, datetime):
        return None
    try:
        return max(0, int((now - heartbeat_at).total_seconds()))
    except Exception:
        return None


def _lease_timestamp_age_seconds(lease_row: dict | None, now: datetime, key: str) -> int | None:
    if not isinstance(lease_row, dict):
        return None
    raw_value = lease_row.get(str(key or "").strip())
    if not raw_value:
        return None
    ts = _as_utc_datetime(raw_value, fallback=None)
    if not isinstance(ts, datetime):
        return None
    try:
        return max(0, int((now - ts).total_seconds()))
    except Exception:
        return None


def _lease_is_live(lease_row: dict | None, now: datetime, orphan_grace: int) -> bool:
    if not isinstance(lease_row, dict):
        return False
    expires_at = _as_utc_datetime(lease_row.get("expires_at"), fallback=None)
    if isinstance(expires_at, datetime) and expires_at > now:
        return True
    heartbeat_age = _lease_heartbeat_age_seconds(lease_row, now)
    return bool(heartbeat_age is not None and heartbeat_age < orphan_grace)


def _coerce_project_lease_rows(project_leases: Any) -> list[dict]:
    if isinstance(project_leases, dict):
        return [project_leases]
    if isinstance(project_leases, list):
        return [dict(row) for row in project_leases if isinstance(row, dict)]
    return []


def classify_runtime_health(
    job: dict,
    now: datetime,
    *,
    project_leases: Any,
    project_concurrency_cap: int = 1,
    global_live_active_lease_count: int = 0,
    maintenance_activated_at: datetime | None = None,
    cfg: ControlConfig,
    parse_worker_pid: Callable[[str | None], int | None],
    pid_alive: Callable[[int | None], bool],
) -> dict | None:
    status = str(job.get("status") or "").lower()
    if status in DURABLE_WAIT_STATUSES:
        # A durable wait (approval, timer, child) holds no worker and has no
        # heartbeat to miss: it is never stuck or orphaned.
        return None
    if status not in {"in_progress", "pending", "resume_pending"}:
        return None

    started_at = job.get("started_at")
    heartbeat_at = job.get("heartbeat_at")
    try:
        started_age_sec = max(0, int((now - started_at).total_seconds())) if started_at else None
    except Exception:
        started_age_sec = None
    try:
        heartbeat_age_sec = (
            max(0, int((now - heartbeat_at).total_seconds())) if heartbeat_at else None
        )
    except Exception:
        heartbeat_age_sec = None

    worker_id = str(job.get("worker_id") or "")
    worker_pid = parse_worker_pid(worker_id)
    worker_alive = pid_alive(worker_pid)

    project_id = str(job.get("project_id") or "").strip()
    job_id = str(job.get("id") or "").strip()

    project_concurrency_cap = max(1, int(project_concurrency_cap or 1))
    lease_rows = _coerce_project_lease_rows(project_leases)
    live_project_leases = [
        row for row in lease_rows if _lease_is_live(row, now, cfg.watchdog_orphan_grace_seconds)
    ]
    matching_live_lease = None
    live_active_lease_count = 0
    recent_pickup_lease = False
    for row in live_project_leases:
        row_active_job_id = str(row.get("active_job_id") or "").strip() or None
        if row_active_job_id is not None:
            live_active_lease_count += 1
        acquired_age = _lease_timestamp_age_seconds(row, now, "acquired_at")
        if acquired_age is not None and acquired_age < cfg.watchdog_pickup_grace_seconds:
            recent_pickup_lease = True
        if row_active_job_id == job_id and matching_live_lease is None:
            matching_live_lease = row
    selected_lease = (
        matching_live_lease
        or (live_project_leases[0] if live_project_leases else None)
        or (lease_rows[0] if lease_rows else None)
    )
    lease_heartbeat_age_sec = _lease_heartbeat_age_seconds(selected_lease, now)
    lease_acquired_age_sec = _lease_timestamp_age_seconds(selected_lease, now, "acquired_at")
    lease_is_live_flag = bool(live_project_leases)
    try:
        lease_active_job_id = str((selected_lease or {}).get("active_job_id") or "").strip() or None
    except (TypeError, ValueError):
        lease_active_job_id = None
    pickup_grace_active = bool(
        (started_age_sec is not None and started_age_sec < cfg.watchdog_pickup_grace_seconds)
        or recent_pickup_lease
    )

    classification = ""
    target_status = ""
    reason = ""

    if status == "in_progress":
        if pickup_grace_active:
            return None
        if matching_live_lease:
            return None
        if live_project_leases and any(
            (
                not str(row.get("active_job_id") or "").strip()
                and (acquired_age := _lease_timestamp_age_seconds(row, now, "acquired_at"))
                is not None
                and acquired_age < cfg.watchdog_pickup_grace_seconds
            )
            for row in live_project_leases
        ):
            return None
        if (
            worker_pid
            and not worker_alive
            and (
                heartbeat_age_sec is None or heartbeat_age_sec >= cfg.watchdog_orphan_grace_seconds
            )
        ):
            classification = "orphaned_worker_missing"
            target_status = "orphaned"
            reason = (
                f"Worker PID {worker_pid} from worker_id '{worker_id}' is not alive; "
                f"heartbeat age={heartbeat_age_sec if heartbeat_age_sec is not None else 'none'}s"
            )
        elif heartbeat_age_sec is not None and heartbeat_age_sec >= cfg.watchdog_stuck_seconds:
            classification = "stuck_heartbeat_timeout"
            target_status = "stuck"
            reason = f"No heartbeat update for {heartbeat_age_sec}s (threshold={cfg.watchdog_stuck_seconds}s)"
        elif (
            heartbeat_age_sec is None
            and started_age_sec is not None
            and started_age_sec >= cfg.watchdog_stuck_seconds
        ):
            classification = "stuck_missing_heartbeat"
            target_status = "stuck"
            reason = (
                f"Job has no heartbeat after {started_age_sec}s in_progress "
                f"(threshold={cfg.watchdog_stuck_seconds}s)"
            )
    elif status == "pending":
        queued_at = job.get("queued_at")
        queue_age_sec = None
        if queued_at:
            effective_now = now
            if maintenance_activated_at is not None:
                try:
                    effective_now = min(now, max(queued_at, maintenance_activated_at))
                except (TypeError, ValueError):
                    effective_now = now
            try:
                queue_age_sec = max(0, int((effective_now - queued_at).total_seconds()))
            except Exception:
                queue_age_sec = None
        if queue_age_sec is not None and queue_age_sec >= cfg.watchdog_stuck_seconds:
            if live_project_leases and live_active_lease_count > 0:
                return None
            if recent_pickup_lease:
                return None
            if not live_project_leases and global_live_active_lease_count > 0:
                return None
            classification = "stuck_pending_queue"
            target_status = "stuck"
            reason = (
                f"Job has been pending in queue for {queue_age_sec}s "
                f"(threshold={cfg.watchdog_stuck_seconds}s)"
            )

    if not classification:
        return None

    return {
        "job_id": job_id,
        "project_id": project_id,
        "current_status": status,
        "classification": classification,
        "target_status": target_status,
        "reason": reason,
        "worker_id": worker_id,
        "worker_pid": worker_pid,
        "worker_alive": worker_alive,
        "heartbeat_age_sec": heartbeat_age_sec,
        "started_age_sec": started_age_sec,
        "lease_active_job_id": lease_active_job_id,
        "lease_heartbeat_age_sec": lease_heartbeat_age_sec,
        "lease_acquired_age_sec": lease_acquired_age_sec,
        "lease_is_live": lease_is_live_flag,
        "lease_live_count": len(live_project_leases),
        "global_live_active_lease_count": int(global_live_active_lease_count or 0),
        "project_concurrency_cap": project_concurrency_cap,
        "pickup_grace_seconds": cfg.watchdog_pickup_grace_seconds,
    }


def _classify_watchdog_recovery(
    job: dict,
    now: datetime,
    *,
    project_leases: Any,
    cfg: ControlConfig,
) -> dict | None:
    status = str(job.get("status") or "").strip().lower()
    if status not in {"stuck", "orphaned"}:
        return None
    # A graph may intentionally finish with ``stuck`` when retry policy is
    # exhausted.  Only watchdog-marked, unfinished jobs are eligible for
    # recovery; otherwise a live lease can resurrect a terminal job.
    if _as_utc_datetime(job.get("finished_at"), fallback=None) is not None:
        return None
    try:
        job_id = str(job.get("id") or "").strip()
    except (TypeError, ValueError):
        return None
    if not job_id:
        return None

    lease_rows = _coerce_project_lease_rows(project_leases)
    live_project_leases = [
        row for row in lease_rows if _lease_is_live(row, now, cfg.watchdog_orphan_grace_seconds)
    ]
    matching_live_lease = None
    for row in live_project_leases:
        try:
            row_active_job_id = str(row.get("active_job_id") or "").strip() or None
        except (TypeError, ValueError):
            row_active_job_id = None
        if row_active_job_id == job_id:
            matching_live_lease = row
            break
    selected_lease = (
        matching_live_lease
        or (live_project_leases[0] if live_project_leases else None)
        or (lease_rows[0] if lease_rows else None)
    )
    try:
        lease_active_job_id = str((selected_lease or {}).get("active_job_id") or "").strip() or None
    except (TypeError, ValueError):
        lease_active_job_id = None
    lease_is_live_flag = bool(live_project_leases)
    lease_heartbeat_age_sec = _lease_heartbeat_age_seconds(selected_lease, now)
    heartbeat_age_sec = None
    heartbeat_at = _as_utc_datetime(job.get("heartbeat_at"), fallback=None)
    if isinstance(heartbeat_at, datetime):
        try:
            heartbeat_age_sec = max(0, int((now - heartbeat_at).total_seconds()))
        except Exception:
            heartbeat_age_sec = None

    reasons: list[str] = []
    if matching_live_lease:
        reasons.append("live_project_lease")
    if heartbeat_age_sec is not None and heartbeat_age_sec < cfg.watchdog_pickup_grace_seconds:
        reasons.append("fresh_job_heartbeat")
    if not reasons:
        return None

    return {
        "job_id": job_id,
        "from_status": status,
        "target_status": "in_progress",
        "reasons": reasons,
        "reason": "Recovered runtime signals detected: " + ", ".join(reasons),
        "heartbeat_age_sec": heartbeat_age_sec,
        "lease_active_job_id": lease_active_job_id,
        "lease_heartbeat_age_sec": lease_heartbeat_age_sec,
        "lease_is_live": lease_is_live_flag,
    }
