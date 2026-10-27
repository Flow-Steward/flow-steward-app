"""Conservative shape proof for authored state reads, separate from availability.

Only an unambiguous successful predecessor chain supplies producer evidence.
Unknown members remain runtime-validated; this is not an element schema engine.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from core.application.orchestration.state_paths import StatePathError, parse_state_path
from core.application.orchestration.transform_selectors import SelectorToken
from core.application.workflows.execution_graph_queries import DefinitionGraph

if TYPE_CHECKING:
    from core.application.registry.io_resolver import IOResolver


def _tokens(value: str) -> tuple[SelectorToken, ...]:
    try:
        return tuple(parse_state_path(value))
    except StatePathError:
        return ()


def _overlaps(left: tuple, right: tuple) -> bool:
    return (bool(left and right) and left[: len(right)] == right) or (
        bool(left and right) and right[: len(left)] == left
    )


def _ref(value: Any) -> tuple[str, str]:
    return (
        (str(value.get("kind", "")), str(value.get("id", "")))
        if isinstance(value, dict)
        else ("", "")
    )


class StateSelectorShape:
    """Bounded authoring evidence for one workflow's existing IOResolver."""

    def __init__(self, resolver: IOResolver, workflow: dict) -> None:
        self.resolver = resolver
        self.workflow = workflow
        self.graph = DefinitionGraph.from_workflow(workflow)
        self.routing = (workflow.get("settings") or {}).get("execution_graph") or {}
        self._writes: dict[str, list[dict]] = {}

    def _step_writes(self, step: dict) -> list[dict]:
        step_id = str(step.get("step_id") or "")
        if step_id not in self._writes:
            self._writes[step_id] = self.resolver.get_step_state_writes(
                step, workflow_account_id=str(self.workflow.get("account_id") or "")
            )
        return self._writes[step_id]

    def _chain(self, target: tuple[str, str], entry: tuple[str, str]) -> list[str] | None:
        # A rework edge can return directly to a container's entry; that boundary
        # must be checked even when this invocation has no predecessor to walk.
        if any(
            e.get("rework") and _ref(e.get("target")) == entry
            for e in self.routing.get("edges", [])
        ):
            return None
        chain: list[str] = []
        seen: set[tuple[str, str]] = set()
        while target != entry:
            if target in seen:
                return None
            seen.add(target)
            incoming = [e for e in self.routing.get("edges", []) if _ref(e.get("target")) == target]
            if len(incoming) != 1:
                return None
            edge = incoming[0]
            source = _ref(edge.get("source"))
            if (
                edge.get("rework")
                or (edge.get("outcome") or {}).get("name") != "success"
                or source[0] != "step"
            ):
                return None
            chain.append(source[1])
            target = source
        return chain

    def _before(self, step: dict) -> list[str] | None:
        target = ("step", str(step.get("step_id") or ""))
        chain: list[str] = []
        loop = self.graph.for_each_of(target[1])
        group_id = step.get("group_id")
        if loop:
            inner = self._chain(target, _ref((loop.get("body") or {}).get("entry")))
            if inner is None:
                return None
            chain += inner
            target = ("for_each", str(loop["for_each_id"]))
            group_id = loop.get("group_id")
        if group_id:
            group = self.graph.group(str(group_id)) or {}
            if (group.get("body") or {}).get("kind") != "sequence":
                return None
            inner = self._chain(target, _ref((group.get("body") or {}).get("entry")))
            if inner is None:
                return None
            chain += inner
            target = ("group", str(group_id))
        outer = self._chain(target, _ref(self.routing.get("entry")))
        return None if outer is None else chain + outer

    def _producer(self, step: dict, path: tuple) -> tuple[dict, dict] | None:
        chain = self._before(step)
        if chain is None:
            return None
        loop = self.graph.for_each_of(str(step.get("step_id") or ""))
        published = [
            _tokens(str((loop or {}).get(name) or "")) for name in ("item_name", "index_name")
        ]
        shadowed = any(_overlaps(path, root) for root in published)
        writes = [
            (producer, row)
            for producer in self.graph.upstream_steps(str(step.get("step_id") or ""))
            if not shadowed or producer.get("for_each_id") == (loop or {}).get("for_each_id")
            for row in self._step_writes(producer)
            if _overlaps(_tokens(str(row.get("name") or "")), path)
        ]
        if len(writes) != 1:
            return None
        producer, row = writes[0]
        if producer.get("step_id") not in chain or _tokens(str(row.get("name") or "")) != path:
            return None
        return producer, row

    def _type(
        self, step: dict, path: tuple, visited: frozenset[str] = frozenset()
    ) -> tuple[str, bool]:
        proof = self._producer(step, path)
        if not proof:
            return "json", False
        producer, row = proof
        if producer.get("step_kind") != "batch":
            return str(row.get("type") or "json"), False
        step_id = str(producer.get("step_id") or "")
        if step_id in visited:
            return "json", False
        source = ((producer.get("override") or {}).get("batch") or {}).get("source_path")
        source_type, _ = self._type(producer, _tokens(str(source or "")), visited | {step_id})
        return ("array", True) if source_type in {"array", "list"} else ("json", False)

    def loop_member(self, step_id: str, selector: str) -> bool:
        """Availability fallback only for parsed, explicit current-item descendants."""
        if not selector.startswith("state."):
            return False
        path = _tokens(selector)
        loop = self.graph.for_each_of(step_id)
        root = _tokens(str((loop or {}).get("item_name") or ""))
        return bool(root and len(path) > len(root) and path[: len(root)] == root)

    def _known_type(self, step: dict, path: tuple) -> tuple[str, bool]:
        kind, chunks = self._type(step, path)
        if kind != "json":
            return kind, chunks
        loop = self.graph.for_each_of(str(step.get("step_id") or ""))
        if not loop:
            return "json", False
        index = _tokens(str(loop.get("index_name") or ""))
        item = _tokens(str(loop.get("item_name") or ""))
        if not path or path not in (index, item):
            return "json", False
        # Body writes can replace/mutate publication. Outside writes are shadowed.
        body_writes = [
            row
            for body_step in self.graph.steps
            if body_step.get("for_each_id") == loop["for_each_id"]
            for row in self._step_writes(body_step)
        ]
        if any(_overlaps(_tokens(str(row.get("name") or "")), path) for row in body_writes):
            return "json", False
        if path == index:
            return "number", False
        source_type, chunked = self._type(step, _tokens(str(loop.get("over") or "")))
        return ("array", False) if source_type == "array" and chunked else ("json", False)

    def source_type(self, step_id: str, selector: str) -> str:
        """Refine a whole selected value only when the same bounded proof knows it."""
        step = self.graph.step(step_id)
        path = _tokens(selector)
        if not step or not path:
            return "json"
        if not selector.startswith("state.") and (len(path) != 1 or path[0].kind != "key"):
            return "json"
        return self._known_type(step, path)[0]

    def problem(self, step_id: str, selector: str) -> str:
        """Describe impossible traversal only when the current producer proves it."""
        path = _tokens(selector)
        step = self.graph.step(step_id)
        if not step or len(path) < 2:
            return ""
        # Prefer the longest actual producer path. Exact writes carry their own type.
        for length in range(len(path), 0, -1):
            kind, chunks = self._known_type(step, path[:length])
            if kind != "json":
                return self._traverse(selector, kind, path[length:], chunks)
        return ""

    @staticmethod
    def _traverse(selector: str, kind: str, remaining: tuple, chunks: bool) -> str:
        for token in remaining:
            if kind == "json":
                return ""
            if kind in {"array", "list"}:
                if token.kind != "index" and not (
                    token.kind == "key" and str(token.value).isdigit()
                ):
                    return f"'{selector}' reads a field from a known list. Select an item with [0], pass the whole list, or use the Template length filter on a whole-list input."
                kind, chunks = ("array" if chunks else "json"), False
            elif kind == "object":
                if token.kind == "index":
                    return f"'{selector}' indexes a known object with [0]. Use an object property (including .0 for a numeric key), or pass the whole object."
                kind = "json"
            else:
                return f"'{selector}' traverses a known {kind} value. Use the value itself or select a field of an object/list producer."
        return ""
