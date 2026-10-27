from __future__ import annotations

from core.application.management.project_cli_projection import project_detail_cli_projection
from core.application.management.query_services_project_core_mixin import (
    ProjectQueryServiceCoreMixin,
)


def _detail() -> dict[str, object]:
    return {
        "project": {
            "id": "proj-1",
            "name": "Demo",
            "data_connection_ids": ["conn-1"],
            "assigned_data_connections": [
                {
                    "connection_id": "conn-1",
                    "display_name": "Warehouse",
                    "schema_snapshot": {
                        "schema_version": "1",
                        "source_kind": "postgres",
                        "resources": [
                            {"resource_id": "table-1", "columns": [{"name": "a"}]},
                            {"resource_id": "table-2", "columns": [{"name": "b"}]},
                            {"resource_id": "table-3", "columns": [{"name": "c"}]},
                        ],
                    },
                }
            ],
            "available_data_connections": [
                {
                    "connection_id": "conn-2",
                    "display_name": "Unassigned",
                    "schema_snapshot": {"resources": [{"resource_id": "must-not-leak"}]},
                }
            ],
        },
        "jobs": [],
    }


def test_project_detail_cli_projection_omits_schema_bodies_by_default() -> None:
    projected = project_detail_cli_projection(_detail())

    project = projected["project"]
    assigned = project["assigned_data_connections"]
    assert assigned == [
        {
            "connection_id": "conn-1",
            "display_name": "Warehouse",
            "schema_resource_count": 3,
            "schema_version": "1",
            "schema_source_kind": "postgres",
        }
    ]
    assert "available_data_connections" not in project
    assert project["available_data_connection_count"] == 1
    assert projected["connection_schema_page"] == {
        "included": False,
        "items": [],
        "page": 1,
        "per_page": 25,
        "total_items": 3,
        "total_pages": 1,
    }


def test_project_detail_cli_projection_paginates_explicit_schema_resources() -> None:
    projected = project_detail_cli_projection(
        _detail(), include_schema=True, schema_page=2, schema_per_page=2
    )

    page = projected["connection_schema_page"]
    assert page["included"] is True
    assert page["page"] == 2
    assert page["per_page"] == 2
    assert page["total_items"] == 3
    assert page["total_pages"] == 2
    assert page["items"] == [
        {
            "connection_id": "conn-1",
            "connection_name": "Warehouse",
            "resource": {"resource_id": "table-3", "columns": [{"name": "c"}]},
        }
    ]
    assert "must-not-leak" not in str(projected)


def test_workspace_connection_options_omit_large_schema_snapshots() -> None:
    class _Store:
        def list_workspace_project_data_connection_options(self, _workspace_id: str):
            return [
                {
                    "connection_id": "conn-1",
                    "display_name": "Warehouse",
                    "schema_snapshot": {"resources": [{"name": "huge"}]},
                }
            ]

    rows = ProjectQueryServiceCoreMixin._workspace_data_connection_rows(_Store(), "ws-1")

    assert rows == [{"connection_id": "conn-1", "display_name": "Warehouse"}]


def test_summarize_data_connection_rows_drops_the_inlined_schema_snapshot():
    """B18 regression.

    GET /api/projects/{id} inlined a full schema_snapshot per assigned data
    connection. A snapshot grows with the source database, so one real project
    returned 298KB, 292KB of it snapshots that no caller reads. The summary must
    keep the fields the project edit page renders and replace the snapshot with
    counts.
    """
    from core.application.management.project_cli_projection import (
        summarize_data_connection_rows,
    )

    rows = summarize_data_connection_rows(
        [
            {
                "connection_id": "conn-1",
                "display_name": "Warehouse",
                "health_status": "healthy",
                "schema_snapshot": {
                    "schema_version": "3",
                    "source_kind": "postgres",
                    "resources": [{"name": f"table_{index}"} for index in range(139)],
                },
            }
        ]
    )

    assert len(rows) == 1
    row = rows[0]
    assert "schema_snapshot" not in row, "the unbounded snapshot is still inlined"
    # Fields the project edit page renders must survive.
    assert row["connection_id"] == "conn-1"
    assert row["display_name"] == "Warehouse"
    assert row["health_status"] == "healthy"
    # The information the snapshot carried is preserved as bounded summary data.
    assert row["schema_resource_count"] == 139
    assert row["schema_version"] == "3"
    assert row["schema_source_kind"] == "postgres"


def test_summarize_data_connection_rows_tolerates_non_list_input():
    from core.application.management.project_cli_projection import (
        summarize_data_connection_rows,
    )

    assert summarize_data_connection_rows(None) == []
    assert summarize_data_connection_rows({"not": "a list"}) == []
    assert summarize_data_connection_rows(["not a mapping"]) == []
