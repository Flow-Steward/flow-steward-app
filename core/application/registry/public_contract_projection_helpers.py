from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime
from typing import Any

logger = logging.getLogger(__name__)

PRIMITIVE_VALUE_TYPES = {"text", "json", "list", "number", "boolean"}
STRUCTURED_VALUE_TYPES = {"json", "list"}


def _normalize_value_type(raw_type: Any) -> str:
    value = str(raw_type or "").strip().lower()
    if value in PRIMITIVE_VALUE_TYPES:
        return value
    if value in {"string", "str"}:
        return "text"
    if value in {"object", "dict", "map"}:
        return "json"
    if value in {"array", "tuple", "set"}:
        return "list"
    if value in {"int", "float", "decimal"}:
        return "number"
    if value in {"integer", "long", "double"}:
        return "number"
    if value in {"bool"}:
        return "boolean"
    return "text"


def _default_schema_for_value_type(value_type: str) -> dict[str, Any]:
    if value_type == "list":
        return {"type": "array", "items": {}}
    return {"type": "object", "additionalProperties": True}


def _humanize_field_key(field_key: str) -> str:
    text = str(field_key or "").strip().replace("_", " ").replace(".", " ").replace("-", " ")
    parts = [part for part in text.split() if part]
    if not parts:
        return str(field_key or "").strip()
    return " ".join(part.capitalize() for part in parts)


def _stable_hash(payload: Any) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:16]


def _list_workflows_safe(db) -> list[dict]:
    list_fn = getattr(db, "list_workflows", None)
    if not callable(list_fn):
        return []
    try:
        return list(list_fn(enabled_only=False) or [])
    except TypeError:
        return list(list_fn(enabled_only=False) or [])


def _to_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            return {}
        try:
            parsed = json.loads(raw)
        except Exception:
            return {}
        if isinstance(parsed, dict):
            return parsed
    return {}


def _normalize_public_contract_items(
    raw_items: Any, *, default_required: bool
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if not isinstance(raw_items, list):
        return rows
    for item in raw_items:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or item.get("field_key") or "").strip()
        if not name:
            continue
        rows.append(
            {
                "name": name,
                "display_name": str(item.get("display_name") or item.get("label") or "").strip(),
                "type": _normalize_value_type(item.get("type")),
                "required": bool(item.get("required", default_required)),
                "description": str(item.get("description") or "").strip(),
            }
        )
    return rows


def _extract_workflow_callable_contract(
    settings: Any,
) -> tuple[bool, list[dict[str, Any]], list[dict[str, Any]]]:
    interface = _extract_workflow_callable_interface(settings)

    return (
        bool(interface.get("enabled")),
        _normalize_public_contract_items(interface.get("expected_inputs"), default_required=False),
        _normalize_public_contract_items(interface.get("expected_outputs"), default_required=True),
    )


def _extract_workflow_callable_interface(settings: Any, *, steps: Any = None) -> dict[str, Any]:
    from core.application.registry.workflow_contract import WorkflowInterface

    return WorkflowInterface.from_settings(settings, steps=steps).dump()


def _tool_is_connector_candidate(tool: dict[str, Any], interface: dict[str, Any]) -> bool:
    _ = interface
    tags = {
        str(tag or "").strip().lower() for tag in (tool.get("tags") or []) if str(tag or "").strip()
    }
    return bool(tags & {"connector", "integration", "external", "provider"})


def _extract_connector_projection_spec(tool: dict[str, Any]) -> dict[str, Any] | None:
    tool_id = str(tool.get("tool_id") or "").strip()
    if not tool_id:
        return None
    interface = _to_object(tool.get("interface"))
    if not interface or not _tool_is_connector_candidate(tool, interface):
        return None

    parameters = interface.get("parameters")
    returns = interface.get("returns")
    functions = interface.get("functions") or []

    if not isinstance(parameters, list) and len(functions) == 1 and isinstance(functions[0], dict):
        parameters = functions[0].get("parameters")
        if not returns:
            returns = functions[0].get("returns")
    elif len(functions) > 1 and not isinstance(parameters, list):
        # Multi-operation tool interfaces need a separate block-per-operation model.
        logger.info(
            "Skipping multi-function connector projection for tool '%s' in this slice", tool_id
        )
        return None

    normalized_inputs = _normalize_public_contract_items(parameters, default_required=False)
    normalized_outputs: list[dict[str, Any]] = []
    if isinstance(returns, dict):
        output_name = str(returns.get("name") or "").strip()
        if output_name:
            normalized_outputs = _normalize_public_contract_items([returns], default_required=True)

    if not normalized_inputs and not normalized_outputs:
        return None

    return {
        "tool_id": tool_id,
        "display_name": str(tool.get("display_name") or tool_id).strip(),
        "description": str(tool.get("description") or "").strip(),
        "family": str(tool.get("category") or "").strip(),
        "enabled": bool(tool.get("enabled", True)),
        "inputs": normalized_inputs,
        "outputs": normalized_outputs,
    }


def _register_public_field_source(
    desired_fields: dict[str, dict[str, Any]],
    desired_contracts: dict[str, dict[str, Any]],
    current_contracts: dict[str, dict[str, Any]],
    contract_backrefs: list[dict[str, str]],
    *,
    field_key: str,
    display_name: str,
    description: str,
    value_type: str,
    required: bool,
    derived_from_kind: str,
    derived_from_ref: str,
    source_entry: dict[str, Any],
    now: datetime,
    contract_id_hint: str = "",
    skill_backref: dict[str, str] | None = None,
) -> None:
    _ = required
    normalized_key = str(field_key or "").strip()
    if not normalized_key:
        return
    normalized_type = _normalize_value_type(value_type)
    normalized_description = str(description or "").strip()
    normalized_display_name = str(display_name or "").strip()
    source_ref = str(derived_from_ref or "").strip()
    source_kind = str(derived_from_kind or "").strip() or "public_io"

    bucket = desired_fields.setdefault(
        normalized_key,
        {
            "field_key": normalized_key,
            "display_name": normalized_display_name or _humanize_field_key(normalized_key),
            "description": normalized_description,
            "value_type": normalized_type,
            "data_contract_id": None,
            "created_from_kind": source_kind,
            "created_from_ref": source_ref,
            "source_hints_json": {"sources": []},
            "manual_allowed": True,
            "enabled": True,
            "projection_mode": "derived",
            "derived_from_kind": source_kind,
            "derived_from_ref": source_ref,
            "sync_status": "active",
            "last_synced_at": now,
            "last_sync_error": None,
        },
    )
    if (
        bucket.get("display_name") == _humanize_field_key(normalized_key)
        and normalized_display_name
    ):
        bucket["display_name"] = normalized_display_name
    if not bucket.get("description") and normalized_description:
        bucket["description"] = normalized_description
    if (
        bucket.get("value_type") not in STRUCTURED_VALUE_TYPES
        and normalized_type in STRUCTURED_VALUE_TYPES
    ):
        bucket["value_type"] = normalized_type
    bucket_sources = bucket["source_hints_json"].setdefault("sources", [])
    bucket_sources.append(source_entry)

    if normalized_type in STRUCTURED_VALUE_TYPES:
        contract_id = str(contract_id_hint or "").strip() or f"auto.{normalized_key}.v1"
        if skill_backref:
            contract_backrefs.append(
                {**skill_backref, "field_key": normalized_key, "contract_id": contract_id}
            )
        bucket["data_contract_id"] = contract_id
        existing_contract = current_contracts.get(contract_id) or {}
        desired_contracts.setdefault(
            contract_id,
            {
                "contract_id": contract_id,
                "version": str(existing_contract.get("version") or "1.0.0"),
                "display_name": str(
                    existing_contract.get("display_name")
                    or normalized_display_name
                    or _humanize_field_key(normalized_key)
                ).strip(),
                "description": str(
                    existing_contract.get("description") or normalized_description
                ).strip(),
                "kind": normalized_type,
                "schema_json": _default_schema_for_value_type(normalized_type),
                "enabled": True,
                "tags": list(existing_contract.get("tags") or ["derived", "auto"]),
                "projection_mode": "derived",
                "derived_from_kind": "field_shape",
                "derived_from_ref": normalized_key,
                "sync_status": "active",
                "last_synced_at": now,
                "last_sync_error": None,
            },
        )
