"""The last review decision of a job, here or down its Invoke chain (ADR 0075).

A typed review is decided on the job that holds the Approval: the job itself or
a child it invoked (at any depth). The decision is kept in that job's approval
history (``meta.approvals``), already masked as the public review is: the
accepted values with their fields, what the person corrected and who decided.

The chain is read down from the job through ``meta.invoke_children`` and only
followed to a child that names this parent (``meta.invoke_parent_job_id``) in
the same project, so a link cannot reach an unrelated job. A finished chain is
read too: unlike the waiting walk, a consumed link still leads to its child.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from typing import Any

from core.application.jobs.public_approval_serializer import serialize_approval_history_entry

#: Jobs read down one chain at most; the Invoke depth limit bounds the levels.
MAX_CHAIN_JOBS = 50


def _text(value: Any) -> str:
    return str(value or "").strip()


def _mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return {}
    return dict(value) if isinstance(value, Mapping) else {}


def _get_job(bl: Any, job_id: str) -> dict[str, Any]:
    if not job_id or not hasattr(bl, "get_job"):
        return {}
    try:
        row = bl.get_job(job_id)
    except Exception:
        return {}
    return dict(row) if isinstance(row, Mapping) else {}


def invoke_chain_jobs(
    bl: Any, job_id: str, row: Mapping[str, Any], *, max_depth: int
) -> list[tuple[str, dict[str, Any]]]:
    """The job and the descendants it invoked, breadth first, bounded and cycle-safe."""
    found: list[tuple[str, dict[str, Any]]] = [(job_id, dict(row))]
    seen = {job_id}
    level = [(job_id, dict(row))]
    for _ in range(max(0, max_depth)):
        next_level: list[tuple[str, dict[str, Any]]] = []
        for parent_id, parent in level:
            children = _mapping(_mapping(parent.get("meta")).get("invoke_children"))
            for child_id in sorted(_text(key) for key in children):
                if not child_id or child_id in seen or len(found) >= MAX_CHAIN_JOBS:
                    continue
                child = _get_job(bl, child_id)
                if (
                    not child
                    or _text(_mapping(child.get("meta")).get("invoke_parent_job_id")) != parent_id
                    or _text(child.get("project_id")) != _text(parent.get("project_id"))
                ):
                    continue
                seen.add(child_id)
                found.append((child_id, child))
                next_level.append((child_id, child))
        if not next_level:
            break
        level = next_level
    return found


def _review_entries(row: Mapping[str, Any]) -> list[dict[str, Any]]:
    approvals = _mapping(row.get("meta")).get("approvals")
    return [
        dict(entry)
        for entry in (approvals if isinstance(approvals, list) else [])
        if isinstance(entry, Mapping)
        and (entry.get("typed_review") or "review" in entry or "review_changes" in entry)
    ]


def last_review_decision(
    bl: Any,
    job_id: str,
    row: Mapping[str, Any],
    *,
    can_read: Callable[[Mapping[str, Any]], bool],
    max_depth: int,
) -> dict[str, Any] | None:
    """The newest decided typed review of the job or its Invoke descendants.

    ``None`` when no review was decided. A decision on a job the caller may not
    read says only that it exists (``access: "none"``), as the review endpoint
    does for a pending one: no values, no author, no job id.
    """
    best: tuple[str, str, dict[str, Any], dict[str, Any]] | None = None
    for holder_id, holder in invoke_chain_jobs(bl, job_id, row, max_depth=max_depth):
        for entry in _review_entries(holder):
            stamp = _text(entry.get("timestamp"))
            if best is None or stamp >= best[0]:
                best = (stamp, holder_id, holder, entry)
    if best is None:
        return None
    _stamp, holder_id, holder, entry = best
    if not can_read(holder):
        return {"access": "none"}
    public = serialize_approval_history_entry(entry)
    approved = bool(public.get("approved"))
    # A rejection accepted nothing: no values and no corrections, only who
    # rejected it and why. Edits made before rejecting never reached the workflow.
    review = public.get("review") if approved and isinstance(public.get("review"), dict) else {}
    return {
        "access": "read",
        "job_id": holder_id,
        "approval_id": public.get("approval_id"),
        "approved": approved,
        "actor": public.get("actor"),
        "actor_type": public.get("actor_type"),
        "timestamp": public.get("timestamp"),
        "comment": public.get("comment"),
        "fields": list(review.get("fields") or []),
        "values": dict(review.get("values") or {}),
        "changes": list(public.get("review_changes") or []) if approved else [],
    }


__all__ = ["MAX_CHAIN_JOBS", "invoke_chain_jobs", "last_review_decision"]
