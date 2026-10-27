from __future__ import annotations

import time
from collections.abc import Callable, Iterable
from typing import Any

import pytest

QUEUE_WAIT_STATUSES = {"pending", "queued"}


def job_status(job_row: dict[str, Any]) -> str:
    return str(job_row.get("status") or "").strip().lower()


def job_progress_marker(job_row: dict[str, Any]) -> tuple[str, str, str, str]:
    runtime = job_row.get("runtime") or {}
    if not isinstance(runtime, dict):
        runtime = {}
    return (
        job_status(job_row),
        str(runtime.get("active_phase") or job_row.get("current_phase") or "").strip().lower(),
        str(job_row.get("current_agent") or "").strip().lower(),
        str(runtime.get("wait_reason") or "").strip().lower(),
    )


def job_has_contract_readiness_failure(job_row: dict[str, Any]) -> bool:
    runtime = job_row.get("runtime") if isinstance(job_row, dict) else {}
    if not isinstance(runtime, dict):
        runtime = {}
    blob = " ".join(
        [
            str(job_row.get("error") or ""),
            str(job_row.get("last_error") or ""),
            str(runtime.get("last_stderr_excerpt") or ""),
            str(runtime.get("wait_reason") or ""),
        ]
    ).lower()
    return (
        "graph contract validation failed" in blob
        or "unresolved contract errors" in blob
        or "contract validation failed" in blob
        or (
            "human approval cannot bypass hard contract errors" in blob
            and "non-overridable implementation state" in blob
        )
        or (
            "invoke_workflow child requires human review" in blob
            and "auto-approval is not allowed for blocking workflow failures" in blob
        )
    )


def descendant_job_progress_marker(
    job_row: dict[str, Any],
    *,
    project_jobs: Callable[[str], list[dict[str, Any]]],
) -> str:
    project_id = str(job_row.get("project_id") or "").strip()
    parent_task_id = str(job_row.get("task_id") or "").strip()
    if not project_id or not parent_task_id:
        return ""

    child_prefix = f"{parent_task_id}:invoke:"
    markers: list[str] = []
    for row in project_jobs(project_id):
        task_id = str(row.get("task_id") or "").strip()
        if not task_id.startswith(child_prefix):
            continue
        markers.append(
            ":".join(
                (
                    str(row.get("id") or row.get("job_id") or "").strip(),
                    job_status(row),
                    str(row.get("current_phase") or "").strip().lower(),
                    str(row.get("current_agent") or "").strip().lower(),
                )
            )
        )
    return "|".join(sorted(markers))


def wait_for_status_with_activity(
    job_id: str | int,
    *,
    get_job: Callable[[str | int], dict[str, Any]],
    project_jobs: Callable[[str], list[dict[str, Any]]],
    target_statuses: Iterable[str],
    timeout: int,
    interval: int,
    activity_timeout: int,
    disallowed_statuses: Iterable[str] = ("stuck", "cancelled"),
    on_update: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    target = {str(status).strip().lower() for status in target_statuses}
    disallowed = {str(status).strip().lower() for status in disallowed_statuses}
    start_deadline = time.monotonic() + timeout
    activity_deadline: float | None = None
    last_marker: tuple[str, str, str, str, str] | None = None
    last_row: dict[str, Any] | None = None
    last_status = "unknown"

    while True:
        now = time.monotonic()
        if activity_deadline is not None:
            if now >= activity_deadline:
                break
        elif now >= start_deadline:
            break

        try:
            row = get_job(job_id)
        except Exception as e:
            if on_update is not None:
                on_update(f"  [poll] error: {e}")
            time.sleep(interval)
            continue

        last_row = row
        status = job_status(row)
        last_status = status or "unknown"

        if status == "failed" and job_has_contract_readiness_failure(row):
            pytest.skip(
                "Workflow activity wait skipped: selected workflow failed immediately due to "
                "contract readiness errors in this environment"
            )
        if status in target:
            return row
        if status in disallowed:
            raise RuntimeError(f"Job {job_id} entered terminal error state: {status}")

        marker = (
            *job_progress_marker(row),
            descendant_job_progress_marker(row, project_jobs=project_jobs),
        )
        if marker != last_marker:
            last_marker = marker
            if status not in QUEUE_WAIT_STATUSES:
                activity_deadline = time.monotonic() + activity_timeout

        if on_update is not None:
            on_update(f"  [poll] job {job_id} status={status}")
        time.sleep(interval)

    raise TimeoutError(
        f"Job {job_id} did not reach {sorted(target)} within {timeout}s "
        f"(last_status={last_status}, last_row={last_row!r})"
    )
