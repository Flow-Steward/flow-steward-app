"""Bounded project-detail projection for the public CLI."""

from __future__ import annotations

from collections.abc import Mapping
from math import ceil
from typing import Any


def _mapping(value: object) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    dump = getattr(value, "dump", None)
    if callable(dump):
        payload = dump()
        return dict(payload) if isinstance(payload, Mapping) else {}
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        payload = model_dump(mode="json")
        return dict(payload) if isinstance(payload, Mapping) else {}
    return {}


def _connection_summary(connection: Mapping[str, Any]) -> tuple[dict[str, Any], list[object]]:
    summary = dict(connection)
    schema = _mapping(summary.pop("schema_snapshot", {}))
    resources = schema.get("resources")
    resource_items = list(resources) if isinstance(resources, list) else []
    summary["schema_resource_count"] = len(resource_items)
    summary["schema_version"] = str(schema.get("schema_version") or "")
    summary["schema_source_kind"] = str(schema.get("source_kind") or "")
    return summary, resource_items


def summarize_data_connection_rows(rows: object) -> list[dict[str, Any]]:
    """Replace each connection's inlined schema_snapshot with summary counts.

    A full snapshot is unbounded — it grows with the source database — and
    inlining one per assigned connection made the project detail response
    hundreds of kilobytes of payload that no caller reads. Callers that need the
    schema itself fetch it from the data-connection endpoints.
    """
    if not isinstance(rows, list):
        return []
    return [_connection_summary(row)[0] for row in rows if isinstance(row, Mapping)]


def project_detail_cli_projection(
    detail: object,
    *,
    include_schema: bool = False,
    schema_page: int = 1,
    schema_per_page: int = 25,
) -> dict[str, Any]:
    """Return CLI-safe project detail with optional paginated assigned schemas."""
    projected = _mapping(detail)
    project = _mapping(projected.get("project"))
    assigned = project.get("assigned_data_connections")
    assigned_rows = list(assigned) if isinstance(assigned, list) else []
    available = project.pop("available_data_connections", [])
    project["available_data_connection_count"] = (
        len(available) if isinstance(available, list) else 0
    )

    summaries: list[dict[str, Any]] = []
    schema_items: list[dict[str, Any]] = []
    for raw_connection in assigned_rows:
        if not isinstance(raw_connection, Mapping):
            continue
        summary, resources = _connection_summary(raw_connection)
        summaries.append(summary)
        connection_id = str(summary.get("connection_id") or "")
        connection_name = str(summary.get("display_name") or connection_id)
        schema_items.extend(
            {
                "connection_id": connection_id,
                "connection_name": connection_name,
                "resource": resource,
            }
            for resource in resources
        )
    project["assigned_data_connections"] = summaries
    projected["project"] = project

    safe_page = max(1, int(schema_page or 1))
    safe_per_page = max(1, min(int(schema_per_page or 25), 200))
    total_items = len(schema_items)
    total_pages = max(1, ceil(total_items / safe_per_page))
    offset = (safe_page - 1) * safe_per_page
    projected["connection_schema_page"] = {
        "included": bool(include_schema),
        "items": schema_items[offset : offset + safe_per_page] if include_schema else [],
        "page": safe_page,
        "per_page": safe_per_page,
        "total_items": total_items,
        "total_pages": total_pages,
    }
    return projected
