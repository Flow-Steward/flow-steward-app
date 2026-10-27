"""Shared LangGraph checkpoint reader for non-transport runtimes."""

from __future__ import annotations

import logging
import threading
import time

from core.infrastructure.config.runtime_config import CONFIG
from core.infrastructure.runtime import state_reader_runtime

logger = logging.getLogger(__name__)

_state_reader_graph = None
_state_reader_graph_dsn = ""
_state_reader_graph_lock = threading.Lock()


def get_checkpointer():
    """Return a configured checkpoint saver for non-transport runtime code."""
    return state_reader_runtime.get_checkpointer(config=CONFIG, logger=logger)


def reset_state_reader_graph() -> None:
    global _state_reader_graph, _state_reader_graph_dsn
    with _state_reader_graph_lock:
        _state_reader_graph = None
        _state_reader_graph_dsn = ""


def get_state_reader_graph(*, force_refresh: bool = False):
    global _state_reader_graph, _state_reader_graph_dsn
    dsn = str(CONFIG.get("langgraph_postgres_uri") or "")
    if not dsn:
        return None

    if force_refresh:
        reset_state_reader_graph()

    if _state_reader_graph is not None and _state_reader_graph_dsn == dsn:
        return _state_reader_graph

    with _state_reader_graph_lock:
        if _state_reader_graph is not None and _state_reader_graph_dsn == dsn:
            return _state_reader_graph
        checkpointer = get_checkpointer()
        if not checkpointer:
            return None
        from core.application.orchestration.graph_runtime import build_registry_graph

        _state_reader_graph = build_registry_graph(
            checkpointer=checkpointer,
            validate_contracts=False,
        )
        _state_reader_graph_dsn = dsn
        return _state_reader_graph


def get_thread_state_snapshot(thread_id: str, *, force_refresh: bool = False):
    if not thread_id:
        return None
    for attempt in range(2):
        try:
            graph = get_state_reader_graph(force_refresh=force_refresh or attempt > 0)
            if not graph:
                return None
            return graph.get_state({"configurable": {"thread_id": thread_id}})
        except Exception as exc:
            if attempt == 0:
                logger.warning(
                    "State reader failed for thread %s — refreshing cached graph: %s",
                    thread_id,
                    exc,
                )
            else:
                logger.debug("State reader retry failed for thread %s: %s", thread_id, exc)
    return None


def get_thread_state_values(thread_id: str, *, force_refresh: bool = False) -> dict:
    snap = get_thread_state_snapshot(thread_id, force_refresh=force_refresh)
    return (snap.values or {}) if snap else {}


def prewarm_state_reader_graph() -> None:
    if not str(CONFIG.get("langgraph_postgres_uri") or "").strip():
        return
    try:
        started_at = time.perf_counter()
        graph = get_state_reader_graph(force_refresh=True)
        if graph is not None:
            logger.info("State reader graph prewarmed in %.3fs", time.perf_counter() - started_at)
    except Exception as exc:
        logger.warning("State reader graph prewarm failed: %s", exc)
