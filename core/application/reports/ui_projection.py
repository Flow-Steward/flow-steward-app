"""Semantic UI projection layer for Dashboard and Job Detail.

Derives simplified, human-readable view models from the canonical
Epic 13 trace/runtime/approval data. Does NOT modify or
replace the canonical API contracts.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from core.application.jobs.workflow_wait_projection import DURABLE_WAIT_STATUSES
from core.application.reports.display_text import build_display_summary

from .job_detail_sections import (
    build_job_detail_sections,
)
from .ui_projection_helpers import (
    NOT_RECORDED,
    _as_datetime,
    _format_cost_knownness_display,
    _format_datetime,
    _format_duration_ms,
    _humanize_identifier,
    _normalize_dict,
    _normalize_role,
    _normalize_rows,
    _status_meta,
    _truncate_text,
)


def _entity_id(value: Any) -> str | None:
    normalized = str(value or "").strip()
    return normalized or None


def _build_header(job: dict[str, Any], project_name: str) -> dict[str, Any]:
    task_summary = str(job.get("task_summary") or "").strip()
    task_title = (
        build_display_summary(task_summary) or task_summary or f"Job #{job.get('id') or '?'}"
    )
    task_description_preview = (
        build_display_summary(task_summary, max_len=180) if task_summary else ""
    )
    if task_summary and (not task_description_preview or task_description_preview == task_title):
        task_description_preview = _truncate_text(" ".join(task_summary.split()), limit=180)
    has_task_description = bool(
        task_summary
        and (
            task_summary != task_title
            or "\n" in task_summary
            or len(task_summary) > max(len(task_title), 0) + 24
        )
    )
    return {
        "project_name": str(project_name or "").strip() or "Unknown project",
        "project_id": _entity_id(job.get("project_id")),
        "task_title": task_title,
        "task_summary": task_summary,
        "task_description_preview": task_description_preview,
        "has_task_description": has_task_description,
        "job_id": _entity_id(job.get("id")),
        "record_facts": [
            {
                "label": "Job ID",
                "value": f"#{_entity_id(job.get('id'))}"
                if _entity_id(job.get("id"))
                else NOT_RECORDED,
            },
            {"label": "Source", "value": _humanize_identifier(job.get("task_source")) or "Manual"},
            {"label": "Mode", "value": _humanize_identifier(job.get("mode")) or NOT_RECORDED},
        ],
    }


#: Started and not finished: the run's duration is still growing, including
#: while it is paused on an approval, a timer or a child workflow.
_RUNNING_JOB_STATUSES = frozenset({"in_progress", *DURABLE_WAIT_STATUSES})


def _run_duration_ms(job: dict[str, Any], status: str, now: datetime | None = None) -> Any:
    """The recorded duration, or how long a running job has run so far.

    A running job's stored duration is not its duration yet (the row holds 0
    until the job finishes), so it is measured from the start instead.
    """
    recorded = job.get("duration_ms")
    if status not in _RUNNING_JOB_STATUSES:
        return recorded
    started = _as_datetime(job.get("started_at"))
    if started is None:
        return recorded
    elapsed = (now or datetime.now(UTC)) - started
    return max(0, int(elapsed.total_seconds() * 1000))


def _build_hero_summary(
    *,
    job: dict[str, Any],
    runtime: dict[str, Any],
    review_workspace: dict[str, Any] | None,
    cost_breakdown: dict[str, Any],
    failure_reason: str,
    lifecycle: dict[str, Any] | None = None,
) -> dict[str, Any]:
    status = str(job.get("status") or "").strip().lower()
    lifecycle_payload = _normalize_dict(lifecycle)
    lifecycle_status = _normalize_dict(lifecycle_payload.get("status"))
    lifecycle_step = _normalize_dict(lifecycle_payload.get("step"))
    meta = _status_meta(status)
    step_label = (
        str(lifecycle_step.get("label") or "").strip()
        or _humanize_identifier(runtime.get("current_step_id"))
        or NOT_RECORDED
    )
    headline = meta["headline"]
    headline_key = ""
    if review_workspace:
        headline = (
            str(review_workspace.get("title") or "").strip()
            or str(review_workspace.get("intro") or "").strip()
            or headline
        )
    elif status == "in_progress" and step_label != NOT_RECORDED:
        headline = f"{step_label} is currently in progress."
    elif status == "completed":
        headline = "Flow Steward completed the requested work."
        headline_key = "react.jobs.completed_requested_work"
    elif status == "retryable_interrupted_step":
        headline = "One step stopped unexpectedly and can be retried from that point."

    summary = str(runtime.get("wait_reason") or "").strip()
    summary_key = ""
    if failure_reason and status in {"failed", "max_iterations"}:
        summary = _truncate_text(failure_reason, limit=260)
    elif not summary and review_workspace:
        summary = str(review_workspace.get("intro") or "").strip()
    elif not summary:
        summary = headline
        summary_key = headline_key

    return {
        "status_label": str(lifecycle_status.get("label") or meta["label"]),
        "status_tone": str(lifecycle_status.get("tone") or meta["tone"]),
        "headline": headline,
        "headline_key": headline_key,
        "summary": summary,
        "summary_key": summary_key,
        "facts": [
            {
                "id": "current_step",
                "label": "Current step",
                "label_key": "react.jobs.current_step",
                "value": step_label,
            },
            {
                "id": "started",
                "label": "Started",
                "label_key": "project_detail.jobs.grid.started",
                "value": _format_datetime(job.get("started_at")),
            },
            {
                "id": "duration",
                "label": "Duration",
                "label_key": "jobs.columns.duration",
                "value": _format_duration_ms(_run_duration_ms(job, status)),
            },
            {
                "id": "last_update",
                "label": "Last update",
                "label_key": "react.jobs.last_update",
                "value": _format_datetime(
                    job.get("heartbeat_at") or runtime.get("heartbeat_at") or job.get("finished_at")
                ),
            },
            {
                "id": "current_cost",
                "label": "Current cost",
                "label_key": "react.jobs.current_cost",
                "value": str(
                    cost_breakdown.get("total_cost_display")
                    or _format_cost_knownness_display(
                        cost_breakdown.get("known_cost_usd")
                        or cost_breakdown.get("total_cost_usd")
                        or 0,
                        str(cost_breakdown.get("cost_knownness") or ""),
                        cost_breakdown.get("unknown_cli_tokens") or 0,
                    )
                ),
            },
        ],
    }


def _build_next_action(
    *,
    job: dict[str, Any],
    runtime: dict[str, Any],
    review_workspace: dict[str, Any] | None,
    failure_reason: str,
    selected_step: dict[str, Any] | None,
    user_role: str,
) -> dict[str, Any]:
    status = str(job.get("status") or "").strip().lower()
    role = _normalize_role(user_role)
    if review_workspace:
        if review_workspace.get("kind") == "clarifying_answer":
            return {
                "kind": "clarifying_answer",
                "tone": "amber",
                "title": "Answer the current clarification request",
                "body": "Review workspace contains the active question. Flow Steward cannot continue until someone answers it.",
            }
        return {
            "kind": "approval_needed",
            "tone": "amber",
            "title": "Review the active gate",
            "body": "Review workspace contains the only active controls for this gate, along with the context Flow Steward needs to continue.",
        }
    if status == "retryable_interrupted_step":
        title = "Review the interrupted step"
        body = "Use the selected-step action to retry from the interruption point."
        if role == "viewer":
            body = "An operator or admin needs to retry the selected step."
        return {"kind": "retry_needed", "tone": "red", "title": title, "body": body}
    if status in {"failed", "max_iterations"}:
        return {
            "kind": "restart_available",
            "tone": "red",
            "title": "Decide whether to rerun this job",
            "body": _truncate_text(
                failure_reason
                or "Review the failure details, then restart the run if you want Flow Steward to try again.",
                limit=260,
            ),
        }
    if status == "completed":
        return {
            "kind": "complete",
            "tone": "green",
            "title": "No action is required",
            "title_key": "react.jobs.no_action_required",
            "body": "This run finished successfully. Restart the job only if you need Flow Steward to run the same task again.",
            "body_key": "react.jobs.completed_restart_help",
        }
    if status == "cancelled":
        return {
            "kind": "cancelled",
            "tone": "gray",
            "title": "This run has stopped",
            "body": "Restart the job if you need Flow Steward to create a new run from the same task.",
        }
    if status == "pending":
        return {
            "kind": "queued",
            "tone": "blue",
            "title": "No action is required right now",
            "body": str(
                runtime.get("wait_reason")
                or "Flow Steward is waiting for a worker to pick up the job."
            ),
        }
    if status == "in_progress":
        step_label = _humanize_identifier(runtime.get("current_step_id")) or "the current step"
        return {
            "kind": "in_progress",
            "tone": "blue",
            "title": "No action is required right now",
            "body": f"Flow Steward is currently working through {step_label}. Review the execution waterfall below if you need more context.",
        }
    if selected_step:
        return {
            "kind": "inspect_step",
            "tone": "gray",
            "title": "Review the selected step",
            "body": "The step inspector explains what happened, why it moved or stopped, and what Flow Steward expects next.",
        }
    return {
        "kind": "none",
        "tone": "gray",
        "title": "No immediate action",
        "body": "This page summarizes the current Flow Steward run state.",
    }


def _build_job_actions(
    *,
    job: dict[str, Any],
    user_role: str,
) -> dict[str, Any]:
    role = _normalize_role(user_role)
    job_id = str(job.get("id") or "").strip()
    page_actions: list[dict[str, Any]] = []
    if role in {"operator", "admin"} and job_id:
        status = str(job.get("status") or "").strip().lower()
        if status in {"pending", "resume_pending", "in_progress", *DURABLE_WAIT_STATUSES}:
            confirm = "Cancel this job?"
            if status == "awaiting_approval":
                confirm = "Cancel this job? The current approval gate will be dismissed."
            page_actions.append(
                {
                    "kind": "cancel_job",
                    "label": "Cancel job",
                    "path": f"/api/jobs/{job_id}/cancel",
                    "method": "post",
                    "confirm": confirm,
                }
            )
        elif status in {"completed", "failed", "cancelled", "max_iterations"}:
            page_actions.append(
                {
                    "kind": "restart_job",
                    "label": "Restart job",
                    "path": f"/api/jobs/{job_id}/restart",
                    "method": "post",
                    "confirm": "Restart this job as a new run?",
                }
            )
    return {
        "page_actions": page_actions,
        "can_open_diagnostics": role in {"operator", "admin"},
    }


# ---------------------------------------------------------------------------
# Job Detail projection
# ---------------------------------------------------------------------------


def project_job_summary(
    job: dict[str, Any],
    runtime: dict[str, Any],
    failure_reason: str = "",
) -> dict[str, Any]:
    """Derive a compact semantic summary block for Job Detail."""
    status = str(job.get("status") or "").strip().lower()
    next_action = _derive_next_action(status, runtime)
    return {
        "summary": str(job.get("task_summary") or "").strip(),
        "status": status,
        "failure_reason": failure_reason,
        "next_action": next_action,
        "duration_display": _format_duration_ms(job.get("duration_ms")),
        "started_at": job.get("started_at"),
        "finished_at": job.get("finished_at"),
        "mode": job.get("mode") or "",
        "test_mode": bool(runtime.get("test_mode")),
    }


def _derive_next_action(status: str, runtime: dict[str, Any]) -> str:
    """Return a human-readable next-action string based on job status."""
    if status == "awaiting_approval":
        return "Review and approve or reject the pending gate."
    if status == "resume_pending":
        return "Approval accepted; waiting for the worker to resume the run."
    if status == "awaiting_timer":
        return "Paused on a Wait step; the run resumes by itself when it is due."
    if status == "awaiting_child":
        return "Waiting for the child workflow to finish before the run continues."
    if status in ("failed", "max_iterations"):
        return "Review the failure reason and restart or acknowledge."
    if status == "retryable_interrupted_step":
        return "Retry the interrupted step or restart the job."
    if status == "pending":
        return "Waiting for a worker to pick up this task."
    if status == "in_progress":
        step = runtime.get("current_step_id") or ""
        agent = runtime.get("active_agent") or ""
        if step and agent:
            return f"Running: {agent} in {step}"
        if step:
            return f"Running: {step}"
        return "Job is running."
    if status == "completed":
        return ""
    if status == "cancelled":
        return "Job was cancelled. Restart if needed."
    return ""


def project_job_detail(
    *,
    job: dict[str, Any],
    runtime: dict[str, Any] | None = None,
    thread_state: dict[str, Any] | None = None,
    approval_gate: dict[str, Any] | None = None,
    approval_history: list[dict[str, Any]] | None = None,
    decision_overrides: list[dict[str, Any]] | None = None,
    cost_breakdown: dict[str, Any] | None = None,
    causal_chain: dict[str, Any] | None = None,
    job_errors: list[dict[str, Any]] | None = None,
    messages: list[dict[str, Any]] | None = None,
    events: list[dict[str, Any]] | None = None,
    incident: dict[str, Any] | None = None,
    runtime_journal: list[dict[str, Any]] | None = None,
    lifecycle: dict[str, Any] | None = None,
    sections: dict[str, Any] | None = None,
    project_name: str = "",
    failure_reason: str = "",
    user_role: str = "viewer",
    include_sections: bool = True,
) -> dict[str, Any]:
    """Project raw Job Detail payloads into a semantic page model."""
    normalized_job = _normalize_dict(job)
    normalized_runtime = _normalize_dict(runtime)
    normalized_thread_state = _normalize_dict(thread_state)
    normalized_approval_gate = _normalize_dict(approval_gate)
    normalized_approval_history = _normalize_rows(approval_history)
    normalized_decision_overrides = _normalize_rows(decision_overrides)
    normalized_cost_breakdown = _normalize_dict(cost_breakdown)
    normalized_causal_chain = _normalize_dict(causal_chain)
    normalized_job_errors = _normalize_rows(job_errors)
    normalized_messages = _normalize_rows(messages)
    normalized_events = _normalize_rows(events)
    normalized_incident = _normalize_dict(incident)
    normalized_runtime_journal = _normalize_rows(runtime_journal)
    normalized_role = _normalize_role(user_role)
    normalized_sections = _normalize_dict(sections)
    if normalized_sections:
        execution_steps = _normalize_rows(normalized_sections.get("execution_steps"))
        selected_step_payload = _normalize_dict(normalized_sections.get("selected_step")) or None
        review_workspace = _normalize_dict(normalized_sections.get("review_workspace")) or None
        previous_decisions = _normalize_rows(normalized_sections.get("previous_decisions"))
        cost_payload = _normalize_dict(normalized_sections.get("cost_breakdown"))
        diagnostics = _normalize_dict(normalized_sections.get("diagnostics"))
    else:
        computed_sections = build_job_detail_sections(
            job=normalized_job,
            runtime=normalized_runtime,
            thread_state=normalized_thread_state,
            approval_gate=normalized_approval_gate,
            approval_history=normalized_approval_history,
            decision_overrides=normalized_decision_overrides,
            cost_breakdown=normalized_cost_breakdown,
            causal_chain=normalized_causal_chain,
            incident=normalized_incident,
            job_errors=normalized_job_errors,
            messages=normalized_messages,
            events=normalized_events,
            runtime_journal=normalized_runtime_journal,
            user_role=normalized_role,
        )
        execution_steps = _normalize_rows(computed_sections.get("execution_steps"))
        selected_step_payload = _normalize_dict(computed_sections.get("selected_step")) or None
        review_workspace = _normalize_dict(computed_sections.get("review_workspace")) or None
        previous_decisions = _normalize_rows(computed_sections.get("previous_decisions"))
        cost_payload = _normalize_dict(computed_sections.get("cost_breakdown"))
        diagnostics = _normalize_dict(computed_sections.get("diagnostics"))

    projection = {
        "header": _build_header(normalized_job, project_name),
        "hero_summary": _build_hero_summary(
            job=normalized_job,
            runtime=normalized_runtime,
            review_workspace=review_workspace,
            cost_breakdown=cost_payload,
            failure_reason=failure_reason,
            lifecycle=_normalize_dict(lifecycle),
        ),
        "next_action": _build_next_action(
            job=normalized_job,
            runtime=normalized_runtime,
            review_workspace=review_workspace,
            failure_reason=failure_reason,
            selected_step=selected_step_payload,
            user_role=normalized_role,
        ),
        "job_actions": _build_job_actions(job=normalized_job, user_role=normalized_role),
        "user_role": normalized_role,
    }
    if not include_sections:
        return projection
    projection.update(
        {
            "review_workspace": review_workspace,
            "execution_steps": execution_steps,
            "selected_step": selected_step_payload,
            "previous_decisions": previous_decisions,
            "cost_breakdown": cost_payload,
            "diagnostics": diagnostics,
        }
    )
    return projection


def user_role_from_auth(auth_user: dict | None) -> str:
    """Extract the effective UI role from the auth payload."""
    if not isinstance(auth_user, dict):
        return "viewer"
    roles = auth_user.get("roles") or []
    if "admin" in roles:
        return "admin"
    if "operator" in roles:
        return "operator"
    return "viewer"
