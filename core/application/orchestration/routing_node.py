"""
Workflow routing node.

Runs before the orchestrator hub to resolve semantic workflow family, re-entry
context, and variant workflow_id deterministically.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.application.orchestration.state import OrchestratorState

logger = logging.getLogger(__name__)

# Selection sources where the caller named the workflow instead of leaving the
# choice to the classifier. Nothing about routing was inferred for these runs.
EXPLICIT_WORKFLOW_SELECTION_SOURCES = frozenset(
    {
        "parent_call",
        "explicit_request",
        "entry_binding",
        "schedule_binding",
        "webhook_binding",
        "resume",
    }
)

_EXPLICIT_SELECTION_SOURCES = EXPLICIT_WORKFLOW_SELECTION_SOURCES


def _should_fail_fast_on_routing_error(
    state: OrchestratorState,
    error: Exception,
) -> bool:
    selection_source = str(state.get("workflow_selection_source") or "").strip().lower()
    if selection_source not in _EXPLICIT_SELECTION_SOURCES:
        return False
    message = str(error or "").strip().lower()
    return (
        "workflow_id is required before orchestration begins" in message
        or "explicit workflow override '" in message
        or "child workflow '" in message
    )


def workflow_routing_node(state: OrchestratorState) -> dict:
    """Resolve workflow_id before the orchestrator hub.

    workflow_id is the authoritative routing field.
    """
    if state.get("workflow_definition_ref") or state.get("run_cursor"):
        # A run with a pinned definition is never re-routed (resume, redelivery).
        return {}

    task_envelope = state.get("task_envelope")
    if not isinstance(task_envelope, dict) or not task_envelope:
        raise ValueError("workflow_routing_node requires explicit task_envelope in state")

    try:
        from core.application.orchestration.supervisor import OrchestratorSupervisor
        from core.application.registry.registry_manager import RegistryManager

        registry = RegistryManager.instance()
        supervisor = OrchestratorSupervisor(registry)
        routing = supervisor.resolve_workflow_routing(task_envelope, state=state)
    except Exception as e:
        if _should_fail_fast_on_routing_error(state, error=e):
            raise
        logger.warning(
            "workflow_routing_node: routing resolution failed (will fallback in orchestrator): %s",
            e,
        )
        return {}

    existing_selection_source = str(state.get("workflow_selection_source") or "").strip().lower()
    preserve_selection_source = existing_selection_source in _EXPLICIT_SELECTION_SOURCES

    return {
        "workflow_id": routing.workflow_id,
        "workflow_selection_source": (
            existing_selection_source
            if preserve_selection_source and existing_selection_source
            else "explicit_request"
        ),
        "effort_hint": routing.classification.effort_hint,
        "risk_level": routing.classification.risk_level,
        "confidence_score": routing.classification.confidence,
        "automation_recommendation": routing.classification.automation_recommendation,
        "routing_reason": routing.routing_reason,
        "routing_signals": routing.routing_signals,
        "reentry_context": routing.reentry_context,
        "resume_parent": bool(routing.reentry_context.get("resume_parent")),
        "feedback_type": routing.reentry_context.get("feedback_type"),
        "parent_task_id": routing.reentry_context.get("parent_task_id"),
        "parent_job_id": routing.reentry_context.get("parent_job_id"),
        "parent_workflow_id": routing.reentry_context.get("parent_workflow_id"),
    }
