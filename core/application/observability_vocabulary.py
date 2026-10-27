from __future__ import annotations

import re
from typing import Any

_WAITING_FOR_APPROVAL_LABEL = "Waiting for approval"

_SPECIAL_WORDS = {
    "api": "API",
    "cli": "CLI",
    "fs": "FS",
    "id": "ID",
    "json": "JSON",
    "mcp": "MCP",
    "slo": "SLO",
    "ui": "UI",
    "url": "URL",
}

_JOB_STATUS_ALIASES = {
    "canceled": "cancelled",
    "max-iterations": "max_iterations",
    "running": "in_progress",
    "triage-needed": "triage_needed",
    "waiting_external": "external_wait",
}

_JOB_STATUS_META: dict[str, dict[str, Any]] = {
    "pending": {
        "label": "Queued",
        "tone": "blue",
        "headline": "Flow Steward is waiting for a worker to pick up this job.",
        "dashboard_group": "running",
        "outcome": "queued",
        "terminal": False,
    },
    "queued": {
        "label": "Queued",
        "tone": "blue",
        "headline": "Flow Steward queued this job for a worker.",
        "dashboard_group": "running",
        "outcome": "queued",
        "terminal": False,
    },
    "in_progress": {
        "label": "In progress",
        "tone": "blue",
        "headline": "Flow Steward is actively working through this job.",
        "dashboard_group": "running",
        "outcome": "running",
        "terminal": False,
    },
    "awaiting_approval": {
        "label": _WAITING_FOR_APPROVAL_LABEL,
        "tone": "amber",
        "headline": "Flow Steward paused and needs a human decision before it can continue.",
        "dashboard_group": "needs_attention",
        "outcome": "awaiting_approval",
        "terminal": False,
    },
    "awaiting_timer": {
        "label": "Waiting for timer",
        "tone": "blue",
        "headline": "Flow Steward paused on a Wait step and resumes the run when it is due.",
        "dashboard_group": "running",
        "outcome": "waiting",
        "terminal": False,
    },
    "awaiting_child": {
        "label": "Waiting for child workflow",
        "tone": "blue",
        "headline": "Flow Steward paused until the child workflow it started finishes.",
        "dashboard_group": "running",
        "outcome": "waiting",
        "terminal": False,
    },
    "resume_pending": {
        "label": "Resume queued",
        "tone": "blue",
        "headline": "Flow Steward recorded the decision and is waiting for a worker to resume the job.",
        "dashboard_group": "running",
        "outcome": "queued",
        "terminal": False,
    },
    "blocked": {
        "label": "Blocked",
        "tone": "amber",
        "headline": "Flow Steward cannot continue until an external blocker is cleared.",
        "dashboard_group": "blocked",
        "outcome": "blocked",
        "terminal": False,
    },
    "external_wait": {
        "label": "Waiting on an external system",
        "tone": "amber",
        "headline": "Flow Steward is paused while it waits on an external dependency.",
        "dashboard_group": "blocked",
        "outcome": "blocked",
        "terminal": False,
    },
    "retryable_interrupted_step": {
        "label": "Stopped on a retryable step",
        "tone": "red",
        "headline": "Flow Steward stopped during one step and can retry from that point.",
        "dashboard_group": "blocked",
        "outcome": "failed",
        "terminal": True,
    },
    "completed": {
        "label": "Completed successfully",
        "label_key": "react.jobs.completed_successfully",
        "tone": "green",
        "headline": "Flow Steward finished this run successfully.",
        "dashboard_group": "done",
        "outcome": "completed",
        "terminal": True,
    },
    "failed": {
        "label": "Failed",
        "tone": "red",
        "headline": "Flow Steward stopped before the job could finish.",
        "dashboard_group": "needs_attention",
        "outcome": "failed",
        "terminal": True,
    },
    "triage_needed": {
        "label": "Needs triage",
        "tone": "red",
        "headline": "Flow Steward stopped and now needs operator triage before another attempt.",
        "dashboard_group": "needs_attention",
        "outcome": "failed",
        "terminal": True,
    },
    "cancelled": {
        "label": "Cancelled",
        "tone": "gray",
        "headline": "This run was cancelled before completion.",
        "dashboard_group": "done",
        "outcome": "cancelled",
        "terminal": True,
    },
    "max_iterations": {
        "label": "Stopped after repeated retries",
        "tone": "red",
        "headline": "Flow Steward stopped after exhausting the allowed retry budget.",
        "dashboard_group": "needs_attention",
        "outcome": "failed",
        "terminal": True,
    },
}

JOB_SUCCESS_STATUSES = frozenset(
    status for status, meta in _JOB_STATUS_META.items() if meta.get("outcome") == "completed"
)
JOB_FAILURE_STATUSES = frozenset(
    status for status, meta in _JOB_STATUS_META.items() if meta.get("outcome") == "failed"
)
JOB_TERMINAL_STATUSES = frozenset(
    status for status, meta in _JOB_STATUS_META.items() if bool(meta.get("terminal"))
)

#: The eight status groups operators filter and read by, in display order:
#: (key, label, stored statuses). A stored status outside every group is
#: unknown or legacy and has no group. The frontend keeps the same table in
#: assets/frontend/src/pages/jobs/status-groups.ts; a unit test pins the two.
JOB_STATUS_GROUPS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("in_progress", "In progress", ("in_progress", "pending", "resume_pending")),
    ("waiting", "Waiting", ("awaiting_timer", "awaiting_child", "external_wait")),
    ("awaiting_approval", _WAITING_FOR_APPROVAL_LABEL, ("awaiting_approval",)),
    ("stuck", "Stuck", ("stuck", "orphaned")),
    ("interrupted", "Interrupted", ("retryable_interrupted_step", "blocked")),
    ("failed", "Failed", ("failed", "max_iterations", "triage_needed")),
    ("completed", "Completed", ("completed",)),
    ("cancelled", "Cancelled", ("cancelled",)),
)

_JOB_STATUS_GROUP_BY_STATUS = {
    status: key for key, _label, statuses in JOB_STATUS_GROUPS for status in statuses
}


def job_status_group(value: Any) -> str:
    """Return the status group key of a stored job status, or "" when it has none."""
    return _JOB_STATUS_GROUP_BY_STATUS.get(normalize_job_status(value), "")


def job_status_group_statuses(group: str) -> tuple[str, ...]:
    """Return the stored statuses of one status group; an unknown key has none."""
    for key, _label, statuses in JOB_STATUS_GROUPS:
        if key == group:
            return statuses
    return ()


_EVENT_STATUS_ALIASES = {
    "dead-lettered": "dead_lettered",
    "partial_failure": "partially_failed",
    "partially-failed": "partially_failed",
}

_EVENT_STATUS_META: dict[str, dict[str, Any]] = {
    "recorded": {
        "label": "Recorded",
        "tone": "blue",
        "retryable": False,
    },
    "matched": {
        "label": "Matched",
        "tone": "blue",
        "retryable": False,
    },
    "queued": {
        "label": "Queued",
        "tone": "blue",
        "retryable": False,
    },
    "dispatching": {
        "label": "Dispatching",
        "tone": "blue",
        "retryable": False,
    },
    "succeeded": {
        "label": "Delivered",
        "tone": "green",
        "retryable": False,
    },
    "skipped": {
        "label": "Skipped",
        "tone": "gray",
        "retryable": False,
    },
    "partially_failed": {
        "label": "Partially failed",
        "tone": "amber",
        "retryable": True,
    },
    "failed": {
        "label": "Failed",
        "tone": "red",
        "retryable": True,
    },
    "dead_lettered": {
        "label": "Dead-lettered",
        "tone": "red",
        "retryable": True,
    },
}

_QUEUE_DELIVERY_STATUS_ALIASES = {
    "claimed": "delivery_succeeded",
    "dead-lettered": "dead_lettered",
}

_QUEUE_DELIVERY_STATUS_META: dict[str, dict[str, Any]] = {
    "queued": {
        "label": "Queued",
        "tone": "blue",
    },
    "retrying_delivery": {
        "label": "Retrying delivery",
        "tone": "amber",
    },
    "delivery_succeeded": {
        "label": "Delivered to worker",
        "tone": "blue",
    },
    "dead_lettered": {
        "label": "Dead-lettered",
        "tone": "red",
    },
    "discarded": {
        "label": "Discarded",
        "tone": "gray",
    },
}

_APPROVAL_STATE_META: dict[str, dict[str, str]] = {
    "awaiting_approval": {"label": _WAITING_FOR_APPROVAL_LABEL, "tone": "amber"},
    "review_available": {"label": "Review available", "tone": "blue"},
    "approval_recorded": {"label": "Approval recorded", "tone": "green"},
    "none": {"label": "No approval gate", "tone": "gray"},
}


def humanize_observability_token(value: Any, *, strip_prefixes: tuple[str, ...] = ()) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    for prefix in strip_prefixes:
        normalized_prefix = str(prefix or "").strip().lower()
        if normalized_prefix and text.lower().startswith(normalized_prefix):
            text = text[len(prefix) :]
            break
    words = [part for part in re.split(r"[\s_.:-]+", text.strip()) if part]
    return " ".join(_SPECIAL_WORDS.get(part.lower(), part.capitalize()) for part in words)


def normalize_job_status(value: Any) -> str:
    normalized = str(value or "").strip().lower()
    return _JOB_STATUS_ALIASES.get(normalized, normalized)


def job_status_meta(value: Any) -> dict[str, Any]:
    code = normalize_job_status(value)
    meta = _JOB_STATUS_META.get(code)
    if meta:
        return {"code": code, **meta}
    return {
        "code": code,
        "label": humanize_observability_token(code) or "Unknown status",
        "tone": "gray",
        "headline": "Flow Steward recorded this run with an uncommon status.",
        "dashboard_group": "needs_attention",
        "outcome": "unknown",
        "terminal": False,
    }


def job_attention_group(
    status: Any,
    acknowledged_at: Any = None,
    *,
    retryable_interrupted_step_status: str = "retryable_interrupted_step",
) -> str:
    normalized = normalize_job_status(status)
    retryable_code = normalize_job_status(retryable_interrupted_step_status)
    if normalized in JOB_FAILURE_STATUSES:
        return "acknowledged" if acknowledged_at else "needs_attention"
    if normalized == "awaiting_approval":
        return "needs_attention"
    if normalized in {"blocked", "external_wait", retryable_code}:
        return "blocked"
    if normalized in {
        "pending",
        "in_progress",
        "resume_pending",
        "awaiting_timer",
        "awaiting_child",
    }:
        # A run paused on a timer or a child workflow needs no one: it resumes
        # by itself, so it stays with the running work.
        return "running"
    if normalized in {"completed", "cancelled"}:
        return "done"
    fallback_group = str(job_status_meta(normalized).get("dashboard_group") or "needs_attention")
    return "acknowledged" if acknowledged_at else fallback_group


def dashboard_result_from_status(status: Any) -> str:
    normalized = normalize_job_status(status)
    if normalized in JOB_SUCCESS_STATUSES:
        return "completed"
    if normalized in JOB_FAILURE_STATUSES:
        return "failed"
    return ""


def normalize_event_status(value: Any) -> str:
    normalized = str(value or "").strip().lower()
    return _EVENT_STATUS_ALIASES.get(normalized, normalized)


def event_status_meta(value: Any) -> dict[str, Any]:
    code = normalize_event_status(value)
    meta = _EVENT_STATUS_META.get(code)
    if meta:
        return {"code": code, **meta}
    return {
        "code": code,
        "label": humanize_observability_token(code) or "Unknown status",
        "tone": "gray",
        "retryable": False,
    }


def normalize_queue_delivery_status(value: Any) -> str:
    normalized = str(value or "").strip().lower()
    return _QUEUE_DELIVERY_STATUS_ALIASES.get(normalized, normalized)


def queue_delivery_status_meta(value: Any) -> dict[str, Any]:
    code = normalize_queue_delivery_status(value)
    meta = _QUEUE_DELIVERY_STATUS_META.get(code)
    if meta:
        return {"code": code, **meta}
    return {
        "code": code,
        "label": humanize_observability_token(code) or "Unknown delivery state",
        "tone": "gray",
    }


def event_type_meta(value: Any) -> dict[str, str]:
    event_type = str(value or "").strip().lower()
    if not event_type:
        return {
            "event_family": "",
            "event_family_label": "",
            "event_category": "",
            "event_category_label": "",
            "event_type_label": "",
        }
    parts = [segment for segment in event_type.split(".") if segment]
    family = parts[0] if parts else ""
    category = parts[1] if len(parts) > 1 else ""
    return {
        "event_family": family,
        "event_family_label": humanize_observability_token(family),
        "event_category": category,
        "event_category_label": humanize_observability_token(category),
        "event_type_label": humanize_observability_token(event_type),
    }


def approval_state_meta(value: Any) -> dict[str, str]:
    code = str(value or "").strip().lower() or "none"
    meta = _APPROVAL_STATE_META.get(code)
    if meta:
        return {"code": code, **meta}
    return {
        "code": code,
        "label": humanize_observability_token(code) or "Approval state recorded",
        "tone": "gray",
    }


__all__ = [
    "JOB_FAILURE_STATUSES",
    "JOB_SUCCESS_STATUSES",
    "JOB_TERMINAL_STATUSES",
    "approval_state_meta",
    "dashboard_result_from_status",
    "event_status_meta",
    "event_type_meta",
    "humanize_observability_token",
    "job_attention_group",
    "job_status_meta",
    "normalize_event_status",
    "normalize_job_status",
]
