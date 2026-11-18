"""A section's history table: the configured values of one page of jobs (ADR 0075).

Values come from what each job stored: the accepted inputs, the final outputs of
a successful run and the author recorded at launch. A value the job does not have
is left out, not invented: no default, no type coercion, no current-contract guess.
"""

from __future__ import annotations

from typing import Any

from core.application.projects.user_sections import ProjectUserSection, stored_sections

#: A run's outputs are its result only once it succeeded; a failed or running job
#: shows its status and safe error instead of a partial result.
SUCCESS_STATUSES = frozenset({"completed", "succeeded", "success"})
_SECRET_KEYS = frozenset({"secret_ref", "secret_id", "$secret", "$secret_ref", "catalog_ref"})
_FILE_KEYS = ("filename", "content_type", "mime_type", "size_bytes", "artifact_id")
_JOB_VALUES = {
    "job_id": "job_id",
    "status": "status",
    "created_at": "queued_at",
    "started_at": "started_at",
    "finished_at": "finished_at",
    "duration_ms": "duration_ms",
}


def find_section(
    config: Any, section_id: str, *, include_hidden: bool
) -> ProjectUserSection | None:
    for section in stored_sections(config):
        if section.id == section_id and (section.visible or include_hidden):
            return section
    return None


def _public(value: Any) -> Any:
    """A stored value as the table may show it: file handles summarized, secrets masked."""
    if isinstance(value, dict):
        if _SECRET_KEYS.intersection(value):
            return "[secret]"
        if (
            "artifact_id" in value
            or "storage_ref" in value
            or "object_key" in value
            or ("filename" in value and "sha256" in value)
        ):
            return {key: value[key] for key in _FILE_KEYS if key in value}
        return {key: _public(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_public(item) for item in value]
    return value


_MISSING = object()


def _at_path(data: Any, path: str) -> Any:
    """The value at `a.b` or `items[].sku`; a list step collects every item's value."""
    segments = path.split(".")
    node: Any = data
    for index, segment in enumerate(segments):
        name, into_items = (segment[:-2], True) if segment.endswith("[]") else (segment, False)
        if not isinstance(node, dict) or name not in node:
            return _MISSING
        node = node[name]
        if into_items:
            if not isinstance(node, list):
                return _MISSING
            rest = ".".join(segments[index + 1 :])
            if not rest:
                return node
            values = [_at_path(item, rest) for item in node]
            return [value for value in values if value is not _MISSING]
    return node


def section_row_values(
    section: ProjectUserSection, row: dict[str, Any], payload: dict[str, Any] | None
) -> dict[str, Any]:
    """Column id -> value for one job; absent values are left out."""
    stored = payload or {}
    succeeded = str(row.get("status") or "").strip().lower() in SUCCESS_STATUSES
    values: dict[str, Any] = {}
    for column in section.columns:
        if column.source == "job":
            if column.path == "submitted_by":
                value = stored.get("submitted_by") or _MISSING
            else:
                value = row.get(_JOB_VALUES.get(column.path, ""), _MISSING)
        elif column.source == "input":
            value = _at_path(stored.get("inputs") or {}, column.path)
        elif succeeded:
            outputs = stored.get("outputs") or {}
            value = outputs.get(column.path, _MISSING)
        else:
            value = _MISSING
        if value is not _MISSING and value is not None:
            values[column.id] = _public(value)
    return values


__all__ = ["SUCCESS_STATUSES", "find_section", "section_row_values"]
