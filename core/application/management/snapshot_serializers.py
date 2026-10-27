from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from re import Pattern
from typing import Any

from core.application.orchestration.agent_message_projection import (
    public_agent_message,
    public_replay_outcome,
)
from core.infrastructure.security.prompt_safety import (
    redact_diagnostic_identifier_for_persistence,
    redact_structure_for_persistence,
)


def _entity_id(value: Any) -> str | None:
    normalized = str(value or "").strip()
    return normalized or None


def redact_snapshot_secrets(value: Any, *, secret_key_re: Pattern[str]):
    """Recursively redact obvious secret-like keys and secret values from config snapshots."""
    if isinstance(value, dict):
        redacted: dict = {}
        for key, item in value.items():
            key_text = str(key or "")
            if secret_key_re.search(key_text):
                redacted[key] = "[REDACTED]"
                continue
            redacted[key] = redact_snapshot_secrets(item, secret_key_re=secret_key_re)
        return redacted
    if isinstance(value, list):
        return [redact_snapshot_secrets(item, secret_key_re=secret_key_re) for item in value]
    if isinstance(value, str) and value:
        # Also scan string values for embedded secrets (e.g. DSNs, API keys in values)
        try:
            from core.infrastructure.security.policy import ScannerConfig
            from core.infrastructure.security.scanners.secrets import SecretScanner

            findings = SecretScanner().scan(value, "index", ScannerConfig())
            if findings:
                return "[REDACTED]"
        except Exception:
            pass
    return value


def build_project_config_snapshot(
    project_config: dict | None, *, secret_key_re: Pattern[str]
) -> dict:
    """Build a JSON-safe, redacted config snapshot for deterministic replay."""
    if not isinstance(project_config, dict):
        return {}
    try:
        serialized = json.dumps(project_config, default=str)
        parsed = json.loads(serialized)
    except Exception:
        return {}
    if not isinstance(parsed, dict):
        return {}
    redacted = redact_snapshot_secrets(parsed, secret_key_re=secret_key_re)
    return redacted if isinstance(redacted, dict) else {}


def json_value_equal(left: Any, right: Any) -> bool:
    try:
        return json.dumps(left, sort_keys=True, default=str) == json.dumps(
            right,
            sort_keys=True,
            default=str,
        )
    except Exception:
        return left == right


def project_settings_diff(
    from_snapshot: dict | None,
    to_snapshot: dict | None,
    *,
    json_value_equal_fn: Callable[[Any, Any], bool] = json_value_equal,
) -> dict:
    """Compute a deterministic structured diff between project settings snapshots."""
    changes: list[dict] = []
    _missing = object()

    def _stable_settings(snapshot: dict | None) -> dict:
        """Remove derived runtime state from current and legacy revision snapshots."""
        if not isinstance(snapshot, dict):
            return {}
        stable = dict(snapshot)
        project = stable.get("project")
        if isinstance(project, dict):
            stable["project"] = {
                key: value for key, value in project.items() if key != "assigned_data_connections"
            }
        return stable

    def _walk(path: str, left: Any, right: Any) -> None:
        if isinstance(left, dict) and isinstance(right, dict):
            keys = sorted(set(left.keys()) | set(right.keys()), key=lambda item: str(item))
            for key in keys:
                child = f"{path}.{key}" if path else str(key)
                _walk(child, left.get(key), right.get(key))
            return
        if isinstance(left, list) and isinstance(right, list):
            max_len = max(len(left), len(right))
            for idx in range(max_len):
                child = f"{path}[{idx}]"
                left_item = left[idx] if idx < len(left) else _missing
                right_item = right[idx] if idx < len(right) else _missing
                if left_item is _missing:
                    changes.append(
                        {"path": child, "change": "added", "from": None, "to": right_item}
                    )
                elif right_item is _missing:
                    changes.append(
                        {"path": child, "change": "removed", "from": left_item, "to": None}
                    )
                else:
                    _walk(child, left_item, right_item)
            return

        if left is _missing and right is _missing:
            return
        if left is _missing:
            changes.append({"path": path or "$", "change": "added", "from": None, "to": right})
            return
        if right is _missing:
            changes.append({"path": path or "$", "change": "removed", "from": left, "to": None})
            return
        if not json_value_equal_fn(left, right):
            changes.append({"path": path or "$", "change": "changed", "from": left, "to": right})

    _walk("", _stable_settings(from_snapshot), _stable_settings(to_snapshot))
    summary = {"total": len(changes), "added": 0, "removed": 0, "changed": 0}
    for item in changes:
        kind = str(item.get("change") or "changed")
        if kind in summary:
            summary[kind] += 1
        else:
            summary["changed"] += 1
    return {"changes": changes, "summary": summary}


def policy_snapshot_hash(policy_snapshot: dict | None) -> str | None:
    if not isinstance(policy_snapshot, dict) or not policy_snapshot:
        return None
    try:
        encoded = json.dumps(policy_snapshot, sort_keys=True, default=str).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()[:16]
    except Exception:
        return None


def _resolved_to_json_obj(
    to_json_obj: Callable[[Any], Any] | None,
) -> Callable[[Any], Any]:
    return to_json_obj or (lambda value: value)


def serialize_log_row(
    row: dict,
    job_id: str | None,
    *,
    to_json_obj: Callable[[Any], Any] | None = None,
) -> dict:
    to_json_obj = _resolved_to_json_obj(to_json_obj)
    payload = to_json_obj(row.get("payload"))
    result = to_json_obj(row.get("result"))
    meta = to_json_obj(row.get("meta"))
    return {
        "id": str(row.get("id") or "").strip(),
        "timestamp": row["timestamp"].isoformat() if row.get("timestamp") else None,
        "agent": row.get("agent"),
        "action_type": row.get("action_type"),
        "status": row.get("status"),
        "duration": row.get("duration"),
        "payload": payload,
        "result": result,
        "error": row.get("error"),
        "meta": meta,
        "tokens_prompt": row.get("tokens_prompt"),
        "tokens_completion": row.get("tokens_completion"),
        "tokens_total": row.get("tokens_total"),
        "cost_usd": float(row.get("cost_usd") or 0) if row.get("cost_usd") is not None else None,
        "model_used": row.get("model_used"),
        "payload_ref": f"log://{job_id}/{row.get('id')}/payload" if payload is not None else None,
        "result_ref": f"log://{job_id}/{row.get('id')}/result" if result is not None else None,
        "meta_ref": f"log://{job_id}/{row.get('id')}/meta" if meta is not None else None,
    }


def serialize_event_row(
    row: dict,
    *,
    to_json_obj: Callable[[Any], Any] | None = None,
) -> dict:
    to_json_obj = _resolved_to_json_obj(to_json_obj)
    detail = to_json_obj(row.get("detail"))
    return {
        "id": str(row.get("id") or "").strip(),
        "timestamp": row["timestamp"].isoformat() if row.get("timestamp") else None,
        "phase": row.get("phase"),
        "agent": row.get("agent"),
        "event_type": row.get("event_type"),
        "severity": row.get("severity"),
        "title": row.get("title"),
        "detail": detail,
    }


def serialize_agent_message_row(
    row: dict,
    *,
    to_json_obj: Callable[[Any], Any] | None = None,
) -> dict:
    to_json_obj = _resolved_to_json_obj(to_json_obj)
    envelope = to_json_obj(row.get("envelope")) or {}
    artifacts = to_json_obj(row.get("artifacts")) or {}
    errors = to_json_obj(row.get("errors")) or []
    caused_by = []
    if isinstance(envelope, dict):
        caused_by_raw = envelope.get("caused_by")
        if isinstance(caused_by_raw, list):
            caused_by = [dict(x) for x in caused_by_raw if isinstance(x, dict)]
    return {
        "id": str(row.get("id") or "").strip(),
        "timestamp": row["timestamp"].isoformat() if row.get("timestamp") else None,
        "job_id": str(row.get("job_id") or "").strip(),
        "correlation_id": row.get("correlation_id"),
        "schema_version": row.get("schema_version"),
        "from_agent": row.get("from_agent"),
        "to_agent": row.get("to_agent"),
        "step_id": row.get("step_id"),
        "step_execution_id": str(row.get("step_execution_id") or "").strip() or None,
        "attempt": row.get("attempt"),
        "group_id": row.get("group_id"),
        "skill": row.get("skill"),
        "status": row.get("status"),
        "summary": row.get("summary"),
        "caused_by": caused_by,
        "artifacts": artifacts,
        "errors": errors if isinstance(errors, list) else [],
        "envelope": public_agent_message(envelope) if isinstance(envelope, dict) else {},
    }


def serialize_decision_override_row(
    row: dict,
    *,
    to_json_obj: Callable[[Any], Any] | None = None,
) -> dict:
    to_json_obj = _resolved_to_json_obj(to_json_obj)
    return {
        "id": row.get("id"),
        "job_id": row.get("job_id"),
        "message_id": row.get("message_id"),
        "workflow_id": row.get("workflow_id"),
        "step_id": row.get("step_id"),
        "step_execution_id": str(row.get("step_execution_id") or "").strip() or None,
        "domain": row.get("domain"),
        "original_value": to_json_obj(row.get("original_value")) or {},
        "override_value": to_json_obj(row.get("override_value")) or {},
        "reason": row.get("reason"),
        "created_by": row.get("created_by"),
        "active": bool(row.get("active", False)),
        "created_at": row["created_at"].isoformat() if row.get("created_at") else None,
    }


def serialize_job_error_row(
    row: dict,
    *,
    to_json_obj: Callable[[Any], Any] | None = None,
) -> dict:
    to_json_obj = _resolved_to_json_obj(to_json_obj)
    context_jsonb = to_json_obj(row.get("context_jsonb")) or {}
    if isinstance(context_jsonb, dict):
        job_id = _entity_id(row.get("job_id"))
        context_jsonb = redact_structure_for_persistence(
            context_jsonb,
            project_id=None,
            job_id=job_id,
            context_type="display",
        )
    else:
        context_jsonb = {}
    job_id = _entity_id(row.get("job_id"))
    return {
        "id": row.get("id"),
        "job_id": row.get("job_id"),
        "message_id": row.get("message_id"),
        "agent_id": row.get("agent_id"),
        "group_id": row.get("group_id"),
        "step_id": row.get("step_id"),
        "step_execution_id": str(row.get("step_execution_id") or "").strip() or None,
        "source": row.get("source"),
        "category": row.get("category"),
        "provider_name": row.get("provider_name"),
        "http_status": row.get("http_status"),
        "provider_error_code": redact_diagnostic_identifier_for_persistence(
            "provider_error_code",
            str(row.get("provider_error_code") or ""),
            project_id=None,
            job_id=job_id,
            context_type="display",
        )
        or None,
        "provider_error_type": redact_diagnostic_identifier_for_persistence(
            "provider_error_type",
            str(row.get("provider_error_type") or ""),
            project_id=None,
            job_id=job_id,
            context_type="display",
        )
        or None,
        "request_id": redact_diagnostic_identifier_for_persistence(
            "request_id",
            str(row.get("request_id") or ""),
            project_id=None,
            job_id=job_id,
            context_type="display",
        )
        or None,
        "retryable": row.get("retryable"),
        "error_message": row.get("error_message"),
        "response_excerpt": row.get("response_excerpt"),
        "traceback": row.get("traceback"),
        "context_jsonb": context_jsonb,
        "created_at": row["created_at"].isoformat() if row.get("created_at") else None,
    }


def serialize_job_step_replay_row(
    row: dict,
    *,
    to_json_obj: Callable[[Any], Any] | None = None,
) -> dict:
    to_json_obj = _resolved_to_json_obj(to_json_obj)
    return {
        "id": row.get("id"),
        "job_id": row.get("job_id"),
        "message_id": row.get("message_id"),
        "correlation_id": row.get("correlation_id"),
        "requested_by": row.get("requested_by"),
        "replay_mode": row.get("replay_mode"),
        "status": row.get("status"),
        "prompt_version": row.get("prompt_version"),
        "policy_snapshot": to_json_obj(row.get("policy_snapshot")) or {},
        "captured_input": to_json_obj(row.get("captured_input")) or {},
        "original_outcome": public_replay_outcome(to_json_obj(row.get("original_outcome")) or {}),
        "replay_outcome": public_replay_outcome(to_json_obj(row.get("replay_outcome")) or {}),
        "diff_excerpt": row.get("diff_excerpt"),
        "error_message": row.get("error_message"),
        "created_at": row["created_at"].isoformat() if row.get("created_at") else None,
        "started_at": row["started_at"].isoformat() if row.get("started_at") else None,
        "finished_at": row["finished_at"].isoformat() if row.get("finished_at") else None,
    }
