"""Application-level persistence for security policy state."""

from __future__ import annotations

import json
from typing import Any, Literal

ProjectSecurityOverrideWriteResult = Literal["saved", "not_found", "storage_unavailable"]


def persist_global_policy(
    bl: Any,
    payload: dict[str, Any],
    *,
    actor: str = "system",
    account_id: Any = None,
) -> bool:
    conn = getattr(bl, "conn", None) if bl else None
    if conn is None:
        return False
    from core.infrastructure.db.system_settings_store import (
        SECURITY_POLICY_SETTINGS_KEY,
        save_system_setting,
    )

    save_system_setting(
        conn=conn,
        setting_key=SECURITY_POLICY_SETTINGS_KEY,
        value=payload,
        updated_by=str(actor or "system").strip() or "system",
        account_id=account_id,
    )
    conn.commit()
    return True


def get_project_security_override(bl: Any, project_id: Any) -> dict[str, Any] | None:
    conn = getattr(bl, "conn", None) if bl else None
    if conn is None:
        return None

    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT config->'security_policy' FROM projects WHERE id = %s",
                (project_id,),
            )
            row = cur.fetchone()
            current = _coerce_json_object(row[0] if row else None)
            if current is not None:
                return current
            return None
    except Exception:
        return None


def update_project_security_override(
    bl: Any, project_id: Any, payload: dict[str, Any]
) -> ProjectSecurityOverrideWriteResult:
    conn = getattr(bl, "conn", None) if bl else None
    if conn is None:
        return "storage_unavailable"
    saved = _save_project_security_override(conn, project_id, payload)
    if saved:
        conn.commit()
        return "saved"
    return "not_found"


def _coerce_json_object(value: Any) -> dict[str, Any] | None:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            parsed = json.loads(text)
        except Exception:
            return None
        if isinstance(parsed, dict):
            return parsed
    return None


def _save_project_security_override(conn: Any, project_id: Any, payload: dict[str, Any]) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE projects
               SET config = jsonb_set(
                   COALESCE(config, '{}'::jsonb),
                   '{security_policy}',
                   %s::jsonb,
                   true
               )
             WHERE id = %s
            """,
            (json.dumps(payload), project_id),
        )
        return int(getattr(cur, "rowcount", 0) or 0) > 0
