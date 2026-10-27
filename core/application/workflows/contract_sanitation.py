from __future__ import annotations

import copy
import json
from typing import Any

_AUTHORING_CONTRACT_TYPES = {
    "array",
    "boolean",
    "file",
    "json",
    "list",
    "number",
    "object",
    "paragraph",
    "select",
    "string",
    "text",
}
_AUTHORING_CONTRACT_TYPE_FALLBACKS = {
    "data_source": "string",
    "dataset": "string",
    "dataset_handle": "string",
    "file_path": "string",
    "recordset": "array",
    "rows": "array",
    "table": "array",
}


def _text(value: Any) -> str:
    return str(value or "").strip()


def safe_contract_type(value: Any) -> str:
    raw = _text(value).casefold()
    if raw in _AUTHORING_CONTRACT_TYPES:
        return raw
    try:
        from core.application.registry.workflow_contract import normalize_field_type

        normalized = normalize_field_type(raw)
    except Exception:
        normalized = raw
    if normalized in _AUTHORING_CONTRACT_TYPES:
        return normalized
    return _AUTHORING_CONTRACT_TYPE_FALLBACKS.get(raw, "json")


def safe_contract_rows(rows: Any) -> list[dict[str, Any]]:
    if not isinstance(rows, list):
        return []
    filtered: list[dict[str, Any]] = []
    for row in rows:
        if isinstance(row, str):
            raw_row = _text(row)
            if raw_row.startswith("{"):
                try:
                    row = json.loads(raw_row)
                except json.JSONDecodeError:
                    continue
        if not isinstance(row, dict):
            continue
        name = _text(row.get("name"))
        if not name:
            continue
        safe_row = copy.deepcopy(row)
        safe_row["type"] = safe_contract_type(safe_row.get("type"))
        filtered.append(safe_row)
    return filtered
