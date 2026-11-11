"""The LangGraph checkpointer every workflow run writes through."""

from __future__ import annotations

import logging
from typing import Any

from core.infrastructure.db.checkpoint_history import prune_thread_history

logger = logging.getLogger(__name__)

_BOUNDED_CLASSES: dict[type, type] = {}


def _prune_after_put(saver: Any, next_config: Any) -> None:
    configurable = (next_config or {}).get("configurable") or {}
    try:
        with saver._cursor() as cur:
            prune_thread_history(
                cur,
                thread_id=str(configurable["thread_id"]),
                checkpoint_ns=str(configurable.get("checkpoint_ns") or ""),
                checkpoint_id=str(configurable["checkpoint_id"]),
            )
    except Exception as exc:
        logger.warning("Checkpoint history pruning failed: %s", type(exc).__name__)


def history_bounded_saver_class() -> type[Any]:
    """PostgresSaver that keeps only the checkpoint it has just saved.

    LangGraph serialises the ``put`` calls of one run, and every pending write of
    the next step belongs to the checkpoint just saved, so removing what is older
    right after the save never touches anything a running step still needs.
    Pruning is best effort: a failure costs disk, never the run.

    The class is derived from whichever PostgresSaver is importable at call
    time, so an import failure surfaces where the caller already handles it.
    """
    from langgraph.checkpoint.postgres import PostgresSaver

    bounded = _BOUNDED_CLASSES.get(PostgresSaver)
    if bounded is None:

        def put(self: Any, config: Any, checkpoint: Any, metadata: Any, new_versions: Any) -> Any:
            next_config = PostgresSaver.put(self, config, checkpoint, metadata, new_versions)
            _prune_after_put(self, next_config)
            return next_config

        bounded = type(
            "HistoryBoundedPostgresSaver",
            (PostgresSaver,),
            {"put": put, "__doc__": history_bounded_saver_class.__doc__},
        )
        _BOUNDED_CLASSES[PostgresSaver] = bounded
    return bounded


__all__ = ["history_bounded_saver_class"]
