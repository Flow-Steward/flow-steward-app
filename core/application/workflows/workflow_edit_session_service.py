"""Compare an edit session's copy with the server, and save it on explicit choice.

The browser owns the edit session (the local copy and its Undo history); the
server keeps no session. These two operations are stateless:

* ``compare_workflow_session`` — three comparisons for the conflict dialog:
  what the author changed (base → local), what the server changed since
  (base → server), and how the two copies differ (local → server). Read-only.
* ``apply_workflow_content`` — "keep my version": the author's whole copy is
  written as one new revision by the one definition writer, guarded by the
  revision the author reviewed. A newer revision is a conflict, never a merge.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from core.application.workflows.workflow_definition_service import (
    ReadinessCheck,
    WorkflowChangeResult,
    replace_workflow_content,
)
from core.application.workflows.workflow_definition_validation import ChildLoader
from core.application.workflows.workflow_edit_session_contract import (
    authoring_content,
    parse_authoring_content,
)
from core.application.workflows.workflow_history import diff_workflow_snapshots


def _comparison(older: Mapping[str, Any], newer: Mapping[str, Any]) -> dict[str, Any]:
    changes = diff_workflow_snapshots(older, newer)
    counts = {"added": 0, "removed": 0, "changed": 0}
    for change in changes:
        kind = str(change.get("change") or "")
        if kind in counts:
            counts[kind] += 1
    return {"changes": changes, "summary": {"total": len(changes), **counts}}


def compare_workflow_session(
    db: Any,
    workflow_id: str,
    *,
    base_content: Any,
    local_content: Any,
    current: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Three comparisons of a session's base and local copy with the current workflow.

    *current* is the caller's scoped read of the workflow; without it the store
    is read. Nothing is written; base and local are data to compare, not a
    source of any permission.
    """
    base = parse_authoring_content(base_content, field="base_content")
    local = parse_authoring_content(local_content, field="local_content")
    workflow = current if current is not None else db.get_workflow(workflow_id)
    if not isinstance(workflow, Mapping):
        raise LookupError(f"Workflow '{workflow_id}' not found")
    server = authoring_content(workflow)
    return {
        "workflow_id": str(workflow.get("workflow_id") or workflow_id),
        "current_revision": int(workflow.get("revision") or 0),
        "current_content": server,
        "base_to_local": _comparison(base, local),
        "base_to_server": _comparison(base, server),
        "local_to_server": _comparison(local, server),
    }


def apply_workflow_content(
    db: Any,
    workflow_id: str,
    *,
    content: Any,
    expected_revision: int,
    author: str,
    reference_access: Any = None,
    authorize: Callable[[dict[str, Any]], None] | None = None,
    readiness: ReadinessCheck | None = None,
    load_child: ChildLoader | None = None,
    max_invoke_depth: int = 5,
    dry_run: bool = False,
) -> WorkflowChangeResult:
    """Write the author's whole reviewed copy as one revision, or judge it (``dry_run``).

    Only authoring fields are accepted; ``enabled``, provenance, notes and input
    authority stay current. The definition writer judges the candidate as an
    Apply and refuses a stale *expected_revision* with a revision conflict.
    """
    candidate = parse_authoring_content(content)
    return replace_workflow_content(
        db,
        workflow_id,
        content=candidate,
        expected_revision=expected_revision,
        author=author,
        reference_access=reference_access,
        authorize=authorize,
        readiness=readiness,
        load_child=load_child,
        max_invoke_depth=max_invoke_depth,
        dry_run=dry_run,
    )


__all__ = ["apply_workflow_content", "compare_workflow_session"]
