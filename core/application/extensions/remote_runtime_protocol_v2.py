"""Remote runtime protocol helpers for Extension Platform v2."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.error import HTTPError
from urllib.parse import urljoin, urlparse
from urllib.request import HTTPRedirectHandler, Request

from core.application.extensions.host_contract import (
    HOST_CONTRACT_VERSION,
    _optional_scope_text,
    build_handshake_request,
    canonical_envelope_scope,
    normalize_execution_scope,
)
from core.application.orchestration.retry_policy import normalize_retry_signal
from core.infrastructure.config.runtime_env import runtime_env as _runtime_env
from core.infrastructure.config.size_limits import clamp_size_limit_bytes

OCTET_STREAM_VALUE = "application/octet-stream"

REQUIRED_ENDPOINTS: tuple[str, ...] = ("handshake", "health", "query", "action", "emit_event")
ARTIFACT_ACCESS_TRANSPORTS: frozenset[str] = frozenset(
    {"handle_only", "artifact_api", "presigned_url"}
)
ARTIFACT_ACCESS_MODES: frozenset[str] = frozenset({"read", "write", "read_write"})
CANONICAL_ARTIFACT_ERROR_CODES: frozenset[str] = frozenset(
    {
        "artifact_not_found",
        "artifact_access_denied",
        "artifact_grant_expired",
        "artifact_upload_missing",
        "artifact_upload_incomplete",
        "artifact_checksum_mismatch",
        "artifact_content_type_invalid",
        "artifact_size_limit_exceeded",
        "artifact_transport_unsupported",
    }
)
_DEFAULT_TTL_SECONDS = 600
_MAX_TTL_SECONDS = 3600
_MIN_TTL_SECONDS = 60
# Cap remote runtime HTTPS response bodies before decode / JSON parse so a hostile
# or compromised remote runtime cannot force unbounded in-memory work.
# Operator-tunable via env (read once at import; clamped to a safe range).
_MAX_REMOTE_RUNTIME_BODY_BYTES = clamp_size_limit_bytes(
    _runtime_env.get("FS_REMOTE_RUNTIME_RESPONSE_MAX_BYTES"),
    default=5 * 1024 * 1024,  # 5 MiB
    minimum=64 * 1024,  # 64 KiB
    maximum=256 * 1024 * 1024,  # 256 MiB
)
_SCOPED_FIELDS: tuple[str, ...] = (
    "account_id",
    "project_id",
    "workflow_id",
    "job_id",
    "run_id",
    "group_id",
    "for_each_id",
    "step_id",
    "step_execution_id",
)
# Grant fields whose explicit null is a binding, never a wildcard: a grant that
# carries ``group_id: null`` is bound to no Group (``for_each_id: null`` to no
# For each; both null is the root scope) and a grant that carries
# ``step_execution_id: null`` to no step execution (workflow level). Only an
# absent key in a broader host-issued grant leaves the field unbound. The other
# fields keep their existing rule (an empty value is unbound) so such a grant
# keeps its documented scope.
_NULL_BOUND_SCOPE_FIELDS: frozenset[str] = frozenset(
    {"group_id", "for_each_id", "step_execution_id"}
)


def validate_required_remote_endpoints(endpoints: dict[str, Any]) -> list[str]:
    missing: list[str] = []
    for key in REQUIRED_ENDPOINTS:
        value = str((endpoints or {}).get(key) or "").strip()
        if not value:
            missing.append(key)
    return missing


def build_artifact_handle(artifact_id: str) -> str:
    aid = str(artifact_id or "").strip()
    if not aid:
        return ""
    if aid.startswith("artifact:"):
        return aid
    return f"artifact:{aid}"


def build_artifact_access_session(
    *,
    api_base_url: str,
    bearer_token: str,
    expires_at: str = "",
    ttl_seconds: int = _DEFAULT_TTL_SECONDS,
    transport: str = "artifact_api",
    now: datetime | None = None,
) -> dict[str, Any]:
    normalized_transport = _normalize_transport(transport)
    if normalized_transport != "artifact_api":
        raise ValueError("artifact_access_session transport must be 'artifact_api'")
    base_url = str(api_base_url or "").strip()
    token = str(bearer_token or "").strip()
    if not base_url:
        raise ValueError("artifact_access_session.api_base_url is required")
    if not token:
        raise ValueError("artifact_access_session.bearer_token is required")
    expiry = str(expires_at or "").strip() or _expiry_from_ttl(ttl_seconds=ttl_seconds, now=now)
    return {
        "transport": "artifact_api",
        "api_base_url": base_url,
        "bearer_token": token,
        "expires_at": expiry,
    }


def build_artifact_access_descriptor(
    *,
    binding_key: str,
    role: str,
    artifact_id: str,
    artifact_handle: str = "",
    artifact_kind: str = "file",
    filename: str = "",
    content_type: str = OCTET_STREAM_VALUE,
    size_bytes: int | None = None,
    checksum_sha256: str = "",
    retention_class: str = "run_window",
    source_kind: str = "",
    source_ref: str = "",
    created_at: str = "",
    expires_at: str = "",
    transport: str = "handle_only",
    mode: str = "",
    access_expires_at: str = "",
    download_url: str = "",
    upload_url: str = "",
    required_headers: dict[str, Any] | None = None,
    platform_grant: dict[str, Any] | None = None,
    single_use: bool = False,
    max_size_bytes: int | None = None,
    scope: dict[str, Any] | None = None,
) -> dict[str, Any]:
    aid = str(artifact_id or "").strip()
    if not aid:
        raise ValueError("artifact_id is required")
    normalized_role = str(role or "").strip().lower() or "input"
    normalized_transport = _normalize_transport(transport)
    normalized_mode = _normalize_mode(mode, role=normalized_role)
    normalized_download = str(download_url or "").strip() or None
    normalized_upload = str(upload_url or "").strip() or None

    if normalized_transport != "presigned_url":
        normalized_download = None
        normalized_upload = None
    if not _mode_allows_read(normalized_mode):
        normalized_download = None
    if not _mode_allows_write(normalized_mode):
        normalized_upload = None

    headers = _normalize_required_headers(
        required_headers if normalized_transport == "presigned_url" else {}
    )
    normalized_size = _as_non_negative_int(size_bytes)
    normalized_max_size = _as_non_negative_int(max_size_bytes)
    if (
        normalized_max_size is None
        and normalized_size is not None
        and _mode_allows_read(normalized_mode)
    ):
        normalized_max_size = normalized_size

    descriptor = {
        "binding_key": str(binding_key or "").strip() or aid,
        "role": normalized_role,
        "artifact_id": aid,
        "artifact_handle": build_artifact_handle(artifact_handle or aid),
        "artifact_kind": str(artifact_kind or "file").strip() or "file",
        "filename": str(filename or "").strip(),
        "content_type": str(content_type or OCTET_STREAM_VALUE).strip() or OCTET_STREAM_VALUE,
        "retention_class": str(retention_class or "run_window").strip() or "run_window",
        "access": {
            "transport": normalized_transport,
            "mode": normalized_mode,
            "expires_at": str(access_expires_at or expires_at or "").strip() or None,
            "download_url": normalized_download,
            "upload_url": normalized_upload,
            "required_headers": headers,
            "platform_grant": dict(platform_grant or {}),
            "single_use": bool(single_use),
            "max_size_bytes": normalized_max_size,
        },
    }
    if normalized_size is not None:
        descriptor["size_bytes"] = normalized_size
    checksum = str(checksum_sha256 or "").strip()
    if checksum:
        descriptor["checksum_sha256"] = checksum
    if source_kind:
        descriptor["source_kind"] = str(source_kind).strip()
    if source_ref:
        descriptor["source_ref"] = str(source_ref).strip()
    if created_at:
        descriptor["created_at"] = str(created_at).strip()
    if expires_at:
        descriptor["expires_at"] = str(expires_at).strip()
    normalized_scope = _normalize_scope(scope)
    if normalized_scope:
        descriptor["scope"] = normalized_scope
    return descriptor


def build_action_envelope(
    *,
    action_id: str,
    input_payload: dict[str, Any],
    scope: dict[str, Any],
    connection: dict[str, Any],
    artifact_inputs: list[dict[str, Any]] | None = None,
    artifact_outputs: list[dict[str, Any]] | None = None,
    artifact_access_session: dict[str, Any] | None = None,
    dataset_grants: list[dict[str, Any]] | None = None,
    idempotency_key: str = "",
    enforce_immutable_artifacts: bool = True,
) -> dict[str, Any]:
    normalized_inputs = _normalize_artifacts(artifact_inputs or [], default_role="input")
    normalized_outputs = _normalize_artifacts(artifact_outputs or [], default_role="output")
    if enforce_immutable_artifacts:
        _assert_output_artifacts_are_new(inputs=normalized_inputs, outputs=normalized_outputs)

    envelope = {
        "contract_version": HOST_CONTRACT_VERSION,
        "kind": "action",
        "action_id": str(action_id or "").strip(),
        "input": dict(input_payload or {}),
        "scope": canonical_envelope_scope(scope),
        "connection": dict(connection or {}),
        "artifacts": {
            "inputs": normalized_inputs,
            "outputs": normalized_outputs,
        },
        "dataset_grants": list(dataset_grants or []),
        "idempotency_key": str(idempotency_key or "").strip(),
    }
    session = _normalize_artifact_access_session(artifact_access_session)
    if session:
        envelope["artifact_access_session"] = session
    return envelope


def build_query_envelope(
    *,
    query_id: str,
    query_payload: dict[str, Any],
    scope: dict[str, Any],
    connection: dict[str, Any],
    artifact_inputs: list[dict[str, Any]] | None = None,
    artifact_access_session: dict[str, Any] | None = None,
) -> dict[str, Any]:
    normalized_inputs = _normalize_artifacts(artifact_inputs or [], default_role="input")
    envelope = {
        "contract_version": HOST_CONTRACT_VERSION,
        "kind": "query",
        "query_id": str(query_id or "").strip(),
        "query": dict(query_payload or {}),
        "scope": canonical_envelope_scope(scope),
        "connection": dict(connection or {}),
        "artifacts": {
            "inputs": normalized_inputs,
            "outputs": [],
        },
    }
    session = _normalize_artifact_access_session(artifact_access_session)
    if session:
        envelope["artifact_access_session"] = session
    return envelope


def build_event_envelope(
    *,
    event_name: str,
    payload: dict[str, Any],
    scope: dict[str, Any],
    connection: dict[str, Any],
) -> dict[str, Any]:
    normalized_event_name = str(event_name or "").strip()
    return {
        "contract_version": HOST_CONTRACT_VERSION,
        "kind": "event",
        "event_name": normalized_event_name,
        "event_type": normalized_event_name,
        "payload": dict(payload or {}),
        "scope": canonical_envelope_scope(scope),
        "connection": dict(connection or {}),
    }


def build_artifact_access_descriptors(
    *,
    artifact_ids: list[str],
    read_url_factory: Callable[[str], str] | None,
    write_url_factory: Callable[[str], str] | None = None,
    ttl_seconds: int = 600,
    role: str = "input",
    binding_key_prefix: str = "artifact",
    transport: str = "",
    now: datetime | None = None,
    content_type: str = OCTET_STREAM_VALUE,
    retention_class: str = "run_window",
    max_size_bytes: int | None = None,
    scope: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    descriptors: list[dict[str, Any]] = []
    ttl = _normalize_ttl(ttl_seconds)
    if transport:
        default_transport = _normalize_transport(transport)
    elif read_url_factory is not None or write_url_factory is not None:
        default_transport = "presigned_url"
    else:
        default_transport = "handle_only"
    expires_at = _expiry_from_ttl(ttl_seconds=ttl, now=now)
    for index, artifact_id in enumerate(artifact_ids):
        aid = str(artifact_id or "").strip()
        if not aid:
            continue
        read_url = str(read_url_factory(aid) or "").strip() if read_url_factory is not None else ""
        write_url = (
            str(write_url_factory(aid) or "").strip() if write_url_factory is not None else ""
        )
        mode = _detect_mode(download_url=read_url, upload_url=write_url, role=role)
        active_transport = default_transport
        if active_transport == "presigned_url" and not read_url and not write_url:
            active_transport = "handle_only"
        descriptor = build_artifact_access_descriptor(
            binding_key=f"{binding_key_prefix}_{index + 1}",
            role=role,
            artifact_id=aid,
            content_type=content_type,
            retention_class=retention_class,
            transport=active_transport,
            mode=mode,
            access_expires_at=expires_at,
            download_url=read_url,
            upload_url=write_url,
            single_use=bool(write_url and not read_url),
            max_size_bytes=max_size_bytes,
            scope=scope,
        )
        descriptor["ttl_seconds"] = ttl
        descriptors.append(descriptor)
    return descriptors


def canonical_artifact_error(
    code: str,
    message: str,
    *,
    artifact_id: str = "",
    transport: str = "",
) -> dict[str, Any]:
    normalized_code = str(code or "").strip()
    if normalized_code not in CANONICAL_ARTIFACT_ERROR_CODES:
        normalized_code = "artifact_transport_unsupported"
    payload = {
        "code": normalized_code,
        "message": str(message or "").strip() or normalized_code,
    }
    if artifact_id:
        payload["artifact_id"] = str(artifact_id).strip()
    if transport:
        payload["transport"] = str(transport).strip()
    return payload


def validate_artifact_descriptor_access(
    descriptor: dict[str, Any],
    *,
    scope: dict[str, Any] | None = None,
    expected_mode: str = "",
    now: datetime | None = None,
) -> dict[str, Any]:
    artifact_id = str((descriptor or {}).get("artifact_id") or "").strip()
    if not artifact_id:
        return {
            "ok": False,
            "error": canonical_artifact_error(
                "artifact_not_found", "artifact_id is missing in descriptor"
            ),
        }
    access = dict((descriptor or {}).get("access") or {})
    transport = str(access.get("transport") or "handle_only").strip().lower()
    if transport not in ARTIFACT_ACCESS_TRANSPORTS:
        return {
            "ok": False,
            "error": canonical_artifact_error(
                "artifact_transport_unsupported",
                f"Unsupported artifact transport '{transport}'",
                artifact_id=artifact_id,
                transport=transport,
            ),
        }

    expires_at = str(access.get("expires_at") or "").strip()
    if expires_at:
        parsed_expiry = _parse_timestamp(expires_at)
        current_now = _coerce_now(now)
        if parsed_expiry is not None and parsed_expiry <= current_now:
            return {
                "ok": False,
                "error": canonical_artifact_error(
                    "artifact_grant_expired",
                    "Artifact grant has expired",
                    artifact_id=artifact_id,
                    transport=transport,
                ),
            }

    grant_scope = _normalize_scope((descriptor or {}).get("scope"))
    request_scope = _normalize_scope(scope)
    for field in _SCOPED_FIELDS:
        if field not in grant_scope:
            continue
        if field in _NULL_BOUND_SCOPE_FIELDS:
            # Explicit null binds the root (or workflow-level) scope; a request
            # that omits or nulls the field is the root scope, so it cannot
            # widen a grant bound to a real group or execution.
            expected = grant_scope.get(field)
            actual = request_scope.get(field)
        else:
            expected = str(grant_scope.get(field) or "").strip()
            if not expected:
                continue
            actual = str(request_scope.get(field) or "").strip()
        if actual != expected:
            return {
                "ok": False,
                "error": canonical_artifact_error(
                    "artifact_access_denied",
                    f"Artifact scope mismatch for '{field}'",
                    artifact_id=artifact_id,
                    transport=transport,
                ),
            }

    normalized_mode = _normalize_mode(
        str(access.get("mode") or "read"), role=str(descriptor.get("role") or "input")
    )
    required_mode = str(expected_mode or "").strip().lower()
    if required_mode:
        if required_mode not in ARTIFACT_ACCESS_MODES:
            return {
                "ok": False,
                "error": canonical_artifact_error(
                    "artifact_transport_unsupported",
                    f"Unsupported expected_mode '{required_mode}'",
                    artifact_id=artifact_id,
                    transport=transport,
                ),
            }
        if required_mode == "read" and not _mode_allows_read(normalized_mode):
            return {
                "ok": False,
                "error": canonical_artifact_error(
                    "artifact_access_denied",
                    "Read access is not granted for artifact",
                    artifact_id=artifact_id,
                    transport=transport,
                ),
            }
        if required_mode == "write" and not _mode_allows_write(normalized_mode):
            return {
                "ok": False,
                "error": canonical_artifact_error(
                    "artifact_access_denied",
                    "Write access is not granted for artifact",
                    artifact_id=artifact_id,
                    transport=transport,
                ),
            }
        if required_mode == "read_write" and normalized_mode != "read_write":
            return {
                "ok": False,
                "error": canonical_artifact_error(
                    "artifact_access_denied",
                    "Read/write access is not granted for artifact",
                    artifact_id=artifact_id,
                    transport=transport,
                ),
            }
    return {"ok": True}


def build_artifact_audit_record(
    *,
    account_id: str,
    project_id: str | int,
    workflow_id: str,
    run_id: str,
    group_id: str | None,
    for_each_id: str | None,
    step_id: str | None,
    step_execution_id: str | None,
    extension_id: str,
    install_id: str,
    operation_id: str,
    artifact_id: str,
    grant_mode: str,
    grant_transport: str,
    issued_at: str,
    expires_at: str,
    used_at: str = "",
    final_status: str = "issued",
) -> dict[str, Any]:
    return {
        "account_id": str(account_id or "").strip(),
        "project_id": project_id,
        "workflow_id": str(workflow_id or "").strip(),
        "run_id": str(run_id or "").strip(),
        **normalize_execution_scope(
            {
                "group_id": group_id,
                "for_each_id": for_each_id,
                "step_id": step_id,
                "step_execution_id": step_execution_id,
            }
        ),
        "extension_id": str(extension_id or "").strip(),
        "install_id": str(install_id or "").strip(),
        "operation_id": str(operation_id or "").strip(),
        "artifact_id": str(artifact_id or "").strip(),
        "grant_mode": str(grant_mode or "").strip(),
        "grant_transport": str(grant_transport or "").strip(),
        "issued_at": str(issued_at or "").strip(),
        "expires_at": str(expires_at or "").strip(),
        "used_at": str(used_at or "").strip() or None,
        "final_status": str(final_status or "").strip() or "issued",
    }


def _normalize_artifact_access_session(value: dict[str, Any] | None) -> dict[str, Any] | None:
    payload = dict(value or {})
    if not payload:
        return None
    if str(payload.get("transport") or "").strip().lower() != "artifact_api":
        raise ValueError("artifact_access_session.transport must be 'artifact_api'")
    return build_artifact_access_session(
        api_base_url=str(payload.get("api_base_url") or ""),
        bearer_token=str(payload.get("bearer_token") or ""),
        expires_at=str(payload.get("expires_at") or ""),
        ttl_seconds=_as_non_negative_int(payload.get("ttl_seconds")) or _DEFAULT_TTL_SECONDS,
        transport="artifact_api",
    )


def _normalize_artifacts(
    rows: list[dict[str, Any]] | None,
    *,
    default_role: str,
) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        if "access" not in row:
            raise ValueError("artifact access descriptor is required")
        access = dict(row.get("access") or {})
        normalized.append(
            build_artifact_access_descriptor(
                binding_key=str(row.get("binding_key") or ""),
                role=str(row.get("role") or default_role),
                artifact_id=str(row.get("artifact_id") or ""),
                artifact_handle=str(row.get("artifact_handle") or ""),
                artifact_kind=str(row.get("artifact_kind") or "file"),
                filename=str(row.get("filename") or ""),
                content_type=str(row.get("content_type") or OCTET_STREAM_VALUE),
                size_bytes=_as_non_negative_int(row.get("size_bytes")),
                checksum_sha256=str(row.get("checksum_sha256") or ""),
                retention_class=str(row.get("retention_class") or "run_window"),
                source_kind=str(row.get("source_kind") or ""),
                source_ref=str(row.get("source_ref") or ""),
                created_at=str(row.get("created_at") or ""),
                expires_at=str(row.get("expires_at") or ""),
                transport=str(access.get("transport") or "handle_only"),
                mode=str(access.get("mode") or ""),
                access_expires_at=str(access.get("expires_at") or ""),
                download_url=str(access.get("download_url") or ""),
                upload_url=str(access.get("upload_url") or ""),
                required_headers=dict(access.get("required_headers") or {}),
                platform_grant=dict(access.get("platform_grant") or {}),
                single_use=bool(access.get("single_use", False)),
                max_size_bytes=_as_non_negative_int(access.get("max_size_bytes")),
                scope=_normalize_scope(row.get("scope")),
            )
        )
    return normalized


def _assert_output_artifacts_are_new(
    *,
    inputs: list[dict[str, Any]],
    outputs: list[dict[str, Any]],
) -> None:
    input_ids = {
        str(item.get("artifact_id") or "").strip()
        for item in inputs
        if str(item.get("artifact_id") or "").strip()
    }
    for output in outputs:
        artifact_id = str(output.get("artifact_id") or "").strip()
        if artifact_id and artifact_id in input_ids:
            raise ValueError(
                "Output artifact_id must differ from input artifact_id "
                "(artifact bodies are immutable; create a new output artifact)"
            )


def _normalize_transport(value: str) -> str:
    transport = str(value or "handle_only").strip().lower() or "handle_only"
    if transport not in ARTIFACT_ACCESS_TRANSPORTS:
        raise ValueError(f"Unsupported artifact transport '{value}'")
    return transport


def _normalize_mode(value: str, *, role: str) -> str:
    if value:
        mode = str(value).strip().lower()
    else:
        mode = "write" if str(role or "").strip().lower() == "output" else "read"
    if mode not in ARTIFACT_ACCESS_MODES:
        raise ValueError(f"Unsupported artifact access mode '{value}'")
    return mode


def _normalize_required_headers(value: dict[str, Any] | None) -> dict[str, str]:
    headers = dict(value or {})
    normalized: dict[str, str] = {}
    for raw_key, raw_value in headers.items():
        key = str(raw_key or "").strip().lower()
        if not key:
            continue
        normalized[key] = str(raw_value or "").strip()
    return normalized


def _normalize_scope(value: dict[str, Any] | None) -> dict[str, Any]:
    scope = dict(value or {})
    normalized: dict[str, Any] = {}
    for field in _SCOPED_FIELDS:
        if field not in scope:
            continue
        raw_value = scope.get(field)
        if field in _NULL_BOUND_SCOPE_FIELDS:
            normalized[field] = _optional_scope_text(raw_value)
        else:
            normalized[field] = str(raw_value).strip() if raw_value is not None else ""
    return normalized


def _mode_allows_read(mode: str) -> bool:
    return mode in {"read", "read_write"}


def _mode_allows_write(mode: str) -> bool:
    return mode in {"write", "read_write"}


def _detect_mode(*, download_url: str, upload_url: str, role: str) -> str:
    has_read = bool(str(download_url or "").strip())
    has_write = bool(str(upload_url or "").strip())
    if has_read and has_write:
        return "read_write"
    if has_write:
        return "write"
    if has_read:
        return "read"
    return _normalize_mode("", role=role)


def _normalize_ttl(ttl_seconds: int) -> int:
    return max(_MIN_TTL_SECONDS, min(int(ttl_seconds or _DEFAULT_TTL_SECONDS), _MAX_TTL_SECONDS))


def _coerce_now(now: datetime | None) -> datetime:
    current = now if isinstance(now, datetime) else datetime.now(UTC)
    if current.tzinfo is None:
        return current.replace(tzinfo=UTC)
    return current.astimezone(UTC)


def _expiry_from_ttl(*, ttl_seconds: int, now: datetime | None) -> str:
    expiry = _coerce_now(now) + timedelta(seconds=_normalize_ttl(ttl_seconds))
    return expiry.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _parse_timestamp(value: str) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _as_non_negative_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        number = int(value)
    except Exception:
        return None
    if number < 0:
        return None
    return number


def _normalize_response(status_code: int, payload: Any) -> dict[str, Any]:
    transport_ok = 200 <= int(status_code) < 300
    if isinstance(payload, dict):
        declared_ok = payload.get("ok")
        ok = transport_ok and declared_ok is True
        result = payload.get("result") if ok else None
        error = payload.get("error")
        commands = payload.get("commands")
        events = payload.get("events")
        reload = payload.get("reload")
        errors = payload.get("errors")
    else:
        ok = False
        result = None
        error = {
            "code": "invalid_response",
            "message": "Remote runtime returned a malformed response",
        }
        commands = []
        events = []
        reload = []
        errors = []
    commands_list = list(commands) if isinstance(commands, list) else []
    events_list = list(events) if isinstance(events, list) else []
    reload_list = list(reload) if isinstance(reload, list) else []
    errors_list = list(errors) if isinstance(errors, list) else []
    if not error and errors_list:
        first_error = errors_list[0]
        if isinstance(first_error, dict):
            error = first_error
        else:
            error = {"code": "remote_error", "message": str(first_error)}
    if not ok and not error:
        error = {
            "code": "invalid_response",
            "message": "Remote runtime returned an invalid success envelope",
        }
    if error and not errors_list:
        errors_list = [error]
    normalized = {
        "ok": ok,
        "status_code": int(status_code),
        "result": result,
        "commands": commands_list,
        "events": events_list,
        "reload": reload_list,
        "errors": errors_list,
        "error": error,
    }
    retry_payload = {
        key: payload[key]
        for key in (
            "operation_kind",
            "request_semantics",
            "failure_class",
            "retryable",
            "retry_after_seconds",
            "definitely_no_external_effect",
            "external_effect_status",
            "provider_error_code",
        )
        if isinstance(payload, dict) and key in payload
    }
    retry_payload["http_status"] = int(status_code)
    normalized.update(normalize_retry_signal(retry_payload))
    return normalized


class _NoRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _ = (req, fp, code, msg, headers, newurl)


def _open_url_no_redirect(request: Request, *, timeout_seconds: float):
    # Pin the connection to a pre-validated IP (Host header + TLS SNI preserved)
    # so a stored/rebound remote runtime base URL cannot reach internal targets.
    from core.application.extensions.url_safety import open_pinned_url

    return open_pinned_url(request, timeout_seconds=timeout_seconds, purpose="remote runtime URL")


@dataclass(slots=True)
class RemoteRuntimeClient:
    base_url: str
    endpoints: dict[str, str]
    bearer_token: str = ""
    timeout_seconds: float = 15.0
    http_call: (
        Callable[[str, str, dict[str, str], dict[str, Any], float], tuple[int, Any]] | None
    ) = None

    def handshake(self, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        path = self._endpoint("handshake")
        return self._call("POST", path, build_handshake_request(payload))

    def health(self) -> dict[str, Any]:
        path = self._endpoint("health")
        return self._call("GET", path, {})

    def invoke_query(self, query_id: str, envelope: dict[str, Any]) -> dict[str, Any]:
        template = self._endpoint("query")
        path = template.replace("{query_id}", str(query_id or "").strip())
        return self._call("POST", path, envelope)

    def invoke_action(self, action_id: str, envelope: dict[str, Any]) -> dict[str, Any]:
        template = self._endpoint("action")
        path = template.replace("{action_id}", str(action_id or "").strip())
        return self._call("POST", path, envelope)

    def send_event(self, envelope: dict[str, Any]) -> dict[str, Any]:
        path = self._endpoint("emit_event")
        return self._call("POST", path, envelope)

    def _endpoint(self, name: str) -> str:
        value = str((self.endpoints or {}).get(name) or "").strip()
        if not value:
            raise ValueError(f"Missing endpoint '{name}' in remote runtime descriptor")
        return value

    def _call(self, method: str, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        token = str(self.bearer_token or "").strip()
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        if token:
            headers["Authorization"] = f"Bearer {token}"

        if self.http_call is not None:
            status, body = self.http_call(
                method, self._url(path), headers, payload, self.timeout_seconds
            )
            return _normalize_response(status, body)

        request_payload = None if method.upper() == "GET" else json.dumps(payload).encode("utf-8")
        req = Request(self._url(path), data=request_payload, method=method.upper(), headers=headers)
        try:
            response = _open_url_no_redirect(req, timeout_seconds=self.timeout_seconds)
        except HTTPError as exc:
            if 300 <= int(exc.code) < 400:
                raise
            # Remote runtimes return the protocol envelope on 4xx/5xx too.
            # Preserve its safe category rather than flattening it to urllib's
            # transport text (for example, ``authentication_error``).
            raw_bytes = exc.read(_MAX_REMOTE_RUNTIME_BODY_BYTES + 1)
            if len(raw_bytes) > _MAX_REMOTE_RUNTIME_BODY_BYTES:
                raise ValueError("Remote runtime response exceeds maximum size") from exc
            try:
                decoded = json.loads(raw_bytes.decode("utf-8")) if raw_bytes else {}
            except Exception:
                decoded = {}
            return _normalize_response(int(exc.code), decoded)
        with response as resp:  # nosec B310
            # Bounded read: fetch at most cap+1 bytes so an oversized body is
            # rejected without materializing the full payload.
            raw_bytes = resp.read(_MAX_REMOTE_RUNTIME_BODY_BYTES + 1)
            if len(raw_bytes) > _MAX_REMOTE_RUNTIME_BODY_BYTES:
                raise ValueError("Remote runtime response exceeds maximum size")
            raw_body = raw_bytes.decode("utf-8")
            try:
                decoded = json.loads(raw_body) if raw_body else {}
            except Exception:
                decoded = {"raw": raw_body}
            return _normalize_response(int(resp.status), decoded)

    def _url(self, path: str) -> str:
        parsed_path = urlparse(str(path or ""))
        if parsed_path.scheme or parsed_path.netloc or str(path or "").startswith("//"):
            raise ValueError(
                "Remote runtime endpoint path must be relative to runtime.remote.base_url"
            )
        base = str(self.base_url or "").rstrip("/") + "/"
        return urljoin(base, str(path or "").lstrip("/"))
