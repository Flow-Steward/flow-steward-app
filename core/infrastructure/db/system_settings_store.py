"""Reusable helpers for account-scoped system settings persistence."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from core.infrastructure.config.platform_identity import DEFAULT_ACCOUNT_ID
from core.infrastructure.db import psycopg_runtime as psycopg_runtime
from core.infrastructure.db.psycopg_runtime import errorcodes

RETENTION_SETTINGS_KEY = "retention_policy"
SECURITY_POLICY_SETTINGS_KEY = "security_policy"
SECURITY_POSTURE_LOOP_STATUS_KEY = "security_posture_loop_status"
WORKFLOW_KNOWLEDGE_SYNC_LOOP_STATUS_KEY = "workflow_knowledge_sync_loop_status"
MAX_SECURITY_POSTURE_FAILURE_REASON = 512
RETENTION_SETTINGS_FIELDS = (
    "action_logs_days",
    "job_logs_days",
    "trash_days",
    "terminal_job_checkpoints_days",
    "revisions_keep",
)
DEFAULT_RETENTION_SETTINGS: dict[str, int] = {
    "action_logs_days": 30,
    "job_logs_days": 30,
    "trash_days": 30,
    "terminal_job_checkpoints_days": 30,
    "revisions_keep": 5,
}
MIN_RETENTION_DAYS = 1
MAX_RETENTION_DAYS = 3650
# Revisions are kept by count, not age: after a quiet month an entity must
# still show what changed last and who changed it.
MIN_REVISIONS_KEEP = 1
# Per item: a hundred items at the ceiling are already ten thousand revisions.
MAX_REVISIONS_KEEP = 100
_MISSING_SETTINGS_STORAGE_PGCODES = frozenset(
    {
        errorcodes.UNDEFINED_TABLE,
        errorcodes.UNDEFINED_COLUMN,
        errorcodes.INVALID_SCHEMA_NAME,
    }
)


def normalize_account_id(account_id: Any) -> str:
    normalized = str(account_id or DEFAULT_ACCOUNT_ID).strip()
    return normalized or DEFAULT_ACCOUNT_ID


def _coerce_days(value: Any, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return int(default)
    return max(MIN_RETENTION_DAYS, min(MAX_RETENTION_DAYS, parsed))


def _coerce_retention_value(field: str, value: Any, default: int) -> int:
    if field != "revisions_keep":
        return _coerce_days(value, default)
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return int(default)
    return max(MIN_REVISIONS_KEEP, min(MAX_REVISIONS_KEEP, parsed))


def _is_missing_settings_storage_error(exc: Exception) -> bool:
    code = str(getattr(exc, "pgcode", "") or "").strip().upper()
    return code in _MISSING_SETTINGS_STORAGE_PGCODES


def normalize_retention_settings(
    settings: dict[str, Any] | None,
    *,
    defaults: dict[str, Any] | None = None,
) -> dict[str, int]:
    base = {
        key: _coerce_retention_value(key, value, DEFAULT_RETENTION_SETTINGS[key])
        for key, value in (defaults or DEFAULT_RETENTION_SETTINGS).items()
        if key in DEFAULT_RETENTION_SETTINGS
    }
    for key, value in DEFAULT_RETENTION_SETTINGS.items():
        base.setdefault(key, _coerce_retention_value(key, value, value))

    payload = settings if isinstance(settings, dict) else {}
    return {
        field: _coerce_retention_value(field, payload.get(field), base[field])
        for field in RETENTION_SETTINGS_FIELDS
    }


def get_system_setting(
    *,
    conn,
    _table_exists=None,
    setting_key: str,
    account_id: Any = None,
) -> dict[str, Any] | None:
    resolved_account_id = normalize_account_id(account_id)
    try:
        with conn.cursor(cursor_factory=psycopg_runtime.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT
                    account_id,
                    setting_key,
                    setting_value_jsonb,
                    updated_at,
                    updated_by
                FROM system_settings
                WHERE account_id = %s
                  AND setting_key = %s
                LIMIT 1
                """,
                (resolved_account_id, str(setting_key or "").strip()),
            )
            row = cur.fetchone()
    except Exception as exc:
        if _is_missing_settings_storage_error(exc):
            return None
        raise
    return dict(row) if isinstance(row, dict) else None


def get_retention_settings(
    *,
    conn,
    table_exists,
    defaults: dict[str, Any] | None = None,
    account_id: Any = None,
) -> dict[str, Any]:
    row = get_system_setting(
        conn=conn,
        _table_exists=table_exists,
        setting_key=RETENTION_SETTINGS_KEY,
        account_id=account_id,
    )
    payload = normalize_retention_settings(
        row.get("setting_value_jsonb") if isinstance(row, dict) else None,
        defaults=defaults,
    )
    payload["account_id"] = normalize_account_id(account_id)
    if isinstance(row, dict):
        updated_at = row.get("updated_at")
        if isinstance(updated_at, datetime):
            payload["updated_at"] = updated_at
        updated_by = str(row.get("updated_by") or "").strip()
        if updated_by:
            payload["updated_by"] = updated_by
    return payload


def save_retention_settings(
    *,
    conn,
    _table_exists=None,
    settings: dict[str, Any] | None,
    actor: str = "system",
    account_id: Any = None,
) -> dict[str, Any]:
    normalized = normalize_retention_settings(settings)
    resolved_account_id = normalize_account_id(account_id)
    updated_by = str(actor or "system").strip() or "system"
    try:
        with conn.cursor(cursor_factory=psycopg_runtime.extras.RealDictCursor) as cur:
            cur.execute(
                """
                INSERT INTO system_settings (
                    account_id,
                    setting_key,
                    setting_value_jsonb,
                    updated_by
                ) VALUES (%s, %s, %s::jsonb, %s)
                ON CONFLICT (account_id, setting_key) DO UPDATE SET
                    setting_value_jsonb = EXCLUDED.setting_value_jsonb,
                    updated_at = NOW(),
                    updated_by = EXCLUDED.updated_by
                RETURNING
                    account_id,
                    setting_key,
                    setting_value_jsonb,
                    updated_at,
                    updated_by
                """,
                (
                    resolved_account_id,
                    RETENTION_SETTINGS_KEY,
                    json.dumps(normalized),
                    updated_by,
                ),
            )
            row = cur.fetchone()
    except Exception as exc:
        if _is_missing_settings_storage_error(exc):
            raise RuntimeError("system_settings table is not available") from exc
        raise

    payload = normalize_retention_settings(
        row.get("setting_value_jsonb") if isinstance(row, dict) else normalized
    )
    payload["account_id"] = resolved_account_id
    if isinstance(row, dict):
        updated_at = row.get("updated_at")
        if isinstance(updated_at, datetime):
            payload["updated_at"] = updated_at
        saved_by = str(row.get("updated_by") or "").strip()
        if saved_by:
            payload["updated_by"] = saved_by
    return payload


def save_system_setting(
    *,
    conn,
    account_id: Any = None,
    setting_key: str,
    value: Any,
    updated_by: str = "system",
) -> dict[str, Any] | None:
    """Generic upsert for any system setting key."""
    resolved_account_id = normalize_account_id(account_id)
    resolved_key = str(setting_key or "").strip()
    if not resolved_key:
        raise ValueError("setting_key is required")
    actor = str(updated_by or "system").strip() or "system"
    try:
        with conn.cursor(cursor_factory=psycopg_runtime.extras.RealDictCursor) as cur:
            cur.execute(
                """
                INSERT INTO system_settings (
                    account_id,
                    setting_key,
                    setting_value_jsonb,
                    updated_by
                ) VALUES (%s, %s, %s::jsonb, %s)
                ON CONFLICT (account_id, setting_key) DO UPDATE SET
                    setting_value_jsonb = EXCLUDED.setting_value_jsonb,
                    updated_at = NOW(),
                    updated_by = EXCLUDED.updated_by
                RETURNING
                    account_id,
                    setting_key,
                    setting_value_jsonb,
                    updated_at,
                    updated_by
                """,
                (
                    resolved_account_id,
                    resolved_key,
                    json.dumps(value),
                    actor,
                ),
            )
            row = cur.fetchone()
    except Exception as exc:
        if _is_missing_settings_storage_error(exc):
            raise RuntimeError("system_settings table is not available") from exc
        raise
    return dict(row) if isinstance(row, dict) else None


def _security_posture_loop_status_value(row: dict[str, Any] | None) -> dict[str, Any]:
    value = row.get("setting_value_jsonb") if isinstance(row, dict) else None
    payload = dict(value) if isinstance(value, dict) else {}
    return {
        "last_attempt_at": str(payload.get("last_attempt_at") or ""),
        "last_success_at": str(payload.get("last_success_at") or ""),
        "last_failure_reason": str(payload.get("last_failure_reason") or "")[
            :MAX_SECURITY_POSTURE_FAILURE_REASON
        ],
        "consecutive_failures": max(0, int(payload.get("consecutive_failures") or 0)),
    }


def get_security_posture_loop_status(*, conn, account_id: Any = None) -> dict[str, Any]:
    row = get_system_setting(
        conn=conn,
        setting_key=SECURITY_POSTURE_LOOP_STATUS_KEY,
        account_id=account_id,
    )
    return _security_posture_loop_status_value(row)


def _save_security_posture_loop_status(
    *, conn, status: dict[str, Any], account_id: Any = None
) -> dict[str, Any]:
    save_system_setting(
        conn=conn,
        account_id=account_id,
        setting_key=SECURITY_POSTURE_LOOP_STATUS_KEY,
        value=status,
        updated_by="control-security-posture",
    )
    return dict(status)


def record_security_posture_loop_attempt(*, conn, account_id: Any = None) -> dict[str, Any]:
    status = get_security_posture_loop_status(conn=conn, account_id=account_id)
    status["last_attempt_at"] = datetime.now(UTC).isoformat()
    return _save_security_posture_loop_status(conn=conn, status=status, account_id=account_id)


def record_security_posture_loop_success(*, conn, account_id: Any = None) -> dict[str, Any]:
    status = get_security_posture_loop_status(conn=conn, account_id=account_id)
    now = datetime.now(UTC).isoformat()
    status.update(
        {
            "last_attempt_at": now,
            "last_success_at": now,
            "last_failure_reason": "",
            "consecutive_failures": 0,
        }
    )
    return _save_security_posture_loop_status(conn=conn, status=status, account_id=account_id)


def record_security_posture_loop_failure(
    *, conn, reason: str, account_id: Any = None
) -> dict[str, Any]:
    status = get_security_posture_loop_status(conn=conn, account_id=account_id)
    status.update(
        {
            "last_attempt_at": datetime.now(UTC).isoformat(),
            "last_failure_reason": str(reason or "security posture iteration failed")[
                :MAX_SECURITY_POSTURE_FAILURE_REASON
            ],
            "consecutive_failures": int(status.get("consecutive_failures") or 0) + 1,
        }
    )
    return _save_security_posture_loop_status(conn=conn, status=status, account_id=account_id)


def get_workflow_knowledge_sync_loop_status(*, conn, account_id: Any = None) -> dict[str, Any]:
    row = get_system_setting(
        conn=conn,
        setting_key=WORKFLOW_KNOWLEDGE_SYNC_LOOP_STATUS_KEY,
        account_id=account_id,
    )
    status = _security_posture_loop_status_value(row)
    value = row.get("setting_value_jsonb") if isinstance(row, dict) else None
    payload = dict(value) if isinstance(value, dict) else {}
    status.update(
        {
            "active_outbox_id": str(payload.get("active_outbox_id") or ""),
            "active_claim_owner": str(payload.get("active_claim_owner") or ""),
            "active_claim_version": max(0, int(payload.get("active_claim_version") or 0)),
        }
    )
    return status


def _save_workflow_knowledge_sync_loop_status(
    *, conn, status: dict[str, Any], account_id: Any = None
) -> dict[str, Any]:
    save_system_setting(
        conn=conn,
        account_id=account_id,
        setting_key=WORKFLOW_KNOWLEDGE_SYNC_LOOP_STATUS_KEY,
        value=status,
        updated_by="control-workflow-knowledge-sync",
    )
    return dict(status)


def record_workflow_knowledge_sync_loop_attempt(
    *,
    conn,
    outbox_id: str = "",
    claim_owner: str = "",
    claim_version: int = 0,
    account_id: Any = None,
) -> dict[str, Any]:
    status = get_workflow_knowledge_sync_loop_status(conn=conn, account_id=account_id)
    status.update(
        {
            "last_attempt_at": datetime.now(UTC).isoformat(),
            "active_outbox_id": str(outbox_id or "").strip(),
            "active_claim_owner": str(claim_owner or "").strip(),
            "active_claim_version": max(0, int(claim_version or 0)),
        }
    )
    return _save_workflow_knowledge_sync_loop_status(
        conn=conn, status=status, account_id=account_id
    )


def record_workflow_knowledge_sync_loop_success(*, conn, account_id: Any = None) -> dict[str, Any]:
    status = get_workflow_knowledge_sync_loop_status(conn=conn, account_id=account_id)
    now = datetime.now(UTC).isoformat()
    status.update(
        {
            "last_attempt_at": now,
            "last_success_at": now,
            "last_failure_reason": "",
            "consecutive_failures": 0,
            "active_outbox_id": "",
            "active_claim_owner": "",
            "active_claim_version": 0,
        }
    )
    return _save_workflow_knowledge_sync_loop_status(
        conn=conn, status=status, account_id=account_id
    )


def record_workflow_knowledge_sync_loop_failure(
    *, conn, reason: str, account_id: Any = None
) -> dict[str, Any]:
    status = get_workflow_knowledge_sync_loop_status(conn=conn, account_id=account_id)
    status.update(
        {
            "last_attempt_at": datetime.now(UTC).isoformat(),
            "last_failure_reason": str(reason or "workflow knowledge sync iteration failed")[
                :MAX_SECURITY_POSTURE_FAILURE_REASON
            ],
            "consecutive_failures": int(status.get("consecutive_failures") or 0) + 1,
            "active_outbox_id": "",
            "active_claim_owner": "",
            "active_claim_version": 0,
        }
    )
    return _save_workflow_knowledge_sync_loop_status(
        conn=conn, status=status, account_id=account_id
    )


__all__ = [
    "DEFAULT_RETENTION_SETTINGS",
    "RETENTION_SETTINGS_FIELDS",
    "RETENTION_SETTINGS_KEY",
    "SECURITY_POLICY_SETTINGS_KEY",
    "SECURITY_POSTURE_LOOP_STATUS_KEY",
    "WORKFLOW_KNOWLEDGE_SYNC_LOOP_STATUS_KEY",
    "get_retention_settings",
    "get_security_posture_loop_status",
    "get_system_setting",
    "get_workflow_knowledge_sync_loop_status",
    "normalize_account_id",
    "normalize_retention_settings",
    "record_security_posture_loop_attempt",
    "record_security_posture_loop_failure",
    "record_security_posture_loop_success",
    "record_workflow_knowledge_sync_loop_attempt",
    "record_workflow_knowledge_sync_loop_failure",
    "record_workflow_knowledge_sync_loop_success",
    "save_retention_settings",
    "save_system_setting",
]
