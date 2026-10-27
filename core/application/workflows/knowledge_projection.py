from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from core.application.workflows.canvas_graph_service import build_workflow_canvas_graph
from core.application.workflows.knowledge_projection_builder import (
    _WorkflowKnowledgeProjectionBuilder,
)
from core.application.workflows.knowledge_projection_common import (
    _scope_text,
    _summary_rows,
    _text,
    _visible_catalog_row,
    agent_node_id,
    group_node_id,
    io_field_node_id,
    reaction_node_id,
    skill_node_id,
    step_node_id,
    tool_action_node_id,
    tool_node_id,
    workflow_node_id,
)
from core.application.workflows.knowledge_projection_models import (
    WorkflowKnowledgeBackend,
    WorkflowKnowledgeDocument,
    WorkflowKnowledgeEdge,
    WorkflowKnowledgeHit,
    WorkflowKnowledgeNeighbor,
    WorkflowKnowledgeNode,
    WorkflowKnowledgeProjection,
)


def build_workflow_knowledge_projection(
    db: Any,
    workflow_id: str,
    *,
    include_related_capabilities: bool = True,
) -> WorkflowKnowledgeProjection:
    workflow = db.get_workflow(workflow_id)
    if not isinstance(workflow, dict):
        raise LookupError(f"Workflow '{workflow_id}' not found")
    canvas_graph = _safe_canvas_graph(db, workflow_id, workflow)
    builder = _WorkflowKnowledgeProjectionBuilder(db)
    builder.add_workflow_projection(
        workflow=workflow,
        canvas_graph=canvas_graph,
        include_related_capabilities=include_related_capabilities,
    )
    return builder.build()


def build_workflow_catalog_projection(
    db: Any,
    *,
    workflow_ids: Sequence[str] | None = None,
    include_capability_catalog: bool = True,
    account_id: str | None = None,
    tenant_id: str | None = None,
    project_id: str | None = None,
    scope: Mapping[str, Any] | None = None,
) -> WorkflowKnowledgeProjection:
    normalized_account_id = _scope_text(scope, "account_id") or _text(account_id)
    normalized_tenant_id = _scope_text(scope, "tenant_id") or _text(tenant_id)
    normalized_project_id = _scope_text(scope, "project_id") or _text(project_id)
    builder = _WorkflowKnowledgeProjectionBuilder(
        db,
        account_id=normalized_account_id,
        tenant_id=normalized_tenant_id,
        project_id=normalized_project_id,
    )
    normalized_ids = [
        str(item or "").strip() for item in (workflow_ids or []) if str(item or "").strip()
    ]
    if not normalized_ids:
        rows = list(
            _summary_rows(
                db,
                "list_workflows_summary",
                enabled_only=False,
                account_id=normalized_account_id,
                tenant_id=normalized_tenant_id,
                project_id=normalized_project_id,
            )
        )
        normalized_ids = [
            str(row.get("workflow_id") or "").strip()
            for row in rows
            if isinstance(row, Mapping) and _visible_catalog_row(row)
        ]
    for workflow_id in normalized_ids:
        workflow = db.get_workflow(workflow_id)
        if not isinstance(workflow, dict):
            continue
        canvas_graph = _safe_canvas_graph(db, workflow_id, workflow)
        builder.add_workflow_projection(
            workflow=workflow,
            canvas_graph=canvas_graph,
            include_related_capabilities=True,
        )
    if include_capability_catalog:
        builder.add_capability_catalog()
    return builder.build()


def _safe_canvas_graph(db: Any, workflow_id: str, workflow: dict[str, Any]) -> dict[str, Any]:
    try:
        payload = build_workflow_canvas_graph(db, workflow_id)
        return dict(payload) if isinstance(payload, Mapping) else {}
    except Exception:
        return {
            "workflow": {
                "id": _text(workflow.get("workflow_id") or workflow_id) or workflow_id,
                "display_name": _text(workflow.get("display_name") or workflow_id) or workflow_id,
                "enabled": bool(workflow.get("enabled", False)),
                "callable_by_parent": False,
            },
            "interface_contract": {},
            "output_contract": {},
            "groups": [],
            "steps": [],
            "edges": [],
            "automation_rules": [],
        }


__all__ = [
    "WorkflowKnowledgeBackend",
    "WorkflowKnowledgeDocument",
    "WorkflowKnowledgeEdge",
    "WorkflowKnowledgeHit",
    "WorkflowKnowledgeNeighbor",
    "WorkflowKnowledgeNode",
    "WorkflowKnowledgeProjection",
    "agent_node_id",
    "build_workflow_catalog_projection",
    "build_workflow_knowledge_projection",
    "group_node_id",
    "io_field_node_id",
    "reaction_node_id",
    "skill_node_id",
    "step_node_id",
    "tool_action_node_id",
    "tool_node_id",
    "workflow_node_id",
]
