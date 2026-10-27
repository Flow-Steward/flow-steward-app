"""Pure traversal and summaries of lifecycle plan trees."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


class TrashLifecycleServiceTreeMixin:
    @staticmethod
    def _row_lifecycle_status(row: Mapping[str, Any]) -> str:
        return "trashed" if row.get("deleted_at") is not None else "active"

    def _collect_selectable_nodes(self, node: Mapping[str, Any]) -> list[dict[str, Any]]:
        nodes: list[dict[str, Any]] = []
        if bool(node.get("selectable")):
            nodes.append(
                {
                    "node_id": str(node.get("node_id") or ""),
                    "entity_type": str(node.get("entity_type") or ""),
                    "entity_id": str(node.get("entity_id") or ""),
                    "label": str(node.get("label") or ""),
                }
            )
        for child in node.get("children") or []:
            if isinstance(child, Mapping):
                nodes.extend(self._collect_selectable_nodes(child))
        return nodes

    def _summarize_tree(self, node: Mapping[str, Any]) -> dict[str, Any]:
        summary = {
            "total_nodes": 0,
            "selectable_nodes": 0,
            "selected_nodes": 0,
            "projects": 0,
            "workflows": 0,
            "datasets": 0,
            "connections": 0,
            "mcp_connectors": 0,
            "mcp_tools": 0,
        }
        self._accumulate_summary(summary, node)
        return summary

    def _accumulate_summary(self, summary: dict[str, Any], node: Mapping[str, Any]) -> None:
        summary["total_nodes"] += 1
        if bool(node.get("selectable")):
            summary["selectable_nodes"] += 1
        if bool(node.get("selected")):
            summary["selected_nodes"] += 1
        entity_type = str(node.get("entity_type") or "").strip()
        if entity_type == "project":
            summary["projects"] += 1
        elif entity_type in {"project_workflow", "registry_workflow"}:
            summary["workflows"] += 1
        elif entity_type == "project_dataset":
            summary["datasets"] += 1
        elif entity_type == "project_connection":
            summary["connections"] += 1
        elif entity_type == "project_mcp_connector":
            summary["mcp_connectors"] += 1
        elif entity_type == "project_mcp_tool":
            summary["mcp_tools"] += 1
        for child in node.get("children") or []:
            if isinstance(child, Mapping):
                self._accumulate_summary(summary, child)

    def _normalize_selected_node_ids(self, selection: Mapping[str, Any] | None) -> set[str]:
        if not isinstance(selection, Mapping):
            return set()
        raw_ids = selection.get("selected_node_ids")
        if not isinstance(raw_ids, list):
            return set()
        return {str(item).strip() for item in raw_ids if str(item).strip()}

    def _collect_selected_node_ids(self, node: Mapping[str, Any]) -> set[str]:
        selected: set[str] = set()
        if bool(node.get("selected")) and str(node.get("node_id") or "").strip():
            selected.add(str(node.get("node_id") or "").strip())
        for child in node.get("children") or []:
            if isinstance(child, Mapping):
                selected.update(self._collect_selected_node_ids(child))
        return selected

    def _collect_selectable_node_ids(self, node: Mapping[str, Any]) -> set[str]:
        selectable: set[str] = set()
        if bool(node.get("selectable")) and str(node.get("node_id") or "").strip():
            selectable.add(str(node.get("node_id") or "").strip())
        for child in node.get("children") or []:
            if isinstance(child, Mapping):
                selectable.update(self._collect_selectable_node_ids(child))
        return selectable

    def _collect_project_root_nodes(self, node: Mapping[str, Any]) -> list[Mapping[str, Any]]:
        project_nodes: list[Mapping[str, Any]] = []
        if str(node.get("entity_type") or "").strip() == "project":
            project_nodes.append(node)
        for child in node.get("children") or []:
            if isinstance(child, Mapping):
                project_nodes.extend(self._collect_project_root_nodes(child))
        return project_nodes

    @staticmethod
    def _is_project_workflow_node(node: Any) -> bool:
        return (
            isinstance(node, Mapping)
            and str(node.get("entity_type") or "").strip() == "project_workflow"
        )

    @staticmethod
    def _connection_node_is_available(node: Mapping[str, Any]) -> bool:
        metadata = node.get("metadata") if isinstance(node, Mapping) else {}
        if not isinstance(metadata, Mapping):
            return False
        if bool(metadata.get("lifecycle_deleted")):
            return False
        return bool(metadata.get("enabled", True))

    @staticmethod
    def _dataset_node_is_available(node: Mapping[str, Any]) -> bool:
        return str(node.get("status") or "").strip().lower() != "trashed"
