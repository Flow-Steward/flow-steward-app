from __future__ import annotations

import contextlib
import logging
import re
from datetime import UTC, datetime
from typing import Any

from core.application.reports.display_text import build_display_summary
from core.infrastructure.monitoring.process_runtime import (
    build_proc_index,
    current_pid,
    process_exists,
    terminate_pid,
)
from core.infrastructure.monitoring.process_storage import (
    load_active_job_rows,
    load_active_project_lease_rows,
    load_latest_agent_message,
    load_reported_worker_subprocess_rows,
    monitoring_observability_dsn,
    reap_stale_worker_reports,
)

logger = logging.getLogger(__name__)
_LEASE_EVIDENCE_UNAVAILABLE = object()
_LLM_PROCESS_PATTERN = re.compile(
    r"langgraph|invoke_agent|build_registry_graph|anthropic.*messages|from core\.graph",
    re.IGNORECASE,
)


class ProcessNotFoundError(Exception):
    """Requested process is not visible to the monitoring runtime."""


class ProcessNotTerminableError(Exception):
    """Requested process is not an active Flow Steward worker subprocess."""


class ProcessTerminationPermissionError(Exception):
    """Runtime does not have permission to terminate the requested process."""


def _get_observability_dsn() -> str:
    return monitoring_observability_dsn()


def _parse_worker_pid(worker_id: str) -> int | None:
    if not worker_id:
        return None
    m = re.search(r"-(\d+)$", worker_id.strip())
    if not m:
        return None
    try:
        return int(m.group(1))
    except Exception:
        return None


def _worker_pids_from_jobs(active_jobs: list[dict]) -> set[int]:
    pids: set[int] = set()
    for job in active_jobs:
        pid = _parse_worker_pid(job.get("worker_id") or "")
        if pid and process_exists(pid):
            pids.add(pid)
    return pids


def _is_descendant(pid: int, ancestor_pids: set[int], proc_index: dict[int, dict]) -> int | None:
    seen: set[int] = set()
    cur = pid
    while cur and cur not in seen:
        seen.add(cur)
        info = proc_index.get(cur) or {}
        ppid = int(info.get("ppid") or 0)
        if ppid in ancestor_pids:
            return ppid
        cur = ppid
    return None


#: How a Compact worker appears in /proc. The supervisor spawns workers as siblings of the
#: API inside one container, so the API can see them directly. On Scalable they live in their
#: own container and no amount of /proc reading will find them, which is correct rather than a
#: failure: the scan simply returns nothing there.
_WORKER_PROCESS_MARKER = "core.entrypoints.cli.worker_process"


def _scan_worker_processes(proc_index: dict[int, dict]) -> list[dict]:
    """Return the worker processes themselves, not their children.

    AC17-R69: the panel counted `subprocesses`, which `_scan_worker_subprocesses` derives
    from the PIDs of **active jobs** and which is empty whenever nothing is running. So a
    Compact install with two healthy workers reported "Worker processes: 0", and the operator
    had no way to confirm that `FS_WORKER_PROCESSES` -- the number the installer asks them for
    -- had taken effect. The workers were in `/proc` the whole time; nothing looked for them.
    """
    rows: list[dict] = []
    for pid, info in sorted(proc_index.items()):
        cmdline = str(info.get("cmdline") or "")
        if _WORKER_PROCESS_MARKER not in cmdline:
            continue
        rows.append(
            {
                "pid": pid,
                "ppid": info.get("ppid"),
                "kind": "worker",
                "cmdline": cmdline[:500],
                "rss_mb": info.get("rss_mb") or 0.0,
            }
        )
    return rows


def _scan_worker_subprocesses(
    active_jobs: list[dict],
) -> tuple[list[dict], dict[int, dict], set[int]]:
    """Return subprocesses that are actual descendants of active worker PIDs."""
    proc_index = build_proc_index()
    worker_pids = _worker_pids_from_jobs(active_jobs)
    if not worker_pids:
        return [], proc_index, worker_pids

    rows: list[dict] = []
    for pid, info in proc_index.items():
        if pid in worker_pids or pid in (1, current_pid()):
            continue
        parent_worker = _is_descendant(pid, worker_pids, proc_index)
        if not parent_worker:
            continue
        cmd = info.get("cmdline") or ""
        kind = "subprocess"
        if "python" in cmd.lower():
            kind = "python"
        rows.append(
            {
                "pid": pid,
                "ppid": info.get("ppid"),
                "worker_pid": parent_worker,
                "kind": kind,
                "cmdline": cmd[:500],
                "rss_mb": info.get("rss_mb") or 0.0,
            }
        )

    rows.sort(key=lambda r: (int(r.get("worker_pid") or 0), int(r["pid"])))
    return rows, proc_index, worker_pids


def _scan_orphan_processes(
    proc_index: dict[int, dict],
    worker_pids: set[int],
    known_subprocess_pids: set[int],
) -> list[dict]:
    rows: list[dict] = []
    for pid, info in proc_index.items():
        if pid in worker_pids or pid in known_subprocess_pids or pid in (1, current_pid()):
            continue
        cmdline = info.get("cmdline") or ""
        if not _LLM_PROCESS_PATTERN.search(cmdline):
            continue
        rows.append(
            {
                "pid": pid,
                "ppid": info.get("ppid"),
                "cmdline": cmdline[:500],
                "rss_mb": info.get("rss_mb") or 0.0,
            }
        )
    rows.sort(key=lambda r: int(r["pid"]))
    return rows


def _get_active_jobs() -> list[dict]:
    """Fetch jobs with active status from the database."""
    try:
        rows = load_active_job_rows()
        jobs = []
        now = datetime.now(UTC)
        for row in rows:
            job = dict(row)
            if job.get("started_at"):
                job["started_at"] = job["started_at"].isoformat()
            heartbeat = job.get("heartbeat_at")
            if heartbeat:
                job["heartbeat_at"] = heartbeat.isoformat()
                try:
                    job["heartbeat_age_sec"] = max(0, int((now - heartbeat).total_seconds()))
                except Exception:
                    job["heartbeat_age_sec"] = None
            else:
                job["heartbeat_age_sec"] = None
            if not job.get("current_agent"):
                meta = job.get("meta") or {}
                if isinstance(meta, dict):
                    job["current_agent"] = meta.get("current_agent", "")
            if not job.get("current_agent") or not job.get("current_step_id"):
                job_id = str(job.get("id") or "").strip()
                latest_msg = load_latest_agent_message(job_id)
                if isinstance(latest_msg, dict):
                    if not job.get("current_agent"):
                        job["current_agent"] = latest_msg.get("from_agent", "")
                    if not job.get("current_step_id"):
                        job["current_step_id"] = latest_msg.get("step_id") or ""
                        job["current_group_id"] = latest_msg.get("group_id") or ""
            job["display_summary"] = build_display_summary(job.get("task_summary"))
            job.pop("meta", None)
            jobs.append(job)
        return jobs
    except Exception as e:
        logger.warning("_get_active_jobs failed: %s", e)
        return []


def _get_active_project_leases() -> list[dict] | object:
    try:
        rows = load_active_project_lease_rows()
        normalized = []
        now = datetime.now(UTC)
        for row in rows:
            if not isinstance(row, dict):
                continue
            heartbeat = row.get("heartbeat_at")
            expires = row.get("expires_at")
            acquired = row.get("acquired_at")
            heartbeat_age = None
            if heartbeat:
                try:
                    heartbeat_age = max(0, int((now - heartbeat).total_seconds()))
                except Exception:
                    heartbeat_age = None
            normalized.append(
                {
                    "project_id": str(row.get("project_id") or "").strip(),
                    "worker_instance_id": str(row.get("worker_instance_id") or ""),
                    # lease_token is authority-bearing material (accepted as proof of
                    # lease ownership in the external-agent report/heartbeat flow). It
                    # must never be serialized into the operator-readable monitoring
                    # payload; expose only a presence flag for diagnostics.
                    "lease_token_present": bool(str(row.get("lease_token") or "").strip()),
                    "heartbeat_at": heartbeat.isoformat() if heartbeat else None,
                    "expires_at": expires.isoformat() if expires else None,
                    "acquired_at": acquired.isoformat() if acquired else None,
                    "active_job_id": str(row.get("active_job_id") or "").strip() or None,
                    "drain_status": str(row.get("drain_status") or ""),
                    "heartbeat_age_sec": heartbeat_age,
                }
            )
        return normalized
    except Exception as e:
        logger.debug("_get_active_project_leases failed: %s", e)
        return _LEASE_EVIDENCE_UNAVAILABLE


def _build_worker_lease_rows(active_jobs: list[dict], leases: list[dict] | object) -> list[dict]:
    workers: dict[str, dict] = {}
    lease_rows = leases if isinstance(leases, list) else []
    for lease in lease_rows:
        if not isinstance(lease, dict):
            continue
        worker_id = str(lease.get("worker_instance_id") or "").strip()
        if not worker_id:
            continue
        workers[worker_id] = {
            "worker_instance_id": worker_id,
            "active_leased_project": str(lease.get("project_id") or "").strip(),
            "lease_heartbeat_age_sec": lease.get("heartbeat_age_sec"),
            "drain_status": str(lease.get("drain_status") or ""),
            "active_job_id": lease.get("active_job_id"),
        }
    for job in active_jobs:
        worker_id = str(job.get("worker_id") or "").strip()
        if not worker_id:
            continue
        if worker_id not in workers:
            workers[worker_id] = {
                "worker_instance_id": worker_id,
                "active_leased_project": None,
                "lease_heartbeat_age_sec": None,
                "drain_status": "",
                "active_job_id": None,
            }
    return sorted(workers.values(), key=lambda row: str(row.get("worker_instance_id") or ""))


def _reap_stale_reported_worker_subprocesses(
    active_jobs: list[dict], leases: list[dict] | object
) -> int:
    if leases is _LEASE_EVIDENCE_UNAVAILABLE or not isinstance(leases, list):
        return 0
    live_worker_ids = {
        str(lease.get("worker_instance_id") or "").strip()
        for lease in leases or []
        if isinstance(lease, dict) and str(lease.get("worker_instance_id") or "").strip()
    }
    stale_by_job: dict[str, set[str]] = {}
    for job in active_jobs or []:
        if not isinstance(job, dict):
            continue
        job_id = str(job.get("id") or "").strip()
        worker_id = str(job.get("worker_id") or "").strip()
        if job_id and worker_id and worker_id not in live_worker_ids:
            stale_by_job.setdefault(job_id, set()).add(worker_id)
    if not stale_by_job:
        return 0
    try:
        return reap_stale_worker_reports(stale_by_job)
    except Exception as exc:
        logger.debug("_reap_stale_reported_worker_subprocesses failed: %s", exc)
        return 0


def _get_reported_worker_subprocesses(active_jobs: list[dict]) -> list[dict]:
    """Return worker-reported subprocess heartbeats for cross-container topology."""
    job_ids = [
        str(job.get("id") or "").strip()
        for job in active_jobs or []
        if isinstance(job, dict) and str(job.get("id") or "").strip()
    ]
    if not job_ids:
        return []
    try:
        rows = load_reported_worker_subprocess_rows(job_ids)
    except Exception as exc:
        logger.debug("_get_reported_worker_subprocesses failed: %s", exc)
        return []

    normalized: list[dict] = []
    now = datetime.now(UTC)
    for row in rows:
        raw_payload = row.get("payload")
        payload: dict[str, Any] = raw_payload if isinstance(raw_payload, dict) else {}
        try:
            pid = int(payload.get("pid") or 0)
        except Exception:
            pid = 0
        if pid <= 0:
            continue
        timestamp = row.get("timestamp")
        age = None
        if isinstance(timestamp, datetime):
            with contextlib.suppress(Exception):
                age = max(0, int((now - timestamp).total_seconds()))
        normalized.append(
            {
                "pid": pid,
                "ppid": None,
                "worker_pid": None,
                "kind": str(payload.get("kind") or "worker_reported"),
                "cmdline": str(payload.get("cmdline") or "")[:500],
                "rss_mb": 0.0,
                "job_id": str(row.get("job_id") or ""),
                "agent": str(row.get("agent") or ""),
                "cwd": str(payload.get("cwd") or ""),
                "worker_id": str(payload.get("worker_id") or ""),
                "reported": True,
                "heartbeat_age_sec": age,
            }
        )
    return normalized


def list_process_payload() -> dict[str, Any]:
    """Return process monitoring payload for the API surface."""
    jobs = _get_active_jobs()
    leases = _get_active_project_leases()
    reaped_count = _reap_stale_reported_worker_subprocesses(jobs, leases)
    subprocesses, proc_index, worker_pids = _scan_worker_subprocesses(jobs)
    worker_processes = _scan_worker_processes(proc_index)
    app_visible_subprocess_pids = {int(p["pid"]) for p in subprocesses}
    reported_subprocesses = _get_reported_worker_subprocesses(jobs)
    # App-container PIDs and worker-container reported PIDs are not a reliable
    # shared namespace. Keep worker reports even when the numeric PID collides
    # with an app-visible process.
    subprocesses.extend(reported_subprocesses)
    orphans = _scan_orphan_processes(
        proc_index,
        worker_pids,
        app_visible_subprocess_pids,
    )
    return {
        "jobs": jobs,
        "subprocesses": subprocesses,
        "suspected_orphans": orphans,
        "project_leases": leases if isinstance(leases, list) else [],
        "project_lease_evidence_unavailable": leases is _LEASE_EVIDENCE_UNAVAILABLE,
        "workers": _build_worker_lease_rows(jobs, leases),
        # AC17-R69: the worker processes themselves, independent of whether a job is running.
        # Empty on Scalable, where they are in another container and genuinely not visible.
        "worker_processes": worker_processes,
        "reaped_worker_subprocess_count": reaped_count,
    }


def terminate_process(pid: int) -> dict[str, Any]:
    """Gracefully terminate an active Flow Steward subprocess by PID."""
    proc_index = build_proc_index()
    proc = proc_index.get(pid)
    if not proc:
        raise ProcessNotFoundError(f"PID {pid} not found")
    cmdline = proc.get("cmdline") or ""

    jobs = _get_active_jobs()
    worker_pids = _worker_pids_from_jobs(jobs)
    is_worker_child = bool(_is_descendant(pid, worker_pids, proc_index))
    is_known_pattern = bool(_LLM_PROCESS_PATTERN.search(cmdline))

    if not is_worker_child and not is_known_pattern:
        raise ProcessNotTerminableError(
            f"PID {pid} is not an active Flow Steward worker subprocess"
        )

    try:
        signal_name = terminate_pid(pid)
        return {"killed": True, "pid": pid, "signal": signal_name}
    except ProcessLookupError:
        return {"killed": False, "pid": pid, "reason": "Process already exited"}
    except PermissionError as exc:
        raise ProcessTerminationPermissionError(f"Cannot kill PID {pid}") from exc
