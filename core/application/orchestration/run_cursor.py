"""Where a run is, and where it goes next, decided only by the frozen definition.

The runtime keeps one cursor per run (outside parallel lanes): the step being
executed, its logical execution id and attempt, and a stack of container frames
— a run-once Group, a For each, or a Group holding a For each (ADR 0038). After
a step reports its outcome, ``resolve_after_step`` follows the execution graph's
single edge for that outcome and returns what happens next. Nothing here reads
the registry, sort order or step config targets.

A transition is one of: dispatch a step, enter a container, finish a body (a
Group's single pass or a For each item), break out of a For each, stop the
workflow, or escalate because a rework budget is exhausted. Rework counters are
durable state carried in the cursor; finite steps and For each items never
count against them.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, ClassVar

from core.application.workflows.execution_graph import (
    NODE_KIND_FOR_EACH,
    NODE_KIND_GROUP,
    NODE_KIND_STEP,
    OUTCOME_ERROR,
    OUTCOME_SUCCESS,
    ROOT_SCOPE,
    RUN_REWORK_TRANSITION_BUDGET,
    Edge,
    ExecutionGraph,
    ForEachExitRef,
    GroupExitRef,
    NodeRef,
    Outcome,
    Scope,
    TargetRef,
    edge_identity,
    for_each_scope,
    group_scope,
    parse_execution_graph,
)
from core.application.workflows.workflow_step_kinds import (
    STEP_KIND_BREAK,
    STEP_KIND_CONTINUE,
    STEP_KIND_STOP_FAIL,
    STEP_KIND_STOP_SUCCESS,
)

RUNTIME_SCHEMA_VERSION = 2

TRANSITION_DISPATCH = "dispatch"
#: Enter a Group or For each node: a new container execution.
TRANSITION_ENTER = "enter"
#: A sequence body reached its exit: a Group pass or a For each item is done.
TRANSITION_BODY_DONE = "body_done"
#: Break: leave the For each now, without its remaining items.
TRANSITION_BREAK = "break"
#: Hand a lanes body (all lanes at their entries) to the parallel executor.
TRANSITION_LANES = "lanes"
TRANSITION_LANE_DONE = "lane_done"
TRANSITION_STOP = "stop"
TRANSITION_ESCALATE = "escalate"

FRAME_GROUP = "group"
FRAME_FOR_EACH = "for_each"


class RunCursorError(RuntimeError):
    """The frozen definition cannot route the run from where it is."""


@dataclass
class GroupFrame:
    """A run-once Group execution. It has no item, source or index."""

    group_id: str
    group_execution_id: str

    kind: ClassVar[str] = FRAME_GROUP

    @property
    def node(self) -> NodeRef:
        return NodeRef(NODE_KIND_GROUP, self.group_id)

    @property
    def container_id(self) -> str:
        return self.group_id

    @property
    def execution_id(self) -> str:
        return self.group_execution_id

    def body_scope(self, lane_id: str | None = None) -> Scope:
        return group_scope(self.group_id, lane_id)

    def dump(self) -> dict[str, Any]:
        return {
            "frame": FRAME_GROUP,
            "group_id": self.group_id,
            "group_execution_id": self.group_execution_id,
        }


@dataclass
class ForEachFrame:
    """One pass of a For each over the items fixed at its entry.

    The selected items (at most the applied limit) live in the state channel
    ``for_each_items`` under this execution id, written once when the pass
    starts; the frame, rewritten with the cursor on every step, keeps only the
    position and the counts. Retries and restarts continue the same pass; a
    rework re-entry is a new execution that selects again.
    """

    for_each_id: str
    for_each_execution_id: str
    selected_count: int = 0
    item_index: int = 0
    source_count: int = 0
    applied_limit: int = 0
    truncated: bool = False
    digest: str = ""
    processed_count: int = 0
    broke_at: int | None = None

    kind: ClassVar[str] = FRAME_FOR_EACH

    @property
    def node(self) -> NodeRef:
        return NodeRef(NODE_KIND_FOR_EACH, self.for_each_id)

    @property
    def container_id(self) -> str:
        return self.for_each_id

    @property
    def execution_id(self) -> str:
        return self.for_each_execution_id

    def body_scope(self, lane_id: str | None = None) -> Scope:
        return for_each_scope(self.for_each_id, lane_id)

    def counts(self) -> dict[str, Any]:
        """The bounded diagnostics of this pass; never the items themselves."""
        return {
            "source_count": self.source_count,
            "selected_count": self.selected_count,
            "processed_count": self.processed_count,
            "applied_limit": self.applied_limit,
            "truncated": self.truncated,
            "broke_at": self.broke_at,
        }

    def dump(self) -> dict[str, Any]:
        return {
            "frame": FRAME_FOR_EACH,
            "for_each_id": self.for_each_id,
            "for_each_execution_id": self.for_each_execution_id,
            "selected_count": self.selected_count,
            "item_index": self.item_index,
            "source_count": self.source_count,
            "applied_limit": self.applied_limit,
            "truncated": self.truncated,
            "digest": self.digest,
            "processed_count": self.processed_count,
            "broke_at": self.broke_at,
        }


Frame = GroupFrame | ForEachFrame


def load_frame(raw: Mapping[str, Any]) -> Frame:
    """One frame of the final checkpoint shape; any other shape is refused.

    A pre-release loop-group frame has no ``frame`` marker. It is never read
    as a Group or For each: the run stops before any further effect.
    """
    marker = raw.get("frame")
    if marker == FRAME_GROUP:
        return GroupFrame(
            group_id=str(raw.get("group_id") or ""),
            group_execution_id=str(raw.get("group_execution_id") or ""),
        )
    if marker == FRAME_FOR_EACH:
        broke_at = raw.get("broke_at")
        return ForEachFrame(
            for_each_id=str(raw.get("for_each_id") or ""),
            for_each_execution_id=str(raw.get("for_each_execution_id") or ""),
            selected_count=int(raw.get("selected_count") or 0),
            item_index=int(raw.get("item_index") or 0),
            source_count=int(raw.get("source_count") or 0),
            applied_limit=int(raw.get("applied_limit") or 0),
            truncated=bool(raw.get("truncated")),
            digest=str(raw.get("digest") or ""),
            processed_count=int(raw.get("processed_count") or 0),
            broke_at=int(broke_at) if isinstance(broke_at, int) else None,
        )
    raise RunCursorError(
        "The checkpoint holds a container frame of a removed pre-release format; start a new run"
    )


@dataclass
class ReworkBudget:
    """Durable rework counters: one for the run, one per limited edge.

    An approval of an exhausted budget raises the allowance; counters are never
    reset, so restarts and retries cannot hand out a fresh budget.
    """

    used: int = 0
    allowance: int = RUN_REWORK_TRANSITION_BUDGET
    edges: dict[str, dict[str, int]] = field(default_factory=dict)

    def dump(self) -> dict[str, Any]:
        return {
            "used": self.used,
            "allowance": self.allowance,
            "edges": {key: dict(value) for key, value in self.edges.items()},
        }

    @classmethod
    def load(cls, raw: Any) -> ReworkBudget:
        if not isinstance(raw, Mapping):
            return cls()
        edges = raw.get("edges")
        return cls(
            used=int(raw.get("used") or 0),
            allowance=int(raw.get("allowance") or RUN_REWORK_TRANSITION_BUDGET),
            edges={
                str(key): {
                    "used": int(value.get("used") or 0),
                    "allowance": int(value.get("allowance") or 0),
                }
                for key, value in (edges.items() if isinstance(edges, Mapping) else [])
                if isinstance(value, Mapping)
            },
        )


@dataclass
class RunCursor:
    step_id: str | None = None
    step_execution_id: str | None = None
    attempt: int = 1
    frames: list[Frame] = field(default_factory=list)
    rework: ReworkBudget = field(default_factory=ReworkBudget)

    def dump(self) -> dict[str, Any]:
        return {
            "step_id": self.step_id,
            "step_execution_id": self.step_execution_id,
            "attempt": self.attempt,
            "frames": [frame.dump() for frame in self.frames],
            "rework": self.rework.dump(),
        }

    @classmethod
    def load(cls, raw: Any) -> RunCursor:
        if not isinstance(raw, Mapping):
            return cls()
        frames = raw.get("frames")
        return cls(
            step_id=str(raw.get("step_id")) if raw.get("step_id") else None,
            step_execution_id=str(raw.get("step_execution_id"))
            if raw.get("step_execution_id")
            else None,
            attempt=int(raw.get("attempt") or 1),
            frames=[load_frame(frame) for frame in frames if isinstance(frame, Mapping)]
            if isinstance(frames, list)
            else [],
            rework=ReworkBudget.load(raw.get("rework")),
        )

    @property
    def scope(self) -> Scope:
        """The scope the current step runs in: the innermost body, or the root."""
        if not self.frames:
            return ROOT_SCOPE
        return self.frames[-1].body_scope(None)

    def frame_of(self, node: NodeRef) -> Frame | None:
        return next((frame for frame in reversed(self.frames) if frame.node == node), None)

    def innermost_for_each(self) -> ForEachFrame | None:
        return next(
            (frame for frame in reversed(self.frames) if isinstance(frame, ForEachFrame)), None
        )


@dataclass(frozen=True)
class Transition:
    kind: str
    step_id: str | None = None
    #: The Group or For each the transition is about.
    container: NodeRef | None = None
    lane_id: str | None = None
    outcome: str | None = None
    case_id: str | None = None
    rework: bool = False
    blocked_edge: tuple[Any, ...] | None = None
    exhausted: tuple[str, ...] = ()
    # Selected authored edge, only after the resolver allowed it.
    edge: Edge | None = None
    source_frame: Frame | None = None

    @property
    def group_id(self) -> str | None:
        """The Group of a group transition (rework escalations name their source)."""
        if self.container is not None and self.container.kind == NODE_KIND_GROUP:
            return self.container.id
        return None


class FrozenDefinition:
    """Read-only view of a pinned snapshot, indexed for the runtime."""

    def __init__(self, definition: Mapping[str, Any]) -> None:
        graph, issues = parse_execution_graph(definition.get("execution_graph"))
        # A step test run executes one pinned step and never routes, so its
        # draft may lack a usable graph; any routing attempt then fails here.
        self._graph = graph
        self._graph_issues = issues
        self.steps = {
            str(step.get("step_id")): dict(step)
            for step in definition.get("steps") or []
            if isinstance(step, Mapping) and step.get("step_id")
        }
        settings = definition.get("settings")
        self.settings: dict[str, Any] = dict(settings) if isinstance(settings, Mapping) else {}
        self.groups = {
            str(group.get("group_id")): dict(group)
            for group in definition.get("groups") or []
            if isinstance(group, Mapping) and group.get("group_id")
        }
        self.for_each = {
            str(item.get("for_each_id")): dict(item)
            for item in definition.get("for_each") or []
            if isinstance(item, Mapping) and item.get("for_each_id")
        }

    @property
    def graph(self) -> ExecutionGraph:
        if self._graph is None:
            raise RunCursorError(
                f"The pinned definition has no usable execution graph: {self._graph_issues}"
            )
        return self._graph

    def step(self, step_id: str) -> dict[str, Any]:
        try:
            return self.steps[step_id]
        except KeyError as exc:
            raise RunCursorError(f"Step '{step_id}' is not in the pinned definition") from exc

    def group(self, group_id: str) -> dict[str, Any]:
        try:
            return self.groups[group_id]
        except KeyError as exc:
            raise RunCursorError(f"Group '{group_id}' is not in the pinned definition") from exc

    def for_each_item(self, for_each_id: str) -> dict[str, Any]:
        try:
            return self.for_each[for_each_id]
        except KeyError as exc:
            raise RunCursorError(
                f"For each '{for_each_id}' is not in the pinned definition"
            ) from exc

    def container(self, node: NodeRef) -> dict[str, Any]:
        if node.kind == NODE_KIND_GROUP:
            return self.group(node.id)
        if node.kind == NODE_KIND_FOR_EACH:
            return self.for_each_item(node.id)
        raise RunCursorError(f"{node.kind} '{node.id}' is not a container")

    def container_scope(self, node: NodeRef) -> Scope:
        """Where the container node sits: a For each may sit in a sequence Group."""
        if node.kind == NODE_KIND_FOR_EACH:
            parent = self.for_each_item(node.id).get("group_id")
            return group_scope(str(parent), None) if parent else ROOT_SCOPE
        return ROOT_SCOPE

    def is_lanes(self, node: NodeRef) -> bool:
        body = self.container(node).get("body")
        return isinstance(body, Mapping) and body.get("kind") == "lanes"

    def body_entry(self, node: NodeRef, lane_id: str | None = None) -> NodeRef:
        body = self.container(node).get("body") or {}
        if lane_id is None:
            entry = body.get("entry") if isinstance(body, Mapping) else None
        else:
            lanes = body.get("lanes") if isinstance(body, Mapping) else None
            entry = next(
                (
                    lane.get("entry")
                    for lane in lanes or []
                    if isinstance(lane, Mapping) and lane.get("lane_id") == lane_id
                ),
                None,
            )
        if not isinstance(entry, Mapping) or not entry.get("id"):
            raise RunCursorError(
                f"{node.kind} '{node.id}' has no entry for {lane_id or 'its body'}"
            )
        return NodeRef(str(entry.get("kind") or NODE_KIND_STEP), str(entry["id"]))

    def lane_ids(self, node: NodeRef) -> list[str]:
        body = self.container(node).get("body") or {}
        lanes = body.get("lanes") if isinstance(body, Mapping) else None
        return [
            str(lane["lane_id"])
            for lane in lanes or []
            if isinstance(lane, Mapping) and lane.get("lane_id")
        ]

    def step_containers(self, step_id: str) -> list[dict[str, Any]]:
        """The step's containers nearest first: its For each, then a Group."""
        step = self.step(step_id)
        chain: list[dict[str, Any]] = []
        group_id = step.get("group_id")
        if step.get("for_each_id"):
            item = self.for_each_item(str(step["for_each_id"]))
            chain.append({**item, "_node": NodeRef(NODE_KIND_FOR_EACH, item["for_each_id"])})
            group_id = item.get("group_id")
        if group_id:
            group = self.group(str(group_id))
            chain.append({**group, "_node": NodeRef(NODE_KIND_GROUP, group["group_id"])})
        return chain


def new_execution_id() -> str:
    return str(uuid.uuid4())


def _node_transition(
    definition: FrozenDefinition, target: TargetRef, *, edge: Edge | None
) -> Transition:
    rework = bool(edge.rework) if edge else False
    outcome = edge.outcome.name if edge else None
    if isinstance(target, GroupExitRef | ForEachExitRef):
        container = (
            NodeRef(NODE_KIND_GROUP, target.group_id)
            if isinstance(target, GroupExitRef)
            else NodeRef(NODE_KIND_FOR_EACH, target.for_each_id)
        )
        kind = TRANSITION_LANE_DONE if target.lane_id is not None else TRANSITION_BODY_DONE
        return Transition(
            kind, container=container, lane_id=target.lane_id, outcome=outcome, edge=edge
        )
    if target.kind in {NODE_KIND_GROUP, NODE_KIND_FOR_EACH}:
        definition.container(target)
        return Transition(
            TRANSITION_ENTER, container=target, outcome=outcome, rework=rework, edge=edge
        )
    # Every step, a Stop included, is dispatched: a Stop runs so its message and
    # terminal status are recorded before resolve_after_step ends the run.
    definition.step(target.id)
    return Transition(
        TRANSITION_DISPATCH, step_id=target.id, outcome=outcome, rework=rework, edge=edge
    )


def resolve_entry(definition: FrozenDefinition) -> Transition:
    """The first transition of a run: the graph's root entry."""
    return _node_transition(definition, definition.graph.entry, edge=None)


def _check_rework(cursor: RunCursor, scope: Any, edge: Edge) -> Transition | None:
    """Count a rework transition, or say which budget blocks it."""
    if not edge.rework:
        return None
    key = "|".join(str(part) for part in edge_identity(scope, edge))
    edge_counter = cursor.rework.edges.get(key)
    if edge.rework_limit is not None and edge_counter is None:
        edge_counter = {"used": 0, "allowance": int(edge.rework_limit)}
    exhausted: list[str] = []
    if cursor.rework.used >= cursor.rework.allowance:
        exhausted.append("run")
    if edge_counter is not None and edge_counter["used"] >= edge_counter["allowance"]:
        exhausted.append("edge")
    if exhausted:
        return Transition(
            TRANSITION_ESCALATE,
            step_id=edge.source.id if edge.source.kind == NODE_KIND_STEP else None,
            container=edge.source if edge.source.kind != NODE_KIND_STEP else None,
            outcome=edge.outcome.name,
            case_id=edge.outcome.case_id,
            rework=True,
            blocked_edge=edge_identity(scope, edge),
            exhausted=tuple(exhausted),
        )
    cursor.rework.used += 1
    if edge_counter is not None:
        edge_counter["used"] += 1
        cursor.rework.edges[key] = edge_counter
    return None


def resolve_after_step(
    definition: FrozenDefinition,
    cursor: RunCursor,
    outcome: Outcome,
) -> Transition:
    """Follow the current step's single edge for *outcome*.

    A Stop step ends the workflow; Continue ends the current For each item and
    Break the whole For each. A step without an edge for the outcome is a
    definition the validator would have refused, so it is an integrity error.
    """
    if not cursor.step_id:
        raise RunCursorError("No step is active")
    step = definition.step(cursor.step_id)
    kind = str(step.get("step_kind") or "")
    if kind == STEP_KIND_STOP_SUCCESS:
        return Transition(TRANSITION_STOP, step_id=cursor.step_id, outcome="success")
    if kind == STEP_KIND_STOP_FAIL:
        return Transition(TRANSITION_STOP, step_id=cursor.step_id, outcome="failed")
    if kind in {STEP_KIND_BREAK, STEP_KIND_CONTINUE}:
        frame = cursor.frames[-1] if cursor.frames else None
        if not isinstance(frame, ForEachFrame) or frame.for_each_id != step.get("for_each_id"):
            raise RunCursorError(f"Step '{cursor.step_id}' ({kind}) is not in its For each body")
        return Transition(
            TRANSITION_BREAK if kind == STEP_KIND_BREAK else TRANSITION_BODY_DONE,
            step_id=cursor.step_id,
            container=frame.node,
            outcome=kind,
        )
    source = NodeRef(NODE_KIND_STEP, cursor.step_id)
    edge = definition.graph.edge_for(source, outcome)
    if edge is None:
        raise RunCursorError(
            f"Step '{cursor.step_id}' has no edge for outcome '{outcome.name}'"
            + (f" case '{outcome.case_id}'" if outcome.case_id else "")
        )
    blocked = _check_rework(cursor, cursor.scope, edge)
    if blocked is not None:
        return blocked
    return _node_transition(definition, edge.target, edge=edge)


def resolve_after_container(
    definition: FrozenDefinition,
    cursor: RunCursor,
    node: NodeRef,
    *,
    failed: bool = False,
) -> Transition:
    """Leave a finished container through its success or error edge in its own scope."""
    outcome = Outcome(OUTCOME_ERROR if failed else OUTCOME_SUCCESS)
    edge = definition.graph.edge_for(node, outcome)
    if edge is None:
        raise RunCursorError(f"{node.kind} '{node.id}' has no edge for outcome '{outcome.name}'")
    blocked = _check_rework(cursor, definition.container_scope(node), edge)
    if blocked is not None:
        return blocked
    return _node_transition(definition, edge.target, edge=edge)


def push_frame(
    definition: FrozenDefinition,
    cursor: RunCursor,
    node: NodeRef,
    *,
    id_factory: Callable[[], str] = new_execution_id,
) -> Frame:
    """A new container execution: every entry, a rework included, starts afresh.

    A For each frame starts without items; the For each runtime fixes them.
    """
    definition.container(node)
    frame: Frame = (
        GroupFrame(group_id=node.id, group_execution_id=id_factory())
        if node.kind == NODE_KIND_GROUP
        else ForEachFrame(for_each_id=node.id, for_each_execution_id=id_factory())
    )
    cursor.frames.append(frame)
    return frame


def body_start(definition: FrozenDefinition, frame: Frame) -> Transition:
    """How a (Group or item) body starts: lanes, its entry step, or a For each."""
    if definition.is_lanes(frame.node):
        return Transition(TRANSITION_LANES, container=frame.node)
    entry = definition.body_entry(frame.node)
    if entry.kind == NODE_KIND_STEP:
        return Transition(TRANSITION_DISPATCH, step_id=entry.id, container=frame.node)
    return Transition(TRANSITION_ENTER, container=entry)


def pop_frame(cursor: RunCursor, node: NodeRef) -> Frame:
    if not cursor.frames or cursor.frames[-1].node != node:
        raise RunCursorError(f"{node.kind} '{node.id}' is not the active container")
    return cursor.frames.pop()


def advance_item(cursor: RunCursor, node: NodeRef) -> bool:
    """After an item's body ended: move to the next item, or say the pass is over."""
    frame = cursor.frames[-1] if cursor.frames else None
    if not isinstance(frame, ForEachFrame) or frame.node != node:
        raise RunCursorError(f"For each '{node.id}' is not the active container")
    frame.processed_count = frame.item_index + 1
    if frame.item_index + 1 < frame.selected_count:
        frame.item_index += 1
        return True
    return False


def break_item(cursor: RunCursor, node: NodeRef) -> ForEachFrame:
    """Break: the current item counts as processed and the rest are skipped."""
    frame = cursor.frames[-1] if cursor.frames else None
    if not isinstance(frame, ForEachFrame) or frame.node != node:
        raise RunCursorError(f"For each '{node.id}' is not the active container")
    frame.processed_count = frame.item_index + 1
    frame.broke_at = frame.item_index
    return frame


def begin_step(
    cursor: RunCursor, step_id: str, *, id_factory: Callable[[], str] = new_execution_id
) -> RunCursor:
    """A new logical entry: a new execution id, attempt 1."""
    cursor.step_id = step_id
    cursor.step_execution_id = id_factory()
    cursor.attempt = 1
    return cursor


def seeded_step_cursor(step_id: str, *, seed: str) -> RunCursor:
    """A cursor already at *step_id* whose execution id is derived from *seed*.

    Isolated runs (step test run, replay) start at one step. Deriving the
    execution id from their own trusted identity means the submitter and the
    worker that restores the run name the same execution.
    """
    execution_id = str(
        uuid.uuid5(uuid.NAMESPACE_URL, f"flow-steward:step-execution:{seed}:{step_id}")
    )
    return begin_step(RunCursor(), step_id, id_factory=lambda: execution_id)


def retry_step(cursor: RunCursor) -> RunCursor:
    """The same logical entry again: same execution id, next attempt."""
    cursor.attempt += 1
    return cursor


def grant_rework_budget(
    cursor: RunCursor, transition: Transition, edge_limit: int | None
) -> RunCursor:
    """Approve an exhausted budget: raise only the exhausted allowances, once.

    Counters keep their values; the blocked transition is then taken exactly once.
    """
    if "run" in transition.exhausted:
        cursor.rework.allowance += RUN_REWORK_TRANSITION_BUDGET
    if "edge" in transition.exhausted and transition.blocked_edge is not None and edge_limit:
        key = "|".join(str(part) for part in transition.blocked_edge)
        counter = cursor.rework.edges.setdefault(key, {"used": 0, "allowance": 0})
        counter["allowance"] += int(edge_limit)
    return cursor


__all__ = [
    "FRAME_FOR_EACH",
    "FRAME_GROUP",
    "RUNTIME_SCHEMA_VERSION",
    "TRANSITION_BODY_DONE",
    "TRANSITION_BREAK",
    "TRANSITION_DISPATCH",
    "TRANSITION_ENTER",
    "TRANSITION_ESCALATE",
    "TRANSITION_LANES",
    "TRANSITION_LANE_DONE",
    "TRANSITION_STOP",
    "ForEachFrame",
    "Frame",
    "FrozenDefinition",
    "GroupFrame",
    "ReworkBudget",
    "RunCursor",
    "RunCursorError",
    "Transition",
    "advance_item",
    "begin_step",
    "body_start",
    "break_item",
    "grant_rework_budget",
    "load_frame",
    "new_execution_id",
    "pop_frame",
    "push_frame",
    "resolve_after_container",
    "resolve_after_step",
    "resolve_entry",
    "retry_step",
    "seeded_step_cursor",
]
