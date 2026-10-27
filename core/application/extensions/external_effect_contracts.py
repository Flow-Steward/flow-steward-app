"""Normalize extension effect identity, observability and host rate-limit policy."""

from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any

from core.infrastructure.security.config import SECURITY_PII_SALT

EXTERNAL_EFFECT_DECLARATION_INVALID = "external_effect_declaration_invalid"


_DEFAULT_RATE_LIMIT_POLICY = {
    "window_seconds": 60,
    "account_max": 120,
    "project_max": 60,
    "connection_max": 30,
}


MAX_RATE_LIMIT_WINDOW_SECONDS = 86400


_RETRY_VOLATILE_KEYS = {
    "attempt",
    "attempt_id",
    "attempt_no",
    "attempt_number",
    "invocation_attempt_id",
    "idempotency_key",
    "retry_attempt",
    "retry_attempt_number",
    "runtime_token",
    "runtime_tokens",
}


class ExternalEffectDeclarationInvalidError(ValueError):
    code = EXTERNAL_EFFECT_DECLARATION_INVALID

    def __init__(self) -> None:
        super().__init__(self.code)


def _as_dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _as_list(value: Any) -> list[Any]:
    return list(value) if isinstance(value, list) else []


def _text(value: Any) -> str:
    return str(value or "").strip()


def _json_safe(value: Any, *, omit_host_retry_metadata: bool = False) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        normalized: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            if omit_host_retry_metadata and key_text in _RETRY_VOLATILE_KEYS:
                continue
            normalized[key_text] = _json_safe(
                item,
                omit_host_retry_metadata=omit_host_retry_metadata,
            )
        return normalized
    if isinstance(value, (list, tuple, set)):
        return [
            _json_safe(item, omit_host_retry_metadata=omit_host_retry_metadata) for item in value
        ]
    isoformat = getattr(value, "isoformat", None)
    if callable(isoformat):
        try:
            return isoformat()
        except Exception:
            return str(value)
    return str(value)


def stable_json_hash(value: Any) -> str:
    encoded = json.dumps(
        _json_safe(value),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        default=str,
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def external_effect_identity_hmac(*, purpose: str, value: Any) -> str:
    from core.infrastructure.security.secret_codec import derive_secret_storage_key

    normalized_purpose = _text(purpose).lower().replace("_", "-")
    if normalized_purpose not in {
        "request-fingerprint",
        "general-scope",
        "direct-intent-scope",
        "workflow-intent",
        "notification-intent",
        "command-intent",
        "rate-limit-bucket",
    }:
        raise ValueError("unsupported external-effect identity purpose")
    encoded = json.dumps(
        _json_safe(value),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        default=str,
    ).encode("utf-8")
    key = derive_secret_storage_key(
        f"flow-steward.external-effect-identity.{normalized_purpose}.v1".encode()
    )
    return f"hmac-sha256:{hmac.new(key, encoded, hashlib.sha256).hexdigest()}"


def _payload_metadata_candidates(payload: dict[str, Any]) -> list[dict[str, Any]]:
    root = _as_dict(payload)
    candidates: list[dict[str, Any]] = []
    seen: set[str] = set()

    def add(value: Any) -> dict[str, Any]:
        row = _as_dict(value)
        if not row:
            return row
        marker = stable_json_hash(row)
        if marker not in seen:
            seen.add(marker)
            candidates.append(row)
        return row

    add(root)
    for key in ("input", "inputs", "request", "payload", "data"):
        add(root.get(key))

    action_payload = add(root.get("action_payload"))
    for key in ("input", "inputs", "request", "payload", "data"):
        add(action_payload.get(key))

    action = add(root.get("action"))
    for key in ("input", "inputs", "request", "payload", "data"):
        add(action.get(key))

    route = add(root.get("route"))
    add(route.get("route_config"))
    return candidates


def external_effect_scope_hash(
    *,
    account_id: str,
    project_id: str | None,
    extension_id: str,
    operation_id: str,
    connection_id: str | None,
    test_mode: bool,
    extension_install_id: str | None = None,
) -> str:
    scope = {
        "account_id": _text(account_id),
        "project_id": _text(project_id),
        "extension_id": _text(extension_id),
        "operation_id": _text(operation_id),
        "connection_id": _text(connection_id),
        "test_mode": bool(test_mode),
    }
    if _text(extension_install_id):
        scope["extension_install_id"] = _text(extension_install_id)
    return external_effect_identity_hmac(purpose="general-scope", value=scope)


def direct_action_external_effect_scope_hash(
    *,
    account_id: str,
    extension_id: str,
    operation_id: str,
    extension_install_id: str | None,
    test_mode: bool,
) -> str:
    """Namespace a caller intent independently from mutable request targets."""
    return external_effect_identity_hmac(
        purpose="direct-intent-scope",
        value={
            "source_type": "api_action",
            "account_id": _text(account_id),
            "extension_id": _text(extension_id),
            "extension_install_id": _text(extension_install_id),
            "operation_id": _text(operation_id),
            "test_mode": bool(test_mode),
        },
    )


def external_effect_declaration_for_operation(
    extension: dict[str, Any] | None,
    operation_id: str,
) -> dict[str, Any]:
    normalized_operation_id = _text(operation_id)
    if not normalized_operation_id:
        return {}
    payload = _as_dict(extension)
    compiled = _as_dict(payload.get("compiled_manifest"))
    source_matches: list[list[dict[str, Any]]] = []
    for source in (payload.get("external_effects"), compiled.get("external_effects")):
        rows = source if isinstance(source, list) else []
        matches = [
            _as_dict(raw)
            for raw in rows
            if _text(_as_dict(raw).get("operation_id")) == normalized_operation_id
        ]
        if len(matches) > 1:
            raise ExternalEffectDeclarationInvalidError()
        source_matches.append(matches)
    declared = [matches[0] for matches in source_matches if matches]
    if len(declared) == 2 and declared[0] != declared[1]:
        raise ExternalEffectDeclarationInvalidError()
    return declared[0] if declared else {}


#: What a notification provider addresses when it delivers. An extension declares
#: it per operation as `external_effects[].observability.target_kind`; `channel` is
#: the default because a provider that says nothing addresses a channel.
DEFAULT_NOTIFICATION_TARGET_KIND = "channel"


def notification_target_kind(extension: dict[str, Any] | None) -> str:
    """Return what this extension's delivery operations address.

    Read from the extension's own typed declaration, so the platform never has to
    know which extension sends mail and which posts to a channel.

    Compilation rejects a manifest whose effects disagree, so more than one kind
    here means the stored descriptor was damaged after the fact. That is not a
    thing to guess about: the default is returned and the caller sees no
    recipient form, which is the recoverable outcome.
    """
    payload = _as_dict(extension)
    rows: list[Any] = []
    if isinstance(payload.get("external_effects"), list):
        rows.extend(payload["external_effects"])
    compiled = _as_dict(payload.get("compiled_manifest"))
    if isinstance(compiled.get("external_effects"), list):
        rows.extend(compiled["external_effects"])
    declared = {
        kind
        for raw in rows
        if (kind := _text(_as_dict(_as_dict(raw).get("observability")).get("target_kind")))
    }
    if len(declared) == 1:
        return declared.pop()
    return DEFAULT_NOTIFICATION_TARGET_KIND


def declared_features(extension: dict[str, Any] | None) -> list[str]:
    """Return the capability tags an extension declares, in manifest order."""
    payload = _as_dict(extension)
    for source in (payload, _as_dict(payload.get("compiled_manifest"))):
        values = source.get("features")
        if isinstance(values, list):
            return [feature for item in values if (feature := _text(item))]
    return []


def _observability_field_names(value: Any) -> list[str]:
    values = value if isinstance(value, list) else []
    return [field for item in values if (field := _text(item))]


def _declared_target_items(
    payload: dict[str, Any],
    observability: dict[str, Any],
) -> list[str]:
    items: list[str] = []
    seen: set[str] = set()
    target_fields = _observability_field_names(observability.get("target_fields"))
    for candidate in _payload_metadata_candidates(payload):
        for key in target_fields:
            raw = candidate.get(key)
            values = raw if isinstance(raw, list) else [raw]
            for value in values:
                text = _text(value).lower()
                if text and text not in seen:
                    seen.add(text)
                    items.append(text)
    return items


def _target_hashes(
    *,
    account_id: str,
    payload: dict[str, Any],
    observability: dict[str, Any],
) -> list[dict[str, str]]:
    account_key = hmac.new(
        SECURITY_PII_SALT.encode("utf-8"),
        _text(account_id).encode("utf-8"),
        hashlib.sha256,
    ).digest()
    hashes: list[dict[str, str]] = []
    target_kind = _text(observability.get("target_kind")) or "target"
    for target in _declared_target_items(payload, observability):
        hashes.append(
            {
                "kind": target_kind,
                "hmac_sha256": hmac.new(
                    account_key,
                    target.encode("utf-8"),
                    hashlib.sha256,
                ).hexdigest(),
            }
        )
    return hashes


def _int_payload_value(payload: dict[str, Any], key: str) -> int:
    for candidate in _payload_metadata_candidates(payload):
        raw = candidate.get(key)
        if str(raw or "").isdigit():
            return max(0, int(raw or 0))
    return 0


def _artifact_size_bytes(row: dict[str, Any]) -> int:
    raw = row.get("size_bytes")
    if str(raw or "").isdigit():
        return max(0, int(raw or 0))
    return 0


def _artifact_refs(
    payload: dict[str, Any],
    observability: dict[str, Any],
) -> list[dict[str, Any]]:
    refs: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    artifact_field = _text(observability.get("artifact_field"))
    if not artifact_field:
        return refs
    for candidate in _payload_metadata_candidates(payload):
        for item in _as_list(candidate.get(artifact_field)):
            row = _as_dict(item)
            artifact_id = _text(row.get("artifact_id") or row.get("artifact_handle"))
            if not artifact_id:
                continue
            filename = _text(row.get("filename") or row.get("name"))
            sha256 = _text(row.get("sha256") or row.get("checksum_sha256"))
            ref = {
                "artifact_id": artifact_id,
                "filename": filename,
                "size_bytes": _artifact_size_bytes(row),
                "sha256": sha256,
            }
            marker: tuple[str, str, str] = (artifact_id, filename, sha256)
            if marker not in seen:
                seen.add(marker)
                refs.append(ref)
    return refs


def _target_summary(payload: dict[str, Any], observability: dict[str, Any]) -> str:
    count = len(_declared_target_items(payload, observability))
    if count <= 0:
        return ""
    label = _text(observability.get("target_label")) or "target"
    return f"{count} {label}" + ("" if count == 1 else "s")


def _positive_int(value: Any, *, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def external_effect_rate_limit_policy(declaration: dict[str, Any]) -> dict[str, int]:
    """Return the host-enforced rate policy for one declared external effect.

    Declarations may tighten the host defaults but cannot loosen them. This keeps
    every external-effect consumer bounded even when a manifest is incomplete.
    """
    declared = _as_dict(_as_dict(declaration).get("rate_limit"))
    policy: dict[str, int] = {
        "window_seconds": max(
            _DEFAULT_RATE_LIMIT_POLICY["window_seconds"],
            min(
                MAX_RATE_LIMIT_WINDOW_SECONDS,
                _positive_int(
                    declared.get("window_seconds"),
                    default=_DEFAULT_RATE_LIMIT_POLICY["window_seconds"],
                ),
            ),
        )
    }
    for key, default in _DEFAULT_RATE_LIMIT_POLICY.items():
        if key == "window_seconds":
            continue
        configured = _positive_int(declared.get(key), default=default)
        policy[key] = min(default, configured)
    return policy


def _rate_limit_scopes(
    *,
    account_id: str,
    project_id: str | None,
    connection_id: str | None,
    effect_kind: str,
    channel: str,
    policy: dict[str, int],
) -> list[dict[str, Any]]:
    common = {
        "account_id": _text(account_id),
        "effect_kind": _text(effect_kind),
        "channel": _text(channel),
    }
    scopes = [
        {
            **common,
            "scope_type": "account",
            "project_id": None,
            "connection_id": None,
            "max_effects": int(policy["account_max"]),
        }
    ]
    normalized_project_id = _text(project_id)
    if normalized_project_id:
        scopes.append(
            {
                **common,
                "scope_type": "project",
                "project_id": normalized_project_id,
                "connection_id": None,
                "max_effects": int(policy["project_max"]),
            }
        )
    normalized_connection_id = _text(connection_id)
    if normalized_project_id and normalized_connection_id:
        scopes.append(
            {
                **common,
                "scope_type": "connection",
                "project_id": normalized_project_id,
                "connection_id": normalized_connection_id,
                "max_effects": int(policy["connection_max"]),
            }
        )
    for scope in scopes:
        scope["scope_hash"] = external_effect_identity_hmac(
            purpose="rate-limit-bucket",
            value={
                "scope_type": scope["scope_type"],
                "account_id": scope["account_id"],
                "project_id": scope["project_id"],
                "connection_id": scope["connection_id"],
                "effect_kind": scope["effect_kind"],
                "channel": scope["channel"],
            },
        )
    return scopes


def build_external_effect_identity(
    *,
    account_id: str,
    project_id: str | None,
    extension_id: str,
    operation_id: str,
    connection_id: str | None,
    source_ref: dict[str, Any],
    request_payload: dict[str, Any],
    test_mode: bool,
    extension_install_id: str | None = None,
) -> dict[str, str]:
    fingerprint_payload = {
        "account_id": _text(account_id),
        "project_id": _text(project_id),
        "extension_id": _text(extension_id),
        "operation_id": _text(operation_id),
        "connection_id": _text(connection_id),
        "source_ref": _json_safe(
            _as_dict(source_ref),
            omit_host_retry_metadata=True,
        ),
        "request_payload": _as_dict(request_payload),
    }
    if _text(extension_install_id):
        fingerprint_payload["extension_install_id"] = _text(extension_install_id)
    request_fingerprint = external_effect_identity_hmac(
        purpose="request-fingerprint",
        value=fingerprint_payload,
    )
    return {
        "idempotency_key": request_fingerprint,
        "idempotency_scope_hash": external_effect_scope_hash(
            account_id=account_id,
            project_id=project_id,
            extension_id=extension_id,
            operation_id=operation_id,
            connection_id=connection_id,
            test_mode=test_mode,
            extension_install_id=extension_install_id,
        ),
        "request_fingerprint": request_fingerprint,
    }
