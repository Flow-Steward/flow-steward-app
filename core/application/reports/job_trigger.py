"""What started a job, in one line a user can read.

A run started by a schedule, a webhook, another workflow or a builder test run
looks the same once it is running; the job keeps the facts in several places
(its task source, its invocation snapshot, its metadata). This module reads
them in one order and says which one it was, with a link where there is one.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def _text(value: Any) -> str:
    return str(value or "").strip()


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def job_trigger(job: Mapping[str, Any]) -> dict[str, Any]:
    """``{kind, label, detail?, link?}`` describing what started ``job``."""
    meta = _mapping(job.get("meta"))
    invocation = _mapping(job.get("invocation"))
    source = _text(job.get("task_source")).lower()
    channel = _text(invocation.get("channel")).lower()
    entry_path = _text(invocation.get("entry_path")).lower()

    def trigger(
        kind: str, label: str, detail: str = "", link: str = "", *, label_key: str = ""
    ) -> dict[str, Any]:
        result: dict[str, Any] = {"kind": kind, "label": label}
        if label_key:
            result["label_key"] = label_key
        if detail:
            result["detail"] = detail
        if link:
            result["link"] = link
        return result

    if source == "step_test_run" or meta.get("step_test_run"):
        step = _text(meta.get("step_id"))
        return trigger(
            "test_run",
            "Step test run from the workflow builder",
            f"step {step}" if step else "",
        )
    if source == "workflow_test_run" or meta.get("workflow_test_run"):
        return trigger(
            "test_run",
            "Test run from the workflow builder",
            label_key="react.jobs.workflow_builder_test_run",
        )
    parent_job = _text(meta.get("invoke_parent_job_id") or invocation.get("origin_job_id"))
    if source == "invoke_workflow" or entry_path == "invoke_workflow":
        parent_workflow = _text(meta.get("invoke_parent_workflow_id"))
        return trigger(
            "parent_workflow",
            "Another workflow",
            f"workflow {parent_workflow}" if parent_workflow else "",
            f"/jobs/{parent_job}" if parent_job else "",
        )
    if meta.get("scheduled_job_id") or source in {"schedule", "scheduled"} or channel == "schedule":
        cron = _text(meta.get("scheduled_cron_expr"))
        if _text(meta.get("scheduled_run_type")) == "manual":
            return trigger(
                "schedule",
                "Schedule, run now",
                f"cron {cron}" if cron else "",
                label_key="react.jobs.schedule_run_now",
            )
        return trigger(
            "schedule",
            "Schedule",
            f"cron {cron}" if cron else "",
            label_key="project_detail.scheduled.fields.schedule",
        )
    if source == "job_restart" or channel == "job_restart":
        origin = _text(meta.get("restart_of_job_id") or invocation.get("origin_job_id"))
        return trigger("restart", "Restart", "", f"/jobs/{origin}" if origin else "")
    if channel == "webhook" or source == "webhook":
        # The entry key is a random part of the public URL: it names nothing.
        return trigger("webhook", "Webhook")
    if channel == "provider_webhook" or source == "provider":
        detail = _text(invocation.get("source_system") or invocation.get("trigger_id"))
        return trigger("provider_event", "Provider event", detail)
    if channel == "mcp" or source == "mcp_workflow":
        return trigger("mcp", "MCP client")
    if source == "step_replay":
        return trigger("replay", "Step replay")
    if source == "command_dispatch":
        return trigger("automation", "Automation")
    if channel == "manual_ui" or entry_path == "manual_ui" or source in {"web", "ui", "chat"}:
        return trigger("manual_ui", "Started in the app")
    return trigger("api", "API")


__all__ = ["job_trigger"]
