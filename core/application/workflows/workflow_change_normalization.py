"""Step payloads of a change set, bound the way the runtime resolves them.

One normalization for every transport that applies a change set (HTTP, MCP
through HTTP, CLI): a participant resolves to its declared agent, a non-agent
kind carries no agent, the skill is derived from the agent and never stored by
hand, and a context injection may only read an allowed reference path.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from core.application.workflows.workflow_change_authorization import STEP_PAYLOAD_KEYS
from core.application.workflows.workflow_step_kinds import (
    STEP_KIND_AGENT,
    normalize_step_kind,
    step_requires_agent,
)


class WorkflowChangeNormalizationError(ValueError):
    """A step payload the author must fix before the change set is judged."""


def normalize_workflow_participants(participants: list | None) -> list[dict]:
    """Declared participants, one per key, each bound to one agent."""
    normalized: list[dict] = []
    seen: set[str] = set()
    for item in participants or []:
        if not isinstance(item, dict):
            continue
        participant_key = str(item.get("participant_key") or "").strip()
        agent_id = str(item.get("agent_id") or item.get("agent") or "").strip()
        if not participant_key or not agent_id or participant_key in seen:
            continue
        seen.add(participant_key)
        normalized.append({"participant_key": participant_key, "agent_id": agent_id})
    return normalized


def validate_context_inject_sources(context_inject: dict | None) -> None:
    """Refuse a context injection that names a removed key or a forbidden source."""
    from core.infrastructure.security.prompt_safety import validate_reference_path

    mapping = context_inject if isinstance(context_inject, dict) else {}
    for var_name, spec in mapping.items():
        if isinstance(spec, dict) and "phase_id" in spec:
            # Workflows have no phases; a message is selected by the step that produced it.
            raise WorkflowChangeNormalizationError(
                f"context_inject '{var_name}' uses removed key 'phase_id'; use 'step_id'"
            )
        source = str((spec.get("source") if isinstance(spec, dict) else spec) or "").strip()
        if not source:
            continue
        try:
            validate_reference_path(source)
        except ValueError as exc:
            raise WorkflowChangeNormalizationError(
                f"context_inject '{var_name}' has forbidden source '{source}': {exc}"
            ) from exc


def _participant_bindings(workflow: dict[str, Any]) -> dict[str, str]:
    settings = workflow.get("settings")
    raw = settings.get("participants") if isinstance(settings, dict) else None
    if not isinstance(raw, list):
        return {}
    bindings: dict[str, str] = {}
    for item in raw:
        if not isinstance(item, dict):
            continue
        participant_key = str(item.get("participant_key") or "").strip()
        agent_id = str(item.get("agent_id") or item.get("agent") or "").strip()
        if participant_key and agent_id:
            bindings[participant_key] = agent_id
    return bindings


def resolve_agent_for_participant(
    workflow: dict[str, Any], *, participant_key: str, agent_id: str
) -> str:
    """The agent a participant is bound to; an explicit other agent is refused."""
    key = str(participant_key or "").strip()
    explicit_agent_id = str(agent_id or "").strip()
    if not key:
        return explicit_agent_id
    bound_agent_id = str(_participant_bindings(workflow).get(key) or "").strip()
    if not bound_agent_id:
        raise WorkflowChangeNormalizationError(
            f"participant_key '{key}' is not declared in workflow participants"
        )
    if explicit_agent_id and explicit_agent_id != bound_agent_id:
        raise WorkflowChangeNormalizationError(
            f"participant_key '{key}' is bound to agent '{bound_agent_id}', "
            f"but payload requested '{explicit_agent_id}'"
        )
    return bound_agent_id


def _stored_step_kind(workflow: dict[str, Any], step_id: Any) -> str:
    for step in workflow.get("steps") or []:
        if isinstance(step, dict) and str(step.get("step_id") or "") == str(step_id or ""):
            return str(step.get("step_kind") or "")
    return ""


def _normalized_step(
    workflow: dict[str, Any],
    raw_step: Any,
    *,
    validate_context_inject: Callable[[dict | None], None],
    stored_kind: str = "",
) -> dict[str, Any]:
    """A step payload with its agent bound the way the runtime resolves it.

    A config patch carries no kind, so the stored step's kind decides.
    """
    payload = dict(raw_step) if isinstance(raw_step, dict) else {}
    step_kind = normalize_step_kind(
        payload.get("step_kind") or stored_kind, default="", preserve_unknown=True
    )
    if "step_kind" in payload and step_kind:
        # Only a payload that names its kind gets it normalized; a config
        # patch never carries one, so the stored kind is not written back.
        payload["step_kind"] = step_kind
    if "participant_key" in payload or "agent_id" in payload:
        agent_id = resolve_agent_for_participant(
            workflow,
            participant_key=str(payload.get("participant_key") or ""),
            agent_id=str(payload.get("agent_id") or ""),
        )
        payload["agent_id"] = agent_id if step_requires_agent(step_kind or STEP_KIND_AGENT) else ""
    payload.pop("skill_id", None)
    validate_context_inject(payload.get("context_inject"))
    return payload


def normalize_change_operations(
    workflow: dict[str, Any],
    operations: list[Any],
    *,
    validate_context_inject: Callable[[dict | None], None] = validate_context_inject_sources,
) -> list[Any]:
    """Every step payload of *operations*, bound against the stored *workflow*."""
    normalized: list[Any] = []
    for operation in operations:
        if not isinstance(operation, dict):
            normalized.append(operation)
            continue
        current = dict(operation)
        op = str(current.get("op") or "")
        payload_key = STEP_PAYLOAD_KEYS.get(op)
        if op != "update_step_config" and isinstance(current.get(payload_key), dict):
            current[payload_key] = _normalized_step(
                workflow, current[payload_key], validate_context_inject=validate_context_inject
            )
        if op == "update_step_config" and isinstance(current.get("patch"), dict):
            current["patch"] = _normalized_step(
                workflow,
                current["patch"],
                validate_context_inject=validate_context_inject,
                stored_kind=_stored_step_kind(workflow, current.get("step_id")),
            )
        normalized.append(current)
    return normalized


__all__ = [
    "WorkflowChangeNormalizationError",
    "normalize_change_operations",
    "normalize_workflow_participants",
    "resolve_agent_for_participant",
    "validate_context_inject_sources",
]
