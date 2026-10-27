"""Root-only hook placement, shared by authoring and definition write boundaries."""

from __future__ import annotations

from collections import deque
from collections.abc import Mapping
from typing import Any

from core.application.workflows.execution_graph import (
    EXECUTION_GRAPH_SETTING,
    NODE_KIND_STEP,
    NODE_KINDS,
    TARGET_KIND_FOR_EACH_EXIT,
    TARGET_KIND_GROUP_EXIT,
)
from core.application.workflows.workflow_step_kinds import STEP_KIND_INVOKE_WORKFLOW

HOOK_ROOT_ONLY_CODE = "workflow.hook.root_only"


def _mapping(value: Any) -> Mapping:
    return value if isinstance(value, Mapping) else {}


def _rows(value: Any) -> list[Mapping]:
    return [item for item in value if isinstance(item, Mapping)] if isinstance(value, list) else []


def _node(value: Any) -> tuple[str, str] | None:
    raw = _mapping(value)
    kind, node_id = raw.get("kind"), raw.get("id")
    if (
        isinstance(kind, str)
        and kind in NODE_KINDS
        and isinstance(node_id, str)
        and node_id.strip()
    ):
        return kind, node_id.strip()
    return None


def _hook_steps(
    definition: Mapping[str, Any],
) -> tuple[list[tuple[tuple[str, str], str]], set[tuple[str, str]]]:
    raw_steps = definition.get("steps")
    steps = raw_steps if isinstance(raw_steps, list) else []
    owned: set[tuple[str, str]] = set()
    hooks: list[tuple[tuple[str, str], str]] = []
    for index, step in enumerate(steps):
        if not isinstance(step, Mapping):
            continue
        step_id = str(step.get("step_id") or "").strip()
        ref = (NODE_KIND_STEP, step_id)
        if step.get("group_id") or step.get("for_each_id"):
            owned.add(ref)
        config = _mapping(_mapping(step.get("override")).get(STEP_KIND_INVOKE_WORKFLOW))
        kind = str(step.get("step_kind") or "").strip().lower()
        if kind == STEP_KIND_INVOKE_WORKFLOW and "hook" in config:
            path = f"steps.{step_id}" if step_id else f"steps[{index}]"
            hooks.append((ref, f"{path}.override.invoke_workflow.hook"))
    return hooks, owned


def _body_entries(definition: Mapping[str, Any]) -> set[tuple[str, str]]:
    entries: set[tuple[str, str]] = set()
    for collection in ("groups", "for_each"):
        for container in _rows(definition.get(collection)):
            body = _mapping(container.get("body"))
            raw_entries = [body.get("entry")]
            raw_entries.extend(lane.get("entry") for lane in _rows(body.get("lanes")))
            for entry in raw_entries:
                if ref := _node(entry):
                    entries.add(ref)
    return entries


def _expand_body_graph(definition: Mapping[str, Any], nested: set[tuple[str, str]]) -> None:
    graph = _mapping(
        definition.get(EXECUTION_GRAPH_SETTING)
        if EXECUTION_GRAPH_SETTING in definition
        else _mapping(definition.get("settings")).get(EXECUTION_GRAPH_SETTING)
    )
    outgoing: dict[tuple[str, str], list[tuple[str, str]]] = {}
    for edge in _rows(graph.get("edges")):
        source = _node(edge.get("source"))
        if source is None:
            continue
        target = _node(edge.get("target"))
        if target is not None:
            outgoing.setdefault(source, []).append(target)
        elif _mapping(edge.get("target")).get("kind") in (
            TARGET_KIND_GROUP_EXIT,
            TARGET_KIND_FOR_EACH_EXIT,
        ):
            nested.add(source)

    pending = deque(nested)
    while pending:
        for target in outgoing.get(pending.popleft(), ()):
            if target not in nested:
                nested.add(target)
                pending.append(target)


def hook_placement_issues(definition: Mapping[str, Any]) -> list[dict[str, str]]:
    """Reject a hook claimed by container metadata or a container body graph.

    Inspect raw rows so invalid or missing ownership cannot hide the declaration
    from normal structural collection. Container exits end traversal: the root
    continuation of a Group or For each is outside its body.
    """
    hooks, nested = _hook_steps(definition)
    if not hooks:
        return []
    nested.update(_body_entries(definition))
    _expand_body_graph(definition, nested)
    return [
        {
            "code": HOOK_ROOT_ONLY_CODE,
            "message": "A hook must be at the workflow root, before or after a Group or For each.",
            "path": path,
        }
        for ref, path in hooks
        if ref in nested
    ]
