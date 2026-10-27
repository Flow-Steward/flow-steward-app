"""Parent ↔ child links of ``invoke_workflow``, kept in the parent job row.

``jobs.meta.invoke_children`` maps a child job id to its invocation entry
(``invocation_key`` = parent job + step execution, workflow, step identity,
input fingerprint). The link is written inside the transaction that creates
the child job, under the parent row lock: a crash can never leave a child
without its parent knowing, a second child for the same invocation is
refused, and a parent whose cancel committed first cannot get a new child.

Each entry is updated field by field (``jsonb_set``), never replaced, so the
cancel and consumption marks never drop the invocation identity.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

#: A parent in one of these states takes no new child.
PARENT_CLOSED_STATUSES = frozenset(
    {"completed", "failed", "cancelled", "max_iterations", "stuck", "orphaned"}
)


class InvokeChildAlreadyLinked(RuntimeError):
    """The invocation already has its child; the caller reuses it."""

    def __init__(self, child_job_id: str) -> None:
        super().__init__(f"invocation already has child job {child_job_id}")
        self.child_job_id = child_job_id


class InvokeParentClosed(RuntimeError):
    """The parent was cancelled or finished before the child could be linked."""

    def __init__(self, status: str) -> None:
        super().__init__(f"parent job is {status}; no child is created")
        self.status = status


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _row_value(row: Any, index: int, key: str) -> Any:
    if isinstance(row, Mapping):
        return row.get(key)
    return row[index] if row is not None and len(row) > index else None


def _as_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return {}
    return dict(value) if isinstance(value, Mapping) else {}


# Bound corrupt ancestry independently of the authored invocation depth limit.
MAX_CANCEL_ANCESTRY_DEPTH = 64


class InvokeCancellationFenceUnavailable(RuntimeError):
    """The execution boundary could not establish durable cancellation state."""

    error_code = "invoke_cancellation_fence_unavailable"

    def __init__(self) -> None:
        super().__init__("Workflow cancellation state is unavailable; execution stopped")


def read_cancellation_job(bl: Any, job_id: str) -> Any:
    """Read the host row without treating store errors as permission to execute."""
    try:
        return bl.get_job(job_id)
    except Exception as exc:
        # The store port can wrap different database drivers. Translate its
        # failures once; callers distinguish unavailability from cancellation.
        raise InvokeCancellationFenceUnavailable() from exc


def ancestor_cancellation_requested(bl: Any, *, job_id: str, job_row: Mapping[str, Any]) -> bool:
    """Observe a requested cancel through authoritative, unconsumed parent links.

    Finished children do not propagate cancellation from their own parents.
    Their independently recorded outgoing requests still apply to live children.
    This is an execution-boundary read, not a lock across effect dispatch.
    """
    from core.application.observability_vocabulary import job_status_meta

    current_id = str(job_id)
    current = job_row
    visited = {current_id}
    for _ in range(MAX_CANCEL_ANCESTRY_DEPTH):
        if job_status_meta(str(current.get("status") or ""))["terminal"] or current.get(
            "finished_at"
        ):
            return False
        parent_id = str(_as_dict(current.get("meta")).get("invoke_parent_job_id") or "").strip()
        if not parent_id:
            return False
        if parent_id in visited:
            raise InvokeCancellationFenceUnavailable()
        parent = read_cancellation_job(bl, parent_id)
        if not isinstance(parent, Mapping) or not parent:
            raise InvokeCancellationFenceUnavailable()
        project_id = str(current.get("project_id") or "").strip()
        if (
            str(parent.get("id") or "") != parent_id
            or not project_id
            or str(parent.get("project_id") or "") != project_id
        ):
            return False
        entry = _as_dict(
            _as_dict(_as_dict(parent.get("meta")).get("invoke_children")).get(current_id)
        )
        if (
            not entry
            or str(entry.get("child_job_id") or "") != current_id
            or "consumed_at" in entry
        ):
            return False
        if "cancel_requested_at" in entry:
            return True
        visited.add(parent_id)
        current_id, current = parent_id, parent
    raise InvokeCancellationFenceUnavailable()


def link_child_in_transaction(
    conn: Any,
    *,
    parent_job_id: str,
    child_job_id: str,
    entry: Mapping[str, Any],
) -> None:
    """Link a just-created child to its parent in the creating transaction.

    Raises :class:`InvokeChildAlreadyLinked` when the invocation already has a
    child and :class:`InvokeParentClosed` when the parent no longer runs; the
    caller's transaction then rolls back the new child row.
    """
    invocation_key = str(entry.get("invocation_key") or "")
    with conn.cursor() as cur:
        cur.execute(
            "SELECT status, meta->'invoke_children' AS invoke_children "
            "FROM jobs WHERE id = %s FOR UPDATE",
            (str(parent_job_id),),
        )
        row = cur.fetchone()
        status = str(_row_value(row, 0, "status") or "").strip().lower()
        if status in PARENT_CLOSED_STATUSES:
            raise InvokeParentClosed(status)
        children = _as_dict(_row_value(row, 1, "invoke_children"))
        for existing_id, existing in children.items():
            if (
                invocation_key
                and isinstance(existing, Mapping)
                and str(existing.get("invocation_key") or "") == invocation_key
            ):
                raise InvokeChildAlreadyLinked(str(existing_id))
        linked = {**dict(entry), "child_job_id": str(child_job_id), "linked_at": _now_iso()}
        cur.execute(
            """
            UPDATE jobs
               SET meta = jsonb_set(
                   COALESCE(meta, '{}'::jsonb),
                   '{invoke_children}',
                   COALESCE(meta->'invoke_children', '{}'::jsonb)
                       || jsonb_build_object(%s::text, %s::jsonb),
                   true
               )
             WHERE id = %s
            """,
            (str(child_job_id), json.dumps(linked, default=str), str(parent_job_id)),
        )


def mark_child(
    conn: Any,
    *,
    parent_job_id: str,
    child_job_id: str,
    fields: Mapping[str, Any],
) -> bool:
    """Set fields on one existing child entry without touching the others."""
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE jobs
               SET meta = jsonb_set(
                   meta,
                   ARRAY['invoke_children', %s::text],
                   (meta->'invoke_children'->%s::text) || %s::jsonb,
                   false
               )
             WHERE id = %s
               AND meta->'invoke_children' ? %s::text
            """,
            (
                str(child_job_id),
                str(child_job_id),
                json.dumps(dict(fields), default=str),
                str(parent_job_id),
                str(child_job_id),
            ),
        )
        return bool(getattr(cur, "rowcount", 0))


INVOKE_CANCEL_PENDING_KEY = "invoke_cancel_pending"


def request_child_cancels(conn: Any, *, parent_job_id: str) -> None:
    """Record a cancel request on every child link not yet consumed or requested.

    Runs in the parent's cancel transaction, under its row lock, so the request
    is durable before the cancel is acknowledged. ``invoke_cancel_pending``
    marks the parent for the control-process cancel scan.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE jobs
               SET meta = jsonb_set(
                   meta,
                   '{invoke_children}',
                   (
                       SELECT jsonb_object_agg(
                           child.key,
                           CASE
                               WHEN jsonb_typeof(child.value) <> 'object'
                                 OR child.value ? 'cancel_requested_at'
                                 OR child.value ? 'consumed_at'
                               THEN child.value
                               ELSE child.value
                                    || jsonb_build_object('cancel_requested_at', NOW()::text)
                           END
                       )
                         FROM jsonb_each(meta->'invoke_children') AS child
                   ),
                   false
               ) || jsonb_build_object(%s::text, true)
             WHERE id = %s
               AND jsonb_typeof(meta->'invoke_children') = 'object'
               AND meta->'invoke_children' <> '{}'::jsonb
            """,
            (INVOKE_CANCEL_PENDING_KEY, str(parent_job_id)),
        )


def pending_child_cancels(conn: Any, *, limit: int) -> list[tuple[str, list[str]]]:
    """Parents with requested, not yet applied child cancels, and those children."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT j.id::text, child.key
              FROM jobs j
              CROSS JOIN LATERAL jsonb_each(j.meta->'invoke_children') AS child
             WHERE j.meta ? %s
               AND jsonb_typeof(child.value) = 'object'
               AND child.value ? 'cancel_requested_at'
               AND NOT (child.value ? 'cancel_applied_at')
             ORDER BY j.id
             LIMIT %s
            """,
            (INVOKE_CANCEL_PENDING_KEY, max(1, int(limit))),
        )
        rows = cur.fetchall() or []
    pending: dict[str, list[str]] = {}
    for row in rows:
        parent_id = str(_row_value(row, 0, "id") or "")
        child_id = str(_row_value(row, 1, "key") or "")
        if parent_id and child_id:
            pending.setdefault(parent_id, []).append(child_id)
    return list(pending.items())


def settle_cancel_pending(conn: Any, *, parent_job_id: str) -> None:
    """Drop the pending mark once every requested child cancel is applied."""
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE jobs
               SET meta = meta - %s::text
             WHERE id = %s
               AND NOT EXISTS (
                   SELECT 1
                     FROM jsonb_each(meta->'invoke_children') AS child
                    WHERE jsonb_typeof(child.value) = 'object'
                      AND child.value ? 'cancel_requested_at'
                      AND NOT (child.value ? 'cancel_applied_at')
               )
            """,
            (INVOKE_CANCEL_PENDING_KEY, str(parent_job_id)),
        )


def child_entry_for_invocation(
    parent_meta: Mapping[str, Any], invocation_key: str
) -> tuple[str, dict[str, Any]] | None:
    """The linked child of one invocation, from committed parent metadata."""
    children = _as_dict(parent_meta.get("invoke_children"))
    for child_id, entry in children.items():
        if isinstance(entry, Mapping) and str(entry.get("invocation_key") or "") == invocation_key:
            return str(child_id), dict(entry)
    return None


__all__ = [
    "INVOKE_CANCEL_PENDING_KEY",
    "PARENT_CLOSED_STATUSES",
    "InvokeChildAlreadyLinked",
    "InvokeParentClosed",
    "child_entry_for_invocation",
    "link_child_in_transaction",
    "mark_child",
    "pending_child_cancels",
    "request_child_cancels",
    "settle_cancel_pending",
]


def settle_checkpointed_children(bl: Any, *, parent_job_id: str, values: Mapping[str, Any]) -> None:
    """Release only results actually present in the parent's committed checkpoint."""
    conn = getattr(bl, "conn", None)
    if conn is None:
        return
    parent = bl.get_job(parent_job_id)
    children = _as_dict(_as_dict((parent or {}).get("meta")).get("invoke_children"))
    if not children:
        return
    changed = False
    for message in values.get("agent_messages") or []:
        if not isinstance(message, Mapping):
            continue
        artifacts = _as_dict(message.get("artifacts"))
        invocation = _as_dict(artifacts.get("_invoke_workflow"))
        child_id = str(invocation.get("child_job_id") or "")
        if not child_id:
            error = artifacts.get("_invoke_workflow_error")
            terminal_failure = error == "child_failed" and artifacts.get(
                "_invoke_workflow_child_status"
            ) in {"completed", "failed", "cancelled", "max_iterations"}
            completed_result_error = error in {
                "missing_child_outputs",
                "missing_child_artifacts",
                "child_artifact_adoption_failed",
            }
            if terminal_failure or completed_result_error:
                child_id = str(artifacts.get("_invoke_workflow_child_job_id") or "")
        entry = children.get(child_id)
        if (
            not isinstance(entry, Mapping)
            or entry.get("consumed_at")
            or not entry.get("step_execution_id")
            or entry.get("step_execution_id") != message.get("step_execution_id")
        ):
            continue
        changed = (
            mark_child(
                conn,
                parent_job_id=parent_job_id,
                child_job_id=child_id,
                fields={"consumed_at": _now_iso()},
            )
            or changed
        )
    if changed:
        conn.commit()
