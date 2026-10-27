"""
Deterministic guardrail checks — fast Python pattern matching.

Each rule_type maps to a checker function with signature:
    (config: dict, agent_id: str, state: OrchestratorState) -> list[GuardrailViolation]

New rule types: add a function and register in _DETERMINISTIC_CHECKERS.
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING

from core.application.orchestration.guardrails.checker import GuardrailViolation

if TYPE_CHECKING:
    from core.application.orchestration.state import OrchestratorState

logger = logging.getLogger(__name__)


def run_deterministic_checks(
    rules: list[dict],
    agent_id: str,
    state: OrchestratorState,
) -> list[GuardrailViolation]:
    """Run all deterministic guardrail checks.

    Args:
        rules: List of rule dicts (from DB) with mode='deterministic'.
        agent_id: The agent that just completed.
        state: Current OrchestratorState with agent outputs.

    Returns:
        List of violations (may be empty).
    """
    violations: list[GuardrailViolation] = []

    for rule in rules:
        rule_type = rule.get("rule_type", "")
        checker = _DETERMINISTIC_CHECKERS.get(rule_type)
        if not checker:
            logger.debug("No deterministic checker for rule_type '%s'", rule_type)
            continue

        try:
            config = rule.get("config") or {}
            rule_violations = checker(config, agent_id, state)
            # Stamp each violation with the rule metadata
            for v in rule_violations:
                v.guardrail_id = rule.get("id", 0)
                v.display_name = rule.get("display_name", rule_type)
                v.severity = rule.get("severity", "error")
            violations.extend(rule_violations)
        except ValueError as exc:
            violations.append(
                GuardrailViolation(
                    guardrail_id=rule.get("id", 0),
                    display_name=rule.get("display_name", rule_type),
                    rule_type=rule_type,
                    severity=rule.get("severity", "error"),
                    message=f"Invalid guardrail configuration: {exc}",
                    details={"error": str(exc)},
                )
            )
        except Exception as e:
            logger.warning("Deterministic check '%s' failed: %s", rule_type, e)

    return violations


# ═══════════════════════════════════════════════════════════════════════════════
# CHECKER IMPLEMENTATIONS
# ═══════════════════════════════════════════════════════════════════════════════


def _get_agent_output_text(agent_id: str, state: OrchestratorState) -> str:
    """Get the agent's raw output text."""
    artifacts = _get_agent_artifacts(agent_id, state)
    value = artifacts.get("raw_output")
    if isinstance(value, str):
        return value
    return ""


def _get_agent_artifacts(agent_id: str, state: OrchestratorState) -> dict[str, object]:
    """Get the latest artifacts payload produced by the agent."""
    from core.application.orchestration.agent_messages import get_state_messages

    for msg in reversed(get_state_messages(state)):
        if str(msg.get("from_agent") or "") != agent_id:
            continue
        raw_artifacts = msg.get("artifacts")
        artifacts: dict[str, object] = raw_artifacts if isinstance(raw_artifacts, dict) else {}
        if artifacts:
            return artifacts
    return {}


# ─── forbidden_patterns ───────────────────────────────────────────────────────


def check_forbidden_patterns(
    config: dict,
    agent_id: str,
    state: OrchestratorState,
) -> list[GuardrailViolation]:
    """Check that forbidden regex patterns don't appear in the agent output.

    Config: {"patterns": ["DROP TABLE", "eval\\("]}
    """
    patterns = config.get("patterns") or []
    if not patterns:
        return []

    text_to_check = _get_agent_output_text(agent_id, state)

    if not text_to_check.strip():
        return []

    violations: list[GuardrailViolation] = []
    for pattern in patterns:
        try:
            if re.search(pattern, text_to_check, re.IGNORECASE):
                violations.append(
                    GuardrailViolation(
                        guardrail_id="",
                        display_name="",
                        rule_type="forbidden_patterns",
                        severity="error",
                        message=f"Forbidden pattern '{pattern}' found in agent output.",
                        details={"pattern": pattern},
                    )
                )
        except re.error:
            logger.warning("Invalid regex pattern in forbidden_patterns: %s", pattern)

    return violations


# ─── required_sections ────────────────────────────────────────────────────────


def check_required_sections(
    config: dict,
    agent_id: str,
    state: OrchestratorState,
) -> list[GuardrailViolation]:
    """Check that agent output contains required sections/keywords.

    Config: {"sections": ["verdict", "summary"]}
    Checks for heading patterns (## Section) or keyword presence (case-insensitive).
    """
    sections = config.get("sections") or []
    if not sections:
        return []

    output = _get_agent_output_text(agent_id, state)
    artifacts = _get_agent_artifacts(agent_id, state)
    if not output and not artifacts:
        return []

    output_lower = output.lower()
    violations: list[GuardrailViolation] = []
    artifact_aliases = {
        "verdict": ("verdict",),
        "summary": ("summary", "report", "raw_output"),
    }

    for section in sections:
        section_lower = section.lower()
        # Check for markdown heading or keyword presence
        heading_pattern = rf"^#{1, 3}\s*{re.escape(section_lower)}"
        has_heading = bool(re.search(heading_pattern, output_lower, re.MULTILINE))
        has_keyword = section_lower in output_lower
        artifact_keys = artifact_aliases.get(section_lower, (section_lower,))
        has_structured_field = any(
            (isinstance(artifacts.get(key), str) and bool(str(artifacts.get(key) or "").strip()))
            or (isinstance(artifacts.get(key), (list, dict)) and bool(artifacts.get(key)))
            for key in artifact_keys
        )

        if not has_heading and not has_keyword and not has_structured_field:
            violations.append(
                GuardrailViolation(
                    guardrail_id="",
                    display_name="",
                    rule_type="required_sections",
                    severity="error",
                    message=f"Required section or keyword '{section}' not found in output.",
                    details={"section": section},
                )
            )

    return violations


_DETERMINISTIC_CHECKERS = {
    "forbidden_patterns": check_forbidden_patterns,
    "required_sections": check_required_sections,
}
