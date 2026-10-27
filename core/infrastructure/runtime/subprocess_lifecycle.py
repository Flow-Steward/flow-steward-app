"""Shared worker subprocess lifecycle logging and reconciliation helpers."""

from __future__ import annotations

import contextlib
import os
import shlex
import signal
import subprocess
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from core.infrastructure.config.runtime_env import runtime_env as _runtime_env

TERMINAL_WORKER_SUBPROCESS_STATUSES = frozenset({"done", "failed", "timeout", "cancelled"})
RUNNING_WORKER_SUBPROCESS_STATUS = "running"


def terminate_process_group(proc: subprocess.Popen, *, wait_timeout: float = 5.0) -> None:
    """Force-stop an isolated subprocess group, falling back to the root process."""
    group_signalled = False
    pid = int(getattr(proc, "pid", 0) or 0)
    if os.name != "nt" and pid > 0 and hasattr(os, "killpg"):
        try:
            process_group_id = int(os.getpgid(pid))
            current_group_id = int(os.getpgrp())
            if process_group_id == pid and process_group_id != current_group_id:
                os.killpg(process_group_id, signal.SIGKILL)
                group_signalled = True
        except ProcessLookupError:
            group_signalled = True
        except OSError:
            pass
    if not group_signalled:
        with contextlib.suppress(ProcessLookupError, OSError):
            proc.kill()
    with contextlib.suppress(subprocess.TimeoutExpired, ProcessLookupError, OSError):
        proc.wait(timeout=wait_timeout)


def normalize_worker_subprocess_status(status: str) -> str:
    normalized = str(status or "").strip().lower()
    if (
        normalized in TERMINAL_WORKER_SUBPROCESS_STATUSES
        or normalized == RUNNING_WORKER_SUBPROCESS_STATUS
    ):
        return normalized
    if normalized in {"error", "failed_error", "failure"}:
        return "failed"
    if normalized in {"timed_out", "timeout_expired"}:
        return "timeout"
    if normalized in {"canceled", "cancel"}:
        return "cancelled"
    return "failed"


def terminal_status_for_returncode(returncode: int | None, *, timed_out: bool = False) -> str:
    if timed_out:
        return "timeout"
    return "done" if returncode == 0 else "failed"


def command_for_log(cmd: list[str] | tuple[str, ...] | str, *, limit: int = 500) -> str:
    if isinstance(cmd, str):
        return cmd[:limit]
    try:
        return shlex.join([str(part) for part in cmd])[:limit]
    except Exception:
        return " ".join(str(part) for part in cmd)[:limit]


def worker_identity(payload: dict[str, Any] | None) -> tuple[str, str, str]:
    data = payload if isinstance(payload, dict) else {}
    return (
        str(data.get("worker_id") or "").strip(),
        str(data.get("pid") or "").strip(),
        str(data.get("kind") or data.get("source") or "").strip(),
    )


def log_worker_subprocess(
    *,
    bl: Any,
    project_id: str | None,
    job_id: str | None,
    task_id: str,
    task_summary: str,
    agent: str,
    status: str,
    pid: int | None,
    cmd: list[str] | tuple[str, ...] | str,
    cwd: str,
    kind: str,
    result: dict[str, Any] | None = None,
    error: str | None = None,
    extra_payload: dict[str, Any] | None = None,
) -> None:
    if not (bl and job_id and project_id and pid):
        return
    normalized_status = normalize_worker_subprocess_status(status)
    worker_id = (
        str(_runtime_env.get("FS_WORKER_ID") or "").strip()
        or f"{_runtime_env.get('HOSTNAME', '') or os.uname().nodename}-{os.getpid()}"
    )
    payload = {
        "pid": int(pid),
        "kind": str(kind or "subprocess").strip() or "subprocess",
        "cmdline": command_for_log(cmd),
        "cwd": str(cwd or ""),
        "worker_id": worker_id,
    }
    if isinstance(extra_payload, dict):
        payload.update(extra_payload)
    with contextlib.suppress(Exception):
        bl.log_action(
            project_id=project_id,
            task_id=task_id,
            task_summary=task_summary,
            job_id=job_id,
            agent=agent,
            action_type="worker_subprocess",
            status=normalized_status,
            payload=payload,
            result=result or None,
            error=(str(error or "")[:4000] or None),
        )


def _row_timestamp(row: dict[str, Any]) -> datetime | None:
    value = row.get("timestamp")
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    text = str(value or "").strip()
    if not text:
        return None
    with contextlib.suppress(ValueError):
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    return None


def _latest_worker_subprocess_rows(
    rows: list[dict[str, Any]],
) -> dict[tuple[str, str, str], dict[str, Any]]:
    latest: dict[tuple[str, str, str], dict[str, Any]] = {}
    for row in rows:
        if str(row.get("action_type") or "").strip() != "worker_subprocess":
            continue
        payload: dict[str, Any] = row.get("payload") if isinstance(row.get("payload"), dict) else {}
        identity = worker_identity(payload)
        if not identity[1]:
            continue
        latest[identity] = row
    return latest


def _load_worker_subprocess_logs(
    bl: Any, job_id: str, *, limit: int = 1000
) -> list[dict[str, Any]]:
    get_logs = getattr(bl, "get_logs", None)
    if not callable(get_logs):
        return []
    with contextlib.suppress(Exception):
        rows = get_logs(job_id=str(job_id), newest=True, limit=limit)
        if isinstance(rows, list):
            return [dict(row) for row in rows if isinstance(row, dict)]
    return []


def terminalize_running_worker_subprocesses(
    bl: Any,
    *,
    job_id: str,
    status: str,
    reason: str,
    limit: int = 1000,
) -> int:
    terminal_status = normalize_worker_subprocess_status(status)
    if terminal_status not in TERMINAL_WORKER_SUBPROCESS_STATUSES:
        terminal_status = "failed"
    rows = _load_worker_subprocess_logs(bl, job_id, limit=limit)
    job_row = {}
    get_job = getattr(bl, "get_job", None)
    if callable(get_job):
        with contextlib.suppress(Exception):
            maybe_job = get_job(str(job_id))
            if isinstance(maybe_job, dict):
                job_row = maybe_job
    project_id = str(job_row.get("project_id") or "").strip()
    task_id = str(job_row.get("task_id") or "").strip()
    task_summary = str(job_row.get("task_summary") or "").strip()
    count = 0
    for row in _latest_worker_subprocess_rows(rows).values():
        if str(row.get("status") or "").strip().lower() != RUNNING_WORKER_SUBPROCESS_STATUS:
            continue
        payload: dict[str, Any] = row.get("payload") if isinstance(row.get("payload"), dict) else {}
        log_worker_subprocess(
            bl=bl,
            project_id=project_id or str(row.get("project_id") or payload.get("project_id") or ""),
            job_id=str(job_id),
            task_id=task_id or str(row.get("task_id") or ""),
            task_summary=task_summary or str(row.get("task_summary") or ""),
            agent=str(row.get("agent") or "worker"),
            status=terminal_status,
            pid=int(payload.get("pid") or 0),
            cmd=str(payload.get("cmdline") or ""),
            cwd=str(payload.get("cwd") or ""),
            kind=str(payload.get("kind") or payload.get("source") or "subprocess"),
            result={"terminalized_by": "subprocess_lifecycle", "reason": reason},
            error=reason if terminal_status in {"failed", "timeout", "cancelled"} else None,
            extra_payload={"worker_id": str(payload.get("worker_id") or "")},
        )
        count += 1
    return count


def reap_stale_worker_subprocesses(
    bl: Any,
    *,
    job_id: str,
    grace_seconds: int = 60,
    now: datetime | None = None,
    pid_exists: Callable[[int], bool] | None = None,
    stale_worker_ids: set[str] | frozenset[str] | list[str] | tuple[str, ...] | None = None,
    limit: int = 1000,
) -> int:
    current_time = now or datetime.now(UTC)
    if current_time.tzinfo is None:
        current_time = current_time.replace(tzinfo=UTC)

    stale_workers = {str(worker_id or "").strip() for worker_id in (stale_worker_ids or [])}
    stale_workers.discard("")

    rows = _load_worker_subprocess_logs(bl, job_id, limit=limit)
    job_row = {}
    get_job = getattr(bl, "get_job", None)
    if callable(get_job):
        with contextlib.suppress(Exception):
            maybe_job = get_job(str(job_id))
            if isinstance(maybe_job, dict):
                job_row = maybe_job
    project_id = str(job_row.get("project_id") or "").strip()
    task_id = str(job_row.get("task_id") or "").strip()
    task_summary = str(job_row.get("task_summary") or "").strip()
    count = 0
    for row in _latest_worker_subprocess_rows(rows).values():
        if str(row.get("status") or "").strip().lower() != RUNNING_WORKER_SUBPROCESS_STATUS:
            continue
        payload: dict[str, Any] = row.get("payload") if isinstance(row.get("payload"), dict) else {}
        timestamp = _row_timestamp(row)
        if timestamp is None or (current_time - timestamp).total_seconds() < grace_seconds:
            continue
        pid = int(payload.get("pid") or 0)
        worker_id = str(payload.get("worker_id") or "").strip()
        if pid_exists is not None:
            if pid > 0 and bool(pid_exists(pid)):
                continue
            reason = "Stale worker subprocess record reaped: process no longer exists."
        elif worker_id not in stale_workers:
            # Worker subprocess PIDs are often from another Docker namespace.
            # Without caller-supplied PID evidence, only reap when the owning
            # worker lease is known stale/missing.
            continue
        else:
            reason = "Stale worker subprocess record reaped: owning worker is no longer live."
        log_worker_subprocess(
            bl=bl,
            project_id=project_id or str(row.get("project_id") or payload.get("project_id") or ""),
            job_id=str(job_id),
            task_id=task_id or str(row.get("task_id") or ""),
            task_summary=task_summary or str(row.get("task_summary") or ""),
            agent=str(row.get("agent") or "worker"),
            status="failed",
            pid=pid,
            cmd=str(payload.get("cmdline") or ""),
            cwd=str(payload.get("cwd") or ""),
            kind=str(payload.get("kind") or payload.get("source") or "subprocess"),
            result={
                "terminalized_by": "subprocess_reaper",
                "reason": reason,
                "worker_id": worker_id,
            },
            error=reason,
            extra_payload={"worker_id": worker_id},
        )
        count += 1
    return count
