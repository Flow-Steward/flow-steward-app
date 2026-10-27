"""Prove parent outputs that stay outside the extracted group.

Only a single definite root writer can establish a retained output. All state
producer rows are inspected for aliases/overlap; flattened suggestion types
are never evidence. Other groups carry facts but establish no new writes.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from core.application.orchestration.state_path_guards import (
    canonical_state_write_parts,
    is_reserved_state_target,
)
from core.application.orchestration.state_paths import StatePathError, parse_state_path
from core.application.orchestration.step_failures import effective_step_policy
from core.application.workflows.workflow_group_extraction_flow import must_writes_before

_Node = tuple[str, str]
_WRITER_KINDS = frozenset({"set_variable", "template", "transform", "dataset_query"})


def _node(ref: Mapping) -> _Node:
    return str(ref.get("kind") or ""), str(ref.get("id") or "")


def _overlaps(name: str, target: str) -> bool:
    try:
        tokens = parse_state_path(target)
    except StatePathError:
        return True
    # Match the same namespaces as the callable result resolver. A whole-object
    # write can erase or shadow any output; index/dotted descendants overlap too.
    for prefix in ("workflow_data", "outputs"):
        if tokens and tokens[0].kind == "key" and tokens[0].value == prefix:
            tokens = tokens[1:]
    return not tokens or (tokens[0].kind == "key" and tokens[0].value == name)


def _stop_container_node(step: Mapping[str, Any], owner: Mapping[str, Any] | None) -> _Node | None:
    """The root-flow node a Stop finishes through: its group, else its For each's container."""
    if step.get("group_id"):
        return "group", step["group_id"]
    if owner and owner.get("group_id"):
        return "group", owner["group_id"]
    if owner:
        return "for_each", owner["for_each_id"]
    return None


def _root_flow(
    workflow: Mapping[str, Any], group_id: str
) -> tuple[_Node, dict[_Node, list[tuple[_Node, bool]]], set[_Node]] | None:
    """Reachable root edges retain errors/rework, while successful Stops are sinks."""
    steps = {s["step_id"]: s for s in workflow.get("steps") or []}
    groups = {g["group_id"]: g for g in workflow.get("groups") or []}
    containers = {f["for_each_id"]: f for f in workflow.get("for_each") or []}
    roots = (
        {
            ("step", key): value
            for key, value in steps.items()
            if not value.get("group_id") and not value.get("for_each_id")
        }
        | {("group", key): value for key, value in groups.items()}
        | {
            ("for_each", key): value
            for key, value in containers.items()
            if not value.get("group_id")
        }
    )
    graph = (workflow.get("settings") or {}).get("execution_graph") or {}
    entry = _node(graph.get("entry") or {})
    selected = ("group", group_id)
    if entry not in roots or selected not in roots:
        return None
    ends = {node for node, owner in roots.items() if owner.get("step_kind") == "stop_success"}
    edges = [
        (
            _node(e.get("source") or {}),
            _node(e.get("target") or {}),
            (e.get("outcome") or {}).get("name") != "error",
        )
        for e in graph.get("edges") or []
        if _node(e.get("source") or {}) in roots
        and _node(e.get("target") or {}) in roots
        and _node(e.get("source") or {}) not in ends
    ]
    reachable = {entry}
    while True:
        added = {target for source, target, _ in edges if source in reachable} - reachable
        if not added:
            break
        reachable.update(added)
    obligations = ends & reachable
    # A Stop inside another group can also finish the run; require facts before
    # that container rather than pretending the inner Stop cannot execute.
    for s in steps.values():
        if s.get("step_kind") != "stop_success":
            continue
        node = _stop_container_node(s, containers.get(str(s.get("for_each_id") or "")))
        if node is not None and node in reachable:
            obligations.add(node)
    if selected not in reachable or not (ends & reachable):
        return None
    incoming = {node: [] for node in reachable}
    for source, target, success in edges:
        if source in reachable and target in reachable:
            incoming[target].append((source, success))
    return entry, incoming, obligations


def _retained_writers(
    workflow: Mapping[str, Any],
    output_rows: Mapping[str, list[dict]],
    normalize_type: Callable[[Any], str],
) -> tuple[dict[str, str], dict[_Node, set[str]]]:
    steps = {s["step_id"]: s for s in workflow.get("steps") or []}
    all_rows = [(key, row) for key, rows in output_rows.items() for row in rows]
    candidates = {}
    writers = {}
    locals_ = {
        str(name)
        for item in workflow.get("for_each") or []
        for name in (item.get("item_name"), item.get("index_name"))
        if name
    }
    for key, row in all_rows:
        parts = canonical_state_write_parts(row.get("name"))
        if parts and parts[0] == "workflow_data":
            parts = parts[1:]
        if parts and parts[0] == "outputs":
            parts = parts[1:]
        name = parts[0] if parts and len(parts) == 1 else ""
        owner = steps.get(key, {})
        kind = normalize_type(row.get("type"))
        if (
            not name.isidentifier()
            or is_reserved_state_target(name)
            or name in locals_
            or not kind
            or owner.get("group_id")
            or owner.get("for_each_id")
            or owner.get("optional")
            or owner.get("disabled")
            or owner.get("step_kind") not in _WRITER_KINDS
            or effective_step_policy(owner).on_error != "fail"
        ):
            continue
        # Even same-type alternative producers or aliases are unproven in this
        # minimal extension. Nested writes do not establish a whole root object.
        if sum(_overlaps(name, str(other.get("name") or "")) for _, other in all_rows) != 1:
            continue
        candidates[name] = kind
        writers.setdefault(("step", key), set()).add(name)
    return candidates, writers


def retained_parent_outputs(
    workflow: Mapping[str, Any],
    group_id: str,
    output_rows: Mapping[str, list[dict]],
    normalize_type: Callable[[Any], str],
) -> dict[str, str]:
    """Return types proven present at every reachable successful end."""
    flow = _root_flow(workflow, group_id)
    if flow is None:
        return {}
    entry, incoming, obligations = flow
    candidates, writers = _retained_writers(workflow, output_rows, normalize_type)
    before = must_writes_before({entry}, incoming, writers)
    definite = set.intersection(*(before[node] for node in obligations))
    return {name: kind for name, kind in candidates.items() if name in definite}
