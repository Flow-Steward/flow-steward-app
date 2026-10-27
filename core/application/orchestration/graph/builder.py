# mypy: ignore-errors
"""
Dynamic Graph Builder — builds a LangGraph StateGraph from the Flow Steward registry.

build_graph_from_registry() creates a hub-and-spoke graph:
- Fixed nodes: task_intake, kb_intake, workflow_routing,
  orchestrator, parallel_executor, execute_step, invoke_workflow_step,
  orchestrator_validation, human_approval_gate
- Dynamic nodes: one per registered+enabled agent
- Fixed edges: every agent → orchestrator (hub)
- Entry point: task_intake

Also rebuilt on SIGHUP (live reload).
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import asdict
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.application.orchestration.state import OrchestratorState
    from core.application.registry.registry_manager import RegistryManager

from core.application.orchestration.checkpoint_requirement import CheckpointUnavailableError
from core.application.orchestration.graph.builder_approval_gate import (
    _content_to_text,
    _extract_artifact_text,
    _is_meaningful_content,
    _latest_message_artifact_text,
    _latest_message_artifacts,
    _pick_gate_content,
    _registry_approval_gate_node,
)

logger = logging.getLogger(__name__)

__all__ = [
    "_content_to_text",
    "_extract_artifact_text",
    "_is_meaningful_content",
    "_latest_message_artifact_text",
    "_latest_message_artifacts",
    "_pick_gate_content",
    "_registry_approval_gate_node",
]


def _has_required_contract_schema(schema: object) -> bool:
    """Return True when schema is a non-empty JSON object."""
    return isinstance(schema, dict) and bool(schema)


def _validation_error_messages(entity_type: str, entity_id: str, validation: object) -> list[str]:
    if not isinstance(validation, dict):
        return []
    messages: list[str] = []
    for issue in validation.get("errors") or []:
        if not isinstance(issue, dict):
            continue
        message = str(issue.get("message") or "").strip()
        if not message:
            continue
        path = str(issue.get("path") or "").strip()
        if path:
            messages.append(f"{entity_type} '{entity_id}' ({path}): {message}")
        else:
            messages.append(f"{entity_type} '{entity_id}': {message}")
    return messages


def _resolve_agent_tool_catalog(registry: RegistryManager, agent_id: str) -> list[dict]:
    """Resolve the typed tool catalog visible to an agent without backend-specific glue."""
    tools = getattr(registry, "tools", None)
    if tools is not None and hasattr(tools, "get_effective_catalog"):
        try:
            return list(tools.get_effective_catalog(agent_id) or [])
        except Exception as exc:
            logger.debug(
                "Agent '%s' effective tool catalog lookup failed via registry.tools: %s",
                agent_id,
                exc,
            )

    db = getattr(registry, "db_store", None)
    if db is not None and hasattr(db, "get_effective_tool_catalog"):
        try:
            return list(db.get_effective_tool_catalog(agent_id) or [])
        except Exception as exc:
            logger.debug(
                "Agent '%s' effective tool catalog lookup failed via db_store: %s", agent_id, exc
            )

    return []


def _normalize_workflow_validation_scope(workflow_ids: object) -> set[str] | None:
    if workflow_ids is None:
        return None
    if isinstance(workflow_ids, str):
        rows = [workflow_ids]
    else:
        try:
            rows = list(workflow_ids)
        except TypeError:
            rows = [workflow_ids]
    normalized = {str(item or "").strip() for item in rows if str(item or "").strip()}
    return normalized or None


def _normalize_workflow_step_scope(scope: object) -> dict[str, str] | None:
    if scope is None:
        return None
    if not isinstance(scope, Mapping):
        raise RuntimeError("Graph step validation scope must be a mapping")
    normalized = {key: str(scope.get(key) or "").strip() for key in ("workflow_id", "step_id")}
    if any(not value for value in normalized.values()):
        raise RuntimeError("Graph step validation scope is incomplete")
    return normalized


def _resolve_workflow_step_target(
    workflows: list[object],
    scope: Mapping[str, str] | None,
) -> tuple[object, object] | None:
    if scope is None:
        return None
    workflow_matches = [
        workflow
        for workflow in workflows
        if str(getattr(workflow, "workflow_id", "") or "").strip() == scope["workflow_id"]
    ]
    if len(workflow_matches) != 1:
        raise RuntimeError("Graph step validation scope does not resolve one workflow")
    workflow = workflow_matches[0]
    step_matches = [
        step
        for step in getattr(workflow, "steps", None) or []
        if str(getattr(step, "step_id", "") or "").strip() == scope["step_id"]
    ]
    if len(step_matches) != 1:
        raise RuntimeError("Graph step validation scope does not resolve one step")
    return workflow, step_matches[0]


def _workflow_registry_references(
    workflows: list[object],
    *,
    step_target: tuple[object, object] | None = None,
) -> tuple[set[str], set[str]]:
    agent_ids: set[str] = set()
    skill_ids: set[str] = set()
    for workflow in workflows:
        if step_target is not None and workflow is not step_target[0]:
            continue
        participant_bindings: dict[str, str] = {}
        for item in getattr(workflow, "participants", None) or []:
            participant_key = str(getattr(item, "participant_key", "") or "").strip()
            agent_id = str(getattr(item, "agent_id", "") or "").strip()
            if participant_key and agent_id:
                participant_bindings[participant_key] = agent_id
        settings = getattr(workflow, "settings", {})
        if not participant_bindings and isinstance(settings, dict):
            for item in settings.get("participants") or []:
                if not isinstance(item, dict):
                    continue
                participant_key = str(item.get("participant_key") or "").strip()
                agent_id = str(item.get("agent_id") or item.get("agent") or "").strip()
                if participant_key and agent_id:
                    participant_bindings[participant_key] = agent_id
        steps = (
            [step_target[1]] if step_target is not None else getattr(workflow, "steps", None) or []
        )
        for step in steps:
            agent_id = str(getattr(step, "agent", "") or "").strip()
            participant_key = str(getattr(step, "participant_key", "") or "").strip()
            if participant_key:
                agent_id = participant_bindings.get(participant_key, agent_id)
            skill_id = str(getattr(step, "skill", "") or "").strip()
            if agent_id:
                agent_ids.add(agent_id)
            if skill_id:
                skill_ids.add(skill_id)
    return agent_ids, skill_ids


def _validate_graph_contracts(
    registry: RegistryManager,
    *,
    workflow_ids: object = None,
    workflow_step_scope: object = None,
) -> list[str]:
    """
    Validate graph readiness using the same contract validators as admin/save paths.
    """
    db = getattr(registry, "db_store", None)
    if db is None:
        raise RuntimeError("Graph contract validation requires registry.db_store")

    try:
        from core.application.registry.contract_validation import (
            validate_agent_readiness,
            validate_skill_readiness,
            validate_workflow_readiness,
        )
    except Exception as exc:
        raise RuntimeError(
            "Graph contract validation requires registry contract validators"
        ) from exc

    errors: list[str] = []
    try:
        agents = list(registry.agents.list_all() or [])
    except Exception:
        agents = []
    try:
        skills = list(registry.skills.list_all() or [])
    except Exception:
        skills = []
    try:
        workflows = list(registry.workflows.list_all() or [])
    except Exception:
        workflows = []

    workflow_scope = _normalize_workflow_validation_scope(workflow_ids)
    normalized_step_scope = _normalize_workflow_step_scope(workflow_step_scope)
    scoped_workflows = [
        workflow
        for workflow in workflows
        if (
            bool(getattr(workflow, "enabled", True))
            or (
                normalized_step_scope is not None
                and str(getattr(workflow, "workflow_id", "") or "").strip()
                == normalized_step_scope["workflow_id"]
            )
        )
        and (
            workflow_scope is None
            or str(getattr(workflow, "workflow_id", "") or "").strip() in workflow_scope
        )
    ]
    if normalized_step_scope is not None and (
        workflow_scope != {normalized_step_scope["workflow_id"]}
    ):
        raise RuntimeError("Graph step validation scope conflicts with workflow scope")
    step_target = _resolve_workflow_step_target(scoped_workflows, normalized_step_scope)
    scoped_agent_ids: set[str] | None = None
    scoped_skill_ids: set[str] | None = None
    if workflow_scope is not None:
        scoped_agent_ids, scoped_skill_ids = _workflow_registry_references(
            scoped_workflows,
            step_target=step_target,
        )
        for agent in agents:
            agent_id = str(getattr(agent, "agent_id", "") or "").strip()
            if agent_id not in scoped_agent_ids:
                continue
            scoped_skill_ids.update(
                str(skill_id or "").strip()
                for skill_id in (getattr(agent, "skills", None) or [])
                if str(skill_id or "").strip()
            )

    for agent in agents:
        if not bool(getattr(agent, "enabled", True)):
            continue
        agent_id = str(getattr(agent, "agent_id", "") or "").strip()
        if not agent_id:
            continue
        if scoped_agent_ids is not None and agent_id not in scoped_agent_ids:
            continue
        errors.extend(
            _validation_error_messages(
                "Agent",
                agent_id,
                validate_agent_readiness(db, agent_id),
            )
        )

    for skill in skills:
        if not bool(getattr(skill, "enabled", True)):
            continue
        skill_id = str(getattr(skill, "skill_id", "") or "").strip()
        if not skill_id:
            continue
        if scoped_skill_ids is not None and skill_id not in scoped_skill_ids:
            continue
        errors.extend(
            _validation_error_messages(
                "Skill",
                skill_id,
                validate_skill_readiness(db, skill_id),
            )
        )

        # Keep graph-level schema checks as an extra guard for runtime manifests.
        input_schema = getattr(skill, "input_schema", None)
        output_schema = getattr(skill, "output_schema", None)
        if not _has_required_contract_schema(input_schema):
            errors.append(f"Skill '{skill_id}' is enabled but missing required input_schema")
        if not _has_required_contract_schema(output_schema):
            errors.append(f"Skill '{skill_id}' is enabled but missing required output_schema")

    for workflow in scoped_workflows:
        workflow_id = str(getattr(workflow, "workflow_id", "") or "").strip()
        if not workflow_id:
            continue
        validation = validate_workflow_readiness(db, workflow_id)
        if step_target is not None:
            from core.application.registry.active_workflow_dependencies import (
                workflow_registry_tool_issues,
            )
            from core.application.workflows.platform_step_test_run_helpers import (
                scope_workflow_validation_for_step,
            )

            target_workflow, target_step = step_target
            selected_workflow = asdict(target_workflow)
            selected_workflow["steps"] = [asdict(target_step)]
            selected_workflow["groups"] = []
            validation = scope_workflow_validation_for_step(
                validation,
                step_id=normalized_step_scope["step_id"],
                selected_registry_tool_issues=workflow_registry_tool_issues(
                    db,
                    selected_workflow,
                ),
            )
        errors.extend(
            _validation_error_messages(
                "Workflow",
                workflow_id,
                validation,
            )
        )

    # Preserve deterministic error output for easier operator triage.
    deduped: list[str] = []
    seen: set[str] = set()
    for item in errors:
        if item in seen:
            continue
        seen.add(item)
        deduped.append(item)
    return deduped


def build_graph_from_registry(
    registry: RegistryManager,
    checkpointer=None,
    *,
    validate_contracts: bool = True,
    workflow_ids: object = None,
    workflow_step_scope: object = None,
):
    """Build the hub-and-spoke LangGraph from core.application.registry.

    Args:
        registry: Initialized RegistryManager with all agents loaded.
        validate_contracts: When False, compile for a pinned run or state read,
            without using live workflows to validate or filter its agent nodes.

    Returns:
        Compiled LangGraph StateGraph.
    """
    from langgraph.graph import END, StateGraph

    from core.application.orchestration.graph.nodes import (
        load_agent_implementation,
        wrap_agent_as_node,
    )
    from core.application.orchestration.state import OrchestratorState

    logger.info("Building registry-mode graph...")

    if validate_contracts:
        contract_errors = _validate_graph_contracts(
            registry,
            workflow_ids=workflow_ids,
            workflow_step_scope=workflow_step_scope,
        )
        if contract_errors:
            preview = "; ".join(contract_errors[:8])
            raise RuntimeError(
                f"Graph contract validation failed ({len(contract_errors)} error(s)): {preview}"
            )

    graph = StateGraph(OrchestratorState)

    # ── Fixed infrastructure nodes ────────────────────────────────────────────
    _add_fixed_nodes(graph, registry)

    # ── Dynamic agent nodes ───────────────────────────────────────────────────
    # A pinned run may still reference agents removed from the live workflow.
    # Keep every enabled agent available when compiling without live validation.
    directly_referenced = (
        _collect_directly_referenced_agents(
            registry,
            workflow_ids=workflow_ids,
            workflow_step_scope=workflow_step_scope,
        )
        if validate_contracts
        else None
    )

    agent_ids = []
    for manifest in registry.agents.list_all():
        if not manifest.enabled:
            logger.debug("Skipping disabled agent: %s", manifest.agent_id)
            continue
        if directly_referenced is not None and manifest.agent_id not in directly_referenced:
            continue
        try:
            tool_catalog = _resolve_agent_tool_catalog(registry, manifest.agent_id)
            if tool_catalog:
                logger.debug(
                    "Agent '%s' typed tool catalog: %s",
                    manifest.agent_id,
                    [
                        f"{item.get('tool_id')}:{item.get('implementation_kind') or ((item.get('implementation') or {}).get('kind') or '')}"
                        for item in tool_catalog
                    ],
                )
            impl = load_agent_implementation(manifest, registry.tools, registry.skills)
            node_fn = wrap_agent_as_node(impl, manifest)
            graph.add_node(manifest.agent_id, node_fn)
            agent_ids.append(manifest.agent_id)
            logger.info("  + Agent node: %s (%s)", manifest.agent_id, manifest.display_name)
        except Exception as e:
            logger.exception("Failed to build node for agent '%s': %s", manifest.agent_id, e)

    # ── Fixed edges ───────────────────────────────────────────────────────────
    graph.add_edge("task_intake", "kb_intake")
    graph.add_edge("kb_intake", "workflow_routing")
    graph.add_edge("workflow_routing", "orchestrator")

    # Every agent returns to the orchestrator hub
    for agent_id in agent_ids:
        graph.add_edge(agent_id, "orchestrator")

    # Parallel executor returns to orchestrator for group evaluation/next routing.
    graph.add_edge("parallel_executor", "orchestrator")

    # Single-step executor returns to orchestrator.
    graph.add_edge("execute_step", "orchestrator")
    graph.add_edge("dataset_query_step", "orchestrator")
    graph.add_edge("tabular_feed_step", "orchestrator")

    # invoke_workflow step node returns to orchestrator after child completion.
    graph.add_edge("invoke_workflow_step", "orchestrator")

    # control-flow step node returns to the orchestrator hub, which follows the
    # execution graph edge for the outcome the step reported.
    graph.add_edge("control_flow_step", "orchestrator")

    # An authored Wait resumes after its timer wake and returns its result.
    graph.add_edge("wait_step", "orchestrator")

    # human_approval_gate returns explicit Command(goto=...) routes for approval,
    # rejection, abort, test-run pauses, and invoke_workflow auto-approval paths.
    # Do not add a static edge here; LangGraph can run static edges alongside
    # Command routes, which can send terminal validation back through orchestrator.

    # Validation is terminal
    graph.add_edge("orchestrator_validation", END)

    # ── Entry point ───────────────────────────────────────────────────────────
    graph.set_entry_point("task_intake")

    # ── Compile with checkpointer ─────────────────────────────────────────────
    # Use the caller-supplied checkpointer if provided; otherwise create one.
    # ``False`` is the explicit sentinel for a compile-only build that never
    # runs. Anything that runs gets a checkpointer or refuses to build.
    if checkpointer is False:
        compiled = graph.compile()
    else:
        if checkpointer is None:
            from core.application.orchestration.graph_runtime import _get_checkpointer

            checkpointer = _get_checkpointer()
        if checkpointer is None:
            raise CheckpointUnavailableError("no checkpointer could be created")
        compiled = graph.compile(checkpointer=checkpointer)

    logger.info(
        "Registry graph built: %d dedicated agent nodes, %d total nodes",
        len(agent_ids),
        len(agent_ids) + 11,
    )
    return compiled


def _collect_directly_referenced_agents(
    registry: RegistryManager,
    workflow_ids: object = None,
    workflow_step_scope: object = None,
) -> set[str]:
    """Return agent IDs referenced by runnable workflows or one scoped draft step."""
    referenced: set[str] = set()
    workflow_scope = _normalize_workflow_validation_scope(workflow_ids)
    normalized_step_scope = _normalize_workflow_step_scope(workflow_step_scope)
    workflows = [
        manifest
        for manifest in registry.workflows.list_all()
        if (
            manifest.enabled
            or (
                normalized_step_scope is not None
                and manifest.workflow_id == normalized_step_scope["workflow_id"]
            )
        )
        and (workflow_scope is None or manifest.workflow_id in workflow_scope)
    ]
    step_target = _resolve_workflow_step_target(workflows, normalized_step_scope)
    for manifest in workflows:
        if not manifest.enabled and step_target is None:
            continue
        if workflow_scope is not None and manifest.workflow_id not in workflow_scope:
            continue
        if step_target is not None and manifest is not step_target[0]:
            continue
        participant_bindings: dict[str, str] = {}
        for item in getattr(manifest, "participants", None) or []:
            key = str(getattr(item, "participant_key", "") or "").strip()
            agent_id = str(getattr(item, "agent_id", "") or "").strip()
            if key and agent_id:
                participant_bindings[key] = agent_id
        if not participant_bindings:
            settings = getattr(manifest, "settings", {})
            if isinstance(settings, dict):
                raw = settings.get("participants")
                if isinstance(raw, list):
                    for item in raw:
                        if not isinstance(item, dict):
                            continue
                        key = str(item.get("participant_key") or "").strip()
                        agent_id = str(item.get("agent_id") or item.get("agent") or "").strip()
                        if key and agent_id:
                            participant_bindings[key] = agent_id
        steps = [step_target[1]] if step_target is not None else manifest.steps
        for step in steps:
            concrete = str(getattr(step, "agent", "") or "").strip()
            participant_key = str(getattr(step, "participant_key", "") or "").strip()
            if participant_key and participant_key in participant_bindings:
                concrete = str(participant_bindings.get(participant_key) or "").strip()
            if concrete:
                referenced.add(concrete)
    return referenced


def _add_fixed_nodes(graph, registry: RegistryManager) -> None:
    """Add the fixed infrastructure nodes to the graph."""
    _ = registry
    from core.application.orchestration.control_flow_step_node import control_flow_step_node
    from core.application.orchestration.graph.nodes import execute_step_node
    from core.application.orchestration.invoke_workflow_step_node import invoke_workflow_step_node
    from core.application.orchestration.operation_retry_wait_node import operation_retry_wait_node
    from core.application.orchestration.parallel_executor_node import parallel_executor_node
    from core.application.orchestration.step_hub import orchestrator_hub_node
    from core.application.orchestration.validation_node import orchestrator_validation_node

    # task_intake — normalizes TaskEnvelope from older or canonical state format
    graph.add_node("task_intake", _task_intake_node)

    # kb_intake — pre-fetches KB context (reuses existing node)
    try:
        from core.application.orchestration.agents.kb_intake_node import kb_intake_node

        graph.add_node("kb_intake", kb_intake_node)
    except ImportError:
        graph.add_node("kb_intake", _passthrough_node)

    # workflow_routing — deterministic family + variant resolution
    try:
        from core.application.orchestration.routing_node import workflow_routing_node

        graph.add_node("workflow_routing", workflow_routing_node)
    except ImportError:
        graph.add_node("workflow_routing", _passthrough_node)

    # orchestrator hub
    graph.add_node("orchestrator", orchestrator_hub_node)

    # runtime parallel lane executor
    graph.add_node("parallel_executor", parallel_executor_node)
    graph.add_node("operation_retry_wait", operation_retry_wait_node)

    # Single-step executor
    graph.add_node("execute_step", execute_step_node)
    graph.add_node("dataset_query_step", execute_step_node)
    graph.add_node("tabular_feed_step", execute_step_node)

    # runtime child-workflow invoke step
    graph.add_node("invoke_workflow_step", invoke_workflow_step_node)

    # runtime control-flow step executor (if/switch/stop/set_variable/for_each/batch)
    graph.add_node("control_flow_step", control_flow_step_node)

    # authored Wait: a durable timer pause
    from core.application.orchestration.wait_step_runtime import wait_step_node

    graph.add_node("wait_step", wait_step_node)

    # final validation
    graph.add_node("orchestrator_validation", orchestrator_validation_node)

    # human approval gate — registry-aware version that always routes back to orchestrator
    graph.add_node("human_approval_gate", _registry_approval_gate_node)


def _task_intake_node(state: OrchestratorState) -> dict:
    """Ensure task_envelope is present before graph execution continues."""
    if state.get("task_envelope"):
        return {}  # Already set
    raise ValueError("task_intake requires explicit task_envelope in state")


def _passthrough_node(state: OrchestratorState) -> dict:
    """No-op passthrough node."""
    return {}


def _human_approval_fallback_node(state: OrchestratorState) -> dict:
    """Minimal human approval gate if the real one isn't available."""
    _ = state
    return {"needs_user_approval": True}
