"""Pure value coercion and series inspection for report generation."""

from __future__ import annotations

from typing import Any

from core.application.auth.config import DEFAULT_TENANT_ID


def _json_obj(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value or 0.0)
    except Exception:
        return float(default)


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value or 0)
    except Exception:
        return int(default)


def _entity_id(value: Any) -> str | None:
    normalized = str(value or "").strip()
    return normalized or None


def _tenant_id(value: Any) -> str:
    normalized = str(value or "").strip()
    return normalized or DEFAULT_TENANT_ID


def _series_has_values(series: dict[str, Any]) -> bool:
    for item in series.get("data") or []:
        if isinstance(item, dict):
            if _safe_float(item.get("value"), default=0.0) > 0:
                return True
            continue
        if _safe_float(item, default=0.0) > 0:
            return True
    return False
