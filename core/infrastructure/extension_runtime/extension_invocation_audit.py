"""Bounded audit projection and persistence for host extension invocations."""

from __future__ import annotations

import json
import logging
from typing import Any

from core.infrastructure.extension_runtime.extension_runtime_helpers import (
    _to_json_obj,
    extension_identifier,
)

logger = logging.getLogger(__name__)


_AUDIT_REQUEST_CONTEXT_KEYS = (
    "trace_id",
    "correlation_id",
    "event_id",
    "parent_span_id",
    "span_id",
    "project_id",
    "job_id",
    "tool_id",
    "function_name",
    "source",
    "connector_install_id",
    "command_type",
)


def _first_present(*values: Any) -> Any:
    for value in values:
        if value is None:
            continue
        if isinstance(value, str):
            normalized = value.strip()
            if not normalized:
                continue
            return normalized
        return value
    return None


def _optional_text(*values: Any) -> str | None:
    value = _first_present(*values)
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _optional_int(*values: Any) -> int | None:
    value = _first_present(*values)
    if value is None:
        return None
    try:
        normalized = int(value)
    except (TypeError, ValueError):
        return None
    return normalized


def _audit_code(value: Any) -> str:
    text = value if isinstance(value, str) else ""
    if not 0 < len(text) <= 128:
        return ""
    return text if all(char.islower() or char.isdigit() or char in "._-" for char in text) else ""


def _audit_identifier(value: Any) -> str | int:
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if not isinstance(value, str) or not 0 < len(value) <= 512:
        return ""
    return value if all(32 <= ord(char) < 127 for char in value) else ""


def _audit_result(value: Any) -> Any:
    if isinstance(value, list):
        return [_audit_result(item) for item in value[:1000] if _audit_result(item) not in ({}, [])]
    if not isinstance(value, dict):
        return {}
    projected: dict[str, Any] = {}
    for key, raw in value.items():
        normalized_key = str(key or "").strip().lower()
        if normalized_key in {"code", "error_code", "status"}:
            code = _audit_code(raw)
            if code:
                projected[normalized_key] = code
        elif normalized_key.endswith("_id") or normalized_key == "id":
            identifier = _audit_identifier(raw)
            if identifier:
                projected[normalized_key] = identifier
        elif normalized_key.endswith("_count") or normalized_key in {
            "count",
            "size",
            "total",
            "definitely_no_external_effect",
        }:
            if isinstance(raw, bool) or (isinstance(raw, int) and not isinstance(raw, bool)):
                projected[normalized_key] = raw
        elif isinstance(raw, (dict, list)):
            nested = _audit_result(raw)
            if nested not in ({}, []):
                projected[normalized_key] = nested
    return projected


def _audit_dataset_page(value: Any) -> dict[str, Any]:
    row = value if isinstance(value, dict) else {}
    handle = str(row.get("dataset_handle") or "").strip()
    if handle.startswith("dataset:"):
        handle = handle.removeprefix("dataset:").strip()
    if not handle.startswith("dst_") or len(handle) > 512:
        return {}
    page_ordinal = row.get("page_ordinal")
    item_count = row.get("item_count")
    if (
        isinstance(page_ordinal, bool)
        or not isinstance(page_ordinal, int)
        or page_ordinal < 0
        or isinstance(item_count, bool)
        or not isinstance(item_count, int)
        or not 0 <= item_count <= 1000
    ):
        return {}
    digest = str(row.get("digest") or "").strip().lower()
    if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
        return {}
    return {
        "dataset_handle": handle,
        "page_ordinal": page_ordinal,
        "item_count": item_count,
        "digest": digest,
    }


def _audit_dataset_counts(value: Any) -> dict[str, int]:
    from core.infrastructure.security.bounded_audit import project_count_fields

    return project_count_fields(value)


def _project_extension_invocation_audit(
    request: Any,
    invocation: Any,
    *,
    dataset_page: bool = False,
) -> tuple[dict[str, Any], dict[str, Any], str | None]:
    raw_request = request if isinstance(request, dict) else {}
    projected_request: dict[str, Any] = {}
    context = raw_request.get("context")
    if isinstance(context, dict):
        project_id = _audit_identifier(context.get("project_id"))
        if project_id:
            projected_request["context"] = {"project_id": project_id}
    target = raw_request.get("target")
    if isinstance(target, dict):
        connection_id = _audit_identifier(target.get("connection_id"))
        if connection_id:
            projected_request["target"] = {"connection_id": connection_id}
    input_payload = raw_request.get("input")
    if isinstance(input_payload, dict):
        safe_input: dict[str, Any] = {}
        for key, raw in input_payload.items():
            normalized_key = str(key or "").strip()
            if normalized_key != "connection_ref" and not normalized_key.endswith("_id"):
                continue
            identifier = _audit_identifier(raw)
            if identifier:
                safe_input[normalized_key] = identifier
        if safe_input:
            projected_request["input"] = safe_input

    raw_invocation = invocation if isinstance(invocation, dict) else {}
    status = _audit_code(str(raw_invocation.get("status") or "").lower()) or "unknown"
    projected_invocation: dict[str, Any] = {"status": status}
    response = raw_invocation.get("response")
    error_code = ""
    if isinstance(response, dict):
        projected_response: dict[str, Any] = {}
        if isinstance(response.get("ok"), bool):
            projected_response["ok"] = response["ok"]
        error_code = _audit_code(response.get("error_code"))
        if error_code:
            projected_response["error_code"] = error_code
        errors = response.get("errors")
        if isinstance(errors, list):
            if dataset_page:
                from core.infrastructure.security.bounded_audit import (
                    project_actionable_errors,
                )

                safe_errors = project_actionable_errors(errors, limit=10)
            else:
                safe_errors = [
                    {"code": code}
                    for item in errors
                    if isinstance(item, dict) and (code := _audit_code(item.get("code")))
                ]
            if safe_errors:
                projected_response["errors"] = safe_errors[: 10 if dataset_page else 1000]
        result = (
            _audit_dataset_counts(response.get("result"))
            if dataset_page
            else _audit_result(response.get("result"))
        )
        if result:
            projected_response["result"] = result
        if projected_response:
            projected_invocation["response"] = projected_response
    return projected_request, projected_invocation, error_code or None


def log_host_extension_invocation(
    *,
    bl: Any,
    extension: dict | None,
    extension_id: str,
    invocation_type: str,
    request: dict | None,
    invocation: dict | None,
    runtime_context: dict | None,
    request_context: dict | None = None,
    query_id: str | None = None,
    action_id: str | None = None,
    command_id: str | None = None,
    external_effect_id: str | None = None,
) -> None:
    if bl is None or not hasattr(bl, "log_extension_invocation"):
        return

    normalized_invocation = _to_json_obj(invocation) or {}
    normalized_runtime_context = _to_json_obj(runtime_context) or {}
    audit_context = _to_json_obj(normalized_invocation.get("audit_context")) or {}
    explicit_request_context = _to_json_obj(request_context) or {}

    normalized_request_context = dict(explicit_request_context)
    for key in _AUDIT_REQUEST_CONTEXT_KEYS:
        value = _first_present(
            explicit_request_context.get(key),
            normalized_runtime_context.get(key),
            audit_context.get(key),
        )
        if value is not None:
            normalized_request_context[key] = value
    dataset_page = _audit_dataset_page(normalized_request_context.get("dataset_page"))
    if dataset_page:
        normalized_request_context = {
            key: value
            for key in _AUDIT_REQUEST_CONTEXT_KEYS
            if (value := _audit_identifier(normalized_request_context.get(key))) not in (None, "")
        }
        normalized_request_context["dataset_page"] = dataset_page
    else:
        normalized_request_context.pop("dataset_page", None)

    status_value = str(normalized_invocation.get("status") or "").strip().lower()
    if status_value == "succeeded":
        normalized_status = "ok"
    elif status_value:
        normalized_status = "failed"
    else:
        normalized_status = "unknown"

    extension_key = extension_identifier(extension) or str(extension_id or "").strip()
    logged_request = _to_json_obj(request) or {}
    logged_invocation = normalized_invocation
    logged_error_text = _optional_text(
        normalized_invocation.get("detail"),
        normalized_invocation.get("error"),
    )
    logged_request, logged_invocation, logged_error_text = _project_extension_invocation_audit(
        logged_request,
        normalized_invocation,
        dataset_page=bool(dataset_page),
    )
    if dataset_page:
        audit_envelope = {
            "request": logged_request,
            "response": logged_invocation,
            "request_context": normalized_request_context,
        }
        if (
            len(
                json.dumps(
                    audit_envelope,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            )
            > 16 * 1024
        ):
            logged_request = {}
            logged_invocation = {
                "status": str(logged_invocation.get("status") or "unknown"),
                "response": {"error_code": "audit_projection_too_large"},
            }
            logged_error_text = "audit_projection_too_large"
    try:
        bl.log_extension_invocation(
            extension_key=extension_key,
            invocation_type=str(invocation_type or "").strip() or "invoke",
            status=normalized_status,
            request=logged_request,
            response=logged_invocation,
            error_text=logged_error_text,
            account_id=_optional_text(
                normalized_runtime_context.get("account_id"),
                audit_context.get("account_id"),
                explicit_request_context.get("account_id"),
            ),
            actor_type=_optional_text(
                normalized_runtime_context.get("actor_type"),
                audit_context.get("actor_type"),
                explicit_request_context.get("actor_type"),
            ),
            actor_id=_optional_text(
                normalized_runtime_context.get("actor_id"),
                audit_context.get("actor_id"),
                explicit_request_context.get("actor_id"),
            ),
            request_context=normalized_request_context,
            query_id=_optional_text(
                query_id,
                normalized_runtime_context.get("query_id"),
                audit_context.get("query_id"),
                explicit_request_context.get("query_id"),
            ),
            action_id=_optional_text(
                action_id,
                normalized_runtime_context.get("action_id"),
                audit_context.get("action_id"),
                explicit_request_context.get("action_id"),
            ),
            command_id=_optional_text(
                command_id,
                normalized_runtime_context.get("command_id"),
                audit_context.get("command_id"),
                explicit_request_context.get("command_id"),
            ),
            external_effect_id=_optional_text(
                external_effect_id,
                normalized_runtime_context.get("external_effect_id"),
                audit_context.get("external_effect_id"),
                explicit_request_context.get("external_effect_id"),
            ),
            job_id=_optional_text(
                normalized_runtime_context.get("job_id"),
                audit_context.get("job_id"),
            ),
            workflow_id=_optional_text(
                normalized_runtime_context.get("workflow_id"),
                audit_context.get("workflow_id"),
            ),
            group_id=_optional_text(
                normalized_runtime_context.get("group_id"),
                audit_context.get("group_id"),
            ),
            for_each_id=_optional_text(
                normalized_runtime_context.get("for_each_id"),
                audit_context.get("for_each_id"),
            ),
            step_id=_optional_text(
                normalized_runtime_context.get("step_id"),
                audit_context.get("step_id"),
            ),
            step_execution_id=_optional_text(
                normalized_runtime_context.get("step_execution_id"),
                audit_context.get("step_execution_id"),
            ),
        )
    except Exception as exc:
        logger.warning(
            "extension invocation audit persistence failed: extension=%s type=%s error=%s",
            extension_identifier(extension) or str(extension_id or "").strip(),
            str(invocation_type or "").strip() or "invoke",
            exc,
        )
