"""Agent-message context collection for semantic guardrails."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .semantic_common import (
    _collect_active_feedback_context,
    _collect_scoped_work_item_context,
    _collect_supporting_workflow_context,
    _synthesize_generic_artifact_context,
)

if TYPE_CHECKING:
    from core.application.orchestration.state import OrchestratorState


def _collect_agent_context(
    agent_id: str,
    state: OrchestratorState,
) -> dict[str, str]:
    """Collect the latest semantic-check context for the given agent."""
    from core.application.orchestration.agent_messages import get_state_messages

    messages = get_state_messages(state)
    context = {
        "raw_output": "",
        "primary_text_artifact": "",
        "supporting_text_artifacts": "",
        "supporting_workflow_context": "",
        "scoped_work_item": "",
        "active_feedback": "",
    }
    for msg in reversed(messages):
        if str(msg.get("from_agent") or "") != agent_id:
            continue
        raw_artifacts = msg.get("artifacts")
        artifacts: dict[str, object] = raw_artifacts if isinstance(raw_artifacts, dict) else {}
        summary = str(msg.get("summary") or "")
        if not context["raw_output"]:
            context["raw_output"] = str(artifacts.get("raw_output") or "")
        if not context["raw_output"]:
            context["raw_output"] = _synthesize_generic_artifact_context(
                summary=summary,
                artifacts=artifacts,
            )
        text_artifacts: list[str] = []
        for key, value in artifacts.items():
            if str(key).startswith("_") or key == "raw_output":
                continue
            if isinstance(value, str) and value.strip():
                text_artifacts.append(value.strip())
        if text_artifacts and not context["primary_text_artifact"]:
            context["primary_text_artifact"] = text_artifacts[0]
            extras = [item for item in text_artifacts[1:] if item != text_artifacts[0]]
            if extras and not context["supporting_text_artifacts"]:
                context["supporting_text_artifacts"] = "\n\n".join(extras[:5])
        if all(context.values()):
            break
    supporting = _collect_supporting_workflow_context(agent_id=agent_id, state=state)
    if supporting:
        context["supporting_workflow_context"] = supporting
        if not context["primary_text_artifact"]:
            if context["raw_output"]:
                context["raw_output"] = f"{context['raw_output']}\n\n{supporting}"
            else:
                context["raw_output"] = supporting
    context["scoped_work_item"] = _collect_scoped_work_item_context(state)
    context["active_feedback"] = _collect_active_feedback_context(state)
    return context
