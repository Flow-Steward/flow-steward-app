"""Decision and diagnostics builders for UI projection."""

from __future__ import annotations

import re
from typing import Any

from .ui_projection_helpers import (
    NOT_RECORDED,
    _bounded_diagnostic_value,
    _human_readable_machine_output,
    _humanize_identifier,
    _message_summary,
    _normalize_dict,
    _normalize_rows,
    _redact_for_ui,
    _serialize_raw,
    _truncate_text,
)

CORRELATION_ID_VALUE = "Correlation ID"
WORKFLOW_STEP_VALUE = "Workflow step"


def _entity_id(value: Any) -> str | None:
    normalized = str(value or "").strip()
    return normalized or None


def _first_text(*values: Any) -> str:
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return ""


_LOW_SIGNAL_DIAGNOSTIC_TEXTS = {
    "no explicit root cause recorded.",
    "review the diagnostics below, then decide whether to retry or escalate.",
}


def _is_low_signal_diagnostic_text(value: Any) -> bool:
    return str(value or "").strip().lower() in _LOW_SIGNAL_DIAGNOSTIC_TEXTS


def _first_meaningful_text(*values: Any) -> str:
    for value in values:
        text = str(value or "").strip()
        if text and not _is_low_signal_diagnostic_text(text):
            return text
    return ""


def _root_cause_has_signal(root_cause: dict[str, Any]) -> bool:
    if not isinstance(root_cause, dict) or not root_cause:
        return False
    if _first_meaningful_text(
        root_cause.get("message"),
        root_cause.get("summary"),
        root_cause.get("detail"),
        root_cause.get("error_message"),
    ):
        return True
    if _first_text(
        root_cause.get("reason_code"),
        root_cause.get("dispatch_id"),
        root_cause.get("event_id"),
    ):
        return True
    category = _first_text(root_cause.get("category")).lower()
    if category and category not in {"unknown", "none"}:
        return True
    attempt_status = _first_text(root_cause.get("attempt_status")).lower()
    return bool(attempt_status and attempt_status not in {"unknown", "none"})


def _failure_summary_has_signal(failure_summary: dict[str, Any]) -> bool:
    if not isinstance(failure_summary, dict) or not failure_summary:
        return False
    if _first_meaningful_text(
        failure_summary.get("summary"),
        failure_summary.get("message"),
        failure_summary.get("reason"),
        failure_summary.get("code"),
        failure_summary.get("error"),
    ):
        return True
    remediation = _normalize_dict(failure_summary.get("remediation"))
    next_action_text = _field_text(failure_summary.get("next_action"))
    remediation_text = _field_text(remediation)
    return bool(
        (next_action_text and not _is_low_signal_diagnostic_text(next_action_text))
        or (remediation_text and not _is_low_signal_diagnostic_text(remediation_text))
    )


_CAUSAL_TOKEN_RE = re.compile(
    r"^(?:[a-z]\d+|[0-9a-f]{8,}(?:-[0-9a-f]{4,})*|"
    r"(?:event|message|node|edge|span|run|corr|correlation|trace|step)[-_:]?[a-z0-9_.:-]*(?:#\d+)?)$",
    re.IGNORECASE,
)


def _field_text(
    value: Any,
    *,
    project_id: str | None = None,
    job_id: str | None = None,
    limit: int = 160,
) -> str:
    if value in (None, "", {}, []):
        return ""
    if isinstance(value, list):
        parts: list[str] = []
        for item in value:
            text = _field_text(item, project_id=project_id, job_id=job_id, limit=limit)
            if text and text not in parts:
                parts.append(text)
        return _truncate_text(", ".join(parts[:4]), limit=limit) if parts else ""
    redacted = _redact_for_ui(value, project_id=project_id, job_id=job_id)
    text = _human_readable_machine_output(redacted, limit=limit)
    if text:
        return text
    if isinstance(redacted, dict):
        for key in (
            "label",
            "name",
            "title",
            "path",
            "lane_key",
            "status_reason",
            "result",
            "reason",
        ):
            nested = _field_text(
                redacted.get(key),
                project_id=project_id,
                job_id=job_id,
                limit=limit,
            )
            if nested:
                return nested
        return ""
    text = str(redacted).strip()
    return _truncate_text(text, limit=limit) if text else ""


def _bounded_tab_text(
    value: Any,
    *,
    project_id: str | None,
    job_id: str | None,
    limit: int,
) -> str:
    safe_value = _bounded_diagnostic_value(
        str(value or ""),
        project_id=project_id,
        job_id=job_id,
    )
    return _truncate_text(safe_value, limit=limit).strip()


def _fact(
    label: str,
    value: Any,
    *,
    project_id: str | None = None,
    job_id: str | None = None,
    limit: int = 160,
) -> dict[str, str] | None:
    text = _field_text(value, project_id=project_id, job_id=job_id, limit=limit)
    if not text or text == NOT_RECORDED:
        return None
    return {"label": label, "value": text}


def _fact_rows(
    candidates: list[tuple[str, Any]],
    *,
    project_id: str | None = None,
    job_id: str | None = None,
    limit: int = 160,
) -> list[dict[str, str]]:
    facts: list[dict[str, str]] = []
    seen: set[str] = set()
    for label, value in candidates:
        item = _fact(label, value, project_id=project_id, job_id=job_id, limit=limit)
        if item is None:
            continue
        key = label.strip().lower()
        if key in seen:
            continue
        seen.add(key)
        facts.append(item)
    return facts


def _joined_values(*values: Any) -> str:
    parts: list[str] = []
    for value in values:
        if isinstance(value, dict):
            parts.extend(str(key).strip() for key in value if str(key).strip())
            continue
        if isinstance(value, list):
            parts.extend(str(item).strip() for item in value if str(item).strip())
            continue
        text = str(value or "").strip()
        if text:
            parts.append(text)
    unique_parts: list[str] = []
    for part in parts:
        if part not in unique_parts:
            unique_parts.append(part)
    return ", ".join(unique_parts[:6])


def _causal_is_raw_token(value: Any) -> bool:
    text = str(value or "").strip()
    if not text:
        return True
    if text.startswith("#") and " " in text:
        return False
    if " " in text:
        return False
    return bool(_CAUSAL_TOKEN_RE.match(text))


def _clean_causal_label(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if text.startswith("#") and " " in text:
        text = text.split(" ", 1)[1]
    trimmed = text.rstrip()
    opening_bracket = trimmed.rfind("[")
    if opening_bracket > 0 and trimmed.endswith("]") and trimmed[opening_bracket - 1].isspace():
        text = trimmed[:opening_bracket].strip()
    text = text.replace("event:", "").replace("message:", "")
    if _causal_is_raw_token(text):
        return ""
    if ":" in text or "_" in text or "-" in text:
        return _humanize_identifier(text.replace(":", " "))
    return text


def _causal_actor_skill(entry: dict[str, Any]) -> str:
    actor = _humanize_identifier(
        _first_text(
            entry.get("producer_display_name"),
            entry.get("agent_display_name"),
            entry.get("from_agent"),
            entry.get("agent"),
            entry.get("agent_id"),
        )
    )
    skill = _humanize_identifier(
        _first_text(
            entry.get("skill"),
            entry.get("skill_id"),
            entry.get("tool"),
            entry.get("tool_name"),
        ).replace(":", " ")
    )
    if actor and skill:
        return f"{actor}: {skill}"
    return skill or actor


def _causal_node_title(node: dict[str, Any], index: int) -> str:
    message = _normalize_dict(node.get("message"))
    event = _normalize_dict(node.get("event"))
    for candidate in (
        event.get("title"),
        _causal_actor_skill({**node, **message}),
        node.get("title"),
        _clean_causal_label(node.get("label")),
        _humanize_identifier(event.get("event_type") or node.get("type") or node.get("kind")),
    ):
        text = str(candidate or "").strip()
        if text:
            return _truncate_text(text, limit=220)
    return f"Causal item {index + 1}"


def _causal_node_summary(node: dict[str, Any]) -> str:
    message = _normalize_dict(node.get("message"))
    event = _normalize_dict(node.get("event"))
    return _truncate_text(
        _field_text(
            _first_text(
                message.get("summary"),
                event.get("summary"),
                event.get("message"),
                node.get("summary"),
                node.get("reason"),
                node.get("detail"),
                node.get("result"),
                _humanize_identifier(
                    event.get("event_type") or node.get("type") or node.get("kind")
                ),
            ),
            limit=220,
        )
        or "Causal evidence was recorded for this run.",
        limit=220,
    )


def _causal_label_map(nodes: list[dict[str, Any]]) -> dict[str, str]:
    labels: dict[str, str] = {}
    for index, node in enumerate(nodes):
        title = _causal_node_title(node, index)
        message = _normalize_dict(node.get("message"))
        event = _normalize_dict(node.get("event"))
        for value in (
            node.get("node_id"),
            node.get("id"),
            node.get("ref"),
            node.get("label"),
            node.get("event_id"),
            node.get("message_id"),
            node.get("correlation_id"),
            message.get("id"),
            message.get("message_id"),
            event.get("id"),
            event.get("event_id"),
        ):
            key = str(value or "").strip()
            if key:
                labels[key] = title
    return labels


def _causal_endpoint_label(value: Any, labels: dict[str, str], fallback: str) -> str:
    raw = str(value or "").strip()
    if raw and raw in labels:
        return labels[raw]
    cleaned = _clean_causal_label(raw)
    if cleaned:
        return cleaned
    return fallback


def _runtime_tab_rows(
    *,
    runtime_journal: list[dict[str, Any]],
    messages: list[dict[str, Any]],
    runtime: dict[str, Any],
    project_id: str | None,
    job_id: str | None,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for index, entry in enumerate(runtime_journal[:16]):
        event_name = (
            _humanize_identifier(entry.get("event") or entry.get("type") or entry.get("kind"))
            or "Runtime update"
        )
        detail = _normalize_dict(entry.get("detail"))
        reason = _first_text(entry.get("reason"), detail.get("reason"), detail.get("message"))
        status_label = _humanize_identifier(
            entry.get("status") or detail.get("target_status") or entry.get("level")
        )
        target_status = _humanize_identifier(
            detail.get("target_status") or entry.get("target_status")
        )
        transition = ""
        if status_label and target_status and status_label != target_status:
            transition = f"{status_label} -> {target_status}"
        elif target_status:
            transition = target_status
        facts = _fact_rows(
            [
                ("Group", _first_text(entry.get("group_id"), detail.get("group_id"))),
                (
                    "Agent",
                    _first_text(
                        entry.get("agent_display_name"),
                        entry.get("from_agent"),
                        entry.get("agent"),
                        detail.get("target_agent"),
                        detail.get("agent"),
                    ),
                ),
                (
                    "Step",
                    _first_text(
                        entry.get("step_id"),
                        entry.get("scope_id"),
                        detail.get("step_id"),
                        detail.get("scope_id"),
                    ),
                ),
                ("Transition", transition),
                (
                    CORRELATION_ID_VALUE,
                    _first_text(entry.get("correlation_id"), detail.get("correlation_id")),
                ),
                (
                    "Caused by",
                    _first_text(
                        entry.get("caused_by"), detail.get("caused_by"), detail.get("trigger")
                    ),
                ),
                (
                    "Artifact keys",
                    _joined_values(
                        entry.get("artifact_keys"),
                        detail.get("artifact_keys"),
                        _normalize_dict(entry.get("artifacts")),
                        _normalize_dict(detail.get("artifacts")),
                    ),
                ),
            ],
            project_id=project_id,
            job_id=job_id,
        )
        summary = _truncate_text(
            _redact_for_ui(
                _first_text(
                    entry.get("message"),
                    entry.get("summary"),
                    reason,
                    detail.get("reason"),
                    detail.get("message"),
                    event_name,
                ),
                project_id=project_id,
                job_id=job_id,
            ),
            limit=220,
        )
        rows.append(
            {
                "id": _first_text(entry.get("id"), entry.get("entry_id"), f"runtime-{index}"),
                "title": event_name,
                "status": status_label or "Recorded",
                "summary": summary or f"{event_name} was recorded for this run.",
                "detail": _truncate_text(
                    _redact_for_ui(reason or event_name, project_id=project_id, job_id=job_id),
                    limit=220,
                ),
                "facts": facts,
                "developer_hint": (
                    "Use the step ID or correlation ID below to trace the same transition across logs, events, and the step inspector."
                    if any(fact["label"] in {"Step", CORRELATION_ID_VALUE} for fact in facts)
                    else ""
                ),
                "timestamp": _first_text(
                    entry.get("timestamp"),
                    entry.get("created_at"),
                    entry.get("occurred_at"),
                ),
                "source": "runtime_journal",
                "raw": _serialize_raw(entry, project_id=project_id, job_id=job_id),
            }
        )
    if rows:
        return rows
    for index, message in enumerate(messages[-8:]):
        facts = _fact_rows(
            [
                ("Step", _first_text(message.get("step_id"))),
                ("Group", _first_text(message.get("group_id"))),
                (
                    "Agent",
                    _first_text(
                        message.get("producer_display_name"),
                        message.get("from_agent"),
                        message.get("agent"),
                    ),
                ),
                ("Skill", _first_text(message.get("skill"), message.get("tool"))),
                (
                    CORRELATION_ID_VALUE,
                    _first_text(message.get("correlation_id"), message.get("message_id")),
                ),
            ],
            project_id=project_id,
            job_id=job_id,
        )
        rows.append(
            {
                "id": _first_text(message.get("id"), message.get("message_id"), f"message-{index}"),
                "title": _humanize_identifier(message.get("step_id")) or WORKFLOW_STEP_VALUE,
                "status": _humanize_identifier(message.get("status")) or "Recorded",
                "summary": _truncate_text(
                    _redact_for_ui(
                        _message_summary(message),
                        project_id=project_id,
                        job_id=job_id,
                    ),
                    limit=220,
                ),
                "detail": _truncate_text(
                    _redact_for_ui(
                        _first_text(message.get("content"), message.get("content_preview")),
                        project_id=project_id,
                        job_id=job_id,
                    ),
                    limit=220,
                ),
                "facts": facts,
                "developer_hint": (
                    "Cross-check this handoff against the selected step if the execution waterfall and runtime feed disagree."
                    if facts
                    else ""
                ),
                "timestamp": _first_text(message.get("timestamp"), message.get("created_at")),
                "source": "message",
                "raw": _serialize_raw(message, project_id=project_id, job_id=job_id),
            }
        )
    if rows:
        return rows
    wait_reason = _first_text(runtime.get("wait_reason"))
    if wait_reason:
        rows.append(
            {
                "id": "runtime-wait-reason",
                "title": "Current runtime state",
                "status": "Waiting",
                "summary": _truncate_text(
                    _redact_for_ui(wait_reason, project_id=project_id, job_id=job_id),
                    limit=220,
                ),
                "detail": "No persisted runtime journal entries were recorded for this run.",
                "facts": _fact_rows(
                    [
                        ("Step", runtime.get("current_step_id")),
                        ("Group", runtime.get("current_group_id")),
                        ("Wait reason", wait_reason),
                    ],
                    project_id=project_id,
                    job_id=job_id,
                ),
                "developer_hint": "Review the wait reason and approval workspace before retrying or cancelling this run.",
                "timestamp": "",
                "source": "runtime",
                "raw": _serialize_raw(runtime, project_id=project_id, job_id=job_id),
            }
        )
    return rows


def _error_tab_rows(
    *,
    job_errors: list[dict[str, Any]],
    runtime: dict[str, Any],
    project_id: str | None,
    job_id: str | None,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    error_rows = job_errors[:12]
    if not error_rows:
        structured = _normalize_dict(runtime.get("last_structured_error"))
        if structured:
            error_rows = [structured]
    for index, entry in enumerate(error_rows):
        category = _humanize_identifier(entry.get("category") or entry.get("source")) or "Error"
        summary = _truncate_text(
            _redact_for_ui(
                _first_text(
                    entry.get("error_message"),
                    entry.get("response_excerpt"),
                    entry.get("message"),
                    entry.get("summary"),
                    category,
                ),
                project_id=project_id,
                job_id=job_id,
            ),
            limit=240,
        )
        facts = _fact_rows(
            [
                ("Source", _first_text(entry.get("source"))),
                ("Code", _first_text(entry.get("code"), entry.get("error_code"))),
                (
                    CORRELATION_ID_VALUE,
                    _first_text(entry.get("correlation_id"), entry.get("request_id")),
                ),
                (
                    "Mode",
                    _first_text(
                        _normalize_dict(entry.get("context_jsonb")).get("mode"),
                        _normalize_dict(entry.get("context_jsonb")).get("scan_mode"),
                    ),
                ),
            ],
            project_id=project_id,
            job_id=job_id,
        )
        rows.append(
            {
                "id": _first_text(entry.get("error_id"), entry.get("id"), f"error-{index}"),
                "title": category,
                "status": "Error",
                "summary": summary or f"{category} was recorded for this run.",
                "detail": _truncate_text(
                    _redact_for_ui(
                        _first_text(entry.get("source"), entry.get("reason"), entry.get("code")),
                        project_id=project_id,
                        job_id=job_id,
                    ),
                    limit=220,
                ),
                "facts": facts,
                "developer_hint": (
                    "Inspect the structured payload for upstream context before retrying this run."
                    if facts
                    else ""
                ),
                "timestamp": _first_text(entry.get("created_at"), entry.get("occurred_at")),
                "source": "job_error",
                "raw": _serialize_raw(entry, project_id=project_id, job_id=job_id),
            }
        )
    return rows


def _override_tab_rows(
    *,
    decision_overrides: list[dict[str, Any]],
    project_id: str | None,
    job_id: str | None,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for index, entry in enumerate(decision_overrides[:12]):
        domain = _bounded_tab_text(
            entry.get("domain"),
            project_id=project_id,
            job_id=job_id,
            limit=120,
        )
        title = _humanize_identifier(domain) or "Decision override"
        summary = _bounded_tab_text(
            _first_text(
                entry.get("reason"),
                entry.get("summary"),
                entry.get("override_value"),
                "An override was recorded for this run.",
            ),
            project_id=project_id,
            job_id=job_id,
            limit=220,
        )
        override_key = _bounded_tab_text(
            entry.get("override_key"),
            project_id=project_id,
            job_id=job_id,
            limit=160,
        )
        actor = _bounded_tab_text(
            entry.get("created_by"),
            project_id=project_id,
            job_id=job_id,
            limit=160,
        )
        original_value = _serialize_raw(
            entry.get("original_value"), project_id=project_id, job_id=job_id
        )
        override_value = _serialize_raw(
            entry.get("override_value"), project_id=project_id, job_id=job_id
        )
        rows.append(
            {
                "id": _bounded_tab_text(
                    _first_text(entry.get("id"), override_key, f"override-{index}"),
                    project_id=project_id,
                    job_id=job_id,
                    limit=160,
                ),
                "title": title,
                "status": "Active" if bool(entry.get("active")) else "Recorded",
                "summary": summary,
                "detail": _bounded_tab_text(
                    _first_text(original_value, override_value),
                    project_id=project_id,
                    job_id=job_id,
                    limit=220,
                ),
                "facts": _fact_rows(
                    [
                        ("Domain", domain),
                        ("Override key", override_key),
                        ("Actor", actor),
                        ("Original", original_value),
                        ("Override", override_value),
                    ],
                    project_id=project_id,
                    job_id=job_id,
                ),
                "developer_hint": (
                    "Confirm that this override still matches the intended execution policy before rerunning the job."
                ),
                "timestamp": _bounded_tab_text(
                    _first_text(entry.get("created_at"), entry.get("updated_at")),
                    project_id=project_id,
                    job_id=job_id,
                    limit=80,
                ),
                "source": "decision_override",
                "raw": _serialize_raw(entry, project_id=project_id, job_id=job_id),
            }
        )
    return rows


def _decision_tab_rows(decisions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [dict(entry) for entry in decisions[:12] if isinstance(entry, dict)]


def _causal_tab_rows(
    *,
    causal_chain: dict[str, Any],
    messages: list[dict[str, Any]],
    project_id: str | None,
    job_id: str | None,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    nodes = _normalize_rows(causal_chain.get("nodes"))
    edges = _normalize_rows(causal_chain.get("edges"))
    labels = _causal_label_map(nodes)
    for index, entry in enumerate(nodes[:8]):
        message = _normalize_dict(entry.get("message"))
        event = _normalize_dict(entry.get("event"))
        rows.append(
            {
                "id": _first_text(entry.get("node_id"), entry.get("id"), f"causal-node-{index}"),
                "title": _causal_node_title(entry, index),
                "status": "Observed",
                "summary": _causal_node_summary(entry),
                "detail": _truncate_text(
                    _field_text(
                        _first_text(
                            message.get("result"), event.get("message"), entry.get("detail")
                        ),
                        project_id=project_id,
                        job_id=job_id,
                        limit=220,
                    ),
                    limit=220,
                ),
                "facts": _fact_rows(
                    [
                        ("Step", _first_text(message.get("step_id"), entry.get("step_id"))),
                        ("Group", _first_text(message.get("group_id"), entry.get("group_id"))),
                        ("Agent", _first_text(message.get("from_agent"), entry.get("agent"))),
                        ("Skill", _first_text(message.get("skill"), entry.get("tool"))),
                        (
                            "Event type",
                            _first_text(
                                event.get("event_type"), entry.get("type"), entry.get("kind")
                            ),
                        ),
                        ("Status", _first_text(message.get("status"), entry.get("status"))),
                        (
                            CORRELATION_ID_VALUE,
                            _first_text(entry.get("correlation_id"), message.get("correlation_id")),
                        ),
                    ],
                    project_id=project_id,
                    job_id=job_id,
                ),
                "developer_hint": (
                    "Trace this node together with its upstream and downstream links to confirm where the run changed direction."
                ),
                "timestamp": _first_text(
                    message.get("timestamp"), event.get("timestamp"), entry.get("timestamp")
                ),
                "source": "causal_node",
                "raw": _serialize_raw(entry, project_id=project_id, job_id=job_id),
            }
        )
    for index, entry in enumerate(edges[:8]):
        source_label = _causal_endpoint_label(
            entry.get("source") or entry.get("from") or entry.get("source_id"),
            labels,
            f"Earlier item {index + 1}",
        )
        target_label = _causal_endpoint_label(
            entry.get("target") or entry.get("to") or entry.get("target_id"),
            labels,
            f"Later item {index + 1}",
        )
        relation = _clean_causal_label(
            _first_text(
                entry.get("reason"),
                entry.get("label"),
                entry.get("relationship"),
                entry.get("type"),
            )
        )
        summary = (
            f"{source_label} led to {target_label}: {relation}."
            if relation
            else f"{source_label} led to {target_label}."
        )
        rows.append(
            {
                "id": _first_text(entry.get("id"), f"causal-edge-{index}"),
                "title": "Causal link",
                "status": "Linked",
                "summary": _truncate_text(summary, limit=220),
                "detail": "",
                "facts": _fact_rows(
                    [
                        ("Source", source_label),
                        ("Target", target_label),
                        ("Relation", relation),
                    ],
                    project_id=project_id,
                    job_id=job_id,
                ),
                "developer_hint": "Review the upstream item first, then validate the downstream effect.",
                "timestamp": "",
                "source": "causal_edge",
                "raw": _serialize_raw(entry, project_id=project_id, job_id=job_id),
            }
        )
    if rows:
        return rows[:12]
    for index, message in enumerate(messages[-5:]):
        rows.append(
            {
                "id": _first_text(
                    message.get("id"), message.get("message_id"), f"causal-message-{index}"
                ),
                "title": _humanize_identifier(message.get("step_id")) or WORKFLOW_STEP_VALUE,
                "status": _humanize_identifier(message.get("status")) or "Observed",
                "summary": _truncate_text(_message_summary(message), limit=220),
                "detail": "",
                "facts": _fact_rows(
                    [
                        ("Agent", _first_text(message.get("from_agent"), message.get("agent"))),
                        ("Skill", _first_text(message.get("skill"))),
                    ],
                    project_id=project_id,
                    job_id=job_id,
                ),
                "developer_hint": "Compare this message with the execution waterfall to see how the run progressed.",
                "timestamp": _first_text(message.get("timestamp"), message.get("created_at")),
                "source": "message",
                "raw": _serialize_raw(message, project_id=project_id, job_id=job_id),
            }
        )
    return rows
