from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from core.application.workflows.capability_visibility import capability_visible_by_policy
from core.application.workflows.knowledge_projection_models import (
    WorkflowKnowledgeDocument,
    WorkflowKnowledgeEdge,
    WorkflowKnowledgeNode,
    WorkflowKnowledgeProjection,
)

_GRAPH_EDGE_KIND_MAP = {
    "seq": "flows_to",
    "if": "branches_if",
    "switch_case": "branches_switch_case",
    "switch_default": "branches_switch_default",
    "loop_back": "loops_to",
    "failure_path": "fails_to",
}
_NULL_LIKE_TEXT = frozenset({"none", "null", "undefined"})


def workflow_node_id(workflow_id: str) -> str:
    return f"workflow:{_text(workflow_id)}"


def group_node_id(workflow_id: str, group_id: str) -> str:
    return f"group:{_text(workflow_id)}:{_text(group_id)}"


def step_node_id(workflow_id: str, step_graph_id: str) -> str:
    return f"step:{_text(workflow_id)}:{_text(step_graph_id)}"


def reaction_node_id(workflow_id: str, reaction_key: str) -> str:
    return f"reaction:{_text(workflow_id)}:{_text(reaction_key)}"


def agent_node_id(agent_id: str) -> str:
    return f"agent:{_text(agent_id)}"


def skill_node_id(skill_id: str) -> str:
    return f"skill:{_text(skill_id)}"


def tool_node_id(tool_id: str) -> str:
    return f"tool:{_text(tool_id)}"


def tool_action_node_id(tool_id: str, operation_id: str) -> str:
    return f"tool_action:{_text(tool_id)}:{_text(operation_id)}"


def io_field_node_id(field_name: str) -> str:
    return f"io_field:{_text(field_name).lower()}"


def step_kind_node_id(step_kind: str) -> str:
    return f"step_kind:{_text(step_kind)}"


def agent_type_node_id(agent_type: str) -> str:
    return f"agent_type:{_text(agent_type)}"


def _visible_catalog_row(row: Mapping[str, Any]) -> bool:
    entity_id = _text(
        row.get("workflow_id")
        or row.get("agent_id")
        or row.get("skill_id")
        or row.get("tool_id")
        or row.get("id")
    )
    if not capability_visible_by_policy(
        entity_id,
        capability=row,
    ):
        return False
    workflow_id = _text(row.get("workflow_id"))
    if workflow_id and "step_count" in row:
        try:
            if int(row.get("step_count") or 0) <= 0:
                return False
        except (TypeError, ValueError):
            pass
    return True


def skill_category_node_id(category: str) -> str:
    return f"skill_category:{_text(category)}"


def tool_category_node_id(category: str) -> str:
    return f"tool_category:{_text(category)}"


def _text(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    return "" if text.lower() in _NULL_LIKE_TEXT else text


def _scope_text(scope: Mapping[str, Any] | None, key: str) -> str:
    if not isinstance(scope, Mapping):
        return ""
    return _text(scope.get(key))


def _summary_rows(
    db: Any,
    method_name: str,
    *,
    enabled_only: bool,
    account_id: str = "",
    tenant_id: str = "",
    project_id: str = "",
) -> list[Any]:
    method = getattr(db, method_name, None)
    if not callable(method):
        return []
    kwargs: dict[str, Any] = {"enabled_only": enabled_only}
    if account_id:
        kwargs["account_id"] = account_id
    if tenant_id:
        kwargs["tenant_id"] = tenant_id
    if project_id:
        kwargs["project_id"] = project_id
    try:
        return _as_list(method(**kwargs) or [])
    except TypeError:
        if "tenant_id" in kwargs:
            kwargs.pop("tenant_id", None)
            try:
                return _as_list(method(**kwargs) or [])
            except TypeError:
                pass
        return _as_list(method(enabled_only=enabled_only) or [])


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _as_list(value: Any) -> list[Any]:
    return list(value) if isinstance(value, list) else []


def _node_richness(node: WorkflowKnowledgeNode) -> tuple[int, int, int]:
    return (
        len(node.text),
        len(node.summary),
        len(node.metadata),
    )


def _document_richness(document: WorkflowKnowledgeDocument) -> tuple[int, int]:
    return (len(document.text), len(document.metadata))


class _ProjectionAccumulator:
    def __init__(self) -> None:
        self._nodes: dict[str, WorkflowKnowledgeNode] = {}
        self._edges: dict[str, WorkflowKnowledgeEdge] = {}
        self._documents: dict[str, WorkflowKnowledgeDocument] = {}

    def add_node(self, node: WorkflowKnowledgeNode) -> None:
        existing = self._nodes.get(node.id)
        if existing is None or _node_richness(node) >= _node_richness(existing):
            if existing is not None:
                merged_metadata = {**existing.metadata, **node.metadata}
                node = WorkflowKnowledgeNode(
                    id=node.id,
                    kind=node.kind,
                    label=node.label or existing.label,
                    summary=node.summary or existing.summary,
                    text=node.text or existing.text,
                    metadata=merged_metadata,
                )
            self._nodes[node.id] = node

    def add_edge(self, edge: WorkflowKnowledgeEdge) -> None:
        self._edges[edge.id] = edge

    def add_document(self, document: WorkflowKnowledgeDocument) -> None:
        existing = self._documents.get(document.id)
        if existing is None or _document_richness(document) >= _document_richness(existing):
            if existing is not None:
                document = WorkflowKnowledgeDocument(
                    id=document.id,
                    entity_id=document.entity_id,
                    entity_kind=document.entity_kind,
                    title=document.title or existing.title,
                    text=document.text or existing.text,
                    metadata={**existing.metadata, **document.metadata},
                )
            self._documents[document.id] = document

    def build(self) -> WorkflowKnowledgeProjection:
        return WorkflowKnowledgeProjection(
            nodes=list(self._nodes.values()),
            edges=list(self._edges.values()),
            documents=list(self._documents.values()),
        )
