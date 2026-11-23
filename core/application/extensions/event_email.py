"""One standard email for every workflow event.

When a workflow notification goes to an email extension, the platform writes the
message: the same plain-text layout for every event, with the icon and title
saying what happened -- done, failed, or waiting for someone. A message the
automation author wrote is kept and shown first.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

_APPROVAL_HEADING = "Needs your approval"

#: (icon, title) per event type. Anything else is a plain notification.
_EVENT_HEADINGS: dict[str, tuple[str, str]] = {
    "job.completed": ("✅", "Workflow completed"),
    "job.failed": ("❌", "Workflow failed"),
    "job.awaiting_approval": ("⚠️", _APPROVAL_HEADING),
    "job.approval_waiting": ("⚠️", _APPROVAL_HEADING),
    "approval.requested": ("⚠️", _APPROVAL_HEADING),
    "job.timer_waiting": ("⏳", "Waiting for a timer"),
    "job.group_started": ("▶️", "Group started"),
    "job.group_completed": ("✅", "Group completed"),
    "job.group_failed": ("❌", "Group failed"),
    "job.step_started": ("▶️", "Step started"),
    "job.step_completed": ("✅", "Step completed"),
    "job.step_failed": ("❌", "Step failed"),
    "job.step_blocked": ("⚠️", "Step blocked"),
}
_DEFAULT_HEADING = ("🔔", "Workflow notification")


def _text(value: Any) -> str:
    return str(value or "").strip()


def _as_dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def event_heading(event_type: str) -> tuple[str, str]:
    """The icon and title for one event type."""
    return _EVENT_HEADINGS.get(_text(event_type), _DEFAULT_HEADING)


def render_event_email(event: Mapping[str, Any], *, base_url: str = "") -> tuple[str, str]:
    """Return the subject and plain-text body for one workflow event."""
    payload = _as_dict(event.get("payload"))
    event_type = _text(event.get("event_type")) or _text(payload.get("source_event_type"))
    # An automation re-sends its trigger under its own event type; the trigger is
    # what the reader cares about.
    source_type = _text(payload.get("source_event_type"))
    if source_type in _EVENT_HEADINGS and event_type not in _EVENT_HEADINGS:
        event_type = source_type
    icon, title = event_heading(event_type)
    workflow = (
        _text(payload.get("workflow_display_name"))
        or _text(payload.get("workflow_name"))
        or _text(event.get("workflow_id"))
    )
    subject = f"{icon} {title}" + (f": {workflow}" if workflow else "")

    lines = [f"{icon} {title}", ""]
    message = _text(payload.get("message"))
    if message:
        lines += [message, ""]
    # A Group line only for a real group, a For each line only for a For
    # each, a Step line only for a step; the root has no label of its own.
    group_id = _text(event.get("group_id")) or _text(payload.get("group_id"))
    for_each_id = _text(event.get("for_each_id")) or _text(payload.get("for_each_id"))
    step_id = _text(event.get("step_id")) or _text(payload.get("step_id"))
    details = [
        ("Workflow", workflow),
        ("Task", _text(payload.get("task_summary"))),
        ("Group", (_text(payload.get("group_label")) or group_id) if group_id else ""),
        (
            "For each",
            (_text(payload.get("for_each_label")) or for_each_id) if for_each_id else "",
        ),
        ("Step", (_text(payload.get("step_label")) or step_id) if step_id else ""),
        ("Due", _text(payload.get("due_at"))),
        ("Error", _text(payload.get("error"))),
    ]
    lines += [f"{label}: {value}" for label, value in details if value]
    run_url = event_run_url(event, base_url=base_url)
    if run_url:
        lines += ["", f"Open the run: {run_url}"]
    lines += ["", "Sent by Flow Steward."]
    return subject, "\n".join(lines).strip() + "\n"


def event_run_url(event: Mapping[str, Any], *, base_url: str = "") -> str:
    """The platform page of the run an event is about, or ``""``.

    Present only when the platform knows its public base URL and the event has a
    job. A propagated approval is decided on the job of the actual Approval, so
    its ``approval_job_id`` wins. The value is the platform's own page and never
    carries a token.
    """
    from urllib.parse import quote

    payload = _as_dict(event.get("payload"))
    job_id = _text(payload.get("approval_job_id")) or _text(event.get("job_id"))
    base = _text(base_url).rstrip("/")
    if not job_id or not base:
        return ""
    return f"{base}/jobs/{quote(job_id, safe='')}"


__all__ = ["event_heading", "event_run_url", "render_event_email"]
