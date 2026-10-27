"""Reusable state-reader graph cache helpers."""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from typing import Any

from core.infrastructure.config.runtime_env import runtime_env as _runtime_env


def _open_postgres_checkpointer(uri: str):
    from langgraph.checkpoint.postgres import PostgresSaver

    return PostgresSaver.from_conn_string(uri)


def get_committed_checkpoint_values(
    thread_id: str,
    *,
    config: dict[str, Any],
    logger,
    open_checkpointer: Callable[[], Any] | None = None,
) -> dict[str, Any]:
    """Read the latest committed channel values through the checkpointer contract."""
    normalized_thread_id = str(thread_id or "").strip()
    uri = str(config.get("langgraph_postgres_uri", "") or "").strip()
    if not normalized_thread_id or not uri:
        return {}

    factory = open_checkpointer or (lambda: _open_postgres_checkpointer(uri))
    for attempt in range(2):
        try:
            with factory() as checkpointer:
                checkpoint_tuple = checkpointer.get_tuple(
                    {"configurable": {"thread_id": normalized_thread_id}}
                )
            checkpoint = getattr(checkpoint_tuple, "checkpoint", None)
            if not isinstance(checkpoint, Mapping):
                return {}
            values = checkpoint.get("channel_values")
            return dict(values) if isinstance(values, Mapping) else {}
        except Exception as exc:
            if attempt == 0:
                logger.warning(
                    "Committed checkpoint read failed for thread %s; reconnecting once: %s",
                    normalized_thread_id,
                    exc,
                )
            else:
                logger.debug(
                    "Committed checkpoint read retry failed for thread %s: %s",
                    normalized_thread_id,
                    exc,
                )
    return {}


def get_committed_pause(
    thread_id: str,
    *,
    config: dict[str, Any],
    logger,
    open_checkpointer: Callable[[], Any] | None = None,
) -> Any | None:
    """The latest committed checkpoint as ``(values, pending interrupts)``.

    Shaped like a LangGraph state snapshot (``values`` and ``tasks[].interrupts``)
    without building a graph: interrupts are the checkpoint's unanswered
    ``__interrupt__`` pending writes. ``None`` when nothing can be read.
    """
    from types import SimpleNamespace

    normalized_thread_id = str(thread_id or "").strip()
    uri = str(config.get("langgraph_postgres_uri", "") or "").strip()
    if not normalized_thread_id or not uri:
        return None
    factory = open_checkpointer or (lambda: _open_postgres_checkpointer(uri))
    try:
        with factory() as checkpointer:
            checkpoint_tuple = checkpointer.get_tuple(
                {"configurable": {"thread_id": normalized_thread_id}}
            )
    except Exception as exc:
        logger.debug("Committed pause read failed for thread %s: %s", normalized_thread_id, exc)
        return None
    checkpoint = getattr(checkpoint_tuple, "checkpoint", None)
    if not isinstance(checkpoint, Mapping):
        return None
    values = checkpoint.get("channel_values")
    writes = list(getattr(checkpoint_tuple, "pending_writes", None) or [])
    answered = {str(write[0]) for write in writes if len(write) >= 2 and write[1] == "__resume__"}
    interrupts: list[Any] = []
    for write in writes:
        if len(write) < 3 or write[1] != "__interrupt__" or str(write[0]) in answered:
            continue
        raw = write[2]
        interrupts.extend(raw if isinstance(raw, (list, tuple)) else [raw])
    return SimpleNamespace(
        values=dict(values) if isinstance(values, Mapping) else {},
        tasks=[SimpleNamespace(interrupts=interrupts)] if interrupts else [],
    )


def get_checkpointer(*, config: dict[str, Any], logger) -> Any:
    """Return a connected PostgresSaver checkpointer instance if configured."""
    uri = str(config.get("langgraph_postgres_uri", "") or "").strip()
    if not uri:
        return None
    try:
        import psycopg
        from langgraph.checkpoint.postgres import PostgresSaver
        from psycopg.rows import dict_row

        conn = psycopg.connect(uri, autocommit=True, row_factory=dict_row)
        return PostgresSaver(conn)
    except Exception as exc:
        logger.warning("PostgresSaver init failed: %s", exc)
        return None


def reset_cached_graph(*, state_lock, set_cached_graph) -> None:
    with state_lock:
        set_cached_graph(None, "")


def get_state_reader_graph(
    *,
    config: dict[str, Any],
    force_refresh: bool,
    state_lock,
    get_cached_graph,
    set_cached_graph,
    get_checkpointer,
    build_graph,
):
    """Return a cached compiled LangGraph for read-only state lookups."""
    dsn = str(config.get("langgraph_postgres_uri", "") or "").strip()
    if not dsn:
        return None

    if force_refresh:
        reset_cached_graph(state_lock=state_lock, set_cached_graph=set_cached_graph)

    cached_graph, cached_dsn = get_cached_graph()
    if cached_graph is not None and cached_dsn == dsn:
        return cached_graph

    with state_lock:
        cached_graph, cached_dsn = get_cached_graph()
        if cached_graph is not None and cached_dsn == dsn:
            return cached_graph
        checkpointer = get_checkpointer()
        if not checkpointer:
            return None
        graph = build_graph(
            checkpointer=checkpointer,
            validate_contracts=False,
        )
        set_cached_graph(graph, dsn)
        return graph


def get_thread_state_snapshot(
    thread_id: str,
    *,
    get_state_reader_graph,
    logger,
):
    """Return a LangGraph checkpoint snapshot with one cache refresh retry."""
    if not thread_id:
        return None
    for attempt in range(2):
        try:
            graph = get_state_reader_graph(force_refresh=attempt > 0)
            if not graph:
                return None
            return graph.get_state({"configurable": {"thread_id": thread_id}})
        except Exception as exc:
            if attempt == 0:
                logger.warning(
                    "State graph read failed for thread %s; refreshing cached graph: %s",
                    thread_id,
                    exc,
                )
            else:
                logger.debug("State graph read retry failed for thread %s: %s", thread_id, exc)
    return None


def prewarm_state_reader_graph(
    *,
    config: dict[str, Any],
    get_state_reader_graph,
    logger,
    getenv=_runtime_env.get,
    perf_counter=time.perf_counter,
) -> None:
    """Compile the read-side LangGraph during startup to avoid first-request stalls."""
    if getenv("PYTEST_CURRENT_TEST"):
        return
    if not str(config.get("langgraph_postgres_uri", "") or "").strip():
        return
    try:
        started_at = perf_counter()
        graph = get_state_reader_graph(force_refresh=True)
        if graph is not None:
            logger.info("State reader graph prewarmed in %.3fs", perf_counter() - started_at)
    except Exception as exc:
        logger.warning("State reader graph prewarm failed: %s", exc)
