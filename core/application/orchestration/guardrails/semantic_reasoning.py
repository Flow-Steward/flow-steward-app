"""Deterministic semantic guardrail heuristics and prompt shaping."""

from __future__ import annotations

from .semantic_common import (
    _CLAUSE_SPLIT_RE,
    _EVIDENCE_TRUNCATION_GUIDANCE,
    _significant_tokens,
    _tokens_overlap,
)


def _heuristic_output_addresses_input(task_summary: str, context: dict[str, str]) -> bool | None:
    """Best-effort deterministic pass check for request-coverage guardrails.

    Returns:
    - True when a structured requirements artifact clearly addresses each request clause
    - None when there is not enough structured context to decide
    """
    target_text = (
        str(context.get("primary_text_artifact") or "").strip()
        or str(context.get("raw_output") or "").strip()
    )
    source_text = str(task_summary or "").strip()
    if not source_text or not target_text:
        return None

    target_tokens = tuple(dict.fromkeys(_significant_tokens(target_text)))
    if not target_tokens:
        return None

    clause_tokens: list[list[str]] = []
    for raw_clause in _CLAUSE_SPLIT_RE.split(source_text):
        tokens = list(dict.fromkeys(_significant_tokens(raw_clause)))
        if tokens:
            clause_tokens.append(tokens)
    if not clause_tokens:
        fallback_tokens = list(dict.fromkeys(_significant_tokens(source_text)))
        if fallback_tokens:
            clause_tokens.append(fallback_tokens)
    if not clause_tokens:
        return None

    for tokens in clause_tokens:
        required_matches = 1 if len(tokens) == 1 else 2
        matched_tokens = sum(
            1 for token in tokens if any(_tokens_overlap(token, target) for target in target_tokens)
        )
        if matched_tokens < required_matches:
            return False
    return True


def _format_rule_prompt(rule: dict, context: dict[str, str]) -> str:
    """Return rule text adapted to the evidence available in this phase."""
    prompt_text = str(rule.get("prompt_text") or "").strip()
    if not prompt_text:
        prompt_text = (
            f"Check if the agent output satisfies the rule: {rule.get('display_name', 'unnamed')}"
        )

    rule_type = str(rule.get("rule_type") or "").strip().lower()
    if rule_type != "output_addresses_input":
        return prompt_text

    scoped_priority_guidance = (
        "When a Current Scoped Work Item is present, evaluate coverage against that scoped "
        "item first. Treat broader Requirements Documents, PRDs, and top-level task wording "
        "as parent roadmap context unless they are explicitly scoped to the same item. "
        "Explicit out-of-scope statements in the scoped item or latest review feedback "
        "mean those broader parent features must not be required for this attempt. If "
        "feedback conflicts, prefer scoped-item requirements and scoped review feedback "
        "over guardrail completeness feedback inferred from broader parent context."
    )
    has_primary_artifact = bool(str(context.get("primary_text_artifact") or "").strip())
    if has_primary_artifact:
        return f"{prompt_text}\n{scoped_priority_guidance} {_EVIDENCE_TRUNCATION_GUIDANCE}"

    return (
        "Check that the available agent output and supporting workflow evidence "
        "substantively address the Current Scoped Work Item when present, otherwise the Task "
        "Description. Do not require a separate structured planning artifact "
        "unless one is present in the artifacts or explicitly required by this "
        "phase's output contract. Treat a missing dedicated planning artifact as "
        "non-blocking when other evidence demonstrates request coverage. "
        f"{scoped_priority_guidance} {_EVIDENCE_TRUNCATION_GUIDANCE}"
    )
