"""Plan a group's relocation using the existing definition change operations.

This module only rewrites graph ownership. The extraction service must first
prove the data boundary and permissions and must validate both resulting
definitions before committing either one.
"""

from __future__ import annotations

import copy
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from core.application.workflows.workflow_definition_service import definition_from_workflow
from core.application.workflows.workflow_definition_validation import STOP_STEP_KINDS


@dataclass(frozen=True)
class GroupExtractionStructure:
    child_definition: dict[str, Any]
    parent_operations: list[dict[str, Any]]


def _ref(kind: str, node_id: str) -> dict[str, str]:
    return {"kind": kind, "id": node_id}


def _available_id(base: str, used: set[str]) -> str:
    result = base
    suffix = 1
    while result in used:
        result = f"{base}_{suffix}"
        suffix += 1
    used.add(result)
    return result


def _moved_subtree(
    definition: Mapping[str, Any], group_id: str
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """The Group's own steps plus every For each it holds, with their bodies."""
    nested = [item for item in definition.get("for_each") or [] if item.get("group_id") == group_id]
    nested_ids = {item["for_each_id"] for item in nested}
    steps = [
        step
        for step in definition["steps"]
        if step.get("group_id") == group_id or step.get("for_each_id") in nested_ids
    ]
    return steps, nested


def extract_group_structure(
    workflow: Mapping[str, Any], group_id: str, invocation: Mapping[str, Any]
) -> GroupExtractionStructure:
    """Return independent child structure and an atomic parent change set.

    The whole Group moves: its own steps, every For each inside it with that
    For each's body, Break/Continue/Collect and all their edges (ADR 0038).
    """
    definition = definition_from_workflow(workflow)
    group = next((g for g in definition["groups"] if g["group_id"] == group_id), None)
    if group is None:
        raise ValueError(f"Group '{group_id}' was not found.")
    members, nested = _moved_subtree(definition, group_id)
    if not members and not nested:
        raise ValueError("An empty group cannot become a child workflow.")
    for member in members:
        if member.get("step_kind") in STOP_STEP_KINDS:
            raise ValueError(
                f"Stop step '{member['step_id']}' ends the parent workflow; "
                "moving it into a child would change that behavior."
            )
    member_ids = {s["step_id"] for s in members}
    nested_ids = {item["for_each_id"] for item in nested}
    invoke = copy.deepcopy(dict(invocation))
    invoke_id = str(invoke.get("step_id") or "").strip()
    if not invoke_id or any(s["step_id"] == invoke_id for s in definition["steps"]):
        raise ValueError("The child invocation needs a unique step identifier.")
    if invoke.get("step_kind") != "invoke_workflow":
        raise ValueError("The replacement must use the existing invoke_workflow step.")
    invoke.update(group_id=None, for_each_id=None, lane_id=None)
    graph = definition["execution_graph"]
    if not isinstance(graph, dict):
        raise ValueError("Save a valid execution graph before converting this group.")
    group_ref = _ref("group", group_id)
    invoke_ref = _ref("step", invoke_id)

    def inside(ref: Mapping[str, Any]) -> bool:
        kind, node_id = ref.get("kind"), ref.get("id")
        return (kind == "step" and node_id in member_ids) or (
            kind == "for_each" and node_id in nested_ids
        )

    def moved_exit(ref: Mapping[str, Any]) -> bool:
        return (ref.get("kind") == "group_exit" and ref.get("group_id") == group_id) or (
            ref.get("kind") == "for_each_exit" and ref.get("for_each_id") in nested_ids
        )

    child_edges: list[dict[str, Any]] = []
    parent_connections: list[dict[str, Any]] = []
    has_error_exit = False
    for original in graph.get("edges") or []:
        edge = copy.deepcopy(original)
        source, target = edge.get("source", {}), edge.get("target", {})
        if inside(source):
            if not inside(target) and not moved_exit(target):
                raise ValueError("A group member has a connection outside its group body.")
            child_edges.append(edge)
        elif source == group_ref or target == group_ref:
            if source == group_ref:
                has_error_exit |= edge.get("outcome", {}).get("name") == "error"
                edge["source"] = copy.deepcopy(invoke_ref)
            if target == group_ref:
                edge["target"] = copy.deepcopy(invoke_ref)
            parent_connections.append({"op": "connect", "edge": edge, "replace": True})
        elif inside(target):
            raise ValueError("A connection enters a group member without entering its group.")

    # The group's own retry policy stays with its body. The call must not add
    # another retry; only an existing outward error branch is reproduced here.
    invoke["error_policy"] = {"on_error": "route_to_step" if has_error_exit else "fail"}
    used = set(member_ids) | nested_ids | {group_id}
    child_steps = copy.deepcopy(members)
    for outcome, kind, base in (
        ("success", "stop_success", "child_done"),
        ("error", "stop_fail", "child_failed"),
    ):
        if outcome == "error" and not has_error_exit:
            continue
        terminal_id = _available_id(base, used)
        child_steps.append(
            {
                "step_id": terminal_id,
                "step_kind": kind,
                "group_id": None,
                "for_each_id": None,
                "lane_id": None,
            }
        )
        child_edges.append(
            {
                "source": copy.deepcopy(group_ref),
                "outcome": {"name": outcome},
                "target": _ref("step", terminal_id),
            }
        )
    operations: list[dict[str, Any]] = [
        {"op": "create_step", "step": invoke},
        {"op": "delete_group", "group_id": group_id, "with_contents": True},
        *parent_connections,
    ]
    if graph.get("entry") == group_ref:
        operations.append({"op": "set_entry", "target": invoke_ref})
    return GroupExtractionStructure(
        child_definition={
            "steps": child_steps,
            "groups": [copy.deepcopy(group)],
            "for_each": copy.deepcopy(nested),
            "execution_graph": {
                "schema_version": graph["schema_version"],
                "entry": group_ref,
                "edges": child_edges,
            },
        },
        parent_operations=operations,
    )
