# mypy: ignore-errors
"""Value, connection-row and grid helpers for platform extension invocations."""

from __future__ import annotations

from typing import Any

from .extension_runtime_validation import _as_dict, _as_list


def _runtime_logger(runtime_context: dict | None = None):
    context = _as_dict(runtime_context)
    return context.get("logger")


def _string_or(value: Any, fallback: str = "") -> str:
    return str(value or fallback).strip()


def _coerce_entity_id(value: Any) -> str | None:
    normalized = str(value or "").strip()
    return normalized or None


def _deep_merge_dict(target: dict[str, Any], source: dict[str, Any]) -> dict[str, Any]:
    for key, value in source.items():
        if isinstance(value, dict) and isinstance(target.get(key), dict):
            _deep_merge_dict(target[key], value)
        else:
            target[key] = value
    return target


def _expand_dotted_object(payload: dict[str, Any]) -> dict[str, Any]:
    normalized: dict[str, Any] = {}
    for raw_key, raw_value in payload.items():
        key = str(raw_key or "").strip()
        if not key or key == "_meta":
            continue
        if isinstance(raw_value, dict):
            value = _expand_dotted_object(raw_value)
        elif isinstance(raw_value, list):
            value = [
                _expand_dotted_object(item) if isinstance(item, dict) else item
                for item in raw_value
            ]
        else:
            value = raw_value
        if "." not in key:
            existing = normalized.get(key)
            if isinstance(existing, dict) and isinstance(value, dict):
                normalized[key] = _deep_merge_dict(existing, value)
            else:
                normalized[key] = value
            continue
        cursor = normalized
        parts = [segment for segment in key.split(".") if segment]
        for part in parts[:-1]:
            next_value = cursor.get(part)
            if not isinstance(next_value, dict):
                next_value = {}
                cursor[part] = next_value
            cursor = next_value
        leaf = parts[-1]
        existing = cursor.get(leaf)
        if isinstance(existing, dict) and isinstance(value, dict):
            cursor[leaf] = _deep_merge_dict(existing, value)
        else:
            cursor[leaf] = value
    return normalized


def _nested_set(payload: dict[str, Any], path: str, value: Any) -> None:
    parts = [segment for segment in str(path or "").split(".") if segment]
    if not parts:
        return
    cursor = payload
    for part in parts[:-1]:
        next_value = cursor.get(part)
        if not isinstance(next_value, dict):
            next_value = {}
            cursor[part] = next_value
        cursor = next_value
    cursor[parts[-1]] = value


def _nested_get(payload: dict[str, Any], path: str, default: Any = None) -> Any:
    parts = [segment for segment in str(path or "").split(".") if segment]
    if not parts:
        return default
    cursor: Any = payload
    for part in parts:
        if not isinstance(cursor, dict) or part not in cursor:
            return default
        cursor = cursor.get(part)
    return cursor


def _project_connection_row_for_extension(
    bl: Any, *, project_id: str, extension_id: str, extension_install_id: str = ""
) -> dict[str, Any]:
    if bl is None:
        return {}
    getter = getattr(bl, "get_project_connection", None)
    if callable(getter):
        try:
            row = getter(
                project_id=str(project_id),
                extension_id=str(extension_id),
                extension_install_id=str(extension_install_id),
                enabled_only=False,
            )
        except Exception:
            row = None
        if isinstance(row, dict):
            return dict(row)
    lister = getattr(bl, "list_project_connections", None)
    if callable(lister):
        try:
            rows = lister(str(project_id), enabled_only=False) or []
        except Exception:
            rows = []
        for row in rows:
            payload = _as_dict(row)
            if _string_or(payload.get("extension_id")).lower() != str(extension_id).lower():
                continue
            if extension_install_id and _string_or(payload.get("extension_install_id")) != str(
                extension_install_id
            ):
                continue
            if payload:
                return dict(payload)
    return {}


def _save_project_connection_config(
    bl: Any,
    *,
    project_id: str,
    extension_id: str,
    connection_config: dict[str, Any],
    extension_install_id: str = "",
    connection_type: str = "",
    account_id: str = "",
    runtime_context: dict | None = None,
) -> bool:
    operation_runner = _as_dict(runtime_context).get("platform_connection_operation")
    if bl is None or not callable(operation_runner):
        return False
    result = operation_runner(
        "config_save",
        store=bl,
        project_id=str(project_id),
        extension_id=str(extension_id),
        extension_install_id=str(extension_install_id),
        account_id=str(account_id),
        connection_type=str(connection_type),
        connection_config=dict(connection_config),
    )
    return bool(_as_dict(result).get("ok"))


def _coerce_positive_int(value: Any) -> int | None:
    try:
        normalized = int(value)
    except (TypeError, ValueError):
        normalized = 0
    return normalized if normalized > 0 else None


def _coerce_grid_pagination(params: dict | None = None) -> tuple[int, int]:
    payload = _as_dict(params)
    page = _coerce_positive_int(payload.get("page")) or 1
    page_size = _coerce_positive_int(payload.get("page_size")) or 25
    page_size = max(1, min(page_size, 500))
    return page, page_size


def _safe_iso_datetime(value: Any) -> str:
    if value is None:
        return ""
    if hasattr(value, "isoformat"):
        try:
            return str(value.isoformat())
        except Exception:
            return str(value)
    return str(value)


def _as_text_set(value: Any) -> set[str]:
    if isinstance(value, list):
        return {str(item or "").strip().lower() for item in value if str(item or "").strip()}
    if isinstance(value, tuple):
        return {str(item or "").strip().lower() for item in value if str(item or "").strip()}
    if value in (None, "", {}):
        return set()
    return {str(value).strip().lower()}


def _matches_grid_filter(item: dict, filter_key: str, expected: Any) -> bool:
    expected_set = _as_text_set(expected)
    if not expected_set:
        return True
    actual = str(item.get(filter_key) or "").strip().lower()
    return actual in expected_set


def _apply_grid_params(
    items: list[dict], params: dict | None = None
) -> tuple[list[dict], int, int, int]:
    payload = _as_dict(params)
    page, page_size = _coerce_grid_pagination(payload)
    search = str(payload.get("search") or "").strip().lower()
    filters = _as_dict(payload.get("filters"))
    sort = _as_list(payload.get("sort"))

    filtered: list[dict] = []
    for row in items:
        item = _as_dict(row)
        if not item:
            continue
        if search:
            haystack = " ".join(
                str(item.get(key) or "")
                for key in (
                    "delivery_id",
                    "route_key",
                    "event_type",
                    "severity",
                    "status",
                    "provider_message_id",
                    "error_text",
                    "workflow_id",
                    "summary",
                )
            ).lower()
            if search not in haystack:
                continue
        include = True
        for filter_key, expected in filters.items():
            if not _matches_grid_filter(item, str(filter_key or "").strip(), expected):
                include = False
                break
        if include:
            filtered.append(item)

    if sort:
        sort_rule = _as_dict(sort[0])
        field = str(sort_rule.get("field") or "").strip()
        direction = str(sort_rule.get("direction") or "asc").strip().lower()
        reverse = direction == "desc"
        if field:
            filtered.sort(
                key=lambda row: str(_as_dict(row).get(field) or "").lower(),
                reverse=reverse,
            )

    total = len(filtered)
    start = (page - 1) * page_size
    end = start + page_size
    return filtered[start:end], page, page_size, total


__all__ = [
    "_apply_grid_params",
    "_as_text_set",
    "_coerce_entity_id",
    "_coerce_grid_pagination",
    "_coerce_positive_int",
    "_deep_merge_dict",
    "_expand_dotted_object",
    "_matches_grid_filter",
    "_nested_get",
    "_nested_set",
    "_project_connection_row_for_extension",
    "_runtime_logger",
    "_safe_iso_datetime",
    "_save_project_connection_config",
    "_string_or",
]
