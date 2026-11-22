"""Which job holds the approval a job waits for, down its Invoke chain (ADR 0075).

A job waits for a person's review when it is ``awaiting_approval`` itself, or
when it is ``awaiting_child`` and somewhere below it, at any depth, an invoked
child waits for approval (``A -> B -> C``, Approval in ``C``: ``A`` and ``B``
wait for review). The links are the ones
:mod:`core.application.workflows.approval_waiting_propagation` walks for the
approval notifications (``trusted_parent_link`` and ``ancestor_waits_on_child``),
read here in one recursive SQL statement instead of a walk per row:

* the child names the parent in ``meta.invoke_parent_job_id``, and both are in
  the same project;
* the parent links exactly that child in ``meta.invoke_children`` with an entry
  that is neither cancelled nor consumed;
* the parent is ``awaiting_child`` on that child (``meta.workflow_wait`` of kind
  ``child``), on the same step execution when the link records one.

The walk starts at the approvals and goes up, ``max_depth`` parents at most,
and never revisits a job (a cycle in the links stops it).
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from typing import Any

from core.infrastructure.db import psycopg_runtime as psycopg_runtime

#: The operator list's status value for "waits for a person's review".
AWAITING_REVIEW_STATUS = "awaiting_review"
#: Used when the caller does not pass the Invoke depth limit.
DEFAULT_REVIEW_CHAIN_DEPTH = 5

_ENTRY = "(parent.meta -> 'invoke_children' -> (child.id::text))"
_LINK = f"""
    parent.id <> child.id
    AND parent.project_id = child.project_id
    AND parent.deleted_at IS NULL
    AND (child.meta ->> 'invoke_parent_job_id') = parent.id::text
    AND (parent.meta -> 'workflow_wait' ->> 'kind') = 'child'
    AND jsonb_typeof({_ENTRY}) = 'object'
    AND {_ENTRY} <> '{{}}'::jsonb
    AND COALESCE({_ENTRY} ->> 'cancel_requested_at', '') = ''
    AND COALESCE({_ENTRY} ->> 'consumed_at', '') = ''
    AND (
        COALESCE({_ENTRY} ->> 'step_execution_id', '') = ''
        OR (parent.meta -> 'workflow_wait' ->> 'step_execution_id')
            = ({_ENTRY} ->> 'step_execution_id')
    )
"""
# The parent join uses the partial index on the waiting parent's child id.
_PARENT_JOIN = """
    JOIN jobs parent
        ON parent.status = 'awaiting_child'
        AND (parent.meta -> 'workflow_wait' ->> 'child_job_id') = child.id::text
"""


def review_chain_cte(max_depth: int) -> tuple[str, list[Any]]:
    """``review_chain(ancestor_id, review_job_id, depth, path)`` as a recursive CTE body.

    One row per waiting ancestor of a pending approval, with the job that holds
    the approval and how many links up the ancestor is.
    """
    depth = max(1, int(max_depth or DEFAULT_REVIEW_CHAIN_DEPTH))
    sql = f"""
    review_chain(ancestor_id, review_job_id, depth, path) AS (
        SELECT parent.id, child.id, 1, ARRAY[child.id, parent.id]
        FROM jobs child
        {_PARENT_JOIN}
        WHERE child.status = 'awaiting_approval'
            AND child.deleted_at IS NULL
            AND {_LINK}
        UNION ALL
        SELECT parent.id, chain.review_job_id, chain.depth + 1, chain.path || parent.id
        FROM review_chain chain
        JOIN jobs child ON child.id = chain.ancestor_id
        {_PARENT_JOIN}
        WHERE chain.depth < %s
            AND NOT parent.id = ANY(chain.path)
            AND {_LINK}
    )
    """
    return sql, [depth]


def awaiting_review_predicate(max_depth: int, *, alias: str = "j") -> tuple[str, list[Any]]:
    """The jobs of ``alias`` that wait for review, here or down their Invoke chain."""
    cte, params = review_chain_cte(max_depth)
    sql = (
        f"(LOWER(COALESCE({alias}.status, '')) = 'awaiting_approval'"
        f" OR (LOWER(COALESCE({alias}.status, '')) = 'awaiting_child'"
        f" AND {alias}.id IN (WITH RECURSIVE {cte} SELECT ancestor_id FROM review_chain)))"
    )
    return sql, params


def _uuid_texts(job_ids: Sequence[Any]) -> list[str]:
    texts: list[str] = []
    for job_id in job_ids:
        try:
            texts.append(str(uuid.UUID(str(job_id))))
        except (TypeError, ValueError):
            continue
    return sorted(set(texts))


def read_awaiting_review_targets(
    conn: Any, *, job_ids: Sequence[Any], max_depth: int
) -> dict[str, dict[str, Any]]:
    """For each given job that waits for review, the job that holds the approval.

    A job ``awaiting_approval`` holds its own; an ``awaiting_child`` job maps to
    the descendant found by the chain, the nearest one when several are. Each
    target carries its identity (project, workflow, account, tenant, workspace)
    so the caller can check that the person may read it. One statement per call.
    """
    ids = _uuid_texts(job_ids)
    if not ids:
        return {}
    cte, cte_params = review_chain_cte(max_depth)
    with conn.cursor(cursor_factory=psycopg_runtime.extras.RealDictCursor) as cur:
        cur.execute(
            f"""
            WITH RECURSIVE {cte},
            targets(job_id, review_job_id, depth) AS (
                SELECT j.id, j.id, 0
                FROM jobs j
                WHERE j.id = ANY(%s::uuid[]) AND j.status = 'awaiting_approval'
                UNION ALL
                SELECT chain.ancestor_id, chain.review_job_id, chain.depth
                FROM review_chain chain
                WHERE chain.ancestor_id = ANY(%s::uuid[])
            )
            SELECT DISTINCT ON (targets.job_id)
                targets.job_id::text AS job_id,
                t.id::text AS review_job_id,
                t.project_id::text AS project_id,
                t.selected_workflow,
                t.status,
                p.account_id::text AS account_id,
                p.tenant_id::text AS tenant_id,
                p.workspace_id::text AS workspace_id
            FROM targets
            JOIN jobs t ON t.id = targets.review_job_id
            JOIN projects p ON p.id = t.project_id
            ORDER BY targets.job_id, targets.depth
            """,
            (*cte_params, ids, ids),
        )
        return {str(row["job_id"]): dict(row) for row in cur.fetchall()}


def read_review_roots(
    conn: Any, *, job_ids: Sequence[Any], max_depth: int
) -> dict[str, dict[str, Any]]:
    """For each given job holding a pending approval, the top of its Invoke chain.

    The chain is followed upwards through ``awaiting_child`` parents, as the
    review queue follows it down; the farthest ancestor is the root, the job an
    operator opened in a section. A job without such a parent is not in the
    answer: it is its own root. One statement per call.
    """
    ids = _uuid_texts(job_ids)
    if not ids:
        return {}
    cte, cte_params = review_chain_cte(max_depth)
    with conn.cursor(cursor_factory=psycopg_runtime.extras.RealDictCursor) as cur:
        cur.execute(
            f"""
            WITH RECURSIVE {cte}
            SELECT DISTINCT ON (chain.review_job_id)
                chain.review_job_id::text AS review_job_id,
                root.id::text AS root_job_id,
                root.selected_workflow AS root_workflow,
                root.project_id::text AS root_project_id
            FROM review_chain chain
            JOIN jobs root ON root.id = chain.ancestor_id
            WHERE chain.review_job_id = ANY(%s::uuid[])
            ORDER BY chain.review_job_id, chain.depth DESC
            """,
            (*cte_params, ids),
        )
        return {str(row["review_job_id"]): dict(row) for row in cur.fetchall()}


__all__ = [
    "AWAITING_REVIEW_STATUS",
    "DEFAULT_REVIEW_CHAIN_DEPTH",
    "awaiting_review_predicate",
    "read_awaiting_review_targets",
    "read_review_roots",
    "review_chain_cte",
]
