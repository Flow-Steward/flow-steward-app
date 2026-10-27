"""Deterministic job failure summary builder."""

from __future__ import annotations

import json
from typing import Any

from core.infrastructure.security.prompt_safety import (
    redact_diagnostic_identifier_for_persistence,
    redact_text_for_persistence,
)

FAILURE_STATUSES = frozenset(
    {
        "failed",
        "max_iterations",
        "retryable_interrupted_step",
        "cancelled",
    }
)

_KIND_TITLES: dict[str, str] = {
    "approval_rejected": "Approval rejected",
    "cancelled": "Job cancelled",
    "max_iterations": "Retry budget exhausted",
    "platform_blocker": "Flow Steward blocker",
    "provider_rate_limit": "Provider rate limit",
    "provider_error": "Provider error",
    "retryable_interrupted": "Retryable step interrupted",
    "security_policy": "Security policy failure",
    "timeout": "Runtime timeout",
    "tool_error": "Tool failure",
    "model_unavailable": "No model available for this agent",
    "workflow_guardrail": "Workflow guardrail failure",
    "workflow_stopped": "Stopped by the workflow",
    "runtime_error": "Runtime failure",
    "unknown": "Failure recorded",
}

_REMEDIATION_CONTRACTS: dict[str, dict[str, Any]] = {
    "approval_rejected": {
        "root_cause": "A human reviewer rejected the approval gate and supplied feedback for rework.",
        "next_action": "Route the feedback to the actionable producer step, rerun the affected work, then request approval again.",
        "steps": [
            "Review the rejection feedback and identify the step that must revise its output.",
            "Rerun the rework path before returning to quality or approval.",
        ],
    },
    "cancelled": {
        "root_cause": "The job was explicitly cancelled before the workflow reached a terminal success state.",
        "next_action": "Confirm the cancellation was intentional, then restart the job if the work is still required.",
        "steps": [
            "Confirm who or what cancelled the job.",
            "Restart only after checking that no duplicate job is already running.",
        ],
    },
    "max_iterations": {
        "root_cause": "The workflow exhausted its configured retry or iteration budget.",
        "next_action": "Inspect the last failed step, reduce ambiguity, then rerun with a tighter task brief or larger budget.",
        "steps": [
            "Open the runtime journal for the final repeated step.",
            "Clarify the task or increase the retry budget only after removing the repeated blocker.",
        ],
    },
    "platform_blocker": {
        "root_cause": "Workflow infrastructure or tool execution environment blocked verification; this is not automatically a task-output defect.",
        "next_action": "Use operator evidence to decide whether to proceed, retry the platform check, or escalate platform maintenance.",
        "steps": [
            "Inspect the blocked check evidence and approval comment.",
            "Do not route to a producer/rework agent unless the reviewer supplied actionable task feedback.",
        ],
    },
    "provider_rate_limit": {
        "root_cause": "The configured model provider rejected the request due to rate limit or quota pressure.",
        "next_action": "Wait for quota recovery or switch the affected agent to another configured provider, then retry.",
        "steps": [
            "Use the request ID when checking provider logs or support dashboards.",
            "Retry after the provider quota window resets or lower concurrency for the workflow.",
        ],
    },
    "provider_error": {
        "root_cause": "The configured model provider returned an error or unreachable response.",
        "next_action": "Check provider status, credentials, model configuration, and request ID before retrying.",
        "steps": [
            "Verify the provider key or CLI login used by the failed agent.",
            "Retry with the same diagnostic IDs attached if provider support is needed.",
        ],
    },
    "retryable_interrupted": {
        "root_cause": "A retryable workflow step was interrupted after a checkpoint was recorded.",
        "next_action": "Resume or replay the interrupted step from its checkpoint.",
        "steps": [
            "Open step replay details for the interrupted step.",
            "Resume from checkpoint instead of starting unrelated new work.",
        ],
    },
    "security_policy": {
        "root_cause": "A prompt, attachment, payload, or runtime action violated a configured security policy.",
        "next_action": "Remove the unsafe input or update the approved policy exception before retrying.",
        "steps": [
            "Review the policy evidence without exposing raw secret material.",
            "Retry only after the violating payload has been removed or approved.",
        ],
    },
    "timeout": {
        "root_cause": "Execution exceeded the configured timeout before the step could complete.",
        "next_action": "Check whether the task is too broad or the dependency is slow, then rerun with a narrower scope or higher timeout.",
        "steps": [
            "Inspect the active step and agent at timeout.",
            "Split broad tasks before increasing timeout limits.",
        ],
    },
    "tool_error": {
        "root_cause": "A tool or MCP call failed during workflow execution.",
        "next_action": "Verify the tool endpoint, authentication, and input payload before replaying the step.",
        "steps": [
            "Check MCP/tool service health and bearer token configuration.",
            "Replay only the failed step once the tool dependency is healthy.",
        ],
    },
    "model_unavailable": {
        "root_cause": (
            "The agent had no usable model route: none was configured, or the configured "
            "provider or extension route could not be reached."
        ),
        "next_action": (
            "Configure a model for the agent, or repair the selected provider route, then "
            "rerun the job. Retrying without changing the route will fail the same way."
        ),
        "steps": [
            "Open the agent in the registry and confirm it has an explicit provider and model.",
            "Check the provider or extension route in Settings and verify its credentials.",
            "Rerun the job only after the route resolves.",
        ],
    },
    "workflow_stopped": {
        "root_cause": "The run reached a stop step the workflow's author placed to end it as failed.",
        "next_action": (
            "This outcome is part of the workflow, not a fault: check the inputs and the step "
            "that routed here before rerunning."
        ),
        "steps": [
            "Open the step before the stop to see which condition sent the run there.",
            "Rerun only with inputs that should take the other path.",
        ],
    },
    "workflow_guardrail": {
        "root_cause": "A workflow contract, validation rule, or guardrail blocked progression.",
        "next_action": "Review the guardrail evidence and update the workflow/task inputs to satisfy the contract.",
        "steps": [
            "Identify the guardrail or contract that rejected the state.",
            "Retry after correcting the workflow input or approved registry contract.",
        ],
    },
    "runtime_error": {
        "root_cause": "The runtime recorded a terminal failure that did not map to a narrower category.",
        "next_action": "Inspect raw diagnostics and runtime journal entries, then retry once the concrete cause is identified.",
        "steps": [
            "Open job errors and runtime journal for the failed step.",
            "Capture diagnostic IDs before escalating to platform engineering.",
        ],
    },
    "unknown": {
        "root_cause": "A failure signal exists, but the deterministic classifier could not identify a narrower category.",
        "next_action": "Inspect raw diagnostics, job errors, and runtime journal entries before retrying.",
        "steps": [
            "Open the raw diagnostic surfaces for this job.",
            "Escalate with job ID and diagnostic IDs if no concrete cause is visible.",
        ],
    },
}

_RETRYABLE_STATUS_CODES = {408, 409, 425, 429, 500, 502, 503, 504}


def build_job_failure_summary(
    *,
    job: dict[str, Any],
    runtime: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a stable, redacted diagnostic summary for job detail consumers."""
    normalized_job = job if isinstance(job, dict) else {}
    normalized_runtime = runtime if isinstance(runtime, dict) else {}
    status = _normalized_text(normalized_job.get("status")).lower()
    if status in {"completed", "succeeded", "success"}:
        return _empty_failure_summary(status)
    structured_error = _normalize_dict(normalized_runtime.get("last_structured_error"))
    context = _normalize_dict(
        structured_error.get("context_jsonb")
        or structured_error.get("context")
        or structured_error.get("metadata")
    )

    message = _first_text(
        normalized_job.get("last_error"),
        normalized_job.get("error"),
        normalized_runtime.get("last_stderr_excerpt"),
        structured_error.get("error_message"),
        structured_error.get("response_excerpt"),
        normalized_runtime.get("last_retry_reason"),
        normalized_runtime.get("last_blocking_reason"),
        _agent_gate_failure_message(normalized_runtime),
    )
    has_failure_signal = (
        status in FAILURE_STATUSES
        or bool(structured_error)
        or bool(message)
        or bool(normalized_runtime.get("last_failure_class"))
        or _has_agent_gate_failure_signal(normalized_runtime)
    )
    if not has_failure_signal:
        return _empty_failure_summary(status)

    project_id = _coerce_entity_id(normalized_job.get("project_id"))
    job_id = _coerce_entity_id(normalized_job.get("id"))
    kind = _classify_failure(
        status=status,
        structured_error=structured_error,
        context=context,
        message=message,
        runtime=normalized_runtime,
    )
    retryable = _resolve_retryable(structured_error, context, runtime=normalized_runtime)
    safe_message = _redact_summary_text(message, project_id=project_id, job_id=job_id)
    if not safe_message:
        safe_message = _default_detail(kind, status)
    title = _KIND_TITLES.get(kind, _KIND_TITLES["unknown"])
    diagnostic_ids = _diagnostic_ids(
        job=normalized_job,
        runtime=normalized_runtime,
        structured_error=structured_error,
        context=context,
        project_id=project_id,
        job_id=job_id,
    )
    evidence = _evidence(
        job=normalized_job,
        runtime=normalized_runtime,
        structured_error=structured_error,
        context=context,
        retryable=retryable,
    )
    remediation = _remediation(kind)

    return {
        "present": True,
        "kind": kind,
        "status": status or "unknown",
        "title": title,
        "summary": _summary_sentence(
            kind=kind,
            title=title,
            structured_error=structured_error,
            retryable=retryable,
        ),
        "detail": _truncate(safe_message, limit=500),
        "severity": "warning" if kind == "cancelled" else "error",
        "retryable": retryable,
        "source": _first_text(structured_error.get("source"), context.get("source")),
        "category": _first_text(structured_error.get("category"), context.get("category")),
        "diagnostic_ids": diagnostic_ids,
        "evidence": evidence,
        "root_cause": remediation["root_cause"],
        "next_action": remediation["next_action"],
        "remediation": {
            "summary": remediation["next_action"],
            "steps": list(remediation["steps"]),
        },
        "evidence_refs": _evidence_refs(
            job=normalized_job,
            runtime=normalized_runtime,
            structured_error=structured_error,
            context=context,
            retryable=retryable,
        ),
        "related_workflows": _related_workflows(
            job=normalized_job,
            runtime=normalized_runtime,
            context=context,
        ),
        "raw_diagnostics_available": bool(structured_error or context or diagnostic_ids),
    }


def _empty_failure_summary(status: str) -> dict[str, Any]:
    return {
        "present": False,
        "kind": "none",
        "status": status or "unknown",
        "title": "No failure recorded",
        "summary": "",
        "detail": "",
        "severity": "info",
        "retryable": None,
        "diagnostic_ids": {},
        "evidence": [],
        "root_cause": "",
        "next_action": "",
        "remediation": {"summary": "", "steps": []},
        "evidence_refs": [],
        "related_workflows": [],
        "raw_diagnostics_available": False,
    }


def _classify_failure(
    *,
    status: str,
    structured_error: dict[str, Any],
    context: dict[str, Any],
    message: str,
    runtime: dict[str, Any],
) -> str:
    if status == "cancelled":
        return "cancelled"
    if status == "max_iterations":
        return "max_iterations"
    if status == "retryable_interrupted_step":
        return "retryable_interrupted"
    if message.lower().startswith("stopped by the workflow"):
        return "workflow_stopped"

    source = _first_text(structured_error.get("source"), context.get("source")).lower()
    category = _first_text(structured_error.get("category"), context.get("category")).lower()
    provider_code = _first_text(
        structured_error.get("provider_error_code"),
        context.get("provider_error_code"),
        context.get("code"),
    ).lower()
    provider_type = _first_text(
        structured_error.get("provider_error_type"),
        context.get("provider_error_type"),
        context.get("type"),
    ).lower()
    failure_class = _first_text(runtime.get("last_failure_class"), category).lower()
    http_status = _coerce_int(structured_error.get("http_status") or context.get("http_status"))
    combined = " ".join(
        item
        for item in (
            source,
            category,
            provider_code,
            provider_type,
            failure_class,
            message.lower(),
        )
        if item
    )

    if failure_class == "approval_rejected":
        return "approval_rejected"
    if failure_class == "platform_blocker":
        return "platform_blocker"
    if failure_class == "runtime_infrastructure":
        return "platform_blocker"
    if source == "security" or category == "policy" or "policy" in combined:
        return "security_policy"
    if http_status == 429 or any(token in combined for token in ("rate_limit", "quota")):
        return "provider_rate_limit"
    if "timeout" in combined or "timed out" in combined:
        return "timeout"
    if any(
        token in combined
        for token in (
            "bwrap",
            "bubblewrap",
            "no permissions to create a new namespace",
            "runtime_infrastructure_failed",
            "runtime infrastructure",
        )
    ):
        return "platform_blocker"
    # AC17-R70: a step that cannot obtain a model produces no valid output, so the validation
    # node records last_failure_class="contract"/"validation" and the guardrail branch below
    # claimed it. The job page then said "Workflow guardrail failure" with no evidence rows,
    # because there is no guardrail and no evidence -- the downstream symptom outranked the
    # upstream cause, and the remediation it offered ("review the guardrail evidence") cannot
    # be carried out. Checked before provider/tool/guardrail because this is a configuration
    # failure and none of those heuristics match it: model_unavailable carries no HTTP status
    # and no provider error code.
    if "model_unavailable" in combined:
        return "model_unavailable"
    if source == "provider" or category == "provider" or http_status or provider_code:
        return "provider_error"
    if source == "tool" or category == "tool" or "tool" in combined or "mcp" in combined:
        return "tool_error"
    if failure_class in {"contract", "guardrail", "validation", "quality"} or category in {
        "contract",
        "guardrail",
        "validation",
    }:
        return "workflow_guardrail"
    if status in FAILURE_STATUSES or structured_error or message:
        return "runtime_error"
    return "unknown"


def _summary_sentence(
    *,
    kind: str,
    title: str,
    structured_error: dict[str, Any],
    retryable: bool | None,
) -> str:
    provider = _first_text(structured_error.get("provider_name"))
    http_status = _coerce_int(structured_error.get("http_status"))
    provider_code = _first_text(structured_error.get("provider_error_code"))
    parts: list[str] = [title]
    if provider:
        parts.append(f"provider={provider}")
    if provider_code:
        parts.append(f"code={provider_code}")
    if http_status is not None:
        parts.append(f"http={http_status}")
    if retryable is not None:
        parts.append("retryable=yes" if retryable else "retryable=no")
    if kind == "provider_rate_limit" and not provider_code:
        parts.append("quota/rate limit signal")
    return "; ".join(parts) + "."


def _diagnostic_ids(
    *,
    job: dict[str, Any],
    runtime: dict[str, Any],
    structured_error: dict[str, Any],
    context: dict[str, Any],
    project_id: str | None,
    job_id: str | None,
) -> dict[str, str]:
    meta = _json_obj(job.get("meta"))
    last_message = _normalize_dict(runtime.get("last_message"))
    last_message_envelope = _normalize_dict(last_message.get("envelope"))
    last_message_meta = _normalize_dict(last_message_envelope.get("metadata"))
    retryable_step = _normalize_dict(runtime.get("retryable_interrupted_step"))

    values: dict[str, Any] = {
        "request_id": _first_text(
            structured_error.get("request_id"),
            context.get("request_id"),
            context.get("x_request_id"),
        ),
        "provider_error_code": _first_text(
            structured_error.get("provider_error_code"),
            context.get("provider_error_code"),
            context.get("code"),
        ),
        "provider_error_type": _first_text(
            structured_error.get("provider_error_type"),
            context.get("provider_error_type"),
            context.get("type"),
        ),
        "trace_id": _first_text(
            meta.get("trace_id"),
            context.get("trace_id"),
            last_message_meta.get("trace_id"),
        ),
        "correlation_id": _first_text(
            context.get("correlation_id"),
            last_message.get("correlation_id"),
            retryable_step.get("correlation_id"),
        ),
        "message_id": _first_text(
            structured_error.get("message_id"),
            context.get("message_id"),
            last_message.get("id"),
        ),
        "error_id": _first_text(structured_error.get("id")),
    }

    sanitized: dict[str, str] = {}
    for key, value in values.items():
        text = _first_text(value)
        if not text:
            continue
        sanitized_value = redact_diagnostic_identifier_for_persistence(
            key,
            text,
            project_id=project_id,
            job_id=job_id,
            context_type="display",
        )
        if isinstance(sanitized_value, str) and sanitized_value:
            sanitized[key] = sanitized_value
    return sanitized


def _evidence(
    *,
    job: dict[str, Any],
    runtime: dict[str, Any],
    structured_error: dict[str, Any],
    context: dict[str, Any],
    retryable: bool | None,
) -> list[dict[str, str]]:
    evidence: list[dict[str, str]] = []
    _append_evidence(evidence, "Status", _first_text(job.get("status")))
    _append_evidence(
        evidence, "Source", _first_text(structured_error.get("source"), context.get("source"))
    )
    _append_evidence(
        evidence,
        "Category",
        _first_text(structured_error.get("category"), context.get("category")),
    )
    _append_evidence(evidence, "Provider", _first_text(structured_error.get("provider_name")))
    _append_evidence(evidence, "HTTP status", _first_text(structured_error.get("http_status")))
    retryable_label = None
    if retryable is not None:
        retryable_label = "yes" if retryable else "no"
    _append_evidence(evidence, "Retryable", retryable_label)
    _append_evidence(
        evidence, "Step", _first_text(job.get("current_step_id"), runtime.get("current_step_id"))
    )
    _append_evidence(
        evidence,
        "Group",
        _first_text(job.get("current_group_id"), runtime.get("current_group_id")),
    )
    _append_evidence(
        evidence, "Agent", _first_text(job.get("current_agent"), runtime.get("active_agent"))
    )
    _append_evidence(evidence, "Failure class", _first_text(runtime.get("last_failure_class")))
    _append_evidence(evidence, "Blocking reason", _first_text(runtime.get("last_blocking_reason")))
    return evidence


def _append_evidence(rows: list[dict[str, str]], label: str, value: Any) -> None:
    text = _first_text(value)
    if not text:
        return
    rows.append({"label": label, "value": _truncate(text, limit=160)})


def _remediation(kind: str) -> dict[str, Any]:
    return dict(_REMEDIATION_CONTRACTS.get(kind) or _REMEDIATION_CONTRACTS["unknown"])


def _evidence_refs(
    *,
    job: dict[str, Any],
    runtime: dict[str, Any],
    structured_error: dict[str, Any],
    context: dict[str, Any],
    retryable: bool | None,
) -> list[str]:
    refs: list[str] = []
    _append_ref(refs, "job.status", job.get("status"))
    _append_ref(refs, "runtime.last_structured_error.source", structured_error.get("source"))
    _append_ref(refs, "runtime.last_structured_error.category", structured_error.get("category"))
    _append_ref(
        refs,
        "runtime.last_structured_error.provider_name",
        structured_error.get("provider_name"),
    )
    _append_ref(
        refs,
        "runtime.last_structured_error.http_status",
        structured_error.get("http_status") or context.get("http_status"),
    )
    _append_ref(refs, "runtime.retryable", retryable)
    _append_ref(refs, "runtime.current_step_id", runtime.get("current_step_id"))
    _append_ref(refs, "runtime.current_group_id", runtime.get("current_group_id"))
    _append_ref(refs, "runtime.active_agent", runtime.get("active_agent"))
    _append_ref(refs, "runtime.last_failure_class", runtime.get("last_failure_class"))
    _append_ref(refs, "runtime.last_blocking_reason", runtime.get("last_blocking_reason"))
    return refs


def _append_ref(refs: list[str], ref: str, value: Any) -> None:
    if value is None or value == "":
        return
    refs.append(ref)


def _related_workflows(
    *,
    job: dict[str, Any],
    runtime: dict[str, Any],
    context: dict[str, Any],
) -> list[dict[str, str]]:
    values: dict[str, str] = {}
    for source, payload in (
        ("job", job),
        ("runtime", runtime),
        ("context", context),
    ):
        for key in ("workflow_id", "selected_workflow_id", "workflow_key", "workflow_path"):
            value = _first_text(payload.get(key))
            if value:
                values.setdefault(value, source)
    return [{"workflow": workflow, "source": source} for workflow, source in values.items()]


def _resolve_retryable(
    structured_error: dict[str, Any],
    context: dict[str, Any],
    *,
    runtime: dict[str, Any],
) -> bool | None:
    for value in (
        structured_error.get("retryable"),
        context.get("retryable"),
        runtime.get("last_retry_retryable"),
    ):
        if isinstance(value, bool):
            return value
        if isinstance(value, str) and value.strip().lower() in {"true", "false"}:
            return value.strip().lower() == "true"
    http_status = _coerce_int(structured_error.get("http_status") or context.get("http_status"))
    if http_status is not None:
        return http_status in _RETRYABLE_STATUS_CODES
    return None


def _default_detail(kind: str, status: str) -> str:
    if kind == "max_iterations":
        return "The workflow stopped after exhausting its configured retry budget."
    if kind == "cancelled":
        return "The job was cancelled before completion."
    if kind == "retryable_interrupted":
        return "One workflow step was interrupted and can be retried from its checkpoint."
    return f"Job entered terminal status {status or 'unknown'} without a detailed error message."


def _agent_gate_failure_message(runtime: dict[str, Any]) -> str:
    message = runtime.get("last_message")
    if not isinstance(message, dict):
        return ""
    status = _first_text(message.get("status")).lower()
    raw_artifacts = message.get("artifacts")
    artifacts: dict[str, Any] = raw_artifacts if isinstance(raw_artifacts, dict) else {}
    verdict = _first_text(
        artifacts.get("verdict"),
        artifacts.get("status"),
    ).lower()
    if status not in {"fail", "failed", "blocked"} and verdict not in {
        "fail",
        "failed",
        "blocked",
    }:
        return ""
    parts: list[str] = []
    for key in ("summary", "content", "message"):
        text = _first_text(message.get(key))
        if text:
            parts.append(text)
    for key in ("failure_feedback", "blocking_reason", "error", "stderr"):
        text = _first_text(artifacts.get(key))
        if text:
            parts.append(text)
    for check in _iter_check_rows(artifacts.get("checks_run")):
        check_status = _first_text(check.get("status")).lower()
        if check.get("passed") is True or check_status in {"pass", "passed", "success"}:
            continue
        label = _first_text(check.get("label"), check.get("check")) or "check"
        reason = _first_text(
            check.get("blocking_reason"),
            check.get("stderr"),
            check.get("stdout"),
            check.get("status"),
        )
        parts.append(f"{label}: {reason}" if reason else f"{label} failed")
    return "; ".join(parts[:3])


def _iter_check_rows(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    if isinstance(value, dict):
        rows: list[dict[str, Any]] = []
        for check_name, raw in value.items():
            row = dict(raw) if isinstance(raw, dict) else {"status": raw}
            row.setdefault("check", check_name)
            rows.append(row)
        return rows
    return []


def _has_agent_gate_failure_signal(runtime: dict[str, Any]) -> bool:
    return bool(_agent_gate_failure_message(runtime))


def _redact_summary_text(
    value: Any,
    *,
    project_id: str | None,
    job_id: str | None,
) -> str:
    text = _first_text(value)
    if not text:
        return ""
    return redact_text_for_persistence(
        text,
        project_id=project_id,
        job_id=job_id,
        context_type="display",
    )


def _normalize_dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _json_obj(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except Exception:
            return {}
        return dict(parsed) if isinstance(parsed, dict) else {}
    return {}


def _first_text(*values: Any) -> str:
    for value in values:
        text = _normalized_text(value)
        if text:
            return text
    return ""


def _normalized_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value).strip()


def _coerce_int(value: Any) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed


def _coerce_entity_id(value: Any) -> str | None:
    normalized = str(value or "").strip()
    return normalized or None


def _truncate(value: str, *, limit: int) -> str:
    if len(value) <= limit:
        return value
    return value[: limit - 1].rstrip() + "..."
