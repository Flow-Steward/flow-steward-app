"""One canonical Template configuration in report mode, for builders and agents.

The step profile and the authoring schema show this example; a unit test keeps
it valid against the Template validator.
"""

from __future__ import annotations

from typing import Any

TEMPLATE_REPORT_EXAMPLE: dict[str, Any] = {
    "format": "report",
    "inputs": [
        {"name": "kpis", "type": "object", "required": True, "source": "state.outputs.kpis"},
        {
            "name": "regions",
            "type": "array",
            "required": True,
            "source": "state.outputs.regions",
        },
    ],
    "report": {
        "schema_version": 1,
        "title": "Weekly sales",
        "locale": "en-US",
        "time_zone": "Europe/Berlin",
        "datasets": [{"id": "regions", "source": {"input": "regions"}}],
        "components": [
            {
                "id": "revenue_total",
                "type": "metric",
                "label": "Revenue",
                "value": {"input": "kpis", "path": "revenue"},
                "format": {"type": "currency", "currency": "EUR"},
                "comparison": {
                    "label": "vs previous week",
                    "value": {"input": "kpis", "path": "revenue_change"},
                    "format": {"type": "percent", "maximum_fraction_digits": 1},
                },
            },
            {
                "id": "region_table",
                "type": "table",
                "caption": "Revenue by region",
                "dataset": "regions",
                "columns": [
                    {
                        "id": "region",
                        "field": "region",
                        "label": "Region",
                        "format": {"type": "text"},
                    },
                    {
                        "id": "revenue",
                        "field": "revenue",
                        "label": "Revenue",
                        "format": {"type": "currency", "currency": "EUR"},
                    },
                ],
                "empty_text": "No regional sales this week.",
            },
            {
                "id": "region_chart",
                "type": "chart",
                "chart_type": "bar",
                "title": "Revenue by region",
                "description": "Bar chart of weekly revenue in euros for each sales region.",
                "dataset": "regions",
                "category": {"field": "region", "label": "Region", "format": {"type": "text"}},
                "series": [
                    {
                        "id": "revenue",
                        "field": "revenue",
                        "label": "Revenue",
                        "format": {"type": "currency", "currency": "EUR"},
                    }
                ],
                "empty_text": "No regional sales this week.",
            },
        ],
    },
    "result_target": "state.outputs.sales_report",
}

__all__ = ["TEMPLATE_REPORT_EXAMPLE"]
