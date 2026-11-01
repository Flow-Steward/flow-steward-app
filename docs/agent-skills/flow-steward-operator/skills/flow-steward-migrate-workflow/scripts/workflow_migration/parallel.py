"""Map independent source branches to existing native Group Parallel Run lanes."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .contracts import require
from .graph import SourceGraph
from .target import edge


@dataclass
class ParallelLayout:
    groups: list[dict[str, Any]] = field(default_factory=list)
    membership: dict[int, dict[str, str]] = field(default_factory=dict)
    forks: dict[int, str] = field(default_factory=dict)


def parallel_layout(graph: SourceGraph) -> ParallelLayout:
    """Each qualified branch is an independent linear lane, without hidden joins."""
    result = ParallelLayout()
    for source, starts in graph.fanouts.items():
        group_id = f"fanout_n{source:04d}"
        lanes = []
        for number, start in enumerate(starts):
            lane_id = f"branch_{number:04d}"
            lanes.append({"lane_id": lane_id, "entry": {"kind": "step", "id": f"n{start:04d}"}})
            current: int | None = start
            while current is not None:
                require(
                    current not in result.membership
                    and current not in graph.fanouts
                    and graph.nodes[current]["type"] != "n8n-nodes-base.if",
                    "parallel_branch_needs_mapping",
                    f"nodes[{current}]",
                    "This branch needs an explicit child-workflow or control-flow mapping.",
                    "Native Parallel Run groups are available; this adapter currently qualifies linear independent lanes. Map conditional/nested branches explicitly.",
                )
                result.membership[current] = {"group_id": group_id, "lane_id": lane_id}
                links = graph.links.get(current, [])
                current = links[0] if links else None
        result.groups.append(
            {
                "group_id": group_id,
                "display_name": "Parallel Run",
                "body": {"kind": "lanes", "lanes": lanes},
            }
        )
        result.forks[source] = group_id
    return result


def lane_http_steps(
    expanded: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Use native template type validation inside lanes; an If is not lane work."""
    request, shape = expanded[:2]
    request["error_policy"] = {"on_error": "fail"}
    shape["error_policy"] = {"on_error": "fail"}
    shape["override"]["template"]["inputs"][0]["type"] = "object"
    shape["override"]["template"]["text"] = "object"
    return [request, shape], [edge(request["step_id"], "success", shape["step_id"])]


def completion_edges(
    graph: SourceGraph, layout: ParallelLayout, ends: dict[int, str], outcomes: dict[int, list[str]]
) -> list[dict[str, Any]]:
    """Close lanes at their typed exit and continue once after the owning group."""
    result = []
    for index in graph.order:
        if index in layout.forks:
            result.append(
                {
                    "source": {"kind": "step", "id": ends[index]},
                    "outcome": {"name": outcomes[index][0]},
                    "target": {"kind": "group", "id": layout.forks[index]},
                    "rework": False,
                }
            )
            continue
        links = graph.links.get(index, [])
        for number, outcome in enumerate(outcomes[index]):
            target = links[number] if number < len(links) else None
            if target is None and index in layout.membership:
                result.append(
                    {
                        "source": {"kind": "step", "id": ends[index]},
                        "outcome": {"name": outcome},
                        "target": {"kind": "group_exit", **layout.membership[index]},
                        "rework": False,
                    }
                )
            else:
                result.append(
                    edge(ends[index], outcome, "done" if target is None else f"n{target:04d}")
                )
    for group in layout.groups:
        result.append(
            {
                "source": {"kind": "group", "id": group["group_id"]},
                "outcome": {"name": "success"},
                "target": {"kind": "step", "id": "done"},
                "rework": False,
            }
        )
    return result
