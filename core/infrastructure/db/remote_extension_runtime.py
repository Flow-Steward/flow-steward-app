"""Remote extension runtime adapter for remote_https installs."""

from __future__ import annotations

import logging
from typing import Any

from core.application.extensions.host_contract import (
    EXECUTION_SCOPE_FIELDS,
    HOST_CONTRACT_VERSION,
    handshake_contract_error,
)
from core.application.extensions.remote_runtime_protocol_v2 import (
    RemoteRuntimeClient,
    build_action_envelope,
    build_event_envelope,
    build_query_envelope,
    validate_required_remote_endpoints,
)

_DEFAULT_REMOTE_ENDPOINTS: dict[str, str] = {
    "handshake": "/handshake",
    "health": "/health",
    "query": "/queries/{query_id}",
    "action": "/actions/{action_id}",
    "emit_event": "/events",
}
_SAFE_REMOTE_ERROR_CATEGORIES = frozenset(
    {
        "authentication_error",
        "rate_limited",
        "timeout",
        "model_unavailable",
        "capability_mismatch",
        "invalid_response",
        "billing_quota_exceeded",
        "upstream_error",
    }
)
logger = logging.getLogger(__name__)


def _as_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    return {}


def _normalize_text(value: Any) -> str:
    return str(value or "").strip()


def is_remote_runtime_enabled(extension: dict[str, Any] | None) -> bool:
    payload = _as_dict(extension)
    return _normalize_text(payload.get("runtime_mode")).lower() == "remote_https"


def _remote_runtime_block(extension: dict[str, Any] | None) -> dict[str, Any]:
    payload = _as_dict(extension)
    runtime_block = _as_dict(payload.get("runtime"))
    if not runtime_block:
        compiled_manifest = _as_dict(payload.get("compiled_manifest"))
        runtime_block = _as_dict(compiled_manifest.get("runtime"))
    return _as_dict(runtime_block.get("remote"))


def _resolve_remote_base_url(extension: dict[str, Any] | None) -> str:
    payload = _as_dict(extension)
    remote = _remote_runtime_block(payload)
    return _normalize_text(payload.get("remote_base_url") or remote.get("base_url"))


def _resolve_remote_endpoints(extension: dict[str, Any] | None) -> dict[str, str]:
    payload = _as_dict(extension)
    remote = _remote_runtime_block(payload)
    descriptor_endpoints = _as_dict(remote.get("endpoints"))
    explicit_endpoints = _as_dict(payload.get("remote_endpoints"))
    merged: dict[str, str] = {}
    for key, default in _DEFAULT_REMOTE_ENDPOINTS.items():
        merged[key] = _normalize_text(
            explicit_endpoints.get(key) or descriptor_endpoints.get(key) or default
        )
    return merged


def _declared_contract_version(extension: dict[str, Any] | None) -> str:
    payload = _as_dict(extension)
    for source in (payload, _as_dict(payload.get("compiled_manifest"))):
        declared = _normalize_text(_as_dict(source.get("runtime")).get("contract_version"))
        if declared:
            return declared
    return ""


def _runtime_availability_error(extension: dict[str, Any] | None) -> str:
    payload = _as_dict(extension)
    declared = _declared_contract_version(payload)
    if declared != HOST_CONTRACT_VERSION:
        # The release declares another contract: never call it "to try".
        return "capability_mismatch"
    runtime_status = _normalize_text(payload.get("runtime_status")).lower()
    if runtime_status == "incompatible":
        return "model_unavailable"
    handshake_status = _normalize_text(payload.get("handshake_status")).lower()
    if handshake_status == "failed":
        return "model_unavailable"
    health_status = _normalize_text(payload.get("health_status")).lower()
    if health_status == "failed":
        return "model_unavailable"
    return ""


def safe_remote_error_category(*values: Any) -> str:
    normalized = [_normalize_text(value).lower() for value in values]
    for value in normalized:
        if value in _SAFE_REMOTE_ERROR_CATEGORIES:
            return value
    combined = " ".join(normalized)
    if "timed out" in combined or "timeout" in combined:
        return "timeout"
    if "rate limit" in combined or "status 429" in combined:
        return "rate_limited"
    if any(marker in combined for marker in ("status 401", "status 403", "unauthorized")):
        return "authentication_error"
    return "upstream_error"


def _build_remote_client(
    extension: dict[str, Any] | None,
    *,
    timeout_seconds: float | None = None,
) -> RemoteRuntimeClient:
    payload = _as_dict(extension)
    base_url = _resolve_remote_base_url(payload)
    if not base_url:
        raise ValueError("remote_base_url is required for remote runtime")
    from core.application.extensions.url_safety import assert_safe_remote_https_url

    base_url = assert_safe_remote_https_url(base_url, purpose="remote_base_url")
    endpoints = _resolve_remote_endpoints(payload)
    missing = validate_required_remote_endpoints(endpoints)
    if missing:
        raise ValueError(f"remote runtime endpoints missing: {', '.join(missing)}")
    timeout = 15.0
    if timeout_seconds is not None:
        try:
            candidate_timeout = float(timeout_seconds)
        except (TypeError, ValueError):
            timeout = 15.0
        else:
            if candidate_timeout <= 0:
                raise ValueError("timeout_seconds must be positive")
            timeout = candidate_timeout
    bearer_token = _normalize_text(payload.get("remote_bearer_token"))
    return RemoteRuntimeClient(
        base_url=base_url,
        endpoints=endpoints,
        bearer_token=bearer_token,
        timeout_seconds=timeout,
    )


def _scope_from_context(
    context: dict[str, Any] | None, runtime_context: dict[str, Any] | None
) -> dict[str, Any]:
    """The invocation scope; envelope builders put it in the host contract shape."""
    merged = _as_dict(context)
    for key in ("account_id", "project_id", "workflow_id", "job_id", *EXECUTION_SCOPE_FIELDS):
        if key not in merged and key in _as_dict(runtime_context):
            merged[key] = _as_dict(runtime_context).get(key)
    return merged


def _connection_from_extension(extension: dict[str, Any] | None) -> dict[str, Any]:
    payload = _as_dict(extension)
    connection = {
        "install_id": _normalize_text(payload.get("install_id")),
        "extension_install_id": _normalize_text(payload.get("extension_install_id")),
        "connection_id": _normalize_text(payload.get("connection_id")),
        "extension_id": _normalize_text(
            payload.get("extension_key") or payload.get("extension_id")
        ),
        "runtime_mode": _normalize_text(payload.get("runtime_mode")),
    }
    # Optional server-to-server connection config / credentials (e.g. an LLM
    # provider extension's upstream API key). Carried only when the host caller
    # explicitly sets it; never originates from untrusted input and is never
    # surfaced to the frontend.
    connection_config = _as_dict(payload.get("connection_config"))
    if connection_config:
        connection["connection_config"] = connection_config
    credentials = _as_dict(payload.get("connection_credentials"))
    if credentials:
        connection["credentials"] = credentials
    return connection


def _as_invocation(response: dict[str, Any], *, mode: str, operation_id: str) -> dict[str, Any]:
    ok = bool(response.get("ok"))
    error = _as_dict(response.get("error"))
    errors = response.get("errors") if isinstance(response.get("errors"), list) else []
    error_code = _normalize_text(
        error.get("code")
        or (errors[0].get("code") if errors and isinstance(errors[0], dict) else "")
    )
    error_message = _normalize_text(
        error.get("message")
        or error.get("detail")
        or response.get("detail")
        or (errors[0].get("message") if errors and isinstance(errors[0], dict) else "")
    )
    safe_error = safe_remote_error_category(error_code, error_message)
    if not ok:
        logger.warning(
            "Remote runtime invocation failed mode=%s operation_id=%s category=%s",
            mode,
            operation_id,
            safe_error,
        )
    return {
        "status": "succeeded" if ok else "failed",
        # Codes are deliberately constrained by the LLM runtime contract and
        # safe to surface to routing/verification.  Keep them intact across
        # the transport boundary so retry and UI policy can distinguish auth,
        # rate-limit, model-unavailable, and invalid-response failures.
        "detail": "" if ok else safe_error,
        "command": [],
        "bundle_root": "",
        "timeout_seconds": None,
        "exit_code": 0 if ok else 2,
        "response": response if ok else {"ok": False, "error": {"code": safe_error}},
        "stdout": "",
        "stderr": "",
        "handler_mode": "remote_https",
        "mode": mode,
        "operation_id": operation_id,
    }


def _failed_invocation(*, mode: str, operation_id: str, detail: str) -> dict[str, Any]:
    safe_error = safe_remote_error_category(detail)
    logger.warning(
        "Remote runtime request failed mode=%s operation_id=%s category=%s",
        mode,
        operation_id,
        safe_error,
    )
    return {
        "status": "failed",
        "detail": safe_error,
        "command": [],
        "bundle_root": "",
        "timeout_seconds": None,
        "exit_code": 2,
        "response": {},
        "stdout": "",
        "stderr": "",
        "handler_mode": "remote_https",
        "mode": mode,
        "operation_id": operation_id,
    }


def remote_handshake(
    extension: dict[str, Any] | None,
    *,
    payload: dict[str, Any] | None = None,
    timeout_seconds: int | None = None,
) -> dict[str, Any]:
    """Negotiate the host contract with a remote runtime.

    The request names :data:`HOST_CONTRACT_VERSION`; a response that does not
    confirm exactly that token fails with ``contract_compatible: False``, so the
    caller marks the install incompatible. Nothing retries with another token.
    """
    try:
        client = _build_remote_client(extension, timeout_seconds=timeout_seconds)
        response = client.handshake(_as_dict(payload))
    except Exception as exc:
        return _failed_invocation(mode="handshake", operation_id="handshake", detail=str(exc))
    invocation = _as_invocation(response, mode="handshake", operation_id="handshake")
    if invocation["status"] != "succeeded":
        return invocation
    contract_error = handshake_contract_error(response)
    if not contract_error:
        invocation["contract_version"] = HOST_CONTRACT_VERSION
        invocation["contract_compatible"] = True
        return invocation
    failed = _failed_invocation(
        mode="handshake", operation_id="handshake", detail="capability_mismatch"
    )
    failed["contract_compatible"] = False
    failed["contract_error"] = contract_error
    return failed


def _dispatch_contract_failure(
    extension: dict[str, Any] | None,
    *,
    mode: str,
    operation_id: str,
    timeout_seconds: float | None,
) -> dict[str, Any] | None:
    """Refuse to dispatch to a runtime that has not confirmed the host contract.

    An install whose recorded handshake is ``ok`` negotiated the contract when
    it was checked; any other state negotiates now, before the first operation
    or event leaves the host.
    """
    availability_error = _runtime_availability_error(extension)
    if availability_error:
        return _failed_invocation(mode=mode, operation_id=operation_id, detail=availability_error)
    if _normalize_text(_as_dict(extension).get("handshake_status")).lower() == "ok":
        return None
    handshake = remote_handshake(
        extension,
        timeout_seconds=int(timeout_seconds) if timeout_seconds else None,
    )
    if handshake.get("status") == "succeeded":
        return None
    detail = "capability_mismatch" if handshake.get("contract_compatible") is False else ""
    return _failed_invocation(
        mode=mode,
        operation_id=operation_id,
        detail=detail or str(handshake.get("detail") or "upstream_error"),
    )


def remote_health(
    extension: dict[str, Any] | None,
    *,
    timeout_seconds: int | None = None,
) -> dict[str, Any]:
    try:
        client = _build_remote_client(extension, timeout_seconds=timeout_seconds)
        response = client.health()
        return _as_invocation(response, mode="health", operation_id="health")
    except Exception as exc:
        return _failed_invocation(mode="health", operation_id="health", detail=str(exc))


def remote_query(
    extension: dict[str, Any] | None,
    *,
    query_id: str,
    query_payload: dict[str, Any] | None,
    context: dict[str, Any] | None = None,
    runtime_context: dict[str, Any] | None = None,
    timeout_seconds: float | None = None,
) -> dict[str, Any]:
    contract_failure = _dispatch_contract_failure(
        extension,
        mode="query",
        operation_id=_normalize_text(query_id),
        timeout_seconds=timeout_seconds,
    )
    if contract_failure is not None:
        return contract_failure

    operation_id = str(query_id or "").strip()
    try:
        client = _build_remote_client(extension, timeout_seconds=timeout_seconds)
        envelope = build_query_envelope(
            query_id=operation_id,
            query_payload=_as_dict(query_payload),
            scope=_scope_from_context(context, runtime_context),
            connection=_connection_from_extension(extension),
        )
        response = client.invoke_query(operation_id, envelope)
        return _as_invocation(response, mode="query", operation_id=operation_id)
    except Exception as exc:
        return _failed_invocation(mode="query", operation_id=operation_id, detail=str(exc))


def remote_action(
    extension: dict[str, Any] | None,
    *,
    action_id: str,
    input_payload: dict[str, Any] | None,
    context: dict[str, Any] | None = None,
    runtime_context: dict[str, Any] | None = None,
    timeout_seconds: float | None = None,
    artifact_inputs: list[dict[str, Any]] | None = None,
    artifact_outputs: list[dict[str, Any]] | None = None,
    dataset_inputs: list[dict[str, Any]] | None = None,
    dataset_outputs: list[dict[str, Any]] | None = None,
    idempotency_key: str = "",
) -> dict[str, Any]:
    contract_failure = _dispatch_contract_failure(
        extension,
        mode="action",
        operation_id=_normalize_text(action_id),
        timeout_seconds=timeout_seconds,
    )
    if contract_failure is not None:
        return contract_failure

    operation_id = str(action_id or "").strip()
    try:
        client = _build_remote_client(extension, timeout_seconds=timeout_seconds)
        envelope = build_action_envelope(
            action_id=operation_id,
            input_payload=_as_dict(input_payload),
            scope=_scope_from_context(context, runtime_context),
            connection=_connection_from_extension(extension),
            artifact_inputs=artifact_inputs,
            artifact_outputs=artifact_outputs,
            dataset_grants=[*list(dataset_inputs or []), *list(dataset_outputs or [])],
            idempotency_key=_normalize_text(idempotency_key),
        )
        response = client.invoke_action(operation_id, envelope)
        return _as_invocation(response, mode="action", operation_id=operation_id)
    except Exception as exc:
        return _failed_invocation(mode="action", operation_id=operation_id, detail=str(exc))


def remote_emit_event(
    extension: dict[str, Any] | None,
    *,
    event_name: str,
    payload: dict[str, Any] | None,
    context: dict[str, Any] | None = None,
    runtime_context: dict[str, Any] | None = None,
    timeout_seconds: int | None = None,
) -> dict[str, Any]:
    contract_failure = _dispatch_contract_failure(
        extension,
        mode="event",
        operation_id=_normalize_text(event_name),
        timeout_seconds=timeout_seconds,
    )
    if contract_failure is not None:
        return contract_failure

    operation_id = str(event_name or "").strip()
    try:
        client = _build_remote_client(extension, timeout_seconds=timeout_seconds)
        envelope = build_event_envelope(
            event_name=operation_id,
            payload=_as_dict(payload),
            scope=_scope_from_context(context, runtime_context),
            connection=_connection_from_extension(extension),
        )
        response = client.send_event(envelope)
        return _as_invocation(response, mode="event", operation_id=operation_id)
    except Exception as exc:
        return _failed_invocation(mode="event", operation_id=operation_id, detail=str(exc))
