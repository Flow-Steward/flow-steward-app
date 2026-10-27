"""
Flow Steward LangGraph StateGraph — the central orchestration graph.

All agents are loaded from the DB registry and run through PromptAgent.
The hub-and-spoke graph routes dynamically through workflow steps and execution groups.
"""

import logging

from core.infrastructure.config import CONFIG

logger = logging.getLogger(__name__)

_BUILTIN_TOOL_IDS = frozenset({"workflow_ops"})

#: LangGraph super-steps one invocation may take. Each executed step is two
#: (the hub and its executor), so a For each of N items over a body of B steps
#: needs about 2·N·(B+1). The hub's own guard stops a run that makes no authored
#: progress; this ceiling only keeps a finite pass from tripping LangGraph's
#: default of about ten thousand (ADR 0038).
GRAPH_RECURSION_LIMIT = 1_000_000


def graph_run_config(thread_id: str) -> dict:
    """The LangGraph config every run and resume of a workflow thread uses."""
    return {"configurable": {"thread_id": thread_id}, "recursion_limit": GRAPH_RECURSION_LIMIT}


def _get_checkpointer():
    """Return the Postgres-backed LangGraph checkpointer, or None when unusable.

    None is never a licence to run without one: the graph builder refuses it.
    """
    uri = str(CONFIG.get("langgraph_postgres_uri") or "")
    if not uri:
        return None
    try:
        import psycopg
        from langgraph.checkpoint.postgres import PostgresSaver
        from psycopg.rows import dict_row

        # autocommit=True is required for PostgresSaver checkpoint persistence.
        conn = psycopg.connect(uri, autocommit=True, row_factory=dict_row)
        return PostgresSaver(conn)
    except Exception as e:
        logger.warning("PostgresSaver init failed in core.graph: %s", type(e).__name__)
        return None


def _registry_appears_unseeded(registry) -> bool:
    """Detect stale singleton state after DB wipe/bootstrap before graph build."""
    try:
        agent_count = len(registry.agents.list_all() or [])
        workflow_count = len(registry.workflows.list_all() or [])
        skill_count = len(registry.skills.list_all() or [])
        tool_ids = {
            str(getattr(tool, "tool_id", "") or "").strip()
            for tool in (registry.tools.list_all() or [])
        }
    except Exception as exc:
        logger.debug("Registry readiness probe failed: %s", exc)
        return False

    return (
        agent_count == 0
        and workflow_count == 0
        and skill_count == 0
        and tool_ids.issubset(_BUILTIN_TOOL_IDS)
    )


def _ensure_registry_ready_for_graph(registry, *, refresh: bool = True):
    """Reload registry from DB before graph build so runtime sees fresh mutations."""
    if not refresh:
        return registry
    try:
        registry.reload_all()
    except Exception as exc:
        logger.warning("Registry reload during graph build failed: %s", exc)
        return registry

    if _registry_appears_unseeded(registry):
        logger.info("Registry still appears unseeded after graph-build reload")
    return registry


def build_registry_graph(
    checkpointer=None,
    *,
    validate_contracts: bool = True,
    workflow_ids: object = None,
    workflow_step_scope: object = None,
    registry=None,
    refresh_registry: bool = True,
):
    """Build and compile the Flow Steward LangGraph StateGraph from the registry.

    Uses the hub-and-spoke architecture:
    - Fixed infrastructure nodes: task_intake, kb_intake
    - Dynamic agent nodes: loaded from registry DB manifests
    - Central orchestrator hub: routes between workflow steps and execution groups
    """
    from core.application.orchestration.graph.builder import build_graph_from_registry
    from core.application.registry.registry_manager import RegistryManager

    active_registry = registry or RegistryManager.instance()
    active_registry = _ensure_registry_ready_for_graph(
        active_registry,
        refresh=refresh_registry,
    )
    logger.info("Building registry-driven graph")
    return build_graph_from_registry(
        active_registry,
        checkpointer=checkpointer,
        validate_contracts=validate_contracts,
        workflow_ids=workflow_ids,
        workflow_step_scope=workflow_step_scope,
    )
