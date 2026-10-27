"""Job Details response projection and bounded public payload."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from core.application.jobs.workflow_run_models import PublicWorkflowResultV1
from core.application.reports.job_detail_payloads import _payload_nested_dict
from core.application.reports.job_trigger import job_trigger

from .ui_projection_common import _bounded_diagnostic_value, _redact_for_ui

_TOP_LEVEL_JOB_FIELDS = (
    "id",
    "status",
    "project_id",
    "thread_id",
    "task_summary",
    "display_summary",
    "task_source",
    "mode",
    "selected_workflow",
    "selected_workflow_display_name",
    "selected_workflow_source",
    "workflow_selection_reason",
    "started_at",
    "finished_at",
    "duration_ms",
    "elapsed_seconds",
    "heartbeat_at",
    "current_step_id",
    "current_group_id",
    "current_agent",
    "failure_summary",
    "invocation",
    "trigger",
    "error",
    # Set while an operator has acknowledged a failed or stuck run.
    "acknowledged_at",
    "acknowledged_by",
)
_TOP_LEVEL_PROJECTION_FIELDS = (
    "header",
    "hero_summary",
    "next_action",
    "job_actions",
    "user_role",
)


def _build_job_bucket(
    *,
    job: dict[str, Any],
    status_payload: dict[str, Any],
    runtime: dict[str, Any],
    lifecycle: dict[str, Any],
) -> dict[str, Any]:
    job_bucket: dict[str, Any] = {}
    for key in _TOP_LEVEL_JOB_FIELDS:
        if key in status_payload and status_payload.get(key) is not None:
            job_bucket[key] = status_payload.get(key)
        elif key in job and job.get(key) is not None:
            job_bucket[key] = job.get(key)
    acknowledged_at = job_bucket.get("acknowledged_at")
    if isinstance(acknowledged_at, datetime):
        job_bucket["acknowledged_at"] = acknowledged_at.isoformat()

    job_bucket["status"] = str(
        lifecycle.get("status", {}).get("code")
        or job_bucket.get("status")
        or status_payload.get("status")
        or job.get("status")
        or ""
    )
    # Both are nullable: a run that has not dispatched a step has no current
    # step, and a root step runs in no group.
    job_bucket["current_step_id"] = (
        str(
            lifecycle.get("step", {}).get("code")
            or job_bucket.get("current_step_id")
            or status_payload.get("current_step_id")
            or job.get("current_step_id")
            or runtime.get("current_step_id")
            or ""
        ).strip()
        or None
    )
    job_bucket["current_group_id"] = (
        str(
            job_bucket.get("current_group_id")
            or status_payload.get("current_group_id")
            or job.get("current_group_id")
            or runtime.get("current_group_id")
            or ""
        ).strip()
        or None
    )
    job_bucket["current_agent"] = str(
        job_bucket.get("current_agent")
        or status_payload.get("current_agent")
        or job.get("current_agent")
        or runtime.get("active_agent")
        or ""
    )
    return job_bucket


def _projection_with_canonical_step(
    projection: dict[str, Any], lifecycle: dict[str, Any]
) -> dict[str, Any]:
    normalized = dict(projection)
    hero_summary = _payload_nested_dict(normalized, "hero_summary")
    facts = hero_summary.get("facts")
    if not isinstance(facts, list):
        return normalized

    step = _payload_nested_dict(lifecycle, "step")
    step_label = str(step.get("label") or step.get("code") or "").strip() or "Not recorded"
    normalized_facts: list[Any] = []
    for fact in facts:
        if not isinstance(fact, dict):
            normalized_facts.append(fact)
            continue
        normalized_fact = dict(fact)
        label = str(normalized_fact.get("label") or "").strip().lower().replace("_", " ")
        if label == "current step":
            normalized_fact["value"] = step_label
        normalized_facts.append(normalized_fact)
    hero_summary["facts"] = normalized_facts
    normalized["hero_summary"] = hero_summary
    return normalized


def _build_contract_payload(
    *,
    job: dict[str, Any],
    status_payload: dict[str, Any],
    runtime: dict[str, Any],
    projection: dict[str, Any],
    lifecycle: dict[str, Any],
    sections: dict[str, Any],
    approval_gate: dict[str, Any],
    approval_history: list[dict[str, Any]],
    causal_chain: dict[str, Any],
    cost_breakdown_raw: dict[str, Any],
    decision_overrides: list[dict[str, Any]],
    incident: dict[str, Any],
    job_errors: list[dict[str, Any]],
    runtime_journal: list[dict[str, Any]],
    section_warnings: list[dict[str, str]],
    thread_state: dict[str, Any],
    workflow_run: dict[str, Any] | None,
) -> dict[str, Any]:
    job_bucket = _build_job_bucket(
        job=job,
        status_payload=status_payload,
        runtime=runtime,
        lifecycle=lifecycle,
    )
    project_id = str(job_bucket.get("project_id") or "").strip() or None
    resolved_job_id = str(job_bucket.get("id") or "").strip() or None

    def _bounded(value: Any) -> Any:
        return _bounded_diagnostic_value(
            value,
            project_id=project_id,
            job_id=resolved_job_id,
        )

    def _bounded_review_workspace(value: Any) -> Any:
        if not isinstance(value, dict):
            return _bounded(value)
        workspace: dict[str, Any] = {}
        for key, field_value in value.items():
            workspace[key] = _bounded(field_value)
        return workspace

    def _bounded_approval_gate(value: Any) -> Any:
        if not isinstance(value, dict):
            return _bounded(value)
        gate: dict[str, Any] = {}
        for key, field_value in value.items():
            if key == "allowed_actions" and isinstance(field_value, list):
                gate[key] = [_bounded(item) for item in field_value]
                continue
            if key in {"actions", "gate_meta", "response_mapping"}:
                # These fields define what decision the UI is allowed to send.
                # Preserve their complete shape; generic list truncation here
                # can turn Reject/Abort/Override into a different contract.
                gate[key] = _redact_for_ui(
                    field_value,
                    project_id=project_id,
                    job_id=resolved_job_id,
                )
                continue
            gate[key] = _bounded(field_value)
        return gate

    bounded_sections = dict(sections)
    if "review_workspace" in sections:
        bounded_sections["review_workspace"] = _bounded_review_workspace(
            sections.get("review_workspace")
        )

    bounded_job_bucket = _bounded(job_bucket)
    if not isinstance(bounded_job_bucket, dict):
        bounded_job_bucket = {}
    # This fixed platform label can resemble a provider token. Restore only
    # the factory's exact public tuple, after redacting every owner-controlled
    # trigger field; unknown keys and details still use the normal redactor.
    public_test_trigger = job_trigger({"task_source": "workflow_test_run"})
    original_trigger = _payload_nested_dict(job_bucket, "trigger")
    bounded_trigger = _payload_nested_dict(bounded_job_bucket, "trigger")
    if bounded_trigger and all(
        original_trigger.get(key) == public_test_trigger[key]
        for key in ("kind", "label", "label_key")
    ):
        bounded_trigger["label_key"] = public_test_trigger["label_key"]
        bounded_job_bucket["trigger"] = bounded_trigger
    bounded_lifecycle = dict(lifecycle)
    bounded_lifecycle.pop("steps", None)
    lifecycle_approval = _payload_nested_dict(bounded_lifecycle, "approval")
    if lifecycle_approval:
        lifecycle_approval["gate"] = _bounded_approval_gate(lifecycle_approval.get("gate") or {})
        lifecycle_approval["history"] = _bounded(lifecycle_approval.get("history") or [])
        lifecycle_approval["review_workspace"] = bounded_sections.get("review_workspace")
        bounded_lifecycle["approval"] = lifecycle_approval
    normalized_projection = _projection_with_canonical_step(projection, lifecycle)
    canonical_projection = {
        key: normalized_projection.get(key)
        for key in _TOP_LEVEL_PROJECTION_FIELDS
        if key in normalized_projection
    }
    payload: dict[str, Any] = dict(bounded_job_bucket)
    payload["runtime"] = _bounded(runtime)
    payload["status_label"] = str(lifecycle.get("status", {}).get("label") or "")
    payload["status_tone"] = str(lifecycle.get("status", {}).get("tone") or "gray")
    payload["step_label"] = str(lifecycle.get("step", {}).get("label") or "")
    payload["approval_state"] = str(lifecycle.get("approval", {}).get("state") or "")
    payload["attention_group"] = str(lifecycle.get("status", {}).get("attention_group") or "")
    payload["job"] = dict(bounded_job_bucket)
    payload["sections"] = bounded_sections
    payload["projection"] = canonical_projection
    for key in _TOP_LEVEL_PROJECTION_FIELDS:
        payload[key] = canonical_projection.get(key)
    payload["approval_gate"] = _bounded_approval_gate(approval_gate)
    payload["approval_history"] = _bounded(approval_history)
    payload["causal_chain"] = _bounded(causal_chain)
    payload["cost_breakdown_raw"] = _bounded(cost_breakdown_raw)
    payload["decision_overrides"] = _bounded(decision_overrides)
    payload["incident"] = _bounded(incident)
    payload["job_errors"] = _bounded(job_errors)
    payload["runtime_journal"] = _bounded(runtime_journal)
    payload["section_warnings"] = section_warnings
    payload["thread_state"] = _bounded(thread_state)
    payload["lifecycle"] = bounded_lifecycle
    # workflow_run is a versioned public integration contract. Its validated
    # state must remain exact; changing it requires a new contract version.
    payload["workflow_run"] = workflow_run
    public_result = status_payload.get("workflow_result")
    payload["workflow_result"] = (
        PublicWorkflowResultV1.model_validate(public_result).model_dump(mode="json")
        if public_result is not None
        else None
    )
    return payload
