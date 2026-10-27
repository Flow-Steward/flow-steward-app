"""Safe projection and hydration for successful external-effect replay."""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from typing import Any

from core.application.extensions.extension_descriptor import llm_plaintext_secret_paths
from core.application.extensions.secret_reflection import value_exposes_secret
from core.infrastructure.db import external_effect_store
from core.infrastructure.security.external_effect_replay_codec import (
    decrypt_external_effect_replay,
    encrypt_external_effect_replay,
    replay_binding_hash,
)

MAX_REPLAY_PLAINTEXT_BYTES = 512 * 1024
MAX_REPLAY_NODES = 65_536
MAX_REPLAY_DEPTH = 64
CONFIRM_LEASE_ERROR_CODE = "external_effect_replay_lease_lost"
PERSISTENCE_ERROR_CODE = "external_effect_replay_persist_failed"
_PROTOCOL_RESPONSE_FIELDS = frozenset(
    {
        "ok",
        "delivered",
        "status",
        "provider_message_id",
        "external_object_id",
        "external_effect_status",
    }
)
_RESERVED_HOST_CONTROL_OUTPUTS = frozenset(
    {
        "commands",
        "events",
        "external_effect",
        "grant_outputs",
        "host_mutations",
        "produced_artifacts",
        "produced_datasets",
        "project_state_updates",
        "session",
        "session_update",
    }
)
_TOMBSTONE_CODES = {
    "retention_expired": "external_effect_replay_expired",
    "extension_uninstalled": "external_effect_replay_extension_uninstalled",
    "ciphertext_missing": "external_effect_replay_missing",
    "ciphertext_corrupt": "external_effect_replay_corrupt",
    "binding_mismatch": "external_effect_replay_binding_mismatch",
}


class ExternalEffectReplayProjectionError(ValueError):
    def __init__(self, code: str) -> None:
        self.code = str(code)
        super().__init__(self.code)


class ExternalEffectReplayPersistenceError(RuntimeError):
    def __init__(
        self,
        code: str = PERSISTENCE_ERROR_CODE,
        *,
        commit_outcome: str = "unknown",
    ) -> None:
        normalized_outcome = str(commit_outcome or "").strip().lower()
        if normalized_outcome not in {"rolled_back", "committed", "unknown"}:
            raise ValueError("invalid external-effect replay commit outcome")
        self.code = str(code)
        self.commit_outcome = normalized_outcome
        super().__init__(self.code)


def _as_dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _operation(extension: Mapping[str, Any], operation_id: str) -> dict[str, Any]:
    compiled = _as_dict(extension.get("compiled_manifest"))
    source_matches: list[list[dict[str, Any]]] = []
    for source in (extension.get("operation_manifest"), compiled.get("operation_manifest")):
        rows = source if isinstance(source, list) else []
        source_matches.append(
            [
                row
                for row in rows
                if isinstance(row, dict)
                and str(row.get("operation_id") or "").strip() == operation_id
            ]
        )
    if any(len(matches) > 1 for matches in source_matches):
        raise ExternalEffectReplayProjectionError(
            "external_effect_replay_operation_contract_invalid"
        )
    declared = [matches[0] for matches in source_matches if matches]
    if not declared:
        raise ExternalEffectReplayProjectionError(
            "external_effect_replay_operation_contract_invalid"
        )
    if len(declared) == 2 and declared[0] != declared[1]:
        raise ExternalEffectReplayProjectionError(
            "external_effect_replay_operation_contract_invalid"
        )
    return declared[0]


def _declared_output_names(operation: Mapping[str, Any]) -> frozenset[str]:
    has_outputs = "outputs" in operation
    has_public_outputs = "public_outputs" in operation
    if not has_outputs and not has_public_outputs:
        raise ExternalEffectReplayProjectionError(
            "external_effect_replay_operation_contract_invalid"
        )
    raw_outputs = operation.get("outputs") if has_outputs else operation.get("public_outputs", [])
    if (
        has_outputs
        and has_public_outputs
        and operation.get("outputs") != operation.get("public_outputs")
    ):
        raise ExternalEffectReplayProjectionError(
            "external_effect_replay_operation_contract_invalid"
        )
    if not isinstance(raw_outputs, list):
        raise ExternalEffectReplayProjectionError(
            "external_effect_replay_operation_contract_invalid"
        )
    names_list: list[str] = []
    for row in raw_outputs:
        if not isinstance(row, dict):
            raise ExternalEffectReplayProjectionError(
                "external_effect_replay_operation_contract_invalid"
            )
        name = str(row.get("name") or "").strip()
        if not name or name in names_list:
            raise ExternalEffectReplayProjectionError(
                "external_effect_replay_operation_contract_invalid"
            )
        names_list.append(name)
    names = frozenset(names_list)
    if not names:
        raise ExternalEffectReplayProjectionError(
            "external_effect_replay_operation_contract_invalid"
        )
    if names & _RESERVED_HOST_CONTROL_OUTPUTS:
        raise ExternalEffectReplayProjectionError(
            "external_effect_replay_operation_contract_invalid"
        )
    return names


def validate_external_effect_replay_contract(
    extension: Mapping[str, Any], operation_id: str
) -> None:
    """Fail closed on an unavailable or ambiguous installed replay contract."""
    _declared_output_names(_operation(extension, str(operation_id or "").strip()))


def _bounded_json_bytes(value: Any, *, max_plaintext_bytes: int) -> bytes:
    pending: list[tuple[Any, int]] = [(value, 0)]
    nodes = 0
    while pending:
        node, depth = pending.pop()
        nodes += 1
        if nodes > MAX_REPLAY_NODES or depth > MAX_REPLAY_DEPTH:
            raise ExternalEffectReplayProjectionError(
                "external_effect_replay_payload_budget_exceeded"
            )
        if isinstance(node, dict):
            pending.extend((key, depth + 1) for key in node)
            pending.extend((nested, depth + 1) for nested in node.values())
        elif isinstance(node, (list, tuple)):
            pending.extend((nested, depth + 1) for nested in node)
        elif node is not None and not isinstance(node, (str, int, float, bool)):
            raise ExternalEffectReplayProjectionError("external_effect_replay_payload_not_json")
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ExternalEffectReplayProjectionError(
            "external_effect_replay_payload_not_json"
        ) from exc
    if len(encoded) > max(1, int(max_plaintext_bytes)):
        raise ExternalEffectReplayProjectionError("external_effect_replay_payload_too_large")
    return encoded


def _safe_output_summaries(host_outputs: Mapping[str, Any], key: str) -> list[dict[str, Any]]:
    from core.application.extensions.external_effects import _produced_output_summary

    kind = "dataset" if key == "produced_datasets" else "artifact"
    return _produced_output_summary(host_outputs.get(key), kind=kind)


def project_external_effect_replay(
    *,
    extension: Mapping[str, Any],
    operation_id: str,
    invocation: Mapping[str, Any],
    protected_values: Iterable[str],
    host_outputs: Mapping[str, Any] | None = None,
    max_plaintext_bytes: int = MAX_REPLAY_PLAINTEXT_BYTES,
) -> dict[str, Any]:
    normalized_operation_id = str(operation_id or "").strip()
    operation = _operation(extension, normalized_operation_id)
    output_names = _declared_output_names(operation)
    response = _as_dict(invocation.get("response"))
    result = _as_dict(response.get("result"))
    safe_response = {
        key: value
        for key, value in response.items()
        if key in _PROTOCOL_RESPONSE_FIELDS or key in output_names
    }
    if result:
        safe_result = {
            key: value
            for key, value in result.items()
            if key in _PROTOCOL_RESPONSE_FIELDS or key in output_names
        }
        if safe_result:
            safe_response["result"] = safe_result

    projected: dict[str, Any] = {
        "status": str(invocation.get("status") or "").strip().lower(),
    }
    if safe_response:
        projected["response"] = safe_response
    trusted_outputs = host_outputs or {}
    for key in ("produced_artifacts", "produced_datasets"):
        rows = _safe_output_summaries(trusted_outputs, key)
        if rows:
            projected[key] = rows
    grants = _as_dict(trusted_outputs.get("grant_outputs"))
    safe_grants = {
        str(key): str(value)
        for key, value in grants.items()
        if str(key).strip() and str(value).strip()
    }
    if safe_grants:
        projected["grant_outputs"] = safe_grants

    encoded = _bounded_json_bytes(projected, max_plaintext_bytes=max_plaintext_bytes)
    if llm_plaintext_secret_paths(projected, path="replay"):
        raise ExternalEffectReplayProjectionError(
            "external_effect_replay_credential_field_forbidden"
        )
    if value_exposes_secret(projected, protected_values):
        raise ExternalEffectReplayProjectionError("external_effect_replay_secret_exposure")
    # Parsing the exact serialized bytes removes tuples and guarantees callers
    # receive the same JSON shape that will later be hydrated.
    return json.loads(encoded)


def serialize_external_effect_replay(
    replay: Mapping[str, Any],
    *,
    max_plaintext_bytes: int = MAX_REPLAY_PLAINTEXT_BYTES,
) -> bytes:
    return _bounded_json_bytes(dict(replay), max_plaintext_bytes=max_plaintext_bytes)


def external_effect_binding(effect: Mapping[str, Any]) -> dict[str, str]:
    return {
        key: str(effect.get(key) or "").strip()
        for key in (
            "effect_id",
            "account_id",
            "project_id",
            "extension_id",
            "extension_install_id",
            "connection_id",
            "operation_id",
            "idempotency_scope_hash",
            "idempotency_key",
            "request_fingerprint",
        )
    }


def encrypt_projected_replay(
    replay: Mapping[str, Any],
    *,
    effect: Mapping[str, Any],
) -> dict[str, Any]:
    plaintext = serialize_external_effect_replay(replay)
    binding = external_effect_binding(effect)
    return {
        "ciphertext": encrypt_external_effect_replay(plaintext, binding=binding),
        "binding_hash": replay_binding_hash(binding),
        "plaintext_bytes": len(plaintext),
    }


def replay_decision_for_effect(effect: Mapping[str, Any]) -> dict[str, Any]:
    state = str(effect.get("replay_state") or "").strip().lower()
    reason = str(effect.get("replay_reason") or "").strip().lower()
    if state == "nonreplayable":
        return {
            "should_spawn": False,
            "error": _TOMBSTONE_CODES.get(reason, "external_effect_replay_unavailable"),
            "invocation": {},
        }
    return {"should_spawn": False, "error": "", "invocation": {}}


def hydrate_external_effect_replay(
    db: Any,
    *,
    effect: Mapping[str, Any],
) -> dict[str, Any]:
    replay = external_effect_store.get_external_effect_replay(
        db,
        effect_id=str(effect.get("effect_id") or "").strip(),
        account_id=str(effect.get("account_id") or "").strip(),
    )
    if not replay:
        return replay_decision_for_effect(
            {"replay_state": "nonreplayable", "replay_reason": "ciphertext_missing"}
        )
    decision = replay_decision_for_effect(replay)
    if decision.get("error"):
        return decision
    ciphertext = str(replay.get("encrypted_payload") or "").strip()
    if not ciphertext:
        return replay_decision_for_effect(
            {"replay_state": "nonreplayable", "replay_reason": "ciphertext_missing"}
        )
    binding = external_effect_binding(effect)
    if str(replay.get("replay_binding_hash") or "") != replay_binding_hash(binding):
        return replay_decision_for_effect(
            {"replay_state": "nonreplayable", "replay_reason": "binding_mismatch"}
        )
    try:
        plaintext = decrypt_external_effect_replay(ciphertext, binding=binding)
        invocation = json.loads(plaintext)
        if not isinstance(invocation, dict):
            raise ValueError("external effect replay must decode to an object")
    except ValueError:
        return replay_decision_for_effect(
            {"replay_state": "nonreplayable", "replay_reason": "ciphertext_corrupt"}
        )
    return {"should_spawn": False, "error": "", "invocation": invocation}


def hydrate_external_effect_claim(
    db: Any,
    claim: Mapping[str, Any],
) -> dict[str, Any]:
    """Attach the canonical invocation to a completed duplicate claim."""
    hydrated = dict(claim)
    if claim.get("error"):
        return hydrated
    effect = _as_dict(claim.get("effect"))
    if claim.get("should_spawn") or str(effect.get("status") or "").lower() != "succeeded":
        return hydrated
    decision = hydrate_external_effect_replay(db, effect=effect)
    hydrated["replay_invocation"] = _as_dict(decision.get("invocation"))
    if decision.get("error"):
        hydrated["error"] = str(decision["error"])
    return hydrated


__all__ = [
    "CONFIRM_LEASE_ERROR_CODE",
    "ExternalEffectReplayPersistenceError",
    "ExternalEffectReplayProjectionError",
    "hydrate_external_effect_claim",
    "hydrate_external_effect_replay",
    "project_external_effect_replay",
    "replay_decision_for_effect",
    "validate_external_effect_replay_contract",
]
