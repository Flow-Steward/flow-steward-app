"""Notification contracts using installed provider extensions."""

from __future__ import annotations

import fnmatch
import uuid
from typing import Any

from core.infrastructure.config.platform_identity import DEFAULT_ACCOUNT_ID
from core.infrastructure.runtime.test_mode import is_test_mode_enabled, parse_test_mode

_SEVERITY_ORDER = {
    "debug": 10,
    "info": 20,
    "warning": 30,
    "error": 40,
    "critical": 50,
}


def _as_dict(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def _as_str(value: Any) -> str:
    return str(value or "").strip()


def _notification_delivery_fields(invocation: dict[str, Any]) -> tuple[dict, bool, str, str]:
    response = _as_dict(invocation.get("response"))
    result = _as_dict(response.get("result"))
    ok = bool(result.get("ok", response.get("ok", True)))
    delivered = bool(result.get("delivered", response.get("delivered", ok)))
    error = _as_str(result.get("error") or response.get("error"))
    provider_message_id = _as_str(
        result.get("provider_message_id") or response.get("provider_message_id")
    )
    return response, delivered, error, provider_message_id


def _normalize_notification_delivery_invocation(
    invocation: dict[str, Any],
) -> dict[str, Any]:
    if _as_str(invocation.get("status")).lower() != "succeeded":
        return invocation
    response = _as_dict(invocation.get("response"))
    result = _as_dict(response.get("result"))
    delivered_value = result.get("delivered", response.get("delivered"))
    if delivered_value is not False:
        return invocation
    explicit_effect_status = _as_str(
        result.get("external_effect_status") or response.get("external_effect_status")
    ).lower()
    definitive_values = [
        payload.get("definitely_no_external_effect")
        for payload in (response, result)
        if "definitely_no_external_effect" in payload
    ]
    definitive_no_effect = bool(definitive_values) and all(
        value is True for value in definitive_values
    )
    definitive_failure = definitive_no_effect and explicit_effect_status in {"", "failed"}
    effect_status = "failed" if definitive_failure else "timeout_unknown"
    code = "notification_not_delivered" if definitive_failure else "external_effect_timeout_unknown"
    normalized_response = {
        **response,
        "external_effect_status": effect_status,
        "error_code": code,
    }
    if definitive_failure:
        normalized_response["definitely_no_external_effect"] = True
    return {
        **invocation,
        "status": "failed",
        "detail": code,
        "response": normalized_response,
        "errors": [{"code": code}],
    }


def _trace_id_for_job(job_id: Any) -> str | None:
    normalized = _as_str(job_id)
    return f"job:{normalized}" if normalized else None


def _normalize_severity(value: Any, *, default: str = "info") -> str:
    normalized = _as_str(value).lower()
    if normalized in _SEVERITY_ORDER:
        return normalized
    return default


def _uuid_text_or_none(value: Any) -> str | None:
    normalized = _as_str(value)
    if not normalized:
        return None
    try:
        return str(uuid.UUID(normalized))
    except Exception:
        return None


def _severity_allows(route_severity: str, event_severity: str) -> bool:
    normalized_route = _normalize_severity(route_severity, default="any")
    if normalized_route == "any":
        return True
    route_rank = _SEVERITY_ORDER.get(normalized_route, _SEVERITY_ORDER["info"])
    event_rank = _SEVERITY_ORDER.get(_normalize_severity(event_severity), _SEVERITY_ORDER["info"])
    return event_rank >= route_rank


def _event_type_matches(route_event_type: str, event_type: str) -> bool:
    candidate = _as_str(route_event_type)
    normalized_event = _as_str(event_type)
    if not candidate or candidate == "*" or candidate == "any":
        return True
    if "*" in candidate or "?" in candidate:
        return fnmatch.fnmatch(normalized_event, candidate)
    return candidate == normalized_event


def _render_notification_message(event: dict, route: dict) -> str:
    payload = _as_dict(event.get("payload"))
    explicit = _as_str(payload.get("message"))
    if explicit:
        return explicit
    event_type = _as_str(event.get("event_type")) or "event"
    severity = _normalize_severity(event.get("severity"))
    job_id = event.get("job_id")
    workflow_id = _as_str(event.get("workflow_id"))
    summary = _as_str(payload.get("summary") or payload.get("text") or payload.get("detail"))
    headline = f"[{severity.upper()}] {event_type}"
    parts = [headline]
    if job_id is not None:
        parts.append(f"job_id={job_id}")
    if workflow_id:
        parts.append(f"workflow_id={workflow_id}")
    if summary:
        parts.append(summary)
    route_cfg = _as_dict(route.get("route_config"))
    suffix = _as_str(route_cfg.get("message_suffix"))
    if suffix:
        parts.append(suffix)
    return "\n".join(parts)


def _is_test_mode_dispatch(bl, event: dict) -> bool:
    """Single safety gate for notification dispatch in test_mode."""
    if is_test_mode_enabled():
        return True
    payload = _as_dict(event.get("payload"))
    nested_payload = _as_dict(payload.get("payload"))
    if parse_test_mode(event.get("test_mode"), False):
        return True
    if parse_test_mode(payload.get("test_mode"), False):
        return True
    if parse_test_mode(nested_payload.get("test_mode"), False):
        return True

    job_id = event.get("job_id")
    if not job_id or bl is None or not hasattr(bl, "get_job"):
        return False
    try:
        job_row = bl.get_job(str(job_id))
    except Exception:
        return False
    meta = _as_dict((job_row or {}).get("meta"))
    return parse_test_mode(meta.get("test_mode"), False)


def _resolve_authoritative_account_id(
    bl, project_id: Any, explicit_account_id: str | None = None
) -> str:
    normalized_account_id = _as_str(explicit_account_id)
    if normalized_account_id:
        return normalized_account_id
    if bl is not None and hasattr(bl, "get_project"):
        try:
            project = bl.get_project(str(project_id)) or {}
        except Exception:
            project = {}
        normalized_account_id = _as_str((project or {}).get("account_id"))
        if normalized_account_id:
            return normalized_account_id
    return DEFAULT_ACCOUNT_ID
