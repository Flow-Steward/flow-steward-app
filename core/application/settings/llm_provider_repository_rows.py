"""Database row normalization for extension-contributed LLM providers."""

from __future__ import annotations

from typing import Any


def _cursor_kwargs() -> dict[str, Any]:
    try:
        _pge2 = __import__("psycopg_runtime.extras", fromlist=["RealDictCursor"])
        return {"cursor_factory": _pge2.RealDictCursor}
    except ModuleNotFoundError:
        try:
            from core.infrastructure.db.psycopg_runtime import extras as _pge2

            return {"cursor_factory": _pge2.RealDictCursor}
        except Exception:
            return {}
    except ImportError:
        return {}
    except Exception:
        return {}


def _row_to_dict(row: Any, description: Any) -> dict[str, Any]:
    if isinstance(row, dict):
        return dict(row)
    columns = [
        col[0] if hasattr(col, "__getitem__") else getattr(col, "name", "")
        for col in (description or [])
    ]
    if isinstance(row, (tuple, list)) and columns and len(columns) == len(row):
        return {str(columns[idx]): row[idx] for idx in range(len(columns))}
    try:
        return dict(row)
    except Exception:
        return {}


def _normalize_model(payload: dict[str, Any]) -> dict[str, Any]:
    payload["id"] = str(payload.get("id") or "").strip()
    if "account_id" in payload:
        payload["account_id"] = str(payload.get("account_id") or "").strip()
    if "provider_source_id" in payload:
        payload["provider_source_id"] = str(payload.get("provider_source_id") or "").strip()
    return payload
