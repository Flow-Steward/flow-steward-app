"""Shared text, evidence, and artifact helpers for semantic guardrails."""

from __future__ import annotations

import json
import logging
import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.application.orchestration.state import OrchestratorState

logger = logging.getLogger(__name__)

_MAX_CONTEXT_CHARS = 10_000
_CLAUSE_SPLIT_RE = re.compile(r"(?:and|then|plus|also)|[;,]")
_TOKEN_RE = re.compile(r"[a-z0-9]+")
_STOPWORDS = {
    "a",
    "an",
    "the",
    "and",
    "or",
    "to",
    "for",
    "of",
    "on",
    "in",
    "at",
    "with",
    "from",
    "into",
    "before",
    "after",
    "during",
    "without",
    "within",
    "over",
    "under",
    "through",
    "by",
    "is",
    "are",
    "be",
    "this",
    "that",
    "these",
    "those",
    "new",
    "minimal",
    "launch",
    "ready",
    "maintainable",
    "fresh",
}
_GENERIC_ACTION_TOKENS = {
    "create",
    "build",
    "implement",
    "prepare",
    "persist",
    "set",
    "setup",
    "stand",
    "correct",
    "fix",
    "resolve",
    "research",
    "investigate",
    "document",
    "support",
    "before",
    "handoff",
}
_TOKEN_CANONICAL_FORMS = {
    "db": "database",
    "database": "database",
    "repo": "repository",
    "repos": "repository",
    "repository": "repository",
    "repositories": "repository",
}
_REDACTION_MARKER_GUIDANCE = (
    "Evidence may contain platform safety redaction placeholders such as "
    "`[REDACTED]`, `[PII:...]`, or `[INJECTION:...:REDACTED]`. These markers are "
    "inserted by sanitizers and are not, by themselves, literal file content or "
    "proof of unrelated, malicious, PII, prompt-injection, or broken behavior. "
    "Do not fail a rule solely because redaction placeholders appear, and do not "
    "infer that a redacted URL/token is unusable solely from the placeholder. "
    "Fail only when surrounding unredacted evidence independently shows a clear "
    "violation of the active scoped work item or latest actionable feedback."
)
_EVIDENCE_TRUNCATION_GUIDANCE = (
    "Prompt evidence excerpts may be intentionally truncated for token budget. "
    "A mid-line ending or missing tail in an excerpt is not proof that the "
    "underlying artifact is truncated or broken. Do not fail solely because an "
    "excerpt is incomplete; fail only when the evidence independently proves the "
    "scoped work item is missing or incorrect."
)


def _canonical_token(token: str) -> str:
    normalized = str(token or "").strip().lower()
    return _TOKEN_CANONICAL_FORMS.get(normalized, normalized)


def _truncate_context(text: str) -> str:
    return str(text or "")[:_MAX_CONTEXT_CHARS]


def _stringify_artifact_value(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (bool, int, float)):
        return str(value)
    if isinstance(value, (dict, list)):
        try:
            return json.dumps(value, ensure_ascii=False, sort_keys=True)
        except TypeError:
            return str(value)
    return ""


def _first_non_empty_text(*values: object) -> str:
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return ""


def _stringify_scoped_work_item(value: object) -> str:
    if isinstance(value, str):
        return value.strip()
    if not isinstance(value, dict):
        return ""

    sections: list[str] = []
    identifier = _first_non_empty_text(
        value.get("id"),
        value.get("key"),
        value.get("slug"),
        value.get("code"),
    )
    title = _first_non_empty_text(
        value.get("title"),
        value.get("name"),
        value.get("summary"),
        value.get("label"),
    )
    if identifier and title:
        sections.append(f"scope: {identifier}: {title}")
    elif title:
        sections.append(f"scope: {title}")
    elif identifier:
        sections.append(f"scope: {identifier}")

    description = _first_non_empty_text(
        value.get("description"),
        value.get("details"),
        value.get("objective"),
    )
    if description:
        sections.append(f"description: {description}")

    for key in ("acceptance_criteria", "criteria", "requirements", "scope_keywords"):
        rendered = _stringify_artifact_value(value.get(key))
        if rendered:
            sections.append(f"{key}: {rendered}")

    return "\n".join(sections)


def _synthesize_generic_artifact_context(
    *,
    summary: str,
    artifacts: dict[str, object],
    include_raw_output: bool = False,
) -> str:
    """Build a generic text view from arbitrary agent artifacts.

    This keeps semantic guardrails workflow-agnostic: when a phase produces useful
    evidence under custom output names, guardrails can still reason over that
    evidence instead of only a small hard-coded set of fields.
    """
    sections: list[str] = []
    summary_text = str(summary or "").strip()
    if summary_text:
        sections.append(f"summary: {summary_text}")

    for raw_key, raw_value in (artifacts or {}).items():
        key = str(raw_key or "").strip()
        if not key or key.startswith("_"):
            continue
        if key == "raw_output" and not include_raw_output:
            continue
        value_text = _stringify_artifact_value(raw_value)
        if not value_text:
            continue
        sections.append(f"{key}: {value_text}")

    return "\n\n".join(sections)


def _collect_supporting_workflow_context(*, agent_id: str, state: OrchestratorState) -> str:
    """Collect recent successful evidence from other agents in the same workflow.

    Later-phase checks such as review or validation often summarize validation work
    instead of restating the full request. Supporting evidence from recent
    upstream phases gives semantic coverage checks the accumulated workflow
    context without hard-coding any particular workflow family. Recent
    non-pass feedback is also useful for rework iterations: semantic checks
    should understand when the latest output is intentionally cleaning up or
    reverting an earlier out-of-scope change.
    """
    from core.application.orchestration.agent_messages import get_state_messages

    evidence_sections: list[str] = []
    feedback_sections: list[str] = []
    seen_agents: set[str] = set()

    for msg in reversed(get_state_messages(state)):
        other_agent = str(msg.get("from_agent") or "").strip()
        if not other_agent or other_agent == agent_id or other_agent in seen_agents:
            continue
        status = str(msg.get("status") or "").strip().lower()
        raw_artifacts = msg.get("artifacts")
        artifacts = raw_artifacts if isinstance(raw_artifacts, dict) else {}
        synthesized = _synthesize_generic_artifact_context(
            summary=str(msg.get("summary") or ""),
            artifacts=artifacts,
            include_raw_output=True,
        )
        if not synthesized:
            continue
        seen_agents.add(other_agent)
        if status == "pass":
            evidence_sections.append(f"{other_agent} evidence:\n{synthesized}")
        elif status:
            feedback_sections.append(f"{other_agent} feedback (status={status}):\n{synthesized}")
        if len(evidence_sections) >= 3 and len(feedback_sections) >= 2:
            break

    sections = feedback_sections[:2] + evidence_sections[:3]
    return "\n\n".join(sections)


def _collect_scoped_work_item_context(state: OrchestratorState) -> str:
    """Return the active work-item context when a workflow is iterating over items.

    This keeps semantic checks portable across workflow families: if the current
    phase is operating on a narrower item than the top-level task summary, use
    that scoped item as the primary relevance target.
    """
    candidate_keys = ("current_item", "item", "record", "subject")
    candidate_containers: list[dict] = []
    state_dict = state if isinstance(state, dict) else {}
    if state_dict:
        candidate_containers.append(dict(state_dict))
        inputs = state_dict.get("inputs")
        if isinstance(inputs, dict):
            candidate_containers.append(inputs)
        materialized_inputs = state_dict.get("materialized_inputs")
        if isinstance(materialized_inputs, dict):
            candidate_containers.append(materialized_inputs)

    for container in candidate_containers:
        for key in candidate_keys:
            rendered = _stringify_scoped_work_item(container.get(key))
            if rendered:
                return rendered
    return ""


def _collect_active_feedback_context(state: OrchestratorState) -> str:
    """Return retry/rework feedback that is active for the current attempt."""
    state_dict = state if isinstance(state, dict) else {}
    if not state_dict:
        return ""

    sections: list[str] = []
    seen: set[str] = set()
    for label, key in (
        ("Current retry feedback", "feedback"),
        ("Parent workflow feedback", "invoke_parent_feedback"),
        ("Review feedback", "review_feedback"),
    ):
        text = str(state_dict.get(key) or "").strip()
        if not text or text in seen:
            continue
        sections.append(f"{label}:\n{text}")
        seen.add(text)

    violations = state_dict.get("guardrail_violations")
    if isinstance(violations, list) and violations:
        rendered: list[str] = []
        for item in violations[:5]:
            if not isinstance(item, dict):
                continue
            rule = _first_non_empty_text(item.get("rule"), item.get("display_name"))
            rule_type = _first_non_empty_text(item.get("rule_type"))
            message = _first_non_empty_text(item.get("message"))
            if not message:
                continue
            prefix = rule or rule_type or "guardrail"
            if rule_type and rule_type not in prefix:
                prefix = f"{prefix} ({rule_type})"
            rendered.append(f"- {prefix}: {message}")
        if rendered:
            sections.append("Latest guardrail violations:\n" + "\n".join(rendered))

    return "\n\n".join(sections)


def _significant_tokens(text: str) -> list[str]:
    tokens = [_canonical_token(token) for token in _TOKEN_RE.findall(str(text or "").lower())]
    return [
        token
        for token in tokens
        if len(token) >= 4 and token not in _STOPWORDS and token not in _GENERIC_ACTION_TOKENS
    ]


def _tokens_overlap(source: str, target: str) -> bool:
    """Return True when two significant tokens share the same lexical stem."""
    source_t = _canonical_token(source)
    target_t = _canonical_token(target)
    if not source_t or not target_t:
        return False
    if source_t == target_t:
        return True

    shorter, longer = sorted((source_t, target_t), key=len)
    return len(shorter) >= 5 and longer.startswith(shorter)
