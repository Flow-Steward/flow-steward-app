"""Canonical workflow identity resolution for durable job records."""

from __future__ import annotations

import json
from typing import Any


def _mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError):
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def resolve_job_workflow_id(job: dict[str, Any]) -> str:
    """Return the first canonical workflow identity stored on a job."""
    meta = _mapping(job.get("meta"))
    invocation = _mapping(meta.get("invocation"))
    for value in (
        job.get("selected_workflow"),
        job.get("selected_workflow_key"),
        job.get("selected_workflow_id"),
        meta.get("selected_workflow_id"),
        invocation.get("selected_workflow_id"),
    ):
        normalized = str(value or "").strip()
        if normalized:
            return normalized
    return ""


__all__ = ["resolve_job_workflow_id"]
