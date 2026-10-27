"""Queue payload serialization and sensitive configuration filtering."""

import json
import uuid
from datetime import UTC, date, datetime
from typing import Any

from core.infrastructure.runtime.worker.queue_lease_store import (
    normalize_entity_id as _normalize_entity_id,
)

_QUEUE_PROJECT_CONFIG_ALLOWLIST = frozenset(
    {
        "project_name",
        "project_id",
        "assigned_data_connections",
        "data_connection_ids",
        "agents_config",
        "project_rules",
        "conventions",
        "operator_default_workflow_id",
        "project_workflows",
        "project_policy",
    }
)

_SENSITIVE_NESTED_KEY_FRAGMENTS = (
    "secret",
    "token",
    "password",
    "credential",
    "api_key",
    "private_key",
    "dsn",
)


def _sanitize_project_config_for_queue(project_config: dict | None) -> dict:
    """Allowlist and scrub nested sensitive fields before queue serialization."""
    source = dict(project_config or {})
    safe: dict[str, Any] = {}

    for key in _QUEUE_PROJECT_CONFIG_ALLOWLIST:
        if key not in source:
            continue
        safe[key] = _strip_nested_sensitive_values(source.get(key))

    return safe


def _strip_nested_sensitive_values(value: Any) -> Any:
    """Recursively drop values whose key names look secret-bearing."""
    if isinstance(value, dict):
        cleaned: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key or "").strip().lower()
            if any(fragment in key_text for fragment in _SENSITIVE_NESTED_KEY_FRAGMENTS):
                continue
            cleaned[key] = _strip_nested_sensitive_values(item)
        return cleaned
    if isinstance(value, list):
        return [_strip_nested_sensitive_values(item) for item in value]
    return value


def _safe_iso_utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _json_default(value: Any) -> Any:
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


def _ready_event_payload(
    *,
    project_id: Any,
    job_id: Any | None = None,
    task_id: str | None = None,
    enqueued_at: str | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "project_id": _normalize_entity_id(project_id),
        "enqueued_at": str(enqueued_at or _safe_iso_utc_now()),
    }
    if job_id is not None:
        payload["job_id"] = _normalize_entity_id(job_id)
    task_id_text = str(task_id or "").strip()
    if task_id_text:
        payload["task_id"] = task_id_text
    return payload


def _decode_ready_event(body: bytes) -> dict:
    payload = json.loads((body or b"{}").decode("utf-8"))
    if not isinstance(payload, dict):
        return {}
    raw_project_id = payload.get("project_id")
    if raw_project_id in (None, ""):
        return {}
    if isinstance(raw_project_id, bool):
        return {}
    project_id = str(raw_project_id or "").strip()
    if not project_id:
        return {}
    payload["project_id"] = project_id
    return payload
