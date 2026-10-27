"""Conservative definite-write analysis for an extracted group's body graph."""

from __future__ import annotations

from collections.abc import Hashable, Mapping
from typing import TypeVar

from core.application.orchestration.step_failures import effective_step_policy


def definite_state_before(
    workflow: Mapping, group: Mapping, output_rows: Mapping
) -> dict[str, set[str]]:
    """Names written on every successful path before each member executes.

    Members are the Group's own steps and the body steps of every For each it
    holds. Optional steps, connector results and host-retried HTTP steps are
    not guaranteed assignments: recovery can continue without their writes.
    Error edges carry the incoming facts, not the failed step's planned outputs.
    Body entries always admit the first iteration with no body-produced values,
    and a For each node writes nothing definite (zero items is a success).
    """
    group_id = group["group_id"]
    nested = {
        f["for_each_id"]: f for f in workflow.get("for_each", []) if f.get("group_id") == group_id
    }
    owners: dict[str, list[Mapping]] = {}
    for s in workflow.get("steps", []):
        if s.get("group_id") == group_id:
            owners[s["step_id"]] = [group]
        elif s.get("for_each_id") in nested:
            # The nearest declared container policy applies, as at runtime.
            owners[s["step_id"]] = [nested[s["for_each_id"]], group]
    members = {s["step_id"]: s for s in workflow.get("steps", []) if s["step_id"] in owners}
    entries: set[str] = set()
    for container in (group, *nested.values()):
        body = container.get("body") or {}
        entries.add(str((body.get("entry") or {}).get("id") or ""))
        entries.update(
            str((lane.get("entry") or {}).get("id") or "") for lane in body.get("lanes") or []
        )
    writes = {
        step_id: {
            str(row.get("name") or "").removeprefix("state.")
            for row in output_rows.get(step_id, [])
            if "." not in str(row.get("name") or "").removeprefix("state.")
        }
        if not member.get("optional")
        and not member.get("disabled")
        and effective_step_policy(member, owners[step_id]).on_error == "fail"
        and member.get("step_kind") in {"template", "transform", "set_variable", "dataset_query"}
        else set()
        for step_id, member in members.items()
    }
    nodes = set(members) | set(nested)
    incoming: dict[str, list[tuple[str, bool]]] = {node: [] for node in nodes}
    graph = (workflow.get("settings") or {}).get("execution_graph") or {}
    for edge in graph.get("edges") or []:
        source, target = edge.get("source") or {}, edge.get("target") or {}
        if (
            source.get("kind") in {"step", "for_each"}
            and target.get("kind") in {"step", "for_each"}
            and source.get("id") in nodes
            and target.get("id") in nodes
        ):
            incoming[target["id"]].append(
                (source["id"], (edge.get("outcome") or {}).get("name") != "error")
            )
    return must_writes_before(entries, incoming, writes)


_Node = TypeVar("_Node", bound=Hashable)


def must_writes_before(
    entries: set[_Node],
    incoming: Mapping[_Node, list[tuple[_Node, bool]]],
    writes: Mapping[_Node, set[str]],
) -> dict[_Node, set[str]]:
    """Intersect predecessor facts; entry paths and failures never invent writes."""
    universe = set().union(*writes.values()) if writes else set()
    before = {node: set(universe) for node in incoming}
    while True:
        updated = {}
        for node, predecessors in incoming.items():
            paths = [
                before[parent] | (writes.get(parent, set()) if success else set())
                for parent, success in predecessors
            ]
            updated[node] = set.intersection(*paths) if paths and node not in entries else set()
        if updated == before:
            return updated
        before = updated
