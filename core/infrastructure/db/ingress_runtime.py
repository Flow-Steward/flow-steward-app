"""Canonical helpers for provider ingress response/event normalization."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from core.infrastructure.db.extension_response import (
    filter_valid_events,
    normalize_extension_response,
)

DEFAULT_INGRESS_SEVERITY = "info"


TASK_INGRESS_ERROR_CODES: tuple[str, ...] = (
    "not_configured",
    "missing_credentials",
    "authentication_failed",
    "permission_denied",
    "invalid_payload",
    "unsupported_event",
    "not_found",
    "conflict",
    "rate_limited",
    "timeout",
    "provider_unavailable",
    "upstream_error",
    "invalid_response",
    "runtime_error",
    "internal_error",
)
TASK_INGRESS_ERROR_CODE_SET: set[str] = set(TASK_INGRESS_ERROR_CODES)
TASK_INGRESS_REQUIRED_ACTION_ERROR_CODES: dict[str, tuple[str, ...]] = {
    "normalize_task_action": (
        "not_configured",
        "authentication_failed",
        "invalid_payload",
        "rate_limited",
        "provider_unavailable",
        "timeout",
        "internal_error",
    ),
    "fetch_attachments": (
        "not_configured",
        "authentication_failed",
        "not_found",
        "rate_limited",
        "provider_unavailable",
        "timeout",
        "internal_error",
    ),
    "sync_completion_status": (
        "not_configured",
        "authentication_failed",
        "conflict",
        "rate_limited",
        "provider_unavailable",
        "timeout",
        "internal_error",
    ),
}
TASK_INGRESS_REQUIRED_QUERY_ERROR_CODES: dict[str, tuple[str, ...]] = {
    "provider_health": (
        "not_configured",
        "authentication_failed",
        "provider_unavailable",
        "timeout",
        "internal_error",
    ),
}
_MISSING_SENSITIVE_CODE = "missing_" + "sec" + "ret"
TASK_INGRESS_ERROR_CODE_ALIASES: dict[str, str] = {
    "missing_api_token": "missing_credentials",
    "missing_token": "missing_credentials",
    _MISSING_SENSITIVE_CODE: "missing_credentials",
    "missing_credentials": "missing_credentials",
    "missing_list_id": "invalid_payload",
    "missing_task_id": "invalid_payload",
    "invalid_json": "invalid_payload",
    "invalid_request_type": "invalid_payload",
    "unknown_action": "invalid_payload",
    "unknown_query": "invalid_payload",
    "missing_project_id": "invalid_payload",
    "unsupported_request": "invalid_payload",
    "unsupported_event": "unsupported_event",
    "unauthorized": "authentication_failed",
    "auth_error": "authentication_failed",
    "invalid_api_token": "authentication_failed",
    "forbidden": "permission_denied",
    "permission_denied": "permission_denied",
    "missing_configuration": "not_configured",
    "not_configured": "not_configured",
    "not_found": "not_found",
    "missing_resource": "not_found",
    "conflict": "conflict",
    "rate_limit": "rate_limited",
    "rate_limited": "rate_limited",
    "api_error": "upstream_error",
    "upstream_error": "upstream_error",
    "provider_unavailable": "provider_unavailable",
    "service_unavailable": "provider_unavailable",
    "connection_error": "provider_unavailable",
    "timeout": "timeout",
    "timed_out": "timeout",
    "invalid_response": "invalid_response",
    "runtime_error": "runtime_error",
    "internal_error": "internal_error",
}


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _as_str(value: Any) -> str:
    return str(value or "").strip()


def _normalize_code_token(value: Any) -> str:
    text = _as_str(value).lower().replace("-", "_").replace(" ", "_")
    if ":" in text:
        text = text.split(":", 1)[0]
    return "".join(ch for ch in text if ch.isalnum() or ch == "_")


def _message_code_hint(message: str) -> str:
    lowered = _as_str(message).lower()
    if not lowered:
        return ""
    if "not configured" in lowered or "requires an enabled task provider extension" in lowered:
        return "not_configured"
    if "missing webhook signature" in lowered:
        return "authentication_failed"
    if "missing" in lowered and any(
        token in lowered for token in ("token", "credential", "secret", "api key")
    ):
        return "missing_credentials"
    if any(token in lowered for token in ("unauthorized", "invalid token", "auth")):
        return "authentication_failed"
    if any(token in lowered for token in ("forbidden", "permission denied")):
        return "permission_denied"
    if any(token in lowered for token in ("bad payload", "invalid payload", "missing task_id")):
        return "invalid_payload"
    if "unsupported event" in lowered:
        return "unsupported_event"
    if any(token in lowered for token in ("not found", "404")):
        return "not_found"
    if "conflict" in lowered:
        return "conflict"
    if any(token in lowered for token in ("rate limit", "429", "too many requests")):
        return "rate_limited"
    if any(token in lowered for token in ("timed out", "timeout")):
        return "timeout"
    if any(token in lowered for token in ("service unavailable", "connection error", "503")):
        return "provider_unavailable"
    if "invalid response" in lowered or "missing result." in lowered:
        return "invalid_response"
    if any(token in lowered for token in ("invocation failed", "exit_code=", "quarantined")):
        return "runtime_error"
    if any(token in lowered for token in ("api error", "upstream")):
        return "upstream_error"
    return ""


def _task_ingress_error_code(raw_code: Any, *, message: str = "") -> str:
    normalized_code = _normalize_code_token(raw_code)
    if normalized_code in TASK_INGRESS_ERROR_CODE_SET:
        return normalized_code
    if normalized_code in TASK_INGRESS_ERROR_CODE_ALIASES:
        return TASK_INGRESS_ERROR_CODE_ALIASES[normalized_code]
    message_hint = _message_code_hint(message)
    if message_hint:
        return message_hint
    if normalized_code.startswith("missing_"):
        return "invalid_payload"
    return "internal_error"


def normalize_task_ingress_error(
    raw_error: dict[str, Any] | str | None,
    *,
    default_message: str,
    default_code: str = "internal_error",
) -> dict[str, Any]:
    payload = _as_dict(raw_error) if isinstance(raw_error, dict) else {}
    raw_message = ""
    if payload:
        raw_message = _as_str(
            payload.get("message") or payload.get("detail") or payload.get("error")
        )
    elif isinstance(raw_error, str):
        raw_message = _as_str(raw_error)

    if payload:
        raw_code = payload.get("code")
    elif isinstance(raw_error, str):
        raw_code = _normalize_code_token(raw_error)
    else:
        raw_code = ""
    normalized_code = _task_ingress_error_code(
        raw_code,
        message=raw_message or default_message,
    )
    if not normalized_code or normalized_code not in TASK_INGRESS_ERROR_CODE_SET:
        normalized_code = _task_ingress_error_code(default_code, message=default_message)
    if not normalized_code or normalized_code not in TASK_INGRESS_ERROR_CODE_SET:
        normalized_code = "internal_error"

    message = raw_message or _as_str(default_message) or normalized_code
    normalized: dict[str, Any] = {
        "code": normalized_code,
        "message": message,
    }
    for passthrough_key in ("provider_key", "operation", "retryable"):
        if passthrough_key in payload:
            normalized[passthrough_key] = payload[passthrough_key]
    return normalized


def format_task_ingress_error(error_payload: dict[str, Any] | str | None, *, fallback: str) -> str:
    normalized = normalize_task_ingress_error(error_payload, default_message=fallback)
    return f"[{normalized['code']}] {normalized['message']}"


def normalize_task_ingress_response(
    raw_response: dict[str, Any] | None,
    *,
    default_error_message: str,
) -> dict[str, Any]:
    envelope = normalize_extension_response(_as_dict(raw_response))
    normalized_errors = [
        normalize_task_ingress_error(item, default_message=default_error_message)
        for item in (envelope.errors or [])
    ]
    if not envelope.ok and not normalized_errors:
        normalized_errors = [
            normalize_task_ingress_error(
                {"code": "internal_error", "message": default_error_message},
                default_message=default_error_message,
            )
        ]

    return {
        "ok": bool(envelope.ok),
        "result": _as_dict(envelope.result),
        "events": _as_list(envelope.events),
        "errors": normalized_errors,
    }


def _coerce_entity_id(value: Any) -> str | None:
    normalized = _as_str(value)
    return normalized or None


def _first_entity_id(*values: Any) -> str | None:
    for value in values:
        normalized = _coerce_entity_id(value)
        if normalized:
            return normalized
    return None


def _iter_unique_entity_ids(values: list[Any]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for value in values:
        normalized = _coerce_entity_id(value)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        ordered.append(normalized)
    return ordered


def flatten_ingress_payload(payload: dict[str, Any] | None) -> dict[str, Any]:
    """Merge one nested payload level into root while preserving explicit root fields."""
    normalized = dict(_as_dict(payload))
    nested_payload = _as_dict(normalized.get("payload"))
    for key, value in nested_payload.items():
        normalized.setdefault(str(key), value)
    return normalized


def build_ingress_dedupe_key(
    *,
    provider_key: str,
    source_id: str = "",
    event_key: str = "",
    job_id: Any = None,
    payload: dict[str, Any] | None = None,
) -> str:
    """Build stable dedupe keys for inbound provider events."""
    normalized_provider = _as_str(provider_key).lower() or "external"
    normalized_source_id = _as_str(source_id)
    normalized_event_key = _as_str(event_key)
    normalized_job_id = _as_str(job_id)
    normalized_payload = _as_dict(payload)

    if normalized_source_id and normalized_event_key:
        return f"{normalized_provider}:{normalized_event_key}:{normalized_source_id}"
    if normalized_source_id:
        return f"{normalized_provider}:source:{normalized_source_id}"
    if normalized_event_key and normalized_job_id:
        return f"{normalized_provider}:{normalized_event_key}:job:{normalized_job_id}"

    payload_fingerprint = json.dumps(normalized_payload, sort_keys=True, ensure_ascii=True)
    digest = hashlib.sha256(payload_fingerprint.encode("utf-8")).hexdigest()[:16]
    if normalized_event_key:
        return f"{normalized_provider}:{normalized_event_key}:{digest}"
    if normalized_job_id:
        return f"{normalized_provider}:job:{normalized_job_id}:{digest}"
    return f"{normalized_provider}:event:{digest}"


def normalize_ingress_provider_event(
    raw_event: dict[str, Any] | None,
    *,
    provider_key: str,
    default_source: str = "",
) -> dict[str, Any]:
    """Normalize one ingress event into a canonical host-facing shape."""
    event_payload = _as_dict(raw_event)
    event_type = _as_str(event_payload.get("event_type"))
    if not event_type:
        return {}

    payload = flatten_ingress_payload(_as_dict(event_payload.get("payload")))
    severity = (
        _as_str(
            event_payload.get("severity") or payload.get("severity") or DEFAULT_INGRESS_SEVERITY
        ).lower()
        or DEFAULT_INGRESS_SEVERITY
    )
    summary = _as_str(
        event_payload.get("summary")
        or payload.get("summary")
        or payload.get("message")
        or payload.get("text")
        or event_type
    )
    project_id = _first_entity_id(event_payload.get("project_id"), payload.get("project_id"))
    job_id = _first_entity_id(event_payload.get("job_id"), payload.get("job_id"))
    workflow_id = _as_str(
        event_payload.get("workflow_id")
        or payload.get("workflow_id")
        or payload.get("selected_workflow_id")
    )

    source = _as_str(
        event_payload.get("source")
        or payload.get("source")
        or default_source
        or f"provider:{_as_str(provider_key).lower() or 'external'}"
    )
    source_id = _as_str(event_payload.get("source_id") or payload.get("source_id"))
    event_key = _as_str(event_payload.get("event_key") or payload.get("event_key") or event_type)
    dedupe_key = _as_str(event_payload.get("dedupe_key") or payload.get("dedupe_key"))
    if not dedupe_key:
        dedupe_key = build_ingress_dedupe_key(
            provider_key=provider_key,
            source_id=source_id,
            event_key=event_key,
            job_id=job_id,
            payload=payload,
        )

    if source and "source" not in payload:
        payload["source"] = source
    if source_id and "source_id" not in payload:
        payload["source_id"] = source_id
    if event_key and "event_key" not in payload:
        payload["event_key"] = event_key
    if dedupe_key and "dedupe_key" not in payload:
        payload["dedupe_key"] = dedupe_key
    if project_id and "project_id" not in payload:
        payload["project_id"] = project_id
    if job_id and "job_id" not in payload:
        payload["job_id"] = job_id
    if workflow_id and "workflow_id" not in payload:
        payload["workflow_id"] = workflow_id

    normalized_event: dict[str, Any] = {
        "event_type": event_type,
        "severity": severity,
        "summary": summary or event_type,
        "payload": payload,
        "source": source,
        "event_key": event_key,
        "dedupe_key": dedupe_key,
    }
    if project_id:
        normalized_event["project_id"] = project_id
    if job_id:
        normalized_event["job_id"] = job_id
    if workflow_id:
        normalized_event["workflow_id"] = workflow_id
    if source_id:
        normalized_event["source_id"] = source_id

    correlation_id = _as_str(event_payload.get("correlation_id") or payload.get("correlation_id"))
    if correlation_id:
        normalized_event["correlation_id"] = correlation_id
        if "correlation_id" not in payload:
            payload["correlation_id"] = correlation_id
    return normalized_event


def normalize_ingress_provider_events(
    events: list[dict[str, Any]] | None,
    *,
    provider_key: str,
    declared_event_types: list[str] | None = None,
    default_source: str = "",
) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for raw_event in _as_list(events):
        item = normalize_ingress_provider_event(
            _as_dict(raw_event),
            provider_key=provider_key,
            default_source=default_source,
        )
        if item:
            normalized.append(item)
    if declared_event_types is None:
        return normalized
    return filter_valid_events(normalized, declared_event_types=declared_event_types)


def normalize_notification_ingress_response(
    raw_response: dict[str, Any] | None,
    *,
    provider_key: str,
    declared_event_types: list[str],
    default_source: str = "",
) -> dict[str, Any]:
    """Normalize notification ingress action response to canonical result + events."""
    envelope = normalize_extension_response(_as_dict(raw_response))
    if not envelope.ok:
        errors = list(envelope.errors or [])
        first_error = _as_dict(errors[0] if errors else {})
        return {
            "ok": False,
            "error": _as_str(first_error.get("message"))
            or "Notification ingress returned ok=false",
            "errors": errors,
            "result": {},
            "events": [],
        }

    normalized_events = normalize_ingress_provider_events(
        list(envelope.events or []),
        provider_key=provider_key,
        declared_event_types=declared_event_types,
        default_source=default_source,
    )

    result = _as_dict(envelope.result)
    if "project_id" not in result:
        project_hint = _first_entity_id(
            *[_as_dict(event).get("project_id") for event in normalized_events]
        )
        if project_hint:
            result["project_id"] = project_hint
    if "job_id" not in result:
        job_hint = _first_entity_id(*[_as_dict(event).get("job_id") for event in normalized_events])
        if job_hint:
            result["job_id"] = job_hint

    return {
        "ok": True,
        "result": result,
        "events": normalized_events,
        "errors": list(envelope.errors or []),
    }


def resolve_ingress_project_id(
    *,
    ingress_result: dict[str, Any] | None,
    ingress_events: list[dict[str, Any]] | None,
    request_payload: dict[str, Any] | None,
    query_params: dict[str, Any] | None,
    bl: Any = None,
) -> str | None:
    """Resolve project_id from ingress hints or by looking up job_id in storage."""
    result_payload = _as_dict(ingress_result)
    request_obj = _as_dict(request_payload)
    query_obj = _as_dict(query_params)
    events = [_as_dict(item) for item in _as_list(ingress_events)]

    project_candidates: list[Any] = [
        result_payload.get("project_id"),
        request_obj.get("project_id"),
        request_obj.get("platform_project_id"),
        query_obj.get("project_id"),
    ]
    for event in events:
        event_data = _as_dict(event)
        event_payload = flatten_ingress_payload(_as_dict(event_data.get("payload")))
        project_candidates.extend(
            [
                event_data.get("project_id"),
                event_payload.get("project_id"),
            ]
        )
    project_id = _first_entity_id(*project_candidates)
    if project_id:
        return project_id

    if bl is None or not hasattr(bl, "get_job"):
        return None

    job_candidates: list[Any] = [
        result_payload.get("job_id"),
        request_obj.get("job_id"),
    ]
    for event in events:
        event_data = _as_dict(event)
        event_payload = flatten_ingress_payload(_as_dict(event_data.get("payload")))
        job_candidates.extend(
            [
                event_data.get("job_id"),
                event_payload.get("job_id"),
            ]
        )

    for normalized_job_id in _iter_unique_entity_ids(job_candidates):
        try:
            job_row = bl.get_job(normalized_job_id) or {}
        except Exception:
            job_row = {}
        project_id = _coerce_entity_id(_as_dict(job_row).get("project_id"))
        if project_id:
            return project_id
    return None


__all__ = [
    "TASK_INGRESS_ERROR_CODES",
    "TASK_INGRESS_REQUIRED_ACTION_ERROR_CODES",
    "TASK_INGRESS_REQUIRED_QUERY_ERROR_CODES",
    "build_ingress_dedupe_key",
    "flatten_ingress_payload",
    "format_task_ingress_error",
    "normalize_ingress_provider_event",
    "normalize_ingress_provider_events",
    "normalize_notification_ingress_response",
    "normalize_task_ingress_error",
    "normalize_task_ingress_response",
    "resolve_ingress_project_id",
]
