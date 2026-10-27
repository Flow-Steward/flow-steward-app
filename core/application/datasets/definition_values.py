"""Pure normalization for dataset definitions and connection values."""

from __future__ import annotations

from typing import Any

_SECRET_REFS_STRUCTURAL_KEYS = {"refs", "values", "env"}


def _normalize_project_ids(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    project_ids: list[str] = []
    for item in value:
        parsed = str(item or "").strip()
        if parsed and parsed not in project_ids:
            project_ids.append(parsed)
    return project_ids


def _normalize_tags(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    tags: list[str] = []
    for item in value:
        parsed = str(item or "").strip()
        if parsed and parsed not in tags:
            tags.append(parsed)
    return tags


def _non_empty_secret_values(secret_refs: dict) -> dict[str, str]:
    values: dict[str, str] = {}
    raw_values = secret_refs.get("values")
    if isinstance(raw_values, dict):
        for name, value in raw_values.items():
            normalized_name = str(name or "").strip()
            normalized_value = str(value or "").strip() if value is not None else ""
            if normalized_name and normalized_value:
                values[normalized_name] = normalized_value
    for name, value in secret_refs.items():
        normalized_name = str(name or "").strip()
        if normalized_name in _SECRET_REFS_STRUCTURAL_KEYS:
            continue
        normalized_value = str(value or "").strip() if value is not None else ""
        if normalized_name and normalized_value:
            values[normalized_name] = normalized_value
    return values


def _dataset_connection_id(dataset: dict[str, Any]) -> str:
    metadata = dataset.get("metadata_jsonb") or dataset.get("metadata")
    metadata = metadata if isinstance(metadata, dict) else {}
    return str(dataset.get("connection_id") or metadata.get("connection_id") or "").strip()


def _apply_schema_defaults(schema: dict, value: dict) -> dict:
    """Apply manifest-declared object defaults before validating a connection."""
    normalized = dict(value)
    properties = schema.get("properties")
    if not isinstance(properties, dict):
        return normalized
    for key, property_schema in properties.items():
        if not isinstance(property_schema, dict):
            continue
        if key not in normalized and "default" in property_schema:
            normalized[key] = property_schema["default"]
        if isinstance(normalized.get(key), dict):
            normalized[key] = _apply_schema_defaults(property_schema, normalized[key])
    return normalized
