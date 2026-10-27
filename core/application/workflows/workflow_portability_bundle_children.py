"""Child workflows a bundle carries together with the workflow that calls them.

A workflow that invokes another workflow is exported with that child (and its
children) in the same bundle, so one file is a complete template. These helpers
answer the questions import asks about such a bundle: which workflows does it
carry, which of them are called by another one, and in what order to create them
so a child exists before the workflow that calls it.
"""

from __future__ import annotations

from typing import Any

from .workflow_portability_common import (
    _as_dict,
    _step_child_workflow_ref,
    _text,
    _workflow_steps,
)


def workflow_child_refs(workflow: dict[str, Any]) -> list[str]:
    """The workflow ids this workflow's steps invoke, in step order, without repeats."""
    refs: list[str] = []
    for step in _workflow_steps(workflow):
        ref = _step_child_workflow_ref(step)
        if ref and ref not in refs:
            refs.append(ref)
    return refs


def bundled_workflow_ids(workflows: list[dict[str, Any]]) -> set[str]:
    """Ids of the full workflow definitions the bundle carries."""
    return {_text(item.get("workflow_id")) for item in workflows if _text(item.get("workflow_id"))}


def bundled_child_ids(workflows: list[dict[str, Any]]) -> set[str]:
    """Bundled workflows that another bundled workflow invokes."""
    carried = bundled_workflow_ids(workflows)
    called: set[str] = set()
    for workflow in workflows:
        called.update(ref for ref in workflow_child_refs(workflow) if ref in carried)
    return called


def root_workflow_id(bundle: dict[str, Any], workflows: list[dict[str, Any]]) -> str:
    """The workflow the bundle was exported for: the one no other bundled workflow calls.

    The export records it as ``metadata.source_workflow_id``; a bundle without that
    falls back to the first workflow nothing else in the bundle invokes.
    """
    carried = bundled_workflow_ids(workflows)
    declared = _text(_as_dict(bundle.get("metadata")).get("source_workflow_id"))
    if declared in carried:
        return declared
    children = bundled_child_ids(workflows)
    for workflow in workflows:
        workflow_id = _text(workflow.get("workflow_id"))
        if workflow_id and workflow_id not in children:
            return workflow_id
    return _text(workflows[0].get("workflow_id")) if workflows else ""


def creation_order(workflows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Children before the workflows that call them; a cycle is cut where it closes."""
    by_id = {_text(item.get("workflow_id")): item for item in workflows}
    ordered: list[dict[str, Any]] = []
    placed: set[str] = set()
    visiting: set[str] = set()

    def place(workflow_id: str) -> None:
        if workflow_id in placed or workflow_id in visiting or workflow_id not in by_id:
            return
        visiting.add(workflow_id)
        for ref in workflow_child_refs(by_id[workflow_id]):
            place(ref)
        visiting.discard(workflow_id)
        placed.add(workflow_id)
        ordered.append(by_id[workflow_id])

    for workflow in workflows:
        place(_text(workflow.get("workflow_id")))
    # Anything without an id keeps its place at the end; import reports it.
    ordered.extend(item for item in workflows if not _text(item.get("workflow_id")))
    return ordered


__all__ = [
    "bundled_child_ids",
    "bundled_workflow_ids",
    "creation_order",
    "root_workflow_id",
    "workflow_child_refs",
]
