"""Pre-step autonomy policy, keyed by the step and its actual execution.

This is the existing project/step autonomy policy, separate from an authored
Approval step. A policy may be addressed to a step id, an ``agent:skill`` pair,
a skill or an agent; an approval opened for it belongs to one step execution,
so a retry of that execution does not ask again and a new entry does.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from core.application.orchestration.active_step import ActiveStep


def _to_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def _to_bool(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
    return bool(value)


def normalize_autonomy_mode(value: Any, fallback: str) -> str:
    raw = str(value or "").strip().lower()
    if raw in {"manual", "human", "human_only"}:
        return "manual"
    if raw in {"assisted", "assist", "supervised"}:
        return "assisted"
    if raw in {"auto", "automatic"}:
        return "auto"
    return fallback


def classification_default_mode(state: Mapping[str, Any]) -> str:
    """The autonomy mode the classifier recommends when the project sets none.

    An unavailable model classifies as ``supervised``, so it never grants
    automatic execution by default.
    """
    recommendation = str(state.get("automation_recommendation") or "").strip().lower()
    if recommendation == "human_only":
        return "manual"
    if recommendation == "supervised":
        return "assisted"
    return "auto"


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def resolve_step_autonomy_policy(
    current: ActiveStep, state: Mapping[str, Any], target_node: str
) -> dict[str, Any]:
    from core.application.orchestration.helpers import resolve_step_skill

    step = current.step
    skill = resolve_step_skill(step, state=state, default="")
    agent = str(step.get("agent") or "").strip() or str(target_node or "").strip()
    project_config = _mapping(state.get("project_config"))
    project_policy = _mapping(state.get("project_policy")) or _mapping(
        project_config.get("project_policy")
    )
    autonomy_cfg = _mapping(
        _mapping(project_config.get("orchestrator")).get("autonomy")
    ) or _mapping(project_config.get("autonomy"))
    mode_default = normalize_autonomy_mode(
        project_policy.get("autonomy_mode", autonomy_cfg.get("default_mode")),
        classification_default_mode(state),
    )
    confidence_default = _to_float(
        project_policy.get(
            "autonomy_confidence_threshold",
            autonomy_cfg.get("min_confidence", autonomy_cfg.get("assisted_min_confidence", 0.75)),
        ),
        0.75,
    )
    confidence_default = max(0.0, min(1.0, confidence_default))
    policy: dict[str, Any] = {
        "enabled": True,
        "mode": mode_default,
        "min_confidence": confidence_default,
        "require_contract_health": _to_bool(autonomy_cfg.get("require_contract_health"), True),
    }
    step_policies = _mapping(autonomy_cfg.get("step_policies") or autonomy_cfg.get("steps"))
    matched = None
    for key in (
        current.step_id,
        f"{agent}:{skill}" if agent and skill else "",
        skill,
        agent,
    ):
        if key and key in step_policies:
            matched = step_policies.get(key)
            break
    for raw_policy in (matched, step.get("autonomy")):
        if raw_policy is None:
            continue
        if isinstance(raw_policy, str):
            policy["mode"] = normalize_autonomy_mode(raw_policy, str(policy["mode"]))
            continue
        if not isinstance(raw_policy, Mapping):
            continue
        if "enabled" in raw_policy:
            policy["enabled"] = _to_bool(raw_policy.get("enabled"), bool(policy["enabled"]))
        policy["mode"] = normalize_autonomy_mode(
            raw_policy.get("mode", raw_policy.get("policy")), str(policy["mode"])
        )
        if "min_confidence" in raw_policy:
            policy["min_confidence"] = max(
                0.0,
                min(1.0, _to_float(raw_policy.get("min_confidence"), policy["min_confidence"])),
            )
        if "require_contract_health" in raw_policy:
            policy["require_contract_health"] = _to_bool(
                raw_policy.get("require_contract_health"), bool(policy["require_contract_health"])
            )
    return policy


def routing_confidence(state: Mapping[str, Any]) -> float:
    return max(0.0, min(1.0, _to_float(state.get("confidence_score"), 1.0)))


def autonomy_gate_reason(
    current: ActiveStep, state: Mapping[str, Any], target_node: str
) -> tuple[str, dict[str, Any]]:
    """Why this execution must pause before it runs, or an empty reason."""
    from core.application.orchestration.routing_node import EXPLICIT_WORKFLOW_SELECTION_SOURCES

    task_source = str(state.get("task_source") or "").strip().lower()
    if bool(state.get("test_mode")) or task_source in {"workflow_test_run", "step_test_run"}:
        return "", {}
    approvals = state.get("autonomy_gate_approvals")
    if isinstance(approvals, Mapping) and approvals.get(current.step_execution_id) is True:
        return "", {}
    policy = resolve_step_autonomy_policy(current, state, target_node)
    if not policy.get("enabled", True):
        return "", {}
    mode = normalize_autonomy_mode(policy.get("mode"), "auto")
    confidence = routing_confidence(state)
    min_confidence = _to_float(policy.get("min_confidence"), 0.75)
    reason = ""
    if mode == "manual":
        reason = "step policy mode=manual"
    elif mode == "assisted" and confidence < min_confidence:
        reason = f"step confidence {confidence:.2f} below assisted threshold {min_confidence:.2f}"
    # Confidence describes the classifier's workflow choice. A caller that named
    # the workflow made no such choice, so only mode=manual pauses those runs.
    if reason and mode != "manual":
        source = str(state.get("workflow_selection_source") or "").strip().lower()
        if source in EXPLICIT_WORKFLOW_SELECTION_SOURCES:
            return "", {}
    return reason, {
        "mode": mode,
        "confidence": confidence,
        "min_confidence": min_confidence,
    }


__all__ = [
    "autonomy_gate_reason",
    "classification_default_mode",
    "normalize_autonomy_mode",
    "resolve_step_autonomy_policy",
    "routing_confidence",
]
