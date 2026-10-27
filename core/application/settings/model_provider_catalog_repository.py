"""Application-owned read repository for custom provider catalog projection."""

from __future__ import annotations

from typing import Any


def _cursor_kwargs() -> dict[str, Any]:
    try:
        from psycopg_runtime.extras import RealDictCursor

        return {"cursor_factory": RealDictCursor}
    except ImportError:
        return {}


def _row_to_dict(row: Any, description: Any) -> dict[str, Any]:
    if isinstance(row, dict):
        return dict(row)
    columns = [
        col[0] if hasattr(col, "__getitem__") else getattr(col, "name", "")
        for col in (description or [])
    ]
    if isinstance(row, (tuple, list)) and columns and len(columns) == len(row):
        return {str(columns[index]): row[index] for index in range(len(row))}
    return {}


def list_routable_model_providers(conn: Any, *, account_id: str) -> list[dict[str, Any]]:
    """Return active, recently verified custom routes for one account."""
    with conn.cursor(**_cursor_kwargs()) as cur:
        cur.execute(
            """
            SELECT *
              FROM model_providers
             WHERE account_id = %s
               AND enabled = true
               AND verification_status = 'passed'
               AND lifecycle = 'active'
               AND verified_at >= now() - interval '30 days'
               AND verified_at <= now()
             ORDER BY name
            """,
            (str(account_id),),
        )
        rows = cur.fetchall()
        description = list(cur.description or [])

    normalized: list[dict[str, Any]] = []
    for row in rows:
        payload = _row_to_dict(row, description)
        if not payload:
            continue
        payload["id"] = str(payload.get("id") or "").strip()
        if "account_id" in payload:
            payload["account_id"] = str(payload.get("account_id") or "").strip()
        payload.pop("api_key_ref", None)
        payload.pop("config_jsonb", None)
        normalized.append(payload)
    return normalized
