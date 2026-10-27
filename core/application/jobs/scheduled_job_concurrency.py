"""Automatic account/project/schedule isolation for scheduled execution locks."""

from __future__ import annotations

from typing import Any

DEFAULT_SCHEDULED_JOB_OVERLAP_REASON = "overlap_active"


def resolve_scheduled_job_concurrency_key(
    *,
    account_id: Any,
    project_id: Any,
    scheduled_job_id: Any,
) -> str:
    """Derive the fixed lock identity for one schedule within its owning scope."""
    account = str(account_id or "").strip()
    project = str(project_id or "").strip()
    scheduled_job = str(scheduled_job_id or "").strip()
    if not account:
        raise ValueError("account_id is required to resolve a scheduled-job lock key")
    if not project:
        raise ValueError("project_id is required to resolve a scheduled-job lock key")
    if not scheduled_job:
        raise ValueError("scheduled_job_id is required to resolve a scheduled-job lock key")
    return f"scheduled_job:{account}:{project}:{scheduled_job}"
