"""Decision and diagnostics builders for UI projection."""

from __future__ import annotations

from typing import Any

from core.infrastructure.security.secret_detection import redact_detected_secrets

from .ui_projection_extras_tabs import (
    WORKFLOW_STEP_VALUE,
    _causal_tab_rows,
    _decision_tab_rows,
    _entity_id,
    _error_tab_rows,
    _fact_rows,
    _failure_summary_has_signal,
    _field_text,
    _first_text,
    _is_low_signal_diagnostic_text,
    _override_tab_rows,
    _root_cause_has_signal,
    _runtime_tab_rows,
)
from .ui_projection_helpers import (
    NOT_RECORDED,
    _bounded_diagnostic_value,
    _decision_tone,
    _humanize_identifier,
    _message_summary,
    _normalize_dict,
    _normalize_rows,
    _redact_for_ui,
    _serialize_raw,
    _truncate_text,
)


def _bounded_semantic_text(
    value: Any,
    *,
    limit: int,
    project_id: str | None,
    job_id: str | None,
) -> str:
    safe_value = _bounded_diagnostic_value(
        str(value or ""),
        project_id=project_id,
        job_id=job_id,
    )
    return _truncate_text(safe_value, limit=limit)


def _bounded_actor_identity(value: Any, *, limit: int = 160) -> str:
    """Preserve an authorized actor identity while still removing credentials."""
    try:
        return _truncate_text(redact_detected_secrets(str(value or "").strip()), limit=limit)
    except Exception:
        return ""


def _build_previous_decisions(
    *,
    approval_history: list[dict[str, Any]],
    decision_overrides: list[dict[str, Any]],
    project_id: str | None = None,
    job_id: str | None = None,
) -> list[dict[str, Any]]:
    decisions: list[dict[str, Any]] = []
    for index, entry in enumerate(approval_history):
        stage = _bounded_semantic_text(
            entry.get("stage") or "Review",
            limit=120,
            project_id=project_id,
            job_id=job_id,
        ).strip()
        action = (
            _bounded_semantic_text(
                entry.get("action"),
                limit=80,
                project_id=project_id,
                job_id=job_id,
            )
            .strip()
            .lower()
        )
        approved = bool(entry.get("approved"))
        if action:
            outcome_label = _humanize_identifier(action)
            tone = _decision_tone(action, approved)
        else:
            outcome_label = "Approved" if approved else "Rejected"
            tone = _decision_tone("", approved)
        summary_bits = [
            _bounded_semantic_text(
                entry.get("question"),
                limit=320,
                project_id=project_id,
                job_id=job_id,
            ).strip(),
            _bounded_semantic_text(
                entry.get("comment"),
                limit=520,
                project_id=project_id,
                job_id=job_id,
            ).strip(),
        ]
        summary = _truncate_text(
            " ".join(bit for bit in summary_bits if bit).strip(),
            limit=700,
        )
        if not summary:
            summary = f"{stage} was {outcome_label.lower()}."
        actor = (
            _bounded_actor_identity(
                entry.get("actor")
                or entry.get("actor_username")
                or entry.get("approved_by")
                or entry.get("created_by")
                or "Human reviewer"
            )
            or "Human reviewer"
        )
        facts = _fact_rows(
            [
                ("Stage", stage),
                ("Outcome", outcome_label),
            ],
            project_id=project_id,
            job_id=job_id,
        )
        facts.insert(2, {"label": "Actor", "value": actor})
        decisions.append(
            {
                "id": f"approval-{index}",
                "kind": "approval",
                "title": f"{stage} decision",
                "summary": summary,
                "status": outcome_label,
                "outcome_label": outcome_label,
                "timestamp": _bounded_semantic_text(
                    entry.get("timestamp"),
                    limit=80,
                    project_id=project_id,
                    job_id=job_id,
                ).strip(),
                "actor": actor,
                "tone": tone,
                "detail": _bounded_semantic_text(
                    entry.get("content_preview"),
                    limit=700,
                    project_id=project_id,
                    job_id=job_id,
                ).strip(),
                "facts": facts,
                "meta": facts,
                "raw": _serialize_raw(entry, project_id=project_id, job_id=job_id),
            }
        )
    for index, entry in enumerate(decision_overrides):
        domain = _bounded_semantic_text(
            _humanize_identifier(entry.get("domain")) or "Workflow",
            limit=120,
            project_id=project_id,
            job_id=job_id,
        )
        original_value = _serialize_raw(entry.get("original_value"))
        override_value = _serialize_raw(entry.get("override_value"))
        summary_parts = []
        if original_value:
            summary_parts.append(f"Original: {_truncate_text(original_value, limit=120)}")
        if override_value:
            summary_parts.append(f"Override: {_truncate_text(override_value, limit=120)}")
        summary = " ".join(summary_parts).strip() or f"{domain} was overridden."
        override_actor = (
            _bounded_actor_identity(entry.get("created_by") or "Operator") or "Operator"
        )
        override_facts = _fact_rows(
            [
                ("Domain", domain),
                ("Original", original_value),
                ("Override", override_value),
            ],
            project_id=project_id,
            job_id=job_id,
        )
        override_facts.insert(1, {"label": "Actor", "value": override_actor})
        decisions.append(
            {
                "id": f"override-{index}",
                "kind": "decision_override",
                "title": f"{domain} override",
                "summary": summary,
                "status": "Override applied"
                if bool(entry.get("active"))
                else "Historical override",
                "outcome_label": "Override applied"
                if bool(entry.get("active"))
                else "Historical override",
                "timestamp": _bounded_semantic_text(
                    entry.get("created_at"),
                    limit=80,
                    project_id=project_id,
                    job_id=job_id,
                ).strip(),
                "actor": override_actor,
                "tone": "amber" if bool(entry.get("active")) else "gray",
                "detail": _bounded_semantic_text(
                    entry.get("reason"),
                    limit=700,
                    project_id=project_id,
                    job_id=job_id,
                ).strip(),
                "facts": override_facts,
                "meta": [],
                "raw": _serialize_raw(entry, project_id=project_id, job_id=job_id),
                "developer_hint": (
                    "Confirm that the override still matches the intended execution policy before rerunning the job."
                ),
            }
        )
    decisions.sort(key=lambda item: str(item.get("timestamp") or ""), reverse=True)
    return decisions


def _build_diagnostics(
    *,
    job: dict[str, Any],
    runtime: dict[str, Any],
    approval_gate: dict[str, Any],
    causal_chain: dict[str, Any],
    incident: dict[str, Any] | None = None,
    job_errors: list[dict[str, Any]],
    messages: list[dict[str, Any]],
    events: list[dict[str, Any]],
    thread_state: dict[str, Any] | None = None,
    runtime_journal: list[dict[str, Any]] | None = None,
    previous_decisions: list[dict[str, Any]] | None = None,
    decision_overrides: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    incident = incident or {}
    runtime_journal = runtime_journal or []
    previous_decisions = previous_decisions or []
    decision_overrides = decision_overrides or []
    project_id = _entity_id(job.get("project_id"))
    job_id = _entity_id(job.get("id"))
    ts = thread_state or {}
    failure_summary = _normalize_dict(job.get("failure_summary"))
    root_cause = _normalize_dict(incident.get("root_cause"))
    job_status = str(job.get("status") or "").strip().lower()
    has_actionable_root_cause = _root_cause_has_signal(root_cause)
    has_actionable_failure_summary = _failure_summary_has_signal(failure_summary)
    has_failure_status = job_status in {"failed", "max_iterations"}
    next_action = _field_text(
        failure_summary.get("next_action"),
        project_id=project_id,
        job_id=job_id,
        limit=220,
    ) or _field_text(
        failure_summary.get("remediation"),
        project_id=project_id,
        job_id=job_id,
        limit=220,
    )
    if _is_low_signal_diagnostic_text(next_action):
        next_action = ""
    if not next_action:
        if ts.get("needs_user_approval") or job_status == "awaiting_approval":
            next_action = "Open the review workspace and continue from the active approval gate."
        elif (
            has_actionable_root_cause
            or has_actionable_failure_summary
            or job_errors
            or str(job.get("error") or "").strip()
            or has_failure_status
        ):
            next_action = "Review the diagnostics below, then decide whether to retry or escalate."
        else:
            next_action = ""

    if ts.get("needs_user_approval"):
        _needs_approval_value = "Yes"
    elif "needs_user_approval" in ts:
        _needs_approval_value = "No"
    else:
        _needs_approval_value = NOT_RECORDED

    facts = [
        {"key": "Job ID", "value": str(job.get("id") or NOT_RECORDED)},
        {"key": "Thread ID", "value": str(job.get("thread_id") or NOT_RECORDED)},
        {"key": "Workflow", "value": str(runtime.get("selected_workflow") or NOT_RECORDED)},
        {
            "key": "Current step",
            "value": _humanize_identifier(runtime.get("current_step_id")) or NOT_RECORDED,
        },
        {
            "key": "Current group",
            "value": _humanize_identifier(runtime.get("current_group_id")) or NOT_RECORDED,
        },
        {
            "key": "Active agent",
            "value": _humanize_identifier(runtime.get("active_agent")) or NOT_RECORDED,
        },
        {"key": "Wait reason", "value": str(runtime.get("wait_reason") or NOT_RECORDED)},
        {"key": "Retry count", "value": str(runtime.get("retry_count") or 0)},
        # LangGraph checkpoint fields
        {
            "key": "State status",
            "value": _humanize_identifier(ts.get("status")) or NOT_RECORDED,
        },
        {
            "key": "Complexity",
            "value": _humanize_identifier(ts.get("complexity")) or NOT_RECORDED,
        },
        {
            "key": "Step execution",
            "value": str(
                _normalize_dict(ts.get("run_cursor")).get("step_execution_id") or NOT_RECORDED
            ),
        },
        {
            "key": "Workflow path",
            "value": (
                " → ".join(str(s) for s in ts["workflow_path"])
                if isinstance(ts.get("workflow_path"), list) and ts["workflow_path"]
                else NOT_RECORDED
            ),
        },
        {
            "key": "Needs approval",
            "value": _needs_approval_value,
        },
    ]

    causal_nodes = _normalize_rows(causal_chain.get("nodes"))
    causal_sequence: list[dict[str, Any]] = []
    if causal_nodes:
        for node in causal_nodes[:8]:
            title = (
                str(node.get("title") or "").strip()
                or str(node.get("label") or "").strip()
                or _humanize_identifier(node.get("type") or node.get("kind"))
                or "Workflow event"
            )
            summary = (
                str(node.get("summary") or "").strip()
                or str(node.get("reason") or "").strip()
                or _humanize_identifier(node.get("type") or node.get("kind"))
            )
            causal_sequence.append({"title": title, "summary": summary})
    else:
        for message in messages[-5:]:
            causal_sequence.append(
                {
                    "title": _humanize_identifier(message.get("step_id")) or WORKFLOW_STEP_VALUE,
                    "summary": _message_summary(message),
                }
            )

    evidence_cards: list[dict[str, Any]] = []
    last_message = messages[-1] if messages else {}
    if last_message:
        evidence_cards.append(
            {
                "title": "Latest handoff",
                "summary": _message_summary(last_message),
                "meta": _humanize_identifier(last_message.get("from_agent")) or "Agent message",
                "raw": _serialize_raw(last_message, project_id=project_id, job_id=job_id),
                "tone": "blue",
            }
        )
    last_routing = _normalize_dict(runtime.get("last_routing_decision"))
    if last_routing:
        evidence_cards.append(
            {
                "title": "Latest routing decision",
                "summary": _truncate_text(
                    str(
                        last_routing.get("title")
                        or last_routing.get("event_type")
                        or "Routing decision"
                    ),
                    limit=220,
                ),
                "meta": str(last_routing.get("timestamp") or "").strip() or "Runtime routing",
                "raw": _serialize_raw(last_routing, project_id=project_id, job_id=job_id),
                "tone": "amber",
            }
        )
    latest_error = (
        job_errors[0] if job_errors else _normalize_dict(runtime.get("last_structured_error"))
    )
    if latest_error:
        evidence_cards.append(
            {
                "title": "Latest structured error",
                "summary": _truncate_text(
                    _redact_for_ui(
                        latest_error.get("error_message")
                        or latest_error.get("response_excerpt")
                        or "Structured error captured.",
                        project_id=project_id,
                        job_id=job_id,
                    ),
                    limit=240,
                ),
                "meta": _humanize_identifier(
                    latest_error.get("category") or latest_error.get("source")
                )
                or "Error",
                "raw": _serialize_raw(latest_error, project_id=project_id, job_id=job_id),
                "tone": "red",
            }
        )
    policy_event = next(
        (
            event
            for event in events
            if str(event.get("rule_id") or "").strip() == "policy:resolution_error"
        ),
        None,
    )
    has_security_error = bool(
        latest_error
        and (
            str(latest_error.get("source") or "").strip().lower() == "security"
            or "policy" in str(latest_error.get("category") or "").strip().lower()
        )
    )
    if has_security_error or policy_event:
        event_id = (
            str(policy_event.get("id") or "").strip() if isinstance(policy_event, dict) else ""
        )
        context = _normalize_dict(latest_error.get("context_jsonb")) if latest_error else {}
        mode = str(context.get("mode") or context.get("scan_mode") or "").strip()
        summary = (
            str((latest_error or {}).get("error_message") or "").strip()
            or str((latest_error or {}).get("response_excerpt") or "").strip()
            or str((policy_event or {}).get("redacted_preview") or "").strip()
            or "Security policy resolution failed during scan execution."
        )
        evidence_cards.insert(
            0,
            {
                "title": "Security cause",
                "summary": _truncate_text(
                    _redact_for_ui(summary, project_id=project_id, job_id=job_id),
                    limit=240,
                ),
                "meta": (
                    "Policy resolution error"
                    + (f" · mode={mode}" if mode else "")
                    + (f" · event #{event_id}" if event_id else "")
                ),
                "raw": _serialize_raw(
                    {
                        "job_error": latest_error or {},
                        "security_event": policy_event or {},
                    },
                    project_id=project_id,
                    job_id=job_id,
                ),
                "tone": "red",
                "event_ref": event_id if event_id else None,
            },
        )
    if approval_gate:
        evidence_cards.append(
            {
                "title": "Current approval payload",
                "summary": _truncate_text(
                    _redact_for_ui(
                        str(
                            approval_gate.get("message")
                            or approval_gate.get("content")
                            or "Approval gate payload available."
                        ),
                        project_id=project_id,
                        job_id=job_id,
                    ),
                    limit=240,
                ),
                "meta": _humanize_identifier(approval_gate.get("stage")) or "Approval",
                "raw": _serialize_raw(approval_gate, project_id=project_id, job_id=job_id),
                "tone": "amber",
            }
        )

    raw_refs = []
    runtime_raw = _serialize_raw(runtime, project_id=project_id, job_id=job_id)
    if runtime_raw:
        raw_refs.append({"title": "Runtime snapshot", "body": runtime_raw})
    if ts:
        raw_refs.append(
            {
                "title": "LangGraph state checkpoint",
                "body": _serialize_raw(ts, project_id=project_id, job_id=job_id),
            }
        )
    if approval_gate:
        raw_refs.append(
            {
                "title": "Approval payload",
                "body": _serialize_raw(approval_gate, project_id=project_id, job_id=job_id),
            }
        )
    if events:
        raw_refs.append(
            {
                "title": f"All events ({len(events)})",
                "body": _serialize_raw(events, project_id=project_id, job_id=job_id),
            }
        )
    if messages:
        raw_refs.append(
            {
                "title": f"All messages ({len(messages)})",
                "body": _serialize_raw(messages, project_id=project_id, job_id=job_id),
            }
        )
    elif last_message:
        raw_refs.append(
            {
                "title": "Latest message envelope",
                "body": _serialize_raw(last_message, project_id=project_id, job_id=job_id),
            }
        )
    if len(job_errors) > 1:
        raw_refs.append(
            {
                "title": f"All errors ({len(job_errors)})",
                "body": _serialize_raw(job_errors, project_id=project_id, job_id=job_id),
            }
        )

    runtime_rows = _runtime_tab_rows(
        runtime_journal=runtime_journal,
        messages=messages,
        runtime=runtime,
        project_id=project_id,
        job_id=job_id,
    )
    error_rows = _error_tab_rows(
        job_errors=job_errors,
        runtime=runtime,
        project_id=project_id,
        job_id=job_id,
    )
    decision_rows = _decision_tab_rows(previous_decisions)
    override_rows = _override_tab_rows(
        decision_overrides=decision_overrides,
        project_id=project_id,
        job_id=job_id,
    )
    causal_rows = _causal_tab_rows(
        causal_chain=causal_chain,
        messages=messages,
        project_id=project_id,
        job_id=job_id,
    )

    return {
        "facts": facts,
        "root_cause": {
            "category": _humanize_identifier(root_cause.get("category")) or "Unknown",
            "message": _truncate_text(
                _redact_for_ui(
                    _first_text(
                        root_cause.get("message"),
                        failure_summary.get("summary"),
                        job.get("error"),
                        "No explicit root cause recorded.",
                    ),
                    project_id=project_id,
                    job_id=job_id,
                ),
                limit=240,
            ),
            "source": _humanize_identifier(root_cause.get("source")) or NOT_RECORDED,
            "reason_code": _first_text(root_cause.get("reason_code")) or NOT_RECORDED,
            "attempt_no": _first_text(root_cause.get("attempt_no")) or NOT_RECORDED,
            "next_action": next_action,
        },
        "causal_sequence": causal_sequence,
        "evidence_cards": evidence_cards[:4],
        "raw_refs": raw_refs,
        "tabs": {
            "errors": {"count": len(error_rows), "rows": error_rows},
            "runtime": {"count": len(runtime_rows), "rows": runtime_rows},
            "decisions": {"count": len(decision_rows), "rows": decision_rows},
            "overrides": {"count": len(override_rows), "rows": override_rows},
            "causal": {"count": len(causal_rows), "rows": causal_rows},
        },
    }
