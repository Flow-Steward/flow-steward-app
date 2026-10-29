from core.application.management.project_grid_rows import serialize_projects_grid_row
from core.application.management.query_models_workflows import ProjectGridRowView


def test_grid_row_carries_the_workflows_the_project_lists() -> None:
    row = serialize_projects_grid_row(
        {"id": "project-1", "name": "Shop", "workflow_ids": ["orders", " ", None, "stock "]}
    )

    assert row["workflow_ids"] == ["orders", "stock"]
    assert ProjectGridRowView.model_validate(row).workflow_ids == ["orders", "stock"]


def test_grid_row_without_bindings_lists_no_workflows() -> None:
    assert serialize_projects_grid_row({"id": "project-1"})["workflow_ids"] == []
