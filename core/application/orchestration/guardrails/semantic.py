"""
Semantic guardrail checks — single haiku LLM call.

Batches all semantic rules into ONE prompt for cost efficiency (~$0.001).
Same pattern as validation_node._llm_validate().
"""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING

from core.application.orchestration.guardrails.checker import GuardrailViolation

from .semantic_common import (
    _EVIDENCE_TRUNCATION_GUIDANCE,
    _REDACTION_MARKER_GUIDANCE,
    _truncate_context,
    logger,
)
from .semantic_context import _collect_agent_context
from .semantic_reasoning import (
    _format_rule_prompt,
    _heuristic_output_addresses_input,
)

if TYPE_CHECKING:
    from core.application.orchestration.state import OrchestratorState


def run_semantic_checks(
    rules: list[dict],
    agent_id: str,
    state: OrchestratorState,
) -> list[GuardrailViolation]:
    """Run semantic (LLM-based) guardrail checks.

    Batches all semantic rules into a SINGLE haiku call for cost efficiency.
    Returns violations for any rules that fail.

    Args:
        rules: List of rule dicts with mode='semantic'.
        agent_id: The agent that just completed.
        state: Current OrchestratorState.

    Returns:
        List of violations (may be empty).
    """
    if not rules:
        return []

    # ── Build context ─────────────────────────────────────────────────────────
    task_summary = state.get("task_summary") or state.get("initial_requirements") or ""
    context = _collect_agent_context(agent_id, state)
    scoped_work_item = str(context.get("scoped_work_item") or "").strip()
    scoped_scope_summary = scoped_work_item or str(task_summary or "").strip()

    # Truncate for token budget
    task_summary_t = _truncate_context(task_summary)
    scoped_work_item_t = _truncate_context(scoped_work_item)
    scoped_scope_summary_t = _truncate_context(scoped_scope_summary)
    raw_output_t = _truncate_context(context.get("raw_output") or "")
    primary_text_artifact_t = _truncate_context(context.get("primary_text_artifact") or "")
    supporting_text_artifacts_t = _truncate_context(context.get("supporting_text_artifacts") or "")
    active_feedback_t = _truncate_context(context.get("active_feedback") or "")
    supporting_workflow_context_t = _truncate_context(
        context.get("supporting_workflow_context") or ""
    )

    rules_to_check: list[dict] = []
    for rule in rules:
        rule_type = str(rule.get("rule_type") or "").strip().lower()
        if rule_type == "output_addresses_input":
            heuristic_pass = _heuristic_output_addresses_input(scoped_scope_summary_t, context)
            if heuristic_pass is True:
                logger.info(
                    "Semantic guardrail '%s' auto-passed via structured request-coverage heuristic",
                    rule.get("display_name") or rule_type,
                )
                continue
        rules_to_check.append(rule)

    if not rules_to_check:
        return []

    # ── Build numbered rules list ─────────────────────────────────────────────
    rule_lines = []
    for i, rule in enumerate(rules_to_check):
        prompt_text = _format_rule_prompt(rule, context)
        rule_lines.append(f"Rule {i + 1}: {prompt_text.strip()}")

    rules_block = "\n".join(rule_lines)

    artifact_sections: list[str] = []
    if primary_text_artifact_t:
        artifact_sections.append(f"## Primary Structured Artifact\n{primary_text_artifact_t}")
    if supporting_text_artifacts_t:
        artifact_sections.append(
            f"## Supporting Structured Artifacts\n{supporting_text_artifacts_t}"
        )
    if active_feedback_t:
        artifact_sections.append(f"## Active Retry / Parent Feedback\n{active_feedback_t}")
    if supporting_workflow_context_t:
        artifact_sections.append(
            f"## Recent Workflow Feedback / Evidence\n{supporting_workflow_context_t}"
        )
    if raw_output_t and raw_output_t not in {primary_text_artifact_t, supporting_text_artifacts_t}:
        artifact_sections.append(f"## Agent Raw Output\n{raw_output_t}")
    if not artifact_sections:
        artifact_sections.append("## Agent Raw Output\n(no text output)")

    artifacts_block = "\n\n".join(artifact_sections)
    scoped_work_item_block = ""
    if scoped_work_item_t and scoped_work_item_t != task_summary_t:
        scoped_work_item_block = f"## Current Scoped Work Item\n{scoped_work_item_t}\n\n"

    # ── Build LLM prompt ──────────────────────────────────────────────────────
    prompt = f"""You are a quality guardrail checker for an AI agent.

## Task Description
{task_summary_t}

{scoped_work_item_block}{artifacts_block}

## Rules to Check
{rules_block}

For each rule, determine if it PASSED (the agent's output complies) or FAILED (violation detected).
Be strict but fair: only flag clear violations, not ambiguous cases.
{_REDACTION_MARKER_GUIDANCE}
{_EVIDENCE_TRUNCATION_GUIDANCE}
If a Current Scoped Work Item is present, use it as the primary coverage and relevance target for
focus/scope/coverage checks. Treat the top-level Task Description and broader Requirements Documents
as parent context unless they are explicitly attached to that scoped item. Explicit out-of-scope
statements in the Current Scoped Work Item or latest review feedback are binding for the current
attempt. Do not fail solely because the wording differs from the original request when the
substantive requested behavior is clearly addressed.
When no structured requirements document is present, treat available agent output and supporting
workflow evidence as the coverage evidence. Do not fail a coverage rule solely because a standalone
requirements artifact is missing unless this phase explicitly had to produce one.
If Active Retry / Parent Feedback is present, treat it as the current attempt's authoritative feedback.

Reply with ONLY a JSON object:
{{
  "results": [
    {{"rule_index": 1, "passed": true, "reason": "..."}},
    {{"rule_index": 2, "passed": false, "reason": "..."}}
  ]
}}"""

    # ── Call haiku ─────────────────────────────────────────────────────────────
    try:
        from langchain_core.messages import HumanMessage, SystemMessage

        from core.application.orchestration.model_provider import get_chat_model

        project_config = state.get("project_config") or {}
        cfg = {
            **project_config,
            "agents_config": {
                **(project_config.get("agents_config") or {}),
            },
        }
        llm = get_chat_model(cfg, agent_name="guardrail_checker")

        messages = [
            SystemMessage(content="You are a precise quality checker. Return only valid JSON."),
            HumanMessage(content=prompt),
        ]
        response = llm.invoke(messages)
        from core.application.orchestration.model_response_text import model_response_text

        content = model_response_text(response)

        return _parse_results(content, rules_to_check)

    except Exception as e:
        logger.warning("Semantic guardrail check failed (non-fatal): %s", e)
        return []


def _parse_results(
    content: str,
    rules: list[dict],
) -> list[GuardrailViolation]:
    """Parse LLM JSON response into GuardrailViolation objects."""
    violations: list[GuardrailViolation] = []

    try:
        # Extract JSON from response (may be wrapped in markdown)
        match = re.search(r"\{[\s\S]+\}", content)
        if not match:
            logger.warning("No JSON found in semantic guardrail response")
            return []

        data = json.loads(match.group(0))
        results = data.get("results") or []

        for result in results:
            idx = result.get("rule_index", 0) - 1  # 1-based → 0-based
            if idx < 0 or idx >= len(rules):
                continue

            passed = result.get("passed", True)
            if not passed:
                rule = rules[idx]
                violations.append(
                    GuardrailViolation(
                        guardrail_id=rule.get("id", 0),
                        display_name=rule.get("display_name", ""),
                        rule_type=rule.get("rule_type", "semantic"),
                        severity=rule.get("severity", "error"),
                        message=result.get("reason", "Semantic check failed"),
                        details={"rule_index": idx + 1},
                    )
                )

    except (json.JSONDecodeError, KeyError, TypeError) as e:
        logger.warning("Failed to parse semantic guardrail response: %s", e)

    return violations


__all__ = [
    "_collect_agent_context",
    "_format_rule_prompt",
    "_heuristic_output_addresses_input",
    "_parse_results",
    "run_semantic_checks",
]
