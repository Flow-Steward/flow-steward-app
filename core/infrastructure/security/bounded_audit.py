"""Small fail-closed projections for untrusted provider diagnostics."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from typing import Any

_SAFE_CODE = re.compile(r"[a-z][a-z0-9_.-]{0,127}")
_SAFE_FIELD = re.compile(r"[A-Za-z_][A-Za-z0-9_.\[\]-]{0,127}")
_REDACTED_PROVIDER_MESSAGE = "[redacted provider message]"
MAX_AUDIT_COUNT_FIELDS = 64
MAX_AUDIT_COUNT_VALUE = (1 << 63) - 1
_SAFE_COUNT_FIELD = re.compile(r"[a-z][a-z0-9_]{0,63}")


def safe_audit_code(value: Any) -> str:
    text = str(value or "").strip().lower()
    return text if _SAFE_CODE.fullmatch(text) else ""


def hashed_audit_identifier(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def project_actionable_errors(value: Any, *, limit: int = 10) -> list[dict[str, Any]]:
    """Keep only bounded operator fields and never persist provider row identity."""
    if not isinstance(value, list):
        return []
    projected: list[dict[str, Any]] = []
    for item in value:
        if len(projected) >= max(0, int(limit)):
            break
        if not isinstance(item, dict):
            if isinstance(item, str) and item.strip():
                projected.append({"message": _REDACTED_PROVIDER_MESSAGE})
            continue
        row: dict[str, Any] = {}
        raw_index = item.get("page_relative_index", item.get("row_index", item.get("index")))
        if isinstance(raw_index, int) and not isinstance(raw_index, bool) and raw_index >= 0:
            row["page_relative_index"] = raw_index
        raw_item_key = item.get("item_key", item.get("item_id", item.get("id")))
        item_key_hash = hashed_audit_identifier(raw_item_key)
        if item_key_hash:
            row["hashed_item_key"] = item_key_hash
        provider_code = safe_audit_code(
            item.get("provider_code", item.get("error_code", item.get("code")))
        )
        if provider_code:
            row["provider_code"] = provider_code
        field = str(item.get("field") or item.get("path") or "").strip()
        if _SAFE_FIELD.fullmatch(field):
            row["field"] = field
        if str(item.get("message") or item.get("detail") or "").strip():
            row["message"] = _REDACTED_PROVIDER_MESSAGE
        if row:
            projected.append(row)
    return projected


def project_count_fields(*values: Any, limit: int = MAX_AUDIT_COUNT_FIELDS) -> dict[str, int]:
    """Project a deterministic bounded set of non-negative counter fields."""
    candidates: dict[str, int] = {}
    pending = [value for value in values if isinstance(value, Mapping)]
    visited = 0
    while pending and visited < 32:
        value = pending.pop(0)
        visited += 1
        for key, raw in value.items():
            name = str(key or "").strip().lower()
            is_count = name.endswith("_count") or name in {"count", "total", "size"}
            if (
                is_count
                and _SAFE_COUNT_FIELD.fullmatch(name)
                and isinstance(raw, int)
                and not isinstance(raw, bool)
                and raw >= 0
            ):
                candidates[name] = min(raw, MAX_AUDIT_COUNT_VALUE)
            if isinstance(raw, Mapping) and len(pending) < 32:
                pending.append(raw)
    resolved_limit = max(0, min(int(limit), MAX_AUDIT_COUNT_FIELDS))
    return dict(sorted(candidates.items())[:resolved_limit])


__all__ = [
    "MAX_AUDIT_COUNT_FIELDS",
    "MAX_AUDIT_COUNT_VALUE",
    "hashed_audit_identifier",
    "project_actionable_errors",
    "project_count_fields",
    "safe_audit_code",
]
