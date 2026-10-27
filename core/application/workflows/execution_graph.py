"""The workflow ``execution_graph`` contract: the one source of routing.

A workflow definition owns steps, run-once groups and For each containers
(ADR 0038). Which node runs next is decided only by this graph — never by ``sort_order``, canvas
positions or targets stored inside a step's config. The graph lives in the
service-owned ``execution_graph`` workflow setting::

    {schema_version: 1, entry: TargetRef, edges: Edge[]}

``group_exit`` targets are the boundary of a group body or lane;
``for_each_exit`` targets end the current item of a For each body or lane. They
are endpoints, not executable nodes, and no synthetic phase stands behind them.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any, ClassVar

EXECUTION_GRAPH_SETTING = "execution_graph"
EXECUTION_GRAPH_SCHEMA_VERSION = 1

NODE_KIND_STEP = "step"
NODE_KIND_GROUP = "group"
NODE_KIND_FOR_EACH = "for_each"
TARGET_KIND_GROUP_EXIT = "group_exit"
TARGET_KIND_FOR_EACH_EXIT = "for_each_exit"
NODE_KINDS = frozenset({NODE_KIND_STEP, NODE_KIND_GROUP, NODE_KIND_FOR_EACH})
CONTAINER_NODE_KINDS = frozenset({NODE_KIND_GROUP, NODE_KIND_FOR_EACH})

OUTCOME_SUCCESS = "success"
OUTCOME_TRUE = "true"
OUTCOME_FALSE = "false"
OUTCOME_CASE = "case"
OUTCOME_DEFAULT = "default"
OUTCOME_APPROVED = "approved"
OUTCOME_REJECTED = "rejected"
OUTCOME_ERROR = "error"
OUTCOME_NAMES = frozenset(
    {
        OUTCOME_SUCCESS,
        OUTCOME_TRUE,
        OUTCOME_FALSE,
        OUTCOME_CASE,
        OUTCOME_DEFAULT,
        OUTCOME_APPROVED,
        OUTCOME_REJECTED,
        OUTCOME_ERROR,
    }
)
#: Outcomes a rework edge may leave from. A success/approved return would be an
#: ordinary success cycle, which is never allowed.
REWORK_SOURCE_OUTCOMES = frozenset(
    {
        OUTCOME_TRUE,
        OUTCOME_FALSE,
        OUTCOME_CASE,
        OUTCOME_DEFAULT,
        OUTCOME_REJECTED,
        OUTCOME_ERROR,
    }
)

#: Rework transitions one run may take before the next one escalates to a human.
#: Finite steps and loop iterations never count against it.
RUN_REWORK_TRANSITION_BUDGET = 50
REWORK_LIMIT_MIN = 1
REWORK_LIMIT_MAX = 50

_EDGE_KEYS = frozenset({"source", "outcome", "target", "rework", "rework_limit"})
_GRAPH_KEYS = frozenset({"schema_version", "entry", "edges"})


@dataclass(frozen=True)
class NodeRef:
    kind: str
    id: str

    def dump(self) -> dict[str, str]:
        return {"kind": self.kind, "id": self.id}


@dataclass(frozen=True)
class GroupExitRef:
    group_id: str
    lane_id: str | None

    kind: ClassVar[str] = TARGET_KIND_GROUP_EXIT

    def dump(self) -> dict[str, Any]:
        return {"kind": TARGET_KIND_GROUP_EXIT, "group_id": self.group_id, "lane_id": self.lane_id}


@dataclass(frozen=True)
class ForEachExitRef:
    """The end of the current item of a For each body or lane."""

    for_each_id: str
    lane_id: str | None

    kind: ClassVar[str] = TARGET_KIND_FOR_EACH_EXIT

    def dump(self) -> dict[str, Any]:
        return {
            "kind": TARGET_KIND_FOR_EACH_EXIT,
            "for_each_id": self.for_each_id,
            "lane_id": self.lane_id,
        }


ExitRef = GroupExitRef | ForEachExitRef
TargetRef = NodeRef | GroupExitRef | ForEachExitRef


@dataclass(frozen=True)
class Outcome:
    name: str
    case_id: str | None = None

    def dump(self) -> dict[str, str]:
        payload = {"name": self.name}
        if self.case_id is not None:
            payload["case_id"] = self.case_id
        return payload


@dataclass(frozen=True)
class Edge:
    source: NodeRef
    outcome: Outcome
    target: TargetRef
    rework: bool = False
    rework_limit: int | None = None

    def dump(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "source": self.source.dump(),
            "outcome": self.outcome.dump(),
            "target": self.target.dump(),
            "rework": self.rework,
        }
        if self.rework_limit is not None:
            payload["rework_limit"] = self.rework_limit
        return payload


@dataclass(frozen=True)
class ExecutionGraph:
    entry: TargetRef
    edges: tuple[Edge, ...]

    def dump(self) -> dict[str, Any]:
        return {
            "schema_version": EXECUTION_GRAPH_SCHEMA_VERSION,
            "entry": self.entry.dump(),
            "edges": [edge.dump() for edge in self.edges],
        }

    def outgoing(self, source: NodeRef) -> tuple[Edge, ...]:
        return tuple(edge for edge in self.edges if edge.source == source)

    def edge_for(self, source: NodeRef, outcome: Outcome) -> Edge | None:
        """The single edge leaving *source* on *outcome*, if one is authored."""
        for edge in self.edges:
            if edge.source == source and edge.outcome == outcome:
                return edge
        return None


def _issue(code: str, message: str, path: str) -> dict[str, str]:
    return {"code": code, "message": message, "path": path, "severity": "error"}


def _text(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _parse_node_ref(raw: Any, path: str, issues: list[dict[str, str]]) -> NodeRef | None:
    if not isinstance(raw, Mapping):
        issues.append(
            _issue("workflow.graph.node_ref_invalid", "Node reference must be an object", path)
        )
        return None
    kind = raw.get("kind")
    node_id = _text(raw.get("id"))
    if kind not in NODE_KINDS or node_id is None or set(raw) - {"kind", "id"}:
        issues.append(
            _issue(
                "workflow.graph.node_ref_invalid",
                "Node reference needs kind 'step', 'group' or 'for_each' and a non-empty id",
                path,
            )
        )
        return None
    return NodeRef(kind=str(kind), id=node_id)


_EXIT_OWNER_KEYS = {
    TARGET_KIND_GROUP_EXIT: "group_id",
    TARGET_KIND_FOR_EACH_EXIT: "for_each_id",
}


def _parse_target_ref(raw: Any, path: str, issues: list[dict[str, str]]) -> TargetRef | None:
    kind = raw.get("kind") if isinstance(raw, Mapping) else None
    owner_key = _EXIT_OWNER_KEYS.get(kind) if isinstance(kind, str) else None
    if owner_key is None:
        return _parse_node_ref(raw, path, issues)
    assert isinstance(raw, Mapping)
    owner_id = _text(raw.get(owner_key))
    lane_raw = raw.get("lane_id")
    lane_ok = lane_raw is None or _text(lane_raw) is not None
    if (
        owner_id is None
        or not lane_ok
        or "lane_id" not in raw
        or set(raw) - {"kind", owner_key, "lane_id"}
    ):
        issues.append(
            _issue(
                f"workflow.graph.{kind}_invalid",
                f"{kind} needs a {owner_key} and an explicit lane_id (string or null)",
                path,
            )
        )
        return None
    if kind == TARGET_KIND_GROUP_EXIT:
        return GroupExitRef(group_id=owner_id, lane_id=_text(lane_raw))
    return ForEachExitRef(for_each_id=owner_id, lane_id=_text(lane_raw))


def _parse_outcome(raw: Any, path: str, issues: list[dict[str, str]]) -> Outcome | None:
    if not isinstance(raw, Mapping) or raw.get("name") not in OUTCOME_NAMES:
        issues.append(
            _issue(
                "workflow.graph.outcome_invalid",
                "Outcome name must be one of " + ", ".join(sorted(OUTCOME_NAMES)),
                path,
            )
        )
        return None
    name = str(raw["name"])
    if set(raw) - {"name", "case_id"}:
        issues.append(_issue("workflow.graph.outcome_invalid", "Unknown outcome key", path))
        return None
    case_id = raw.get("case_id")
    if name == OUTCOME_CASE:
        if _text(case_id) is None:
            issues.append(
                _issue("workflow.graph.case_id_required", "A case outcome needs a case_id", path)
            )
            return None
        return Outcome(name=name, case_id=_text(case_id))
    if case_id is not None:
        issues.append(
            _issue(
                "workflow.graph.case_id_unexpected",
                f"Outcome '{name}' does not take a case_id",
                path,
            )
        )
        return None
    return Outcome(name=name)


def _parse_rework(
    raw: Mapping[str, Any], path: str, issues: list[dict[str, str]]
) -> tuple[bool, int | None] | None:
    rework = raw.get("rework", False)
    if not isinstance(rework, bool):
        issues.append(
            _issue("workflow.graph.rework_invalid", "rework must be a boolean", f"{path}.rework")
        )
        return None
    if "rework_limit" not in raw:
        return rework, None
    limit = raw.get("rework_limit")
    if not rework:
        issues.append(
            _issue(
                "workflow.graph.rework_limit_unexpected",
                "rework_limit is only allowed on a rework edge",
                f"{path}.rework_limit",
            )
        )
        return None
    if (
        isinstance(limit, bool)
        or not isinstance(limit, int)
        or not (REWORK_LIMIT_MIN <= limit <= REWORK_LIMIT_MAX)
    ):
        issues.append(
            _issue(
                "workflow.graph.rework_limit_invalid",
                f"rework_limit must be an integer from {REWORK_LIMIT_MIN} to {REWORK_LIMIT_MAX}",
                f"{path}.rework_limit",
            )
        )
        return None
    return rework, limit


def parse_execution_graph(
    raw: Any, *, path: str = "execution_graph"
) -> tuple[ExecutionGraph | None, list[dict[str, str]]]:
    """Parse the stored contract shape; return ``None`` with issues when invalid.

    This checks shape only. Whether the references, ports and scopes make an
    executable definition is the definition validator's job.
    """
    issues: list[dict[str, str]] = []
    if raw is None:
        return None, [_issue("workflow.graph.missing", "The workflow has no execution graph", path)]
    if not isinstance(raw, Mapping):
        return None, [_issue("workflow.graph.invalid", "execution_graph must be an object", path)]
    version = raw.get("schema_version")
    if isinstance(version, bool) or version != EXECUTION_GRAPH_SCHEMA_VERSION:
        return None, [
            _issue(
                "workflow.graph.schema_version_unsupported",
                f"execution_graph schema_version must be {EXECUTION_GRAPH_SCHEMA_VERSION}",
                f"{path}.schema_version",
            )
        ]
    issues.extend(
        _issue(
            "workflow.graph.key_unknown", f"Unknown execution_graph key '{key}'", f"{path}.{key}"
        )
        for key in sorted(set(raw) - _GRAPH_KEYS)
    )
    entry = (
        _parse_target_ref(raw.get("entry"), f"{path}.entry", issues)
        if raw.get("entry") is not None
        else None
    )
    if raw.get("entry") is None:
        issues.append(
            _issue(
                "workflow.graph.entry_missing", "The workflow has no entry node", f"{path}.entry"
            )
        )
    edges_raw = raw.get("edges", [])
    if not isinstance(edges_raw, list):
        issues.append(
            _issue("workflow.graph.edges_invalid", "edges must be a list", f"{path}.edges")
        )
        edges_raw = []
    edges: list[Edge] = []
    for index, edge_raw in enumerate(edges_raw):
        edge_path = f"{path}.edges[{index}]"
        if not isinstance(edge_raw, Mapping):
            issues.append(
                _issue("workflow.graph.edge_invalid", "Edge must be an object", edge_path)
            )
            continue
        unknown = sorted(set(edge_raw) - _EDGE_KEYS)
        if unknown:
            issues.extend(
                _issue(
                    "workflow.graph.key_unknown", f"Unknown edge key '{key}'", f"{edge_path}.{key}"
                )
                for key in unknown
            )
            continue
        source = _parse_node_ref(edge_raw.get("source"), f"{edge_path}.source", issues)
        outcome = _parse_outcome(edge_raw.get("outcome"), f"{edge_path}.outcome", issues)
        target = _parse_target_ref(edge_raw.get("target"), f"{edge_path}.target", issues)
        rework = _parse_rework(edge_raw, edge_path, issues)
        if source is None or outcome is None or target is None or rework is None:
            continue
        edges.append(
            Edge(
                source=source,
                outcome=outcome,
                target=target,
                rework=rework[0],
                rework_limit=rework[1],
            )
        )
    if issues or entry is None:
        return None, issues
    return ExecutionGraph(entry=entry, edges=tuple(edges)), []


Scope = tuple[str, str] | tuple[str, str, str | None]
ROOT_SCOPE: Scope = ("root", "")


SCOPE_KIND_GROUP = "group"
SCOPE_KIND_FOR_EACH = "for_each"


def group_scope(group_id: str, lane_id: str | None) -> Scope:
    return (SCOPE_KIND_GROUP, group_id, lane_id)


def for_each_scope(for_each_id: str, lane_id: str | None) -> Scope:
    return (SCOPE_KIND_FOR_EACH, for_each_id, lane_id)


def exit_scope(target: ExitRef) -> Scope:
    """The body or lane scope an exit belongs to."""
    if isinstance(target, ForEachExitRef):
        return for_each_scope(target.for_each_id, target.lane_id)
    return group_scope(target.group_id, target.lane_id)


def edge_identity(scope: Scope, edge: Edge) -> tuple[Any, ...]:
    """Stable identity of an edge for per-edge rework counters in a frozen graph.

    A source has at most one edge per outcome, so scope + source + outcome +
    case_id names the edge without depending on list order.
    """
    return (scope, edge.source.kind, edge.source.id, edge.outcome.name, edge.outcome.case_id)


def dump_execution_graph(entry: TargetRef, edges: Iterable[Edge]) -> dict[str, Any]:
    return ExecutionGraph(entry=entry, edges=tuple(edges)).dump()


__all__ = [
    "CONTAINER_NODE_KINDS",
    "EXECUTION_GRAPH_SCHEMA_VERSION",
    "EXECUTION_GRAPH_SETTING",
    "NODE_KIND_FOR_EACH",
    "NODE_KIND_GROUP",
    "NODE_KIND_STEP",
    "OUTCOME_APPROVED",
    "OUTCOME_CASE",
    "OUTCOME_DEFAULT",
    "OUTCOME_ERROR",
    "OUTCOME_FALSE",
    "OUTCOME_NAMES",
    "OUTCOME_REJECTED",
    "OUTCOME_SUCCESS",
    "OUTCOME_TRUE",
    "REWORK_LIMIT_MAX",
    "REWORK_LIMIT_MIN",
    "REWORK_SOURCE_OUTCOMES",
    "ROOT_SCOPE",
    "RUN_REWORK_TRANSITION_BUDGET",
    "SCOPE_KIND_FOR_EACH",
    "SCOPE_KIND_GROUP",
    "TARGET_KIND_FOR_EACH_EXIT",
    "TARGET_KIND_GROUP_EXIT",
    "Edge",
    "ExecutionGraph",
    "ExitRef",
    "ForEachExitRef",
    "GroupExitRef",
    "NodeRef",
    "Outcome",
    "Scope",
    "TargetRef",
    "dump_execution_graph",
    "edge_identity",
    "exit_scope",
    "for_each_scope",
    "group_scope",
    "parse_execution_graph",
]
