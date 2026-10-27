"""Task classifier for generic planning metadata."""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass

from core.application.orchestration.model_response_text import (
    model_response_text as response_text,
)

logger = logging.getLogger(__name__)


def _envelope_title(task_envelope: dict) -> str:
    if not isinstance(task_envelope, dict):
        return ""
    title = str(task_envelope.get("title") or "").strip()
    if title:
        return title
    inputs = task_envelope.get("inputs")
    if isinstance(inputs, dict):
        return str(inputs.get("title") or "").strip()
    return ""


def _envelope_description(task_envelope: dict) -> str:
    if not isinstance(task_envelope, dict):
        return ""
    description = str(task_envelope.get("description") or "").strip()
    if description:
        return description
    inputs = task_envelope.get("inputs")
    if isinstance(inputs, dict):
        return str(inputs.get("description") or "").strip()
    return ""


@dataclass
class TaskClassification:
    effort_hint: str = "standard"  # micro | small | standard | major
    confidence: float = 0.7  # 0.0-1.0
    risk_level: str = "medium"  # low | medium | high
    automation_recommendation: str = "supervised"  # auto | supervised | human_only
    reasoning: str = ""
    task_type: str = ""

    def __post_init__(self) -> None:
        effort = str(self.effort_hint or "").strip().lower()
        if effort not in {"micro", "small", "standard", "major"}:
            effort = "standard"
        self.effort_hint = effort

        risk = str(self.risk_level or "").strip().lower()
        if risk not in {"low", "medium", "high"}:
            risk = "medium"
        self.risk_level = risk

        self.task_type = str(self.task_type or "").strip().lower()


_CLASSIFICATION_PROMPT = """You are a task classifier for a general workflow orchestration system.

Return ONLY a JSON object with these fields:
- effort_hint: one of "micro" | "small" | "standard" | "major"
- confidence: float 0.0-1.0
- risk_level: one of "low" | "medium" | "high"
- automation_recommendation: one of "auto" | "supervised" | "human_only"
- reasoning: 1-2 sentence explanation
"""


def classify_task(
    task_envelope: dict,
    kb_context: str = "",
    project_config: dict | None = None,
    deterministic: bool = False,
) -> TaskClassification:
    if deterministic:
        return _default_classification()
    try:
        return _classify_with_llm(task_envelope, kb_context, project_config)
    except Exception as exc:
        logger.warning("LLM classification failed (%s), using the default classification", exc)
        return _default_classification()


def _classify_with_llm(
    task_envelope: dict,
    kb_context: str,
    project_config: dict | None,
) -> TaskClassification:
    from langchain_core.messages import HumanMessage, SystemMessage

    from core.application.orchestration.model_provider import get_chat_model

    cfg = {
        **(project_config or {}),
        "agents_config": {
            **((project_config or {}).get("agents_config") or {}),
        },
    }
    llm = get_chat_model(cfg, agent_name="classifier")

    title = _envelope_title(task_envelope)
    description = _envelope_description(task_envelope)
    priority = str(task_envelope.get("priority") or "").strip().lower() or "medium"
    source_system = str(task_envelope.get("source_system") or "").strip() or "manual"
    metadata = task_envelope.get("metadata") if isinstance(task_envelope, dict) else {}
    metadata = dict(metadata) if isinstance(metadata, dict) else {}

    task_text = (
        f"Title: {title}\n"
        f"Description: {description}\n"
        f"Priority: {priority}\n"
        f"Source System: {source_system}\n"
        f"Metadata: {json.dumps(metadata, default=str)}\n"
    )
    if kb_context:
        task_text += f"\nRelevant project context:\n{kb_context[:1000]}"

    response = llm.invoke(
        [SystemMessage(content=_CLASSIFICATION_PROMPT), HumanMessage(content=task_text)]
    )
    content = response_text(response)
    match = re.search(r"\{[\s\S]+\}", content)
    if not match:
        raise ValueError(f"No JSON in classifier response: {content[:200]}")
    data = json.loads(match.group(0))

    return TaskClassification(
        effort_hint=str(data.get("effort_hint") or "standard"),
        confidence=float(data.get("confidence", 0.7)),
        risk_level=str(data.get("risk_level", "medium")),
        automation_recommendation=str(data.get("automation_recommendation", "supervised")),
        reasoning=str(data.get("reasoning", "")),
    )


DEFAULT_CLASSIFICATION_REASONING = "default_classification"


def _default_classification() -> TaskClassification:
    """Neutral classification used when the model path is skipped or unavailable.

    It carries no domain keywords and fails safe: without a model verdict every
    task gets the same standard effort, medium risk and a supervised execution
    recommendation, so an unavailable model never grants automatic execution.
    """
    return TaskClassification(
        effort_hint="standard",
        confidence=0.6,
        risk_level="medium",
        automation_recommendation="supervised",
        reasoning=DEFAULT_CLASSIFICATION_REASONING,
    )
