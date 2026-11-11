"""Bounded LangGraph checkpoint history.

LangGraph's PostgresSaver keeps every checkpoint of a thread, each with its own
copy of every changed channel in ``checkpoint_blobs`` and the step's writes in
``checkpoint_writes``. Workflow state carries ``agent_messages`` as one growing
list that every step rewrites whole, so a run of N steps stored N copies of a
list of up to N messages: 1.9 GB for one 434-step run on the demo stand.

Flow Steward reads only the latest checkpoint of a thread (``get_state``,
resume, retry); it never walks the history. Keeping the latest checkpoint, its
pending writes and the blobs it references is therefore everything a thread
needs, and its storage stays the size of its current state.

This module holds no LangGraph import so the control plane, which runs the
retention pass, can use it without the orchestration runtime.
"""

from __future__ import annotations

from typing import Any

# One statement, so every delete sees the same snapshot. The kept checkpoint must
# exist and use the current format: a v<4 checkpoint still reads its parent's
# pending sends, so its parent cannot go. Checkpoint ids order by time as text,
# which is how PostgresSaver itself finds the latest one.
PRUNE_THREAD_HISTORY_SQL = """
WITH kept AS (
    SELECT checkpoint
    FROM checkpoints
    WHERE thread_id = %(thread_id)s
      AND checkpoint_ns = %(checkpoint_ns)s
      AND checkpoint_id = %(checkpoint_id)s
      AND COALESCE((checkpoint ->> 'v')::int, 0) >= 4
),
dropped_writes AS (
    DELETE FROM checkpoint_writes
    WHERE thread_id = %(thread_id)s
      AND checkpoint_ns = %(checkpoint_ns)s
      AND checkpoint_id < %(checkpoint_id)s
      AND EXISTS (SELECT 1 FROM kept)
    RETURNING 1
),
dropped_blobs AS (
    DELETE FROM checkpoint_blobs AS blob
    WHERE blob.thread_id = %(thread_id)s
      AND blob.checkpoint_ns = %(checkpoint_ns)s
      AND EXISTS (SELECT 1 FROM kept)
      AND NOT EXISTS (
          SELECT 1
          FROM kept, jsonb_each_text(kept.checkpoint -> 'channel_versions') AS version
          WHERE version.key = blob.channel
            AND version.value = blob.version
      )
    RETURNING 1
),
dropped_checkpoints AS (
    DELETE FROM checkpoints
    WHERE thread_id = %(thread_id)s
      AND checkpoint_ns = %(checkpoint_ns)s
      AND checkpoint_id < %(checkpoint_id)s
      AND EXISTS (SELECT 1 FROM kept)
    RETURNING 1
)
SELECT
    (SELECT count(*) FROM dropped_writes)
    + (SELECT count(*) FROM dropped_blobs)
    + (SELECT count(*) FROM dropped_checkpoints) AS dropped
"""

# The latest checkpoint of every namespace of the given threads.
LATEST_THREAD_CHECKPOINTS_SQL = """
SELECT DISTINCT ON (thread_id, checkpoint_ns)
    thread_id, checkpoint_ns, checkpoint_id
FROM checkpoints
WHERE thread_id = ANY(%(thread_ids)s)
ORDER BY thread_id, checkpoint_ns, checkpoint_id DESC
"""


def _first_value(row: Any) -> Any:
    if row is None:
        return None
    if isinstance(row, dict):
        return next(iter(row.values()), None)
    return row[0]


def prune_thread_history(
    cur: Any,
    *,
    thread_id: str,
    checkpoint_ns: str,
    checkpoint_id: str,
) -> int:
    """Drop every checkpoint of one thread namespace older than ``checkpoint_id``.

    Also drops their pending writes and every blob the kept checkpoint does not
    reference. Returns the number of rows removed.
    """
    cur.execute(
        PRUNE_THREAD_HISTORY_SQL,
        {
            "thread_id": str(thread_id),
            "checkpoint_ns": str(checkpoint_ns or ""),
            "checkpoint_id": str(checkpoint_id),
        },
    )
    return int(_first_value(cur.fetchone()) or 0)


def prune_threads_to_latest(cur: Any, thread_ids: list[str]) -> int:
    """Keep only the latest checkpoint of every namespace of ``thread_ids``.

    Callers pass threads with no writer: a running thread prunes itself on
    every checkpoint it saves.
    """
    if not thread_ids:
        return 0
    cur.execute(LATEST_THREAD_CHECKPOINTS_SQL, {"thread_ids": list(thread_ids)})
    latest = [
        (row["thread_id"], row["checkpoint_ns"], row["checkpoint_id"])
        if isinstance(row, dict)
        else (row[0], row[1], row[2])
        for row in cur.fetchall()
    ]
    return sum(
        prune_thread_history(
            cur,
            thread_id=thread_id,
            checkpoint_ns=checkpoint_ns,
            checkpoint_id=checkpoint_id,
        )
        for thread_id, checkpoint_ns, checkpoint_id in latest
    )


__all__ = [
    "LATEST_THREAD_CHECKPOINTS_SQL",
    "PRUNE_THREAD_HISTORY_SQL",
    "prune_thread_history",
    "prune_threads_to_latest",
]
