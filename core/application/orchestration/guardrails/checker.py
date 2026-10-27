"""
GuardrailChecker — main entry point for post-execution guardrail validation.

Called from the step hub (step_guardrails.check_step_guardrails) when an agent
step returns a successful result. Loads applicable rules from DB, runs deterministic
checks first (fast), then semantic checks (single haiku LLM call) if needed.

Usage:
    result = check_guardrails(agent_id="writer", state=state, db_store=db_store)
    if result and not result.passed:
        # route to retry with result.feedback_text
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from core.application.management.scope import scope_from_state

if TYPE_CHECKING:
    from core.application.orchestration.state import OrchestratorState
    from core.application.registry.db_store import RegistryDBStore

logger = logging.getLogger(__name__)


@dataclass
class GuardrailViolation:
    """A single guardrail rule violation."""

    guardrail_id: str
    display_name: str
    rule_type: str
    severity: str  # "error" | "warning"
    message: str  # human-readable violation description
    details: dict = field(default_factory=dict)


@dataclass
class GuardrailResult:
    """Aggregated result of all guardrail checks for one agent execution."""

    passed: bool
    violations: list[GuardrailViolation] = field(default_factory=list)
    warnings: list[GuardrailViolation] = field(default_factory=list)

    @property
    def feedback_text(self) -> str:
        """Build feedback string for retry routing."""
        if not self.violations:
            return ""
        lines = [
            "## Guardrail Violations\n",
            "Your previous output violated the following quality rules. "
            "Please fix all violations and try again.\n",
        ]
        for v in self.violations:
            lines.append(f"- **{v.display_name}** ({v.rule_type}): {v.message}")
        if self.warnings:
            lines.append("\n### Warnings (non-blocking):\n")
            for w in self.warnings:
                lines.append(f"- {w.display_name}: {w.message}")
        return "\n".join(lines)

    def to_dicts(self) -> list[dict]:
        """Serialize violations for state storage."""
        return [
            {
                "guardrail_id": v.guardrail_id,
                "display_name": v.display_name,
                "rule_type": v.rule_type,
                "severity": v.severity,
                "message": v.message,
                "details": dict(v.details or {}),
            }
            for v in self.violations + self.warnings
        ]


def check_guardrails(
    agent_id: str,
    state: OrchestratorState,
    db_store: RegistryDBStore,
) -> GuardrailResult | None:
    """Run all applicable guardrail checks for the given agent.

    1. Load rules matching this agent + project (or global)
    2. Merge rule config with project-level overrides
    3. Run deterministic checks (fast)
    4. If semantic rules exist, run LLM checks (single haiku call)
    5. Return combined result

    Returns None if no rules exist or on error (non-fatal).
    """
    try:
        project_config = state.get("project_config") or {}
        project_id = _extract_project_id(state)
        account_id = scope_from_state(state).account_id

        rules = db_store.list_guardrails_for_agent(agent_id, project_id, account_id=account_id)
        if not rules:
            return None

        # Merge per-project config overrides into each rule
        guardrails_config = project_config.get("guardrails_config") or {}
        merged_rules = []
        for rule in rules:
            merged = dict(rule)
            rule_type = rule.get("rule_type", "")
            project_override = guardrails_config.get(rule_type) or {}
            if project_override:
                # Merge: project config overrides rule defaults
                base_config = rule.get("config") or {}
                merged["config"] = {**base_config, **project_override}
            merged_rules.append(merged)

        # Split by mode
        deterministic_rules = [r for r in merged_rules if r.get("mode") == "deterministic"]
        semantic_rules = [r for r in merged_rules if r.get("mode") == "semantic"]
        task_source = str(state.get("task_source") or "").strip().lower()
        skip_semantic_for_test_run = task_source in {"workflow_test_run", "step_test_run"}

        all_violations: list[GuardrailViolation] = []

        # ── Deterministic checks (fast) ──────────────────────────────────────
        if deterministic_rules:
            from core.application.orchestration.guardrails.deterministic import (
                run_deterministic_checks,
            )

            all_violations.extend(run_deterministic_checks(deterministic_rules, agent_id, state))

        # ── Semantic checks (haiku LLM call) ─────────────────────────────────
        # Only run if deterministic checks passed (no errors) AND semantic rules exist
        deterministic_errors = [v for v in all_violations if v.severity == "error"]
        if semantic_rules and skip_semantic_for_test_run:
            logger.info(
                "Skipping semantic guardrails for test run task_source='%s'",
                task_source,
            )
        elif semantic_rules and not deterministic_errors:
            from core.application.orchestration.guardrails.semantic import run_semantic_checks

            all_violations.extend(run_semantic_checks(semantic_rules, agent_id, state))

        # Split into errors and warnings
        errors = [v for v in all_violations if v.severity == "error"]
        warnings = [v for v in all_violations if v.severity == "warning"]

        passed = len(errors) == 0

        logger.info(
            "Guardrail check for agent '%s': %s (%d errors, %d warnings, %d rules checked)",
            agent_id,
            "PASSED" if passed else "FAILED",
            len(errors),
            len(warnings),
            len(merged_rules),
        )

        return GuardrailResult(passed=passed, violations=errors, warnings=warnings)

    except Exception as e:
        logger.warning("Guardrail check failed (non-fatal): %s", e, exc_info=True)
        return None


def _extract_project_id(state: OrchestratorState) -> str | None:
    """Extract UUID project_id from state."""
    project_config = state.get("project_config") or {}
    pid = project_config.get("project_id") or project_config.get("id")
    if pid is not None:
        normalized = str(pid).strip()
        if normalized:
            return normalized
    return None
