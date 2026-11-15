"""Grid links and grid filters on extension pages: the compiler's rules."""

from __future__ import annotations

from typing import Any

import pytest

from core.application.extensions.grid_link_contract import validate_grid_link_contract


def _grid(**fields: Any) -> dict[str, Any]:
    return {
        "component_id": "runs",
        "type": "grid",
        "query_id": "runs.list",
        "columns": [
            {
                "key": "name",
                "label": "Run",
                "type": "link",
                "link": {"page": "run_detail", "params": {"run_id": "{row.run_id}"}},
            },
            {"key": "status", "label": "Status", "type": "badge"},
        ],
        "row_fields": ["run_id", "job_path"],
        **fields,
    }


def _manifest(grid: dict[str, Any]) -> dict[str, Any]:
    return {
        "query_definitions": [
            {"query_id": "runs.list"},
            {"query_id": "statuses.list"},
            {"query_id": "remote.list", "connection": {"connection_type_ids": ["profile"]}},
        ],
        "ui_manifest": {
            "pages": [
                {"page_id": "runs", "components": [{"type": "section", "components": [grid]}]},
                {"page_id": "run_detail", "components": []},
            ]
        },
    }


def _codes(grid: dict[str, Any]) -> list[str]:
    issues: list[dict] = []
    validate_grid_link_contract(_manifest(grid), issues=issues, manifest_path="extension.yaml")
    return [issue["code"] for issue in issues]


def test_a_valid_grid_with_links_and_filters_passes() -> None:
    grid = _grid(
        row_link={"path_field": "job_path"},
        filters=[
            {"param": "status", "label": "Status", "options": [{"value": "open", "label": "Open"}]},
            {
                "param": "kind",
                "type": "select",
                "options_query": {
                    "query_id": "statuses.list",
                    "value_field": "id",
                    "label_field": "name",
                },
            },
            {"param": "q", "label": "Search", "type": "text", "placeholder": "Name"},
        ],
    )

    assert _codes(grid) == []


@pytest.mark.parametrize(
    ("link", "code"),
    [
        ({"page": "missing"}, "grid_link_page_unknown"),
        ({"page": "run_detail", "params": {"run_id": "{row.unknown}"}}, "grid_link_field_unknown"),
        ({"page": "run_detail", "params": {"Run": "{row.run_id}"}}, "grid_link_params_invalid"),
        ({"page": "run_detail", "params": {"run_id": "{other}"}}, "grid_link_params_invalid"),
        ({"page": "run_detail", "path_field": "job_path"}, "grid_link_invalid"),
        ({"url": "https://example.test"}, "grid_link_invalid"),
        ({"path_field": "nope"}, "grid_link_field_unknown"),
    ],
)
def test_a_link_the_host_cannot_honour_is_refused(link: dict[str, Any], code: str) -> None:
    assert code in _codes(_grid(row_link=link))


def test_a_column_link_needs_the_link_type() -> None:
    grid = _grid()
    grid["columns"][1]["link"] = {"path_field": "job_path"}

    assert _codes(grid) == ["grid_link_invalid"]


@pytest.mark.parametrize(
    ("filters", "code"),
    [
        ([{"param": "Status", "options": [{"value": "a", "label": "A"}]}], "grid_filter_invalid"),
        ([{"param": "s", "options": []}], "grid_filter_invalid"),
        ([{"param": "s"}], "grid_filter_invalid"),
        (
            [{"param": "s", "type": "text", "options": [{"value": "a", "label": "A"}]}],
            "grid_filter_invalid",
        ),
        (
            [
                {"param": "s", "type": "text"},
                {"param": "s", "type": "text"},
            ],
            "grid_filter_param_duplicate",
        ),
        (
            [{"param": "s", "options_query": {"query_id": "nope", "value_field": "id"}}],
            "grid_filter_query_unknown",
        ),
        (
            [{"param": "s", "options_query": {"query_id": "remote.list", "value_field": "id"}}],
            "grid_filter_query_connection",
        ),
    ],
)
def test_a_malformed_filter_is_refused(filters: list[dict[str, Any]], code: str) -> None:
    assert code in _codes(_grid(filters=filters))
