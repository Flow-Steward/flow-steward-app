"""Step and task context helpers for graph node execution.

The step an agent node runs is the run cursor's active step, read from the
run's pinned definition; nothing here looks at sort order or a phase list.
"""

from __future__ import annotations

import copy
from typing import TYPE_CHECKING, Any

from core.application.orchestration.active_step import active_step

if TYPE_CHECKING:
    from core.application.orchestration.state import OrchestratorState
    from core.application.registry.models import AgentManifest


def extract_task_context(state: OrchestratorState) -> dict:
    """Extract task-level context from state for agent input."""
    initial_requirements = state.get("initial_requirements") or state.get("task_summary", "")
    return {
        "task_id": state.get("task_id", ""),
        "task_summary": state.get("task_summary", ""),
        "project_name": state.get("project_name", ""),
        "project_config": state.get("project_config", {}),
        "kb_context": state.get("kb_context", ""),
        "initial_requirements": initial_requirements,
        "task_envelope": state.get("task_envelope"),
    }


def extract_step_context(state: OrchestratorState) -> dict[str, Any]:
    """Context of the active step: its identity, display name and pinned config."""
    current = active_step(state)
    if current is None:
        return {}
    return {
        "step_id": current.step_id,
        "step_execution_id": current.step_execution_id,
        "attempt": current.attempt,
        "group_id": current.group_id,
        "for_each_id": current.for_each_id,
        "step_display_name": str(current.step.get("display_name") or ""),
        "workflow_id": state.get("workflow_id", ""),
        # A private copy: the pinned definition is shared by every node of the run.
        "_step": copy.deepcopy(current.step),
    }


def get_current_skill(state: OrchestratorState, manifest: AgentManifest) -> str:
    """Determine which skill to use for this invocation."""
    from core.application.orchestration.helpers import resolve_step_skill
    from core.application.workflows.workflow_step_kinds import runtime_actor_for_step

    current = active_step(state)
    if current is not None and runtime_actor_for_step(current.step) == manifest.agent_id:
        resolved = resolve_step_skill(current.step, state=state)
        if resolved:
            return resolved
    return manifest.skills[0] if manifest.skills else ""


def resolve_agent_timeout_seconds(manifest: AgentManifest) -> int:
    """Resolve per-agent timeout from manifest config."""
    raw_minutes = getattr(manifest, "timeout_minutes", None)
    if raw_minutes in (None, ""):
        raw_minutes = (getattr(manifest, "config", None) or {}).get("timeout_minutes")
    try:
        minutes = float(str(raw_minutes).strip())
    except (TypeError, ValueError):
        return 0
    if minutes <= 0:
        return 0
    return max(1, int(minutes * 60))
