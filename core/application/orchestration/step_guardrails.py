"""Quality guardrails on a successful agent step result.

Guardrails are authored for AI agent output; deterministic executors are never
checked. A result is checked once per step execution and attempt. Violations
regenerate the same execution with feedback until the guardrail budget is
spent, then the run pauses for a recovery approval. There is no hidden reroute
to another step: a different continuation must be an explicit graph edge.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from core.application.orchestration.active_step import ActiveStep
from core.application.workflows.workflow_step_kinds import STEP_KIND_AGENT, normalize_step_kind

logger = logging.getLogger(__name__)

GUARDRAIL_PASS = "pass"
GUARDRAIL_RETRY = "retry"
GUARDRAIL_ESCALATE = "escalate"


@dataclass(frozen=True)
class GuardrailVerdict:
    action: str
    update: dict[str, Any] = field(default_factory=dict)
    feedback: str = ""
    reason: str = ""
    detail: dict[str, Any] = field(default_factory=dict)


def _violation_payloads(violations: list[Any]) -> list[dict[str, Any]]:
    return [
        {
            "rule": getattr(item, "display_name", ""),
            "message": getattr(item, "message", ""),
            "rule_type": getattr(item, "rule_type", ""),
            "details": dict(getattr(item, "details", {}) or {}),
        }
        for item in violations
    ]


def _feedback_text(violations: list[Any]) -> str:
    text = "## Guardrail Violations\n\n"
    text += (
        "Your output violated the following quality guardrails. Fix ALL violations and retry:\n\n"
    )
    for item in violations:
        text += f"- **{item.display_name}**: {item.message}\n"
    return text


def _guardrail_budget(step: Mapping[str, Any]) -> int:
    """The step's own retry allowance plus one guardrail regeneration."""
    policy = step.get("error_policy")
    try:
        retries = int(policy.get("max_retries") or 0) if isinstance(policy, Mapping) else 0
    except (TypeError, ValueError):
        retries = 0
    return max(0, retries) + 1


def check_step_guardrails(
    state: Mapping[str, Any],
    current: ActiveStep,
    message: Mapping[str, Any],
    *,
    emit_event: Any,
) -> GuardrailVerdict | None:
    """Check an accepted successful agent result; None when nothing applies.

    A failure to run the checker is logged and never blocks the run, as before.
    """
    if (
        normalize_step_kind(current.step.get("step_kind"), default=STEP_KIND_AGENT)
        != STEP_KIND_AGENT
    ):
        return None
    agent_id = str(message.get("from_agent") or "").strip()
    if not agent_id:
        return None
    check_key = f"{current.step_execution_id}:{current.attempt}"
    results = state.get("guardrail_check_results")
    results = dict(results) if isinstance(results, Mapping) else {}
    previous = results.get(check_key)
    if isinstance(previous, Mapping) and previous.get("status") == "pass":
        return None
    try:
        from core.application.orchestration.guardrails.checker import check_guardrails
        from core.application.registry.registry_manager import RegistryManager

        registry = RegistryManager.instance()
        if not registry._db_store:
            return None
        result = check_guardrails(agent_id, dict(state), registry._db_store)  # type: ignore[arg-type]
    except Exception as exc:
        logger.warning("Guardrail check failed (non-fatal): %s", exc)
        return None
    violations = list(getattr(result, "violations", []) or []) if result is not None else []
    warnings = list(getattr(result, "warnings", []) or []) if result is not None else []
    for warning in warnings:
        emit_event(
            state,
            "guardrails",
            "guardrail_warning",
            f"Guardrail warning: {warning.display_name}",
            {"rule_type": warning.rule_type, "severity": "warning", "message": warning.message},
            severity="warn",
        )
    if not violations:
        results[check_key] = {
            "status": "pass",
            "step_id": current.step_id,
            "agent_id": agent_id,
            "warnings_count": len(warnings),
        }
        return GuardrailVerdict(GUARDRAIL_PASS, update={"guardrail_check_results": results})

    for violation in violations:
        emit_event(
            state,
            "guardrails",
            "guardrail_violation",
            f"Guardrail violation: {violation.display_name}",
            {
                "rule_type": violation.rule_type,
                "severity": "error",
                "message": violation.message,
                "details": dict(getattr(violation, "details", {}) or {}),
            },
            severity="error",
        )
    rule_types = sorted(
        {str(getattr(item, "rule_type", "") or "").strip().lower() for item in violations} - {""}
    )
    retry_key = f"{current.step_execution_id}:{','.join(rule_types) or 'unknown'}"
    counts = state.get("guardrail_retry_counts")
    counts = dict(counts) if isinstance(counts, Mapping) else {}
    try:
        used = max(0, int(counts.get(retry_key) or 0))
    except (TypeError, ValueError):
        used = 0
    budget = _guardrail_budget(current.step)
    detail = {
        "violations_count": len(violations),
        "retry_key": retry_key,
        "retry_count": used,
        "retry_limit": budget,
    }
    feedback = _feedback_text(violations)
    payloads = _violation_payloads(violations)
    if used >= budget:
        return GuardrailVerdict(
            GUARDRAIL_ESCALATE,
            update={"guardrail_violations": payloads, "guardrail_retry_counts": counts},
            feedback=feedback,
            reason=f"Guardrail violations persisted after {used} retries",
            detail=detail,
        )
    counts[retry_key] = used + 1
    return GuardrailVerdict(
        GUARDRAIL_RETRY,
        update={"guardrail_violations": payloads, "guardrail_retry_counts": counts},
        feedback=feedback,
        reason=f"{len(violations)} guardrail violations",
        detail={**detail, "retry_count": used + 1},
    )


__all__ = [
    "GUARDRAIL_ESCALATE",
    "GUARDRAIL_PASS",
    "GUARDRAIL_RETRY",
    "GuardrailVerdict",
    "check_step_guardrails",
]
