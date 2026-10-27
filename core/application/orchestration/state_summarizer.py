"""
StateSummarizer — compresses OrchestratorState to ~500 tokens before orchestrator re-entry.

Uses claude-haiku (cheap) to produce a structured summary of the state
so the orchestrator hub doesn't need to pass the full state through every
routing decision.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

from core.application.orchestration.journal import coerce_journal_entries, journal_message
from core.application.orchestration.run_cursor import RunCursor

if TYPE_CHECKING:
    from core.application.orchestration.state import OrchestratorState

logger = logging.getLogger(__name__)

_SUMMARY_PROMPT = """You are a state summarizer for a workflow orchestrator.

Produce a compact markdown summary (max 400 words) of the current workflow state.
Include:
1. What has completed so far
2. What is currently active or waiting
3. Important outputs or artifacts that were produced
4. Any issues, blockers, or pending approvals
5. Confidence level (high/medium/low) in the current path

Be factual and concise. Do not repeat yourself. Omit any section that is empty.
"""


def summarize_state(state: OrchestratorState) -> str:
    """Compress state to ~500 tokens for orchestrator context.

    Falls back to a simple structured summary if LLM is unavailable.
    """
    try:
        return _summarize_with_llm(state)
    except Exception as e:
        logger.debug("State summarizer LLM failed (%s) — using fallback", e)
        return _summarize_fallback(state)


def _summarize_with_llm(state: OrchestratorState) -> str:
    from langchain_core.messages import HumanMessage, SystemMessage

    from core.application.orchestration.model_provider import get_chat_model

    project_config = state.get("project_config") or {}
    _cfg = {
        **project_config,
        "agents_config": {
            **(project_config.get("agents_config") or {}),
        },
    }
    llm = get_chat_model(_cfg, agent_name="state_summarizer")

    # Build condensed state representation
    from core.application.orchestration.agent_messages import get_state_messages

    agent_messages = get_state_messages(state)
    cursor = RunCursor.load(state.get("run_cursor"))
    journal_raw = (state.get("orchestrator_journal") or [])[-10:]
    journal = coerce_journal_entries(journal_raw)

    state_snapshot = {
        "task": state.get("task_summary") or "",
        "workflow_id": state.get("workflow_id") or "",
        "current_step_id": state.get("current_step_id") or "",
        "current_group_id": state.get("current_group_id") or "",
        "iteration_count": state.get("iteration_count", 0),
        "step_attempt": cursor.attempt if cursor.step_id else 0,
        "agent_message_count": len(agent_messages),
        "recent_journal": journal,
        "status": state.get("status") or "",
        "needs_user_approval": bool(state.get("needs_user_approval")),
    }

    messages = [
        SystemMessage(content=_SUMMARY_PROMPT),
        HumanMessage(
            content=f"State snapshot:\n{json.dumps(state_snapshot, indent=2, default=str)[:3000]}"
        ),
    ]

    response = llm.invoke(messages)
    from core.application.orchestration.model_response_text import model_response_text

    return model_response_text(response)


def _summarize_fallback(state: OrchestratorState) -> str:
    """Simple structured fallback — no LLM needed."""
    lines = []
    lines.append("## State Summary")
    lines.append(f"**Task:** {state.get('task_summary', 'Unknown')}")
    lines.append(f"**Workflow:** {state.get('workflow_id', 'none')}")
    lines.append(f"**Current Step:** {state.get('current_step_id') or 'none'}")
    if state.get("current_group_id"):
        lines.append(f"**Current Group:** {state.get('current_group_id')}")
    lines.append(f"**Iterations:** {state.get('iteration_count', 0)}")
    lines.append(f"**Status:** {state.get('status', 'unknown')}")

    cursor = RunCursor.load(state.get("run_cursor"))
    if cursor.step_id and cursor.attempt > 1:
        lines.append(f"**Step Attempt:** {cursor.attempt}")
    journal = coerce_journal_entries((state.get("orchestrator_journal") or [])[-5:])
    if journal:
        lines.append("**Recent Journal:**")
        for entry in journal:
            msg = journal_message(entry)
            if msg:
                lines.append(f"- {msg}")

    from core.application.orchestration.agent_messages import get_state_messages

    agent_messages = get_state_messages(state)
    if agent_messages:
        completed_agents = []
        for msg in agent_messages:
            if not isinstance(msg, dict):
                continue
            agent_id = str(msg.get("from_agent") or "")
            if agent_id and agent_id not in completed_agents:
                completed_agents.append(agent_id)
        lines.append(f"**Completed Agents:** {', '.join(completed_agents)}")

    if state.get("needs_user_approval"):
        lines.append("**Pending Approval:** yes")

    return "\n".join(lines)
