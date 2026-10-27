# mypy: ignore-errors
"""Catalog helpers for workflow-owned trigger definitions."""

from __future__ import annotations

from typing import Any

from core.infrastructure.extension_runtime.extension_runtime import (
    normalize_extension_row_for_runtime,
)

BUILTIN_WORKFLOW_TRIGGER_ID = "incoming-webhook-post"


def _as_dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _as_list(value: Any) -> list[Any]:
    return list(value) if isinstance(value, list) else []


def _text(value: Any) -> str:
    return str(value or "").strip()


def builtin_workflow_trigger_catalog_entry() -> dict[str, Any]:
    return {
        "trigger_family": "incoming_webhook",
        "catalog_id": BUILTIN_WORKFLOW_TRIGGER_ID,
        "trigger_type_id": BUILTIN_WORKFLOW_TRIGGER_ID,
        "provider_key": "",
        "display_name": "Incoming Webhook/Post",
        "description": "Public POST endpoint that maps external requests into workflow inputs.",
        "config_schema": {},
        "ui_schema": {},
        "extension_id": None,
        "extension_kind": None,
    }


def normalize_workflow_trigger_definitions(
    raw_value: Any,
    *,
    extension_id: str = "",
    extension_kind: str = "",
) -> list[dict[str, Any]]:
    definitions: list[dict[str, Any]] = []
    for idx, raw_item in enumerate(_as_list(raw_value)):
        item = _as_dict(raw_item)
        trigger_type_id = _text(item.get("trigger_type_id") or item.get("id"))
        provider_key = _text(item.get("provider_key")).lower()
        display_name = _text(item.get("display_name") or item.get("title"))
        description = _text(item.get("description"))
        if not trigger_type_id or not provider_key or not display_name:
            continue
        config_schema = _as_dict(item.get("config_schema"))
        ui_schema = _as_dict(item.get("ui_schema"))
        definitions.append(
            {
                "trigger_family": "provider",
                "catalog_id": f"{provider_key}:{trigger_type_id}",
                "trigger_type_id": trigger_type_id,
                "provider_key": provider_key,
                "display_name": display_name,
                "description": description,
                "config_schema": config_schema,
                "ui_schema": ui_schema,
                "event_name": _text(item.get("event_name") or item.get("event")),
                "event_metadata": _as_dict(item.get("event_metadata")),
                "provider_ingress_url_required": bool(item.get("provider_ingress_url_required")),
                "extension_id": extension_id or None,
                "extension_kind": extension_kind or None,
                "definition_index": idx,
            }
        )
    return definitions


def list_workflow_trigger_catalog(bl) -> list[dict[str, Any]]:
    catalog: list[dict[str, Any]] = [builtin_workflow_trigger_catalog_entry()]
    if bl is None or not hasattr(bl, "list_extensions"):
        return catalog
    try:
        rows = bl.list_extensions(enabled_only=True) or []
    except Exception:
        rows = []
    for row in rows:
        extension = normalize_extension_row_for_runtime(dict(row or {}))
        extension_id = _text(extension.get("extension_key") or extension.get("extension_id"))
        extension_kind = _text(
            extension.get("kind") or extension.get("compiled_manifest", {}).get("kind")
        )
        compiled_manifest = _as_dict(extension.get("compiled_manifest"))
        catalog.extend(
            normalize_workflow_trigger_definitions(
                compiled_manifest.get("workflow_triggers"),
                extension_id=extension_id,
                extension_kind=extension_kind,
            )
        )
    return catalog


def find_provider_trigger_definition(
    bl,
    *,
    provider_key: str,
    trigger_type_id: str,
) -> dict[str, Any]:
    normalized_provider_key = _text(provider_key).lower()
    normalized_trigger_type_id = _text(trigger_type_id)
    for item in list_workflow_trigger_catalog(bl):
        if item.get("trigger_family") != "provider":
            continue
        if _text(item.get("provider_key")).lower() != normalized_provider_key:
            continue
        if _text(item.get("trigger_type_id")) != normalized_trigger_type_id:
            continue
        return dict(item)
    return {}


def validate_provider_trigger_config(
    definition: dict[str, Any] | None,
    trigger_config: dict[str, Any] | None,
    *,
    enabled: bool,
) -> tuple[str, list[dict[str, Any]], str]:
    config = _as_dict(trigger_config)
    catalog_definition = _as_dict(definition)
    config_schema = _as_dict(catalog_definition.get("config_schema"))
    required = [_text(item) for item in _as_list(config_schema.get("required")) if _text(item)]
    errors: list[dict[str, Any]] = []
    for field_name in required:
        value = config.get(field_name)
        empty = value is None or (isinstance(value, str) and not _text(value))
        if empty:
            errors.append(
                {
                    "code": "required",
                    "field": field_name,
                    "message": f"'{field_name}' is required.",
                }
            )
    event_name = _text(
        config.get("event_name") or config.get("event") or catalog_definition.get("event_name")
    )
    readiness_status = "ready" if enabled and not errors else "draft"
    if enabled and errors:
        readiness_status = "invalid"
    return readiness_status, errors, event_name
