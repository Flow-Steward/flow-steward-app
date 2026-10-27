"""Serialization and identifier normalization for external-effect records."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any
from uuid import UUID


def _row_to_dict(row: Any, description: Any = None) -> dict[str, Any]:
    if isinstance(row, dict):
        payload = dict(row)
    else:
        columns = [
            col[0] if hasattr(col, "__getitem__") else getattr(col, "name", "")
            for col in (description or [])
        ]
        if not isinstance(row, (tuple, list)) or len(columns) != len(row):
            return {}
        payload = {str(columns[idx]): row[idx] for idx in range(len(columns))}
    for key, value in list(payload.items()):
        if isinstance(value, datetime):
            payload[key] = value.isoformat()
    for key in (
        "source_ref_jsonb",
        "target_hashes_jsonb",
        "artifact_refs_jsonb",
        "response_summary_jsonb",
        "minted_artifact_ids_jsonb",
        "minted_dataset_ids_jsonb",
    ):
        if isinstance(payload.get(key), str):
            try:
                parsed = json.loads(payload[key])
            except Exception:
                parsed = (
                    []
                    if key
                    in {
                        "target_hashes_jsonb",
                        "artifact_refs_jsonb",
                        "minted_artifact_ids_jsonb",
                        "minted_dataset_ids_jsonb",
                    }
                    else {}
                )
            payload[key] = parsed
    return payload


def _jsonb(value: Any, *, default: Any) -> str:
    payload = value if isinstance(value, (dict, list)) else default
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


def _uuid_text(value: Any) -> str | None:
    try:
        return str(UUID(str(value or "").strip()))
    except (TypeError, ValueError, AttributeError):
        return None
