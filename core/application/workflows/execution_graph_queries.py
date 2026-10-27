"""Questions authoring asks of a definition's execution graph.

"What can step X read?" used to mean "outputs of earlier phases". With the
graph as the only routing source it means: steps that run before X on some path
from the entry. Inside a group that is the steps before X in its own body or
lane, plus everything that runs before the group itself in the root. Parallel
sibling lanes run concurrently, so they are never upstream of each other, and
rework edges are back edges, so they never make a later step upstream.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from core.application.workflows.execution_graph import (
    EXECUTION_GRAPH_SETTING,
    NODE_KIND_FOR_EACH,
    NODE_KIND_GROUP,
    NODE_KIND_STEP,
)

_NODE_KINDS = (NODE_KIND_STEP, NODE_KIND_GROUP, NODE_KIND_FOR_EACH)


def _text(value: Any) -> str:
    return str(value or "").strip()


def _ref_key(ref: Any) -> tuple[str, str] | None:
    if not isinstance(ref, Mapping):
        return None
    kind = ref.get("kind")
    node_id = _text(ref.get("id"))
    if kind not in _NODE_KINDS or not node_id:
        return None
    return str(kind), node_id


def _forward_edges(graph: Any) -> Iterable[tuple[tuple[str, str], tuple[str, str]]]:
    edges = graph.get("edges") if isinstance(graph, Mapping) else None
    for edge in edges if isinstance(edges, list) else []:
        if not isinstance(edge, Mapping) or edge.get("rework") is True:
            continue
        source = _ref_key(edge.get("source"))
        target = _ref_key(edge.get("target"))
        if source and target:
            yield source, target


class DefinitionGraph:
    """Read-only view over steps, Groups, For each containers and the graph."""

    def __init__(
        self,
        steps: Iterable[Any],
        groups: Iterable[Any],
        graph: Any,
        for_each: Iterable[Any] = (),
    ) -> None:
        self.steps = [dict(step) for step in steps if isinstance(step, Mapping)]
        self.groups = [dict(group) for group in groups if isinstance(group, Mapping)]
        self.for_each = [dict(item) for item in for_each if isinstance(item, Mapping)]
        self._order = {_text(step.get("step_id")): index for index, step in enumerate(self.steps)}
        self._step_group = {
            _text(step.get("step_id")): _text(step.get("group_id")) or None for step in self.steps
        }
        self._step_for_each = {
            _text(step.get("step_id")): _text(step.get("for_each_id")) or None
            for step in self.steps
        }
        self._for_each_group = {
            _text(item.get("for_each_id")): _text(item.get("group_id")) or None
            for item in self.for_each
        }
        self._predecessors: dict[tuple[str, str], set[tuple[str, str]]] = {}
        for source, target in _forward_edges(graph):
            self._predecessors.setdefault(target, set()).add(source)

    @classmethod
    def from_workflow(cls, workflow: Mapping[str, Any]) -> DefinitionGraph:
        settings = workflow.get("settings")
        graph = settings.get(EXECUTION_GRAPH_SETTING) if isinstance(settings, Mapping) else None
        return cls(
            workflow.get("steps") or [],
            workflow.get("groups") or [],
            graph,
            workflow.get("for_each") or [],
        )

    def step(self, step_id: str) -> dict[str, Any] | None:
        return next((step for step in self.steps if _text(step.get("step_id")) == step_id), None)

    def group(self, group_id: str) -> dict[str, Any] | None:
        return next(
            (group for group in self.groups if _text(group.get("group_id")) == group_id), None
        )

    def group_of(self, step_id: str) -> dict[str, Any] | None:
        group_id = self._step_group.get(step_id)
        return self.group(group_id) if group_id else None

    def for_each_of(self, step_id: str) -> dict[str, Any] | None:
        """The For each whose body (or lane) holds the step directly."""
        for_each_id = self._step_for_each.get(step_id)
        return next(
            (item for item in self.for_each if _text(item.get("for_each_id")) == for_each_id),
            None,
        )

    def _ancestors(self, node: tuple[str, str]) -> set[tuple[str, str]]:
        seen: set[tuple[str, str]] = set()
        stack = list(self._predecessors.get(node, ()))
        while stack:
            current = stack.pop()
            if current in seen:
                continue
            seen.add(current)
            stack.extend(self._predecessors.get(current, ()))
        return seen

    def _steps_of_for_each(self, for_each_id: str) -> list[str]:
        return [step_id for step_id, owner in self._step_for_each.items() if owner == for_each_id]

    def _steps_of_group(self, group_id: str) -> list[str]:
        """Steps of the Group, including the bodies of the For each it holds."""
        steps = [step_id for step_id, owner in self._step_group.items() if owner == group_id]
        for for_each_id, owner in self._for_each_group.items():
            if owner == group_id:
                steps.extend(self._steps_of_for_each(for_each_id))
        return steps

    def _container_chain(self, step_id: str) -> list[tuple[str, str]]:
        """The step's containers, nearest first: its For each, then a Group."""
        chain: list[tuple[str, str]] = []
        group_id = self._step_group.get(step_id)
        for_each_id = self._step_for_each.get(step_id)
        if for_each_id:
            chain.append((NODE_KIND_FOR_EACH, for_each_id))
            group_id = self._for_each_group.get(for_each_id)
        if group_id:
            chain.append((NODE_KIND_GROUP, group_id))
        return chain

    def upstream_step_ids(self, step_id: str) -> list[str]:
        """Steps that run before *step_id* on some path, in definition order."""
        normalized = _text(step_id)
        if normalized not in self._step_group:
            return []
        upstream: set[str] = set()

        def collect(nodes: Iterable[tuple[str, str]]) -> None:
            for kind, node_id in nodes:
                if kind == NODE_KIND_STEP:
                    upstream.add(node_id)
                elif kind == NODE_KIND_FOR_EACH:
                    upstream.update(self._steps_of_for_each(node_id))
                else:
                    upstream.update(self._steps_of_group(node_id))

        collect(self._ancestors((NODE_KIND_STEP, normalized)))
        for container in self._container_chain(normalized):
            collect(self._ancestors(container))
        upstream.discard(normalized)
        return sorted(upstream, key=lambda item: self._order.get(item, len(self._order)))

    def upstream_steps(self, step_id: str) -> list[dict[str, Any]]:
        return [step for item in self.upstream_step_ids(step_id) if (step := self.step(item))]


def upstream_step_ids(workflow: Mapping[str, Any], step_id: str) -> list[str]:
    return DefinitionGraph.from_workflow(workflow).upstream_step_ids(step_id)


__all__ = ["DefinitionGraph", "upstream_step_ids"]
