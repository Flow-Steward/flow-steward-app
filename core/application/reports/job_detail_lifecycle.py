"""Project job lifecycle, approval and failure information for detail views."""

from __future__ import annotations

from typing import Any

from core.application.observability_vocabulary import (
    approval_state_meta,
    humanize_observability_token,
    job_status_meta,
)
from core.application.reports.job_detail_payloads import _payload_nested_dict


def _approval_state(
    *,
    status: str,
    approval_gate: dict[str, Any],
    approval_history: list[dict[str, Any]],
) -> str:
    normalized_status = str(status or "").strip().lower()
    if normalized_status == "awaiting_approval":
        return "awaiting_approval"
    if normalized_status == "resume_pending":
        return "resume_pending"
    if approval_gate:
        return "review_available"
    if approval_history:
        return "approval_recorded"
    return "none"


def _status_terminal(status: str) -> bool:
    return str(status or "").strip().lower() in {
        "completed",
        "failed",
        "cancelled",
        "max_iterations",
        "retryable_interrupted_step",
    }


def _step_payload(*, status_payload: dict[str, Any], runtime: dict[str, Any]) -> dict[str, str]:
    step_code = str(
        status_payload.get("current_step_id") or runtime.get("current_step_id") or ""
    ).strip()
    return {
        "code": step_code,
        "label": humanize_observability_token(step_code) or step_code,
    }


def _root_cause_payload(
    *,
    incident: dict[str, Any],
    diagnostics_root_cause: dict[str, Any],
) -> dict[str, Any]:
    root_cause = _payload_nested_dict(incident, "root_cause")
    category_code = str(root_cause.get("category") or "").strip()
    payload = dict(root_cause)
    payload["category"] = category_code
    payload["category_label"] = humanize_observability_token(category_code) or str(
        diagnostics_root_cause.get("category") or ""
    )
    payload["next_action"] = str(diagnostics_root_cause.get("next_action") or "").strip()
    return payload


def _build_lifecycle_contract(
    *,
    normalized_job_id: str,
    status_payload: dict[str, Any],
    runtime: dict[str, Any],
    incident: dict[str, Any],
    approval_gate: dict[str, Any],
    approval_history: list[dict[str, Any]],
    sections: dict[str, Any],
) -> dict[str, Any]:
    normalized_sections = dict(sections) if isinstance(sections, dict) else {}
    review_workspace = _payload_nested_dict(normalized_sections, "review_workspace")
    diagnostics = _payload_nested_dict(normalized_sections, "diagnostics")
    diagnostics_tabs = _payload_nested_dict(diagnostics, "tabs")
    diagnostics_root_cause = _payload_nested_dict(diagnostics, "root_cause")
    cost_payload = _payload_nested_dict(normalized_sections, "cost_breakdown")
    status_meta = job_status_meta(status_payload.get("status"))
    step = _step_payload(status_payload=status_payload, runtime=runtime)
    approval_state_code = _approval_state(
        status=str(status_payload.get("status") or ""),
        approval_gate=approval_gate,
        approval_history=approval_history,
    )
    approval_meta = approval_state_meta(approval_state_code)
    return {
        "job_id": str(status_payload.get("id") or normalized_job_id),
        "status": {
            "code": str(status_meta.get("code") or ""),
            "label": str(status_meta.get("label") or ""),
            "label_key": str(status_meta.get("label_key") or ""),
            "tone": str(status_meta.get("tone") or "gray"),
            "headline": str(status_meta.get("headline") or ""),
            "terminal": bool(
                status_meta.get(
                    "terminal", _status_terminal(str(status_payload.get("status") or ""))
                )
            ),
            "outcome": str(status_meta.get("outcome") or ""),
            "attention_group": str(status_meta.get("dashboard_group") or ""),
        },
        "step": step,
        "runtime": dict(diagnostics_tabs.get("runtime") or {}),
        "errors": dict(diagnostics_tabs.get("errors") or {}),
        "costs": cost_payload,
        "approval": {
            "state": approval_state_code,
            "label": approval_meta["label"],
            "tone": approval_meta["tone"],
            "gate": approval_gate,
            "history": approval_history,
            "review_workspace": review_workspace,
        },
        "root_cause": _root_cause_payload(
            incident=incident,
            diagnostics_root_cause=diagnostics_root_cause,
        ),
    }
