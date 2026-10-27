"""
Shared helper functions for the orchestrator package.

Extracted from hub_node.py to eliminate cross-module wrapper imports.
Used by hub_node, parallel_executor_node, and invoke_workflow_step_node.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from core.application.workflows.workflow_step_kinds import (
    STEP_KIND_AGENT,
    normalize_step_kind,
    runtime_actor_for_step,
)

# ── Generic utilities ───────────────────────────────────────────────────


def now_iso() -> str:
    """Return current UTC timestamp in ISO format."""
    return datetime.now(UTC).isoformat()


def to_bool(value: Any, default: bool = False) -> bool:
    """Coerce a value to bool, handling string flags like '1', 'true', 'yes'."""
    if value is None:
        return bool(default)
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
    return bool(value)


def has_value(value: Any) -> bool:
    """Return True if value is non-None, non-empty."""
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, tuple, set, dict)):
        return len(value) > 0
    return True


def to_dict(value: Any) -> dict:
    """Coerce value to dict (handles str JSON, None)."""
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            return {}
        try:
            parsed = json.loads(raw)
        except Exception:
            return {}
        if isinstance(parsed, dict):
            return parsed
    return {}


def to_float(value: Any, default: float = 0.0) -> float:
    """Coerce value to float, returning default on failure."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


# ── Phase step navigation ───────────────────────────────────────────────


def get_phase_steps(phase_dict: dict) -> list[dict]:
    """Return the step list from a phase dict."""
    steps = phase_dict.get("steps") or phase_dict.get("_steps") or []
    return steps if isinstance(steps, list) else []


def get_phase_step_cursor(phase_dict: dict) -> int:
    """Return the current step cursor, clamped to valid range."""
    steps = get_phase_steps(phase_dict)
    if not steps:
        return 0
    try:
        cursor = int(phase_dict.get("_current_step_index", 0) or 0)
    except (TypeError, ValueError):
        cursor = 0
    execution_order = phase_dict.get("_execution_order")
    if isinstance(execution_order, list) and execution_order:
        return max(0, min(cursor, len(execution_order) - 1))
    return max(0, min(cursor, len(steps) - 1))


def get_phase_step_index(phase_dict: dict) -> int:
    """Return the actual step index (resolving execution order indirection)."""
    steps = get_phase_steps(phase_dict)
    if not steps:
        return 0
    execution_order = phase_dict.get("_execution_order")
    cursor = get_phase_step_cursor(phase_dict)
    if isinstance(execution_order, list) and execution_order:
        try:
            idx = int(execution_order[cursor])
        except (TypeError, ValueError, IndexError):
            idx = cursor
    else:
        idx = cursor
    if idx < 0:
        return 0
    if idx >= len(steps):
        return len(steps) - 1
    return idx


def get_active_step(phase_dict: dict) -> dict | None:
    """Return the currently active step dict, or None."""
    steps = get_phase_steps(phase_dict)
    if not steps:
        return None
    return steps[get_phase_step_index(phase_dict)]


def get_agent_from_step(step: dict | None) -> str:
    """Extract concrete agent ID from a step dict.

    Returns empty string when a concrete ``agent_id`` is not present.
    """
    if not isinstance(step, dict):
        return ""
    return runtime_actor_for_step(step, default="")


def get_step_participant_key(step: dict | None) -> str:
    """Return workflow participant binding key."""
    if not isinstance(step, dict):
        return ""
    return str(step.get("participant_key") or "").strip()


def resolve_step_agent(
    step: dict | None,
    registry,
    *,
    state: Mapping[str, Any] | None = None,
) -> str:
    """Resolve step executor to a concrete ``agent_id`` using participant bindings."""
    if not isinstance(step, dict):
        return ""
    step_kind = normalize_step_kind(step.get("step_kind"), default=STEP_KIND_AGENT)
    if step_kind != STEP_KIND_AGENT:
        return runtime_actor_for_step(step, default="")
    explicit_agent = get_agent_from_step(step)
    if explicit_agent:
        return explicit_agent

    participant_key = get_step_participant_key(step)
    if not participant_key:
        return ""

    hinted_agent = str(
        step.get("_participant_agent_id") or step.get("participant_agent_id") or ""
    ).strip()
    if hinted_agent:
        return hinted_agent

    state_obj = state if isinstance(state, dict) else {}
    state_bindings = state_obj.get("workflow_participants")
    if isinstance(state_bindings, dict):
        candidate = str(state_bindings.get(participant_key) or "").strip()
        if candidate:
            return candidate
    if isinstance(state_bindings, list):
        for item in state_bindings:
            if not isinstance(item, dict):
                continue
            key = str(item.get("participant_key") or "").strip()
            candidate = str(item.get("agent_id") or item.get("agent") or "").strip()
            if key == participant_key and candidate:
                return candidate

    workflow_id = str(state_obj.get("workflow_id") or "").strip()
    workflows = getattr(registry, "workflows", None)
    if workflow_id and workflows is not None and hasattr(workflows, "get_workflow"):
        try:
            workflow = workflows.get_workflow(workflow_id)
        except Exception:
            workflow = None
        participants = getattr(workflow, "participants", None)
        if isinstance(participants, list):
            for item in participants:
                key = str(getattr(item, "participant_key", "") or "").strip()
                candidate = str(getattr(item, "agent_id", "") or "").strip()
                if key == participant_key and candidate:
                    return candidate

    return ""


def resolve_primary_skill_for_agent(agent_id: str, registry: Any | None = None) -> str:
    """Resolve the primary skill executed by an agent."""
    normalized_agent_id = str(agent_id or "").strip()
    if not normalized_agent_id:
        return ""
    if registry is None:
        try:
            from core.application.registry.registry_manager import RegistryManager

            registry = RegistryManager.instance()
        except Exception:
            registry = None
    db = getattr(registry, "db_store", None)
    if db is None:
        return ""
    try:
        agent = db.get_agent(normalized_agent_id)
    except Exception:
        agent = None
    if isinstance(agent, dict):
        base_skill = str(agent.get("base_skill") or "").strip()
        if base_skill:
            return base_skill
    with_keys = []
    try:
        with_keys = db.get_agent_skills(normalized_agent_id) or []
    except Exception:
        with_keys = []
    for row in with_keys:
        skill_id = str((row or {}).get("skill_id") or "").strip()
        if skill_id:
            return skill_id
    return ""


def resolve_step_skill(
    step: dict | None,
    registry: Any | None = None,
    *,
    state: Mapping[str, Any] | None = None,
    default: str = "",
) -> str:
    """Resolve step skill label for runtime metadata.

    Explicit step skill configuration is authoritative. Agent steps fall back
    to the resolved agent's primary skill only when the step does not name one.
    """
    if not isinstance(step, dict):
        return str(default or "").strip()
    explicit = str(step.get("skill") or step.get("skill_id") or "").strip()
    if explicit:
        return explicit
    step_kind = normalize_step_kind(step.get("step_kind"), default=STEP_KIND_AGENT)
    if step_kind != STEP_KIND_AGENT:
        return str(default or "").strip()
    if registry is None:
        try:
            from core.application.registry.registry_manager import RegistryManager

            registry = RegistryManager.instance()
        except Exception:
            registry = None
    agent_id = resolve_step_agent(step, registry, state=state)
    if not agent_id:
        return ""
    return resolve_primary_skill_for_agent(agent_id, registry) or str(default or "").strip()


# ── Parallel execution helpers ──────────────────────────────────────────


def parallel_lane_id(step: dict, index: int) -> str:
    """Return the parallel lane ID for a step."""
    group = str((step or {}).get("parallel_group") or "").strip()
    if group:
        return group
    return f"serial_{index}"


def resolve_parallel_concurrency_cap(state: dict) -> int:
    """Return the concurrency cap from project policy."""
    project_policy = state.get("project_policy")
    if not isinstance(project_policy, dict):
        project_config = state.get("project_config")
        if isinstance(project_config, dict):
            project_policy = project_config.get("project_policy")
    if not isinstance(project_policy, dict):
        return 1
    raw = project_policy.get("concurrency_cap")
    try:
        return max(1, int(raw or 1))
    except (TypeError, ValueError):
        return 1


def compute_phase_execution_order(phase_dict: dict, state: dict) -> list[int]:
    """Compute step execution order respecting parallel groups and concurrency."""
    steps = get_phase_steps(phase_dict)
    if not steps:
        return []

    has_parallel = any(str((step or {}).get("parallel_group") or "").strip() for step in steps)
    if not has_parallel:
        return list(range(len(steps)))

    lane_to_steps: dict[str, list[int]] = {}
    lane_order: list[str] = []
    for idx, step in enumerate(steps):
        lane = parallel_lane_id(step if isinstance(step, dict) else {}, idx)
        if lane not in lane_to_steps:
            lane_to_steps[lane] = []
            lane_order.append(lane)
        lane_to_steps[lane].append(idx)

    concurrency_cap = resolve_parallel_concurrency_cap(state)
    active_lanes = list(lane_order[:concurrency_cap])
    pending_lanes = list(lane_order[concurrency_cap:])
    execution_order: list[int] = []

    while active_lanes:
        for lane in tuple(active_lanes):
            queue = lane_to_steps.get(lane) or []
            if queue:
                execution_order.append(queue.pop(0))
        active_lanes = [lane for lane in active_lanes if lane_to_steps.get(lane)]
        while len(active_lanes) < concurrency_cap and pending_lanes:
            next_lane = pending_lanes.pop(0)
            if lane_to_steps.get(next_lane):
                active_lanes.append(next_lane)

    if len(execution_order) != len(steps):
        return list(range(len(steps)))
    return execution_order


def ensure_phase_execution_runtime(phase_dict: dict, state: dict) -> dict:
    """Populate phase execution metadata (order, parallelism, cursor)."""
    steps = get_phase_steps(phase_dict)
    if not steps:
        phase_dict["_execution_order"] = []
        phase_dict["_parallel_concurrency_cap"] = resolve_parallel_concurrency_cap(state)
        phase_dict["_has_parallel_groups"] = False
        phase_dict["_parallel_groups"] = []
        phase_dict["_current_step_index"] = 0
        return phase_dict

    has_parallel = any(str((step or {}).get("parallel_group") or "").strip() for step in steps)
    execution_order = phase_dict.get("_execution_order")
    if not isinstance(execution_order, list) or len(execution_order) != len(steps):
        execution_order = compute_phase_execution_order(phase_dict, state)

    phase_dict["_execution_order"] = [int(i) for i in execution_order]
    phase_dict["_parallel_concurrency_cap"] = resolve_parallel_concurrency_cap(state)
    phase_dict["_has_parallel_groups"] = bool(has_parallel)
    if has_parallel:
        phase_dict["_parallel_groups"] = [
            parallel_lane_id(step if isinstance(step, dict) else {}, idx)
            for idx, step in enumerate(steps)
        ]
    else:
        phase_dict["_parallel_groups"] = []
    try:
        cursor = int(phase_dict.get("_current_step_index", 0) or 0)
    except (TypeError, ValueError):
        cursor = 0
    cursor = max(0, min(cursor, max(0, len(execution_order) - 1)))
    phase_dict["_current_step_index"] = cursor
    return phase_dict
