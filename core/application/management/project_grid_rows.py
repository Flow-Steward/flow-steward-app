"""Projection of a stored project row into the projects-grid payload."""

from __future__ import annotations

from typing import Any


def serialize_projects_grid_row(project: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(project.get("id") or "").strip(),
        "name": str(project.get("name") or "").strip(),
        "workspace_id": str(project.get("workspace_id") or "").strip(),
        "workspace_key": str(project.get("workspace_key") or "").strip(),
        "workspace_name": str(
            project.get("workspace_name")
            or project.get("workspace_display_name")
            or project.get("workspace_key")
            or ""
        ).strip(),
        "updated_at": str(project.get("updated_at") or "").strip(),
        "job_count": int(project.get("job_count") or 0),
        "operator_default_workflow_id": str(
            project.get("operator_default_workflow_id") or ""
        ).strip(),
        "workflow_ids": [
            workflow_id
            for value in project.get("workflow_ids") or []
            if (workflow_id := str(value or "").strip())
        ],
    }
