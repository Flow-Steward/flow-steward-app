"""Generic account-scoped external effect claim/confirm helpers."""

from __future__ import annotations

import contextlib
from datetime import UTC, datetime, timedelta
from typing import Any

from core.application.extensions.external_effect_contracts import (
    DEFAULT_NOTIFICATION_TARGET_KIND as DEFAULT_NOTIFICATION_TARGET_KIND,
)
from core.application.extensions.external_effect_contracts import (
    EXTERNAL_EFFECT_DECLARATION_INVALID as EXTERNAL_EFFECT_DECLARATION_INVALID,
)
from core.application.extensions.external_effect_contracts import (
    MAX_RATE_LIMIT_WINDOW_SECONDS as MAX_RATE_LIMIT_WINDOW_SECONDS,
)
from core.application.extensions.external_effect_contracts import (
    ExternalEffectDeclarationInvalidError as ExternalEffectDeclarationInvalidError,
)
from core.application.extensions.external_effect_contracts import (
    _artifact_refs,
    _as_dict,
    _as_list,
    _int_payload_value,
    _rate_limit_scopes,
    _safe_external_effect_guidance,
    _target_hashes,
    _target_summary,
    _text,
    redact_external_effect_guidance,
)
from core.application.extensions.external_effect_contracts import (
    apply_external_effect_confirmation as apply_external_effect_confirmation,
)
from core.application.extensions.external_effect_contracts import (
    build_external_effect_identity as build_external_effect_identity,
)
from core.application.extensions.external_effect_contracts import (
    declared_features as declared_features,
)
from core.application.extensions.external_effect_contracts import (
    direct_action_external_effect_scope_hash as direct_action_external_effect_scope_hash,
)
from core.application.extensions.external_effect_contracts import (
    external_effect_declaration_for_operation as external_effect_declaration_for_operation,
)
from core.application.extensions.external_effect_contracts import (
    external_effect_identity_hmac as external_effect_identity_hmac,
)
from core.application.extensions.external_effect_contracts import (
    external_effect_rate_limit_policy as external_effect_rate_limit_policy,
)
from core.application.extensions.external_effect_contracts import (
    external_effect_scope_hash as external_effect_scope_hash,
)
from core.application.extensions.external_effect_contracts import (
    notification_target_kind as notification_target_kind,
)
from core.application.extensions.external_effect_contracts import (
    stable_json_hash as stable_json_hash,
)
from core.infrastructure.db import external_effect_store

from .secret_reflection import (
    SECRET_EXPOSURE_CODE,
    SECRET_EXPOSURE_MESSAGE,
    value_exposes_secret,
)

EXTERNAL_EFFECT_STATUSES = {
    "claimed",
    "running",
    "succeeded",
    "failed",
    "timeout_unknown",
    "suppressed",
    "expired",
}
NON_SPAWN_STATUSES = {"claimed", "running", "succeeded", "timeout_unknown", "suppressed"}
RETRYABLE_STATUSES = {"failed", "expired"}
EXTERNAL_EFFECT_CONFIRMATION_LEASE_LOST = "external_effect_confirmation_lease_lost"
EXTERNAL_EFFECT_CLAIM_TRANSITION_LOST = "external_effect_claim_transition_lost"
_CONFIRMED_REPLAY_INVOCATION = "__confirmed_replay_invocation"


class ExternalEffectClaimTransitionLostError(RuntimeError):
    code = EXTERNAL_EFFECT_CLAIM_TRANSITION_LOST

    def __init__(self) -> None:
        super().__init__(self.code)


class ExternalEffectConfirmationLeaseLostError(RuntimeError):
    code = EXTERNAL_EFFECT_CONFIRMATION_LEASE_LOST
    commit_outcome = "rolled_back"

    def __init__(self) -> None:
        super().__init__(self.code)


def external_effect_confirmation_exception_code(exc: Exception) -> str:
    if isinstance(
        exc,
        (
            ExternalEffectConfirmationLeaseLostError,
            external_effect_store.ExternalEffectReplayLeaseLostError,
        ),
    ):
        return EXTERNAL_EFFECT_CONFIRMATION_LEASE_LOST
    return "external_effect_replay_persist_failed"


def external_effect_confirmation_failure_invocation(exc: Exception) -> dict[str, Any]:
    code = external_effect_confirmation_exception_code(exc)
    return {
        "status": "failed",
        "detail": code,
        "response": {
            "ok": False,
            "error_code": code,
            "external_effect_status": "timeout_unknown",
        },
        "errors": [{"code": code}],
    }


def fail_external_effect_before_spawn(
    db: Any,
    *,
    effect_id: str,
    claim_token: str,
    code: str,
) -> dict[str, Any] | None:
    """Fence a host-proven pre-provider failure from claimed to failed."""
    normalized_code = _text(code) or "external_effect_pre_spawn_failed"
    return confirm_external_effect_from_invocation(
        db,
        effect_id=effect_id,
        claim_token=claim_token,
        invocation={
            "status": "failed",
            "detail": normalized_code,
            "response": {
                "ok": False,
                "error_code": normalized_code,
                "external_effect_status": "failed",
                "definitely_no_external_effect": True,
            },
            "errors": [{"code": normalized_code}],
        },
        expected_current_status="claimed",
    )


def external_effect_spawn_exception_invocation() -> dict[str, Any]:
    code = "external_effect_provider_invocation_exception"
    return {
        "status": "failed",
        "detail": code,
        "response": {
            "ok": False,
            "error_code": code,
            "external_effect_status": "timeout_unknown",
        },
        "errors": [{"code": code}],
    }


def external_effect_output_finalization_exception_invocation() -> dict[str, Any]:
    code = "external_effect_output_finalization_failed"
    return {
        "status": "failed",
        "detail": code,
        "response": {
            "ok": False,
            "error_code": code,
            "external_effect_status": "timeout_unknown",
        },
        "errors": [{"code": code}],
    }


def external_effect_session_persistence_exception_invocation() -> dict[str, Any]:
    code = "external_effect_session_persistence_failed"
    return {
        "status": "failed",
        "detail": code,
        "response": {
            "ok": False,
            "error_code": code,
            "external_effect_status": "timeout_unknown",
        },
        "errors": [{"code": code}],
    }


def public_confirmed_external_effect(
    confirmed_effect: dict[str, Any],
) -> dict[str, Any]:
    return {
        str(key): value
        for key, value in confirmed_effect.items()
        if str(key) != _CONFIRMED_REPLAY_INVOCATION
    }


def _expires_at(ttl_seconds: int) -> str:
    ttl = max(1, int(ttl_seconds or 1))
    return (datetime.now(UTC) + timedelta(seconds=ttl)).isoformat()


def claim_external_effect(
    db: Any,
    *,
    declaration: dict[str, Any],
    account_id: str,
    project_id: str | None,
    extension_id: str,
    extension_install_id: str | None = None,
    connection_id: str | None = None,
    operation_id: str,
    source_type: str,
    source_ref: dict[str, Any],
    request_payload: dict[str, Any],
    idempotency_key_override: str | None = None,
    test_mode: bool = False,
    ttl_seconds: int = 300,
    resolution_ref: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Claim an external effect and return spawn guidance plus the row."""
    normalized_declaration = _as_dict(declaration)
    effect_kind = _text(normalized_declaration.get("effect_kind")) or "external_effect"
    channel = _text(normalized_declaration.get("channel")) or "generic"
    observability = _as_dict(normalized_declaration.get("observability"))
    identity = build_external_effect_identity(
        account_id=account_id,
        project_id=project_id,
        extension_id=extension_id,
        operation_id=operation_id,
        connection_id=connection_id,
        source_ref=source_ref,
        request_payload=request_payload,
        test_mode=test_mode,
        extension_install_id=extension_install_id,
    )
    override = _text(idempotency_key_override)
    if override:
        identity["idempotency_key"] = override
        identity["idempotency_scope_hash"] = direct_action_external_effect_scope_hash(
            account_id=account_id,
            extension_id=extension_id,
            extension_install_id=extension_install_id,
            operation_id=operation_id,
            test_mode=test_mode,
        )
    artifacts = _artifact_refs(request_payload, observability)
    target_hashes = _target_hashes(
        account_id=account_id,
        payload=request_payload,
        observability=observability,
    )
    payload_size_field = _text(observability.get("payload_size_field"))
    claim_values: dict[str, Any] = {
        "account_id": account_id,
        "project_id": project_id,
        "extension_id": extension_id,
        "extension_install_id": extension_install_id,
        "connection_id": connection_id,
        "operation_id": operation_id,
        "effect_kind": effect_kind,
        "channel": channel,
        "source_type": source_type,
        "source_ref_jsonb": _as_dict(source_ref),
        "idempotency_key": identity["idempotency_key"],
        "idempotency_scope_hash": identity["idempotency_scope_hash"],
        "request_fingerprint": identity["request_fingerprint"],
        "target_count": len(target_hashes),
        "artifact_count": len(artifacts),
        "artifact_total_bytes": sum(int(item.get("size_bytes") or 0) for item in artifacts),
        "estimated_payload_bytes": _int_payload_value(request_payload, payload_size_field)
        if payload_size_field
        else 0,
        "target_summary_redacted": _target_summary(request_payload, observability),
        "target_hashes_jsonb": target_hashes,
        "artifact_refs_jsonb": artifacts,
        "test_mode": bool(test_mode),
        "expires_at": _expires_at(ttl_seconds),
    }
    if resolution_ref is not None:
        from core.application.extensions.external_effect_resolution import (
            claim_resolved_external_effect,
            effect_intent,
        )

        row, should_spawn = claim_resolved_external_effect(
            db,
            resolution_ref=resolution_ref,
            intent=effect_intent(claim_values),
            expires_at=claim_values["expires_at"],
        )
    else:
        row, should_spawn = external_effect_store.claim_external_effect(db, **claim_values)
    if (
        not should_spawn
        and _text(row.get("request_fingerprint")) != identity["request_fingerprint"]
    ):
        return {
            "effect": row,
            "should_spawn": False,
            "suppressed": False,
            "deduped": True,
            "error": "external_effect_idempotency_conflict",
        }
    suppress = bool(test_mode) and (
        _text(normalized_declaration.get("test_mode_behavior")) == "suppress"
    )
    if suppress:
        if should_spawn and row.get("effect_id"):
            suppressed_row = mark_external_effect_suppressed(
                db,
                effect_id=_text(row.get("effect_id")),
                claim_token=_text(row.get("claim_token")) or None,
                reason="test_mode_suppressed",
            )
            if not suppressed_row:
                raise ExternalEffectClaimTransitionLostError
            row = suppressed_row
        return {
            "effect": row,
            "should_spawn": False,
            "suppressed": True,
            "deduped": not should_spawn,
        }
    if should_spawn:
        policy = external_effect_rate_limit_policy(normalized_declaration)
        rate_limit = external_effect_store.consume_external_effect_rate_limits(
            db,
            scopes=_rate_limit_scopes(
                account_id=account_id,
                project_id=project_id,
                connection_id=connection_id,
                effect_kind=effect_kind,
                channel=channel,
                policy=policy,
            ),
            window_seconds=policy["window_seconds"],
        )
        if not rate_limit.get("allowed"):
            retry_after_seconds = max(1, int(rate_limit.get("retry_after_seconds") or 1))
            failed_row = external_effect_store.update_external_effect_status(
                db,
                effect_id=_text(row.get("effect_id")),
                claim_token=_text(row.get("claim_token")) or None,
                status="failed",
                status_reason="host_rate_limited",
                error_code="external_effect_rate_limited",
                error_text="Host external-effect rate limit reached",
                response_summary_jsonb={
                    "definitely_no_external_effect": True,
                    "rate_limited_scope": _text(rate_limit.get("scope_type")),
                    "retry_after_seconds": retry_after_seconds,
                },
                expected_current_status="claimed",
            )
            if not failed_row:
                raise ExternalEffectClaimTransitionLostError
            row = failed_row
            return {
                "effect": row,
                "should_spawn": False,
                "suppressed": False,
                "deduped": False,
                "rate_limited": True,
                "retry_after_seconds": retry_after_seconds,
                "error": "external_effect_rate_limited",
            }
    return {
        "effect": row,
        "should_spawn": bool(should_spawn),
        "suppressed": False,
        "deduped": not bool(should_spawn),
    }


def claim_external_effect_for_action(
    db: Any,
    *,
    extension: dict[str, Any] | None,
    account_id: str,
    project_id: str | None,
    extension_id: str,
    extension_install_id: str | None = None,
    connection_id: str | None = None,
    operation_id: str,
    source_type: str,
    source_ref: dict[str, Any],
    request_payload: dict[str, Any],
    idempotency_key_override: str | None = None,
    test_mode: bool = False,
    resolution_ref: dict[str, Any] | None = None,
) -> dict[str, Any]:
    declaration = external_effect_declaration_for_operation(extension, operation_id)
    if resolution_ref is not None and not declaration:
        return {
            "declared": False,
            "effect": {},
            "should_spawn": False,
            "error": "external_effect_resolution_declaration_missing",
        }
    if not declaration:
        return {"declared": False, "effect": {}, "should_spawn": True}
    from core.application.extensions.external_effect_replay import (
        validate_external_effect_replay_contract,
    )

    validate_external_effect_replay_contract(_as_dict(extension), operation_id)
    if not _text(extension_install_id):
        from core.application.extensions.external_effect_replay import (
            ExternalEffectReplayProjectionError,
        )

        raise ExternalEffectReplayProjectionError("external_effect_install_identity_required")
    if db is None or not callable(getattr(db, "cursor", None)):
        return {
            "declared": True,
            "effect": {},
            "should_spawn": False,
            "error": "external_effect_claim_unavailable",
        }
    try:
        claim = claim_external_effect(
            db,
            declaration=declaration,
            account_id=account_id,
            project_id=project_id,
            extension_id=extension_id,
            extension_install_id=extension_install_id,
            connection_id=connection_id,
            operation_id=operation_id,
            source_type=source_type,
            source_ref=source_ref,
            request_payload=request_payload,
            idempotency_key_override=idempotency_key_override,
            test_mode=test_mode,
            resolution_ref=resolution_ref,
        )
    except ExternalEffectClaimTransitionLostError as exc:
        return {
            "declared": True,
            "effect": {},
            "should_spawn": False,
            "error": exc.code,
        }
    except Exception:
        return {
            "declared": True,
            "effect": {},
            "should_spawn": False,
            "error": "external_effect_claim_failed",
        }
    return {"declared": True, **claim}


def mark_external_effect_running(
    db: Any,
    *,
    effect_id: str,
    claim_token: str | None = None,
) -> dict[str, Any] | None:
    if not _text(effect_id):
        return None
    return external_effect_store.update_external_effect_status(
        db,
        effect_id=effect_id,
        claim_token=_text(claim_token) or None,
        status="running",
        status_reason="invocation_started",
        expected_current_status="claimed",
    )


def mark_external_effect_suppressed(
    db: Any,
    *,
    effect_id: str,
    claim_token: str | None = None,
    reason: str = "suppressed",
) -> dict[str, Any] | None:
    if not _text(effect_id):
        return None
    return external_effect_store.update_external_effect_status(
        db,
        effect_id=effect_id,
        claim_token=_text(claim_token) or None,
        status="suppressed",
        status_reason=reason,
        response_summary_jsonb={"suppressed": True, "reason": reason},
        expected_current_status="claimed",
    )


def _response_summary(
    invocation: dict[str, Any], host_outputs: dict[str, Any] | None = None
) -> dict[str, Any]:
    response = _as_dict(invocation.get("response"))
    result = _as_dict(response.get("result"))
    summary: dict[str, Any] = {}
    for key in ("ok", "delivered", "status", "error_code"):
        if key in response:
            summary[key] = response.get(key)
        elif key in result:
            summary[key] = result.get(key)
    if invocation.get("status"):
        summary["invocation_status"] = invocation.get("status")
    guidance = _safe_external_effect_guidance(result.get("guidance"))
    if guidance:
        summary["guidance"] = guidance
    trusted_outputs = host_outputs or {}
    produced_artifacts = _produced_output_summary(
        trusted_outputs.get("produced_artifacts"), kind="artifact"
    )
    if produced_artifacts:
        summary["produced_artifacts"] = produced_artifacts
    produced_datasets = _produced_output_summary(
        trusted_outputs.get("produced_datasets"), kind="dataset"
    )
    if produced_datasets:
        summary["produced_datasets"] = produced_datasets
    grant_outputs = _as_dict(trusted_outputs.get("grant_outputs"))
    if grant_outputs:
        summary["grant_outputs"] = {
            str(key): str(value)
            for key, value in grant_outputs.items()
            if str(key).strip() and str(value).strip()
        }
    return summary


def _produced_output_summary(value: Any, *, kind: str) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    id_field = "dataset_id" if kind == "dataset" else "artifact_id"
    handle_field = "dataset_handle" if kind == "dataset" else "artifact_handle"
    rows: list[dict[str, Any]] = []
    for raw in value:
        row = _as_dict(raw)
        object_id = _text(row.get(id_field))
        if not object_id:
            continue
        handle = _text(row.get(handle_field)) or f"{kind}:{object_id}"
        summary = {
            id_field: object_id,
            handle_field: handle,
        }
        binding_key = _text(row.get("binding_key"))
        if binding_key:
            summary["binding_key"] = binding_key
        for key in ("size_bytes", "sha256", "row_count", "content_type"):
            if key in row and row.get(key) not in (None, ""):
                summary[key] = row.get(key)
        rows.append(summary)
    return rows


def _effect_metadata_payloads(invocation: dict[str, Any]) -> tuple[dict[str, Any], ...]:
    response = _as_dict(invocation.get("response"))
    result = _as_dict(response.get("result"))
    return tuple(
        payload
        for payload in (
            _as_dict(response.get("external_effect")),
            _as_dict(result.get("external_effect")),
            response,
            result,
        )
        if payload
    )


def _effect_result_payload(invocation: dict[str, Any]) -> dict[str, Any]:
    """Return the canonical business/effect payload for compatibility callers."""
    response = _as_dict(invocation.get("response"))
    result = _as_dict(response.get("result"))
    for candidate in (
        response.get("external_effect"),
        result.get("external_effect"),
        result,
        response,
    ):
        payload = _as_dict(candidate)
        if payload:
            return payload
    return {}


def _effect_metadata_value(
    payloads: tuple[dict[str, Any], ...],
    *field_names: str,
) -> Any:
    for payload in payloads:
        for field_name in field_names:
            if field_name in payload and payload.get(field_name) not in (None, ""):
                return payload.get(field_name)
    return None


def _effect_status_values(
    response: dict[str, Any],
    result: dict[str, Any],
) -> frozenset[str]:
    response_effect = _as_dict(response.get("external_effect"))
    result_effect = _as_dict(result.get("external_effect"))
    values = (
        response.get("external_effect_status"),
        result.get("external_effect_status"),
        response.get("effect_status"),
        result.get("effect_status"),
        response_effect.get("status"),
        response_effect.get("effect_status"),
        result_effect.get("status"),
        result_effect.get("effect_status"),
    )
    return frozenset(_text(value).lower() for value in values if _text(value))


def _definitive_no_effect_values(
    response: dict[str, Any],
    result: dict[str, Any],
) -> tuple[Any, ...]:
    payloads = (
        _as_dict(response.get("external_effect")),
        _as_dict(result.get("external_effect")),
        response,
        result,
    )
    return tuple(
        payload["definitely_no_external_effect"]
        for payload in payloads
        if "definitely_no_external_effect" in payload
    )


def _project_state_commit_error_code(invocation: dict[str, Any]) -> str:
    for raw in _as_list(invocation.get("errors")):
        row = _as_dict(raw)
        code = _text(row.get("code"))
        if code.startswith("project_state_"):
            return code
    detail = _text(invocation.get("detail"))
    return detail if detail.startswith("project_state_") else ""


def confirm_external_effect_from_invocation(
    db: Any,
    *,
    effect_id: str,
    invocation: dict[str, Any],
    claim_token: str | None = None,
    account_id: str | None = None,
    extension: dict[str, Any] | None = None,
    operation_id: str | None = None,
    protected_values: Any = (),
    host_outputs: dict[str, Any] | None = None,
    expected_current_status: str = "running",
    expected_project_id: str | None = None,
    expected_connection_id: str | None = None,
) -> dict[str, Any] | None:
    normalized_effect_id = _text(effect_id)
    if not normalized_effect_id:
        return None
    response = _as_dict(invocation.get("response"))
    result = _as_dict(response.get("result"))
    explicit_effect = _as_dict(response.get("external_effect")) or _as_dict(
        result.get("external_effect")
    )
    metadata_payloads = _effect_metadata_payloads(invocation)
    explicit_status = _text(
        response.get("external_effect_status")
        or result.get("external_effect_status")
        or explicit_effect.get("status")
        or _effect_metadata_value(metadata_payloads, "effect_status")
    ).lower()
    declared_statuses = _effect_status_values(response, result)
    definitive_values = _definitive_no_effect_values(response, result)
    invalid_definitive_metadata = any(type(value) is not bool for value in definitive_values) or (
        len(set(definitive_values)) > 1
        if all(type(value) is bool for value in definitive_values)
        else False
    )
    definitely_no_effect = bool(definitive_values) and definitive_values[0] is True
    allowed_effect_statuses = frozenset({"succeeded", "failed", "timeout_unknown", "suppressed"})
    metadata_conflict = (
        invalid_definitive_metadata
        or not declared_statuses.issubset(allowed_effect_statuses)
        or len(declared_statuses) > 1
        or "suppressed" in declared_statuses
        or (bool(declared_statuses & {"succeeded", "timeout_unknown"}) and definitely_no_effect)
    )
    metadata_error_code = "external_effect_metadata_conflict" if metadata_conflict else ""
    invocation_status = _text(invocation.get("status")).lower()
    project_state_error_code = _project_state_commit_error_code(invocation)
    if project_state_error_code or metadata_conflict:
        final_status = "timeout_unknown"
    elif invocation_status != "succeeded" or explicit_status == "failed":
        final_status = "failed" if definitely_no_effect else "timeout_unknown"
    elif explicit_status in {"succeeded", "timeout_unknown", "suppressed"}:
        final_status = explicit_status
    elif invocation_status == "succeeded":
        final_status = "succeeded"
    else:
        # A generic extension failure can be ambiguous after process spawn. Only
        # extensions that explicitly declare a definitive no-effect failure get
        # marked failed.
        final_status = "failed" if definitely_no_effect else "timeout_unknown"
    provider_message_id = (
        _text(_effect_metadata_value(metadata_payloads, "provider_message_id", "message_id"))
        or None
    )
    external_object_id = (
        _text(_effect_metadata_value(metadata_payloads, "external_object_id")) or None
    )
    error_code = (
        project_state_error_code
        or metadata_error_code
        or _text(_effect_metadata_value(metadata_payloads, "error_code"))
        or None
    )
    error_text = (
        _text(
            _effect_metadata_value(metadata_payloads, "error", "error_text")
            or invocation.get("detail")
        )
        or None
    )
    status_reason = (
        project_state_error_code
        or metadata_error_code
        or _text(_effect_metadata_value(metadata_payloads, "status_reason"))
        or final_status
    )
    response_summary = _response_summary(invocation, host_outputs)
    persisted_metadata = {
        "status_reason": status_reason,
        "provider_message_id": provider_message_id,
        "external_object_id": external_object_id,
        "error_code": error_code,
        "error_text": error_text,
        "response_summary": response_summary,
    }
    if value_exposes_secret(persisted_metadata, protected_values):
        if final_status == "succeeded":
            final_status = "timeout_unknown"
        status_reason = SECRET_EXPOSURE_CODE
        provider_message_id = None
        external_object_id = None
        error_code = SECRET_EXPOSURE_CODE
        error_text = SECRET_EXPOSURE_MESSAGE
        response_summary = {}
    response_summary = redact_external_effect_guidance(
        response_summary, project_id=expected_project_id
    )
    normalized_account_id = _text(account_id)
    normalized_operation_id = _text(operation_id)
    normalized_claim_token = _text(claim_token)
    fenced_current_status = (
        "claimed" if _text(expected_current_status).lower() == "claimed" else "running"
    )
    if final_status != "succeeded":
        updated_effect = external_effect_store.update_external_effect_status(
            db,
            effect_id=normalized_effect_id,
            claim_token=normalized_claim_token or None,
            status=final_status,
            status_reason=status_reason,
            provider_message_id=provider_message_id,
            external_object_id=external_object_id,
            error_code=error_code,
            error_text=error_text,
            response_summary_jsonb=response_summary,
            expected_current_status=fenced_current_status,
        )
        if not updated_effect:
            raise ExternalEffectConfirmationLeaseLostError()
        return updated_effect

    replay: dict[str, Any] = {}
    encrypted: dict[str, Any] = {}
    effect: dict[str, Any] = {}
    store_confirmation_started = False
    try:
        from core.application.extensions.external_effect_replay import (
            ExternalEffectReplayPersistenceError,
            encrypt_projected_replay,
            project_external_effect_replay,
        )

        if (
            not normalized_claim_token
            or not normalized_account_id
            or not normalized_operation_id
            or not isinstance(extension, dict)
        ):
            raise ExternalEffectReplayPersistenceError()
        effect = external_effect_store.get_external_effect(db, effect_id=normalized_effect_id) or {}
        installed_extension_id = _text(
            extension.get("extension_key") or extension.get("extension_id")
        )
        installed_extension_install_id = _text(
            extension.get("extension_install_id") or extension.get("install_id")
        )
        if (
            not effect
            or _text(effect.get("account_id")) != normalized_account_id
            or not installed_extension_id
            or _text(effect.get("extension_id")) != installed_extension_id
            or _text(effect.get("extension_install_id")) != installed_extension_install_id
            or _text(effect.get("operation_id")) != normalized_operation_id
            or _text(effect.get("project_id")) != _text(expected_project_id)
            or _text(effect.get("connection_id")) != _text(expected_connection_id)
        ):
            raise ExternalEffectReplayPersistenceError()
        replay = project_external_effect_replay(
            extension=extension,
            operation_id=normalized_operation_id,
            invocation=invocation,
            protected_values=protected_values,
            host_outputs=host_outputs,
        )
        encrypted = encrypt_projected_replay(replay, effect=effect)
        store_confirmation_started = True
        confirmed_effect = external_effect_store.confirm_external_effect_with_replay(
            db,
            effect_id=normalized_effect_id,
            account_id=normalized_account_id,
            claim_token=normalized_claim_token,
            status=final_status,
            status_reason=status_reason,
            provider_message_id=provider_message_id,
            external_object_id=external_object_id,
            error_code=error_code,
            error_text=error_text,
            response_summary_jsonb=response_summary,
            replay_ciphertext=encrypted["ciphertext"],
            replay_binding_hash=encrypted["binding_hash"],
            replay_plaintext_bytes=encrypted["plaintext_bytes"],
        )
        return {**confirmed_effect, _CONFIRMED_REPLAY_INVOCATION: replay}
    except Exception as exc:
        from core.application.extensions.external_effect_replay import (
            PERSISTENCE_ERROR_CODE,
            ExternalEffectReplayPersistenceError,
        )

        if isinstance(exc, external_effect_store.ExternalEffectReplayLeaseLostError):
            raise ExternalEffectConfirmationLeaseLostError() from exc

        commit_outcome = getattr(
            exc,
            "commit_outcome",
            "unknown" if store_confirmation_started else "rolled_back",
        )
        if commit_outcome in {"committed", "unknown"} and encrypted and replay:
            try:
                reread_effect = external_effect_store.get_exact_committed_external_effect_replay(
                    db,
                    effect=effect,
                    replay_ciphertext=encrypted["ciphertext"],
                    replay_binding_hash=encrypted["binding_hash"],
                    replay_plaintext_bytes=encrypted["plaintext_bytes"],
                )
                if reread_effect:
                    return {**reread_effect, _CONFIRMED_REPLAY_INVOCATION: replay}
            except Exception:
                commit_outcome = "unknown"

        with contextlib.suppress(Exception):
            external_effect_store.update_external_effect_status(
                db,
                effect_id=normalized_effect_id,
                claim_token=normalized_claim_token or None,
                status="timeout_unknown",
                status_reason=PERSISTENCE_ERROR_CODE,
                error_code=PERSISTENCE_ERROR_CODE,
                error_text="Successful provider response could not be persisted for replay",
                response_summary_jsonb={},
                expected_current_status="running",
            )
        if isinstance(exc, ExternalEffectReplayPersistenceError):
            if exc.commit_outcome == commit_outcome:
                raise
            raise ExternalEffectReplayPersistenceError(commit_outcome=commit_outcome) from exc
        raise ExternalEffectReplayPersistenceError(commit_outcome=commit_outcome) from exc


def cleanup_stale_external_effects(
    db: Any,
    *,
    account_id: str,
) -> dict[str, int]:
    return external_effect_store.cleanup_stale_external_effects(
        db,
        account_id=account_id,
    )


def register_external_effect_output_reconciliation(
    db: Any,
    *,
    effect_id: str,
    account_id: str,
    claim_token: str,
    prepared_grants: Any,
) -> None:
    if not effect_id or prepared_grants is None:
        return
    external_effect_store.register_external_effect_output_reconciliation(
        db,
        effect_id=effect_id,
        account_id=account_id,
        claim_token=claim_token,
        minted_artifact_ids=list(
            getattr(prepared_grants, "minted_artifact_output_ids", None) or []
        ),
        minted_dataset_ids=list(getattr(prepared_grants, "minted_dataset_output_ids", None) or []),
    )


def reconcile_external_effect_outputs(
    db: Any,
    *,
    account_id: str,
    artifact_plane: Any,
    limit: int = 100,
) -> int:
    if artifact_plane is None:
        return 0
    from core.application.extensions.runtime_grants import (
        cleanup_output_grant_placeholder_status,
    )

    resolved = 0
    rows = external_effect_store.list_external_effect_output_reconciliations_for_cleanup(
        db,
        account_id=account_id,
        limit=limit,
    )
    for row in rows:
        artifact_ids = frozenset(row.get("minted_artifact_ids_jsonb") or [])
        dataset_ids = frozenset(row.get("minted_dataset_ids_jsonb") or [])
        all_absent = True
        try:
            for object_id in artifact_ids:
                status = cleanup_output_grant_placeholder_status(
                    artifact_plane,
                    object_id,
                    kind="artifact",
                    allowed_object_ids=artifact_ids,
                )
                if status == "unknown":
                    all_absent = False
            for object_id in dataset_ids:
                status = cleanup_output_grant_placeholder_status(
                    artifact_plane,
                    object_id,
                    kind="dataset",
                    allowed_object_ids=dataset_ids,
                )
                if status == "unknown":
                    all_absent = False
        except Exception:
            continue
        if not all_absent:
            continue
        deleted = external_effect_store.resolve_external_effect_output_reconciliation(
            db,
            effect_id=_text(row.get("effect_id")),
            account_id=account_id,
            claim_token=_text(row.get("claim_token")),
            claim_version=int(row.get("claim_version") or 1),
        )
        if deleted:
            resolved += 1
    return resolved
