"""Workflow definitions for tests: steps, Groups, For each containers, one graph.

Most tests care about one step's contract, not about routing. ``linear_workflow``
gives such a step a runnable place: root steps run in the order given and end at
a Stop Success, so readiness reports only what the test is about.
"""

from __future__ import annotations

from itertools import pairwise
from typing import Any

STOP_STEP_ID = "done"


def node(node_id: str, kind: str = "step") -> dict[str, str]:
    return {"kind": kind, "id": node_id}


def edge(
    source: str,
    target: dict[str, Any],
    outcome: str = "success",
    *,
    source_kind: str = "step",
    case_id: str | None = None,
    rework: bool = False,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "source": node(source, source_kind),
        "outcome": {"name": outcome},
        "target": target,
        "rework": rework,
    }
    if case_id is not None:
        payload["outcome"]["case_id"] = case_id
    return payload


def stop_step(step_id: str = STOP_STEP_ID, kind: str = "stop_success") -> dict[str, Any]:
    return {"step_id": step_id, "step_kind": kind, "display_name": step_id}


def linear_graph(step_ids: list[str]) -> dict[str, Any]:
    edges = [edge(source, node(target)) for source, target in pairwise(step_ids)]
    return {"schema_version": 1, "entry": node(step_ids[0]) if step_ids else None, "edges": edges}


def linear_workflow(
    *steps: dict[str, Any],
    workflow_id: str = "test_workflow",
    with_stop: bool = True,
    **fields: Any,
) -> dict[str, Any]:
    """A workflow whose root steps run in the given order, then stop successfully.

    Extra keyword fields (``enabled``, ``settings``, ``account_id``, …) land on
    the workflow row; given ``settings`` keep their keys and gain the graph.
    """
    ordered = [dict(step) for step in steps]
    for index, step in enumerate(ordered):
        step.setdefault("step_id", f"step_{index + 1}")
        step.setdefault("group_id", None)
        step.setdefault("for_each_id", None)
        step.setdefault("lane_id", None)
    if with_stop and not any(
        step.get("step_kind") in {"stop_success", "stop_fail"} for step in ordered
    ):
        ordered.append(stop_step())
    settings = dict(fields.pop("settings", {}) or {})
    settings.setdefault("execution_graph", linear_graph([step["step_id"] for step in ordered]))
    return {
        "workflow_id": workflow_id,
        "display_name": fields.pop("display_name", workflow_id),
        "enabled": fields.pop("enabled", True),
        "settings": settings,
        "steps": ordered,
        "groups": fields.pop("groups", []),
        "for_each": fields.pop("for_each", []),
        **fields,
    }


__all__ = ["STOP_STEP_ID", "edge", "linear_graph", "linear_workflow", "node", "stop_step"]
