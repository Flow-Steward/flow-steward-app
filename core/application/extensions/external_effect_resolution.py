"""Resolve a known ambiguous ledger effect without inventing a provider result."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from core.application.extensions.external_effect_contracts import stable_json_hash
from core.application.extensions.external_effect_replay import (
    external_effect_binding,
    hydrate_external_effect_replay,
)
from core.application.extensions.external_effect_resolution_repository import (
    ExternalEffectResolutionRepository,
)
from core.infrastructure.db.db_pool import (
    PooledConnectionProxy,
    active_transaction_connection,
    transaction_connection,
)
from core.infrastructure.security.external_effect_replay_codec import replay_binding_hash
from core.infrastructure.security.secret_detection import redact_detected_secrets

RESOLUTION_COMMAND = "external_effect.resolution"
CONSUMPTION_COMMAND = "external_effect.resolution.consume"
RETRY_AUTHORIZED = "retry_authorized"
PENDING_RETRY_REASONS = frozenset({"operator_confirmed_no_effect", "approval_retry_authorized"})
_INTENT_FIELDS = (
    "account_id",
    "project_id",
    "extension_id",
    "extension_install_id",
    "connection_id",
    "operation_id",
    "effect_kind",
    "channel",
    "source_type",
    "idempotency_key",
    "idempotency_scope_hash",
    "request_fingerprint",
)


class ExternalEffectResolutionError(ValueError):
    """Stable safe domain failure; never includes a provider payload or lease token."""

    def __init__(self, code: str) -> None:
        self.code = f"external_effect_resolution_{code}"
        super().__init__(self.code)


def resolution_command_id(*, account_id: str, effect_id: str, fenced_version: int) -> str:
    return str(
        uuid5(
            NAMESPACE_URL,
            f"flow-steward:effect-resolution:{account_id}:{effect_id}:{fenced_version}",
        )
    )


def effect_intent(effect: Mapping[str, Any]) -> dict[str, Any]:
    """The complete persisted logical intent, independent of a lease rotation."""
    return {
        **{key: str(effect.get(key) or "") for key in _INTENT_FIELDS},
        "source_ref_jsonb": dict(effect.get("source_ref_jsonb") or {}),
        "test_mode": bool(effect.get("test_mode")),
    }


def _original_failure(effect: Mapping[str, Any]) -> dict[str, str]:
    """Keep bounded failure provenance even after an authorized retry clears the row."""
    limits = {"status": 64, "status_reason": 500, "error_code": 200, "error_text": 1000}
    summary = json.dumps(effect.get("response_summary_jsonb") or {}, default=str)
    return {
        **{
            key: redact_detected_secrets(str(effect.get(key) or ""))[:limit]
            for key, limit in limits.items()
        },
        "response_summary_redacted": redact_detected_secrets(summary)[:4096],
    }


def _uuid(value: Any) -> str:
    try:
        return str(UUID(str(value)))
    except (TypeError, ValueError, AttributeError) as exc:
        raise ExternalEffectResolutionError("not_found") from exc


def resolve_external_effect(
    db: Any,
    *,
    account_id: str,
    project_id: str | None,
    effect_id: str,
    observed_version: int,
    action: str,
    comment: str,
    actor_id: str,
    actor_type: str = "user",
    approval_id: str = "",
    expected_source_ref: Mapping[str, Any] | None = None,
    account_wide_authorized: bool = False,
    authorization: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Persist decision/audit/fence together; nest inside Approval's job transaction.

    Projectless scope is exact NULL scope, never an account-wide wildcard.
    Workflow callers must supply the host's exact persisted source reference.
    """
    if action not in {"approve", "reject", RETRY_AUTHORIZED}:
        raise ExternalEffectResolutionError("action_invalid")
    if action == RETRY_AUTHORIZED and not isinstance(authorization, Mapping):
        raise ExternalEffectResolutionError("authorization_required")
    authorization_data = dict(authorization) if isinstance(authorization, Mapping) else {}
    reason = str(comment or "").strip()
    if action != RETRY_AUTHORIZED and not reason:
        raise ExternalEffectResolutionError("comment_required")
    if not str(actor_id or "").strip():
        raise ExternalEffectResolutionError("actor_required")
    if not project_id and not account_wide_authorized:
        raise ExternalEffectResolutionError("account_scope_required")
    account_id, effect_id = _uuid(account_id), _uuid(effect_id)
    project_id = _uuid(project_id) if project_id else None
    if type(observed_version) is not int or observed_version < 1:
        raise ExternalEffectResolutionError("version_invalid")
    fenced_version = observed_version + 1
    command_id = resolution_command_id(
        account_id=account_id, effect_id=effect_id, fenced_version=fenced_version
    )
    request_hash = stable_json_hash(
        {
            "action": action,
            "comment": reason,
            "actor_id": actor_id,
            "actor_type": actor_type,
            "approval_id": approval_id,
            **({"authorization": dict(authorization)} if authorization is not None else {}),
        }
    )
    with transaction_connection(db) as conn:
        repository = ExternalEffectResolutionRepository(conn)
        effect = repository.lock_effect(
            account_id=account_id, project_id=project_id, effect_id=effect_id
        )
        if effect is None:
            raise ExternalEffectResolutionError("not_found")
        source = effect.get("source_ref_jsonb") or {}
        if (effect.get("source_type") == "workflow_step" and expected_source_ref is None) or (
            expected_source_ref is not None and dict(expected_source_ref) != source
        ):
            raise ExternalEffectResolutionError("source_mismatch")
        existing = repository.receipt(account_id=account_id, command_id=command_id)
        if existing is not None:
            if (
                existing["command_type"] != RESOLUTION_COMMAND
                or existing["payload"].get("request_hash") != request_hash
            ):
                raise ExternalEffectResolutionError("already_resolved")
            return dict(existing["result"])
        # The fenced no-effect version is NOT a new ambiguity. A freshly loaded
        # tab must wait for the authorized retry to produce a new observation.
        if effect.get("status_reason") in PENDING_RETRY_REASONS:
            raise ExternalEffectResolutionError("already_resolved")
        if int(effect["claim_version"]) != observed_version:
            raise ExternalEffectResolutionError("stale")
        if effect.get("status") != "timeout_unknown":
            raise ExternalEffectResolutionError("not_ambiguous")
        intent = effect_intent(effect)
        if action == RETRY_AUTHORIZED and (
            authorization_data.get("approval_id") != approval_id
            or type(authorization_data.get("approved")) is not bool
            or (authorization_data.get("association") or {}).get("effect", {}).get("intent")
            != intent
        ):
            raise ExternalEffectResolutionError("authorization_invalid")
        replay_available = False
        pending_outputs = action == "approve" and repository.has_pending_outputs(effect)
        if action == "approve" and not pending_outputs:
            replay_available = bool(
                hydrate_external_effect_replay(conn, effect=effect).get("invocation")
            )
        fenced = repository.fence(effect, action=action)
        if action == "approve" and not replay_available:
            repository.tombstone_replay(
                fenced,
                binding_hash=replay_binding_hash(external_effect_binding(fenced)),
                reason=(
                    "operator_confirmed_success_outputs_unverified"
                    if pending_outputs
                    else "operator_confirmed_success_without_response"
                ),
            )
        resolution_ref = {
            "command_id": command_id,
            "effect_id": effect_id,
            "fenced_version": fenced_version,
            "approval_id": str(approval_id),
        }
        if action == RETRY_AUTHORIZED:
            resolution_ref["phase"] = "issued"
        result = {
            "resolution_ref": resolution_ref,
            "action": action,
            "observed_version": observed_version,
            "fenced_version": fenced_version,
        }
        repository.insert_receipt(
            account_id=account_id,
            command_id=command_id,
            command_type=RESOLUTION_COMMAND,
            actor_type=actor_type,
            actor_id=actor_id,
            target={"effect_id": effect_id, "project_id": project_id},
            payload={
                "intent": intent,
                "original_failure": _original_failure(effect),
                "request_hash": request_hash,
                "action": action,
                "comment": reason,
                "approval_id": str(approval_id),
                "observed_version": observed_version,
                "fenced_version": fenced_version,
                **({"authorization": dict(authorization)} if authorization is not None else {}),
            },
            result=result,
        )
    return result


def claim_resolved_external_effect(
    db: Any,
    *,
    resolution_ref: Mapping[str, Any],
    intent: Mapping[str, Any],
    expires_at: str | None,
) -> tuple[dict[str, Any], bool]:
    """Consume only after durable receipt validation and return only after commit.

    A provider must never receive spawn permission inside an enclosing transaction.
    A commit exception (including an unknown outcome) therefore returns no claim.
    """
    if not isinstance(resolution_ref, Mapping):
        raise ExternalEffectResolutionError("reference_invalid")
    target = getattr(db, "conn", None) or db
    if active_transaction_connection(db) is not None or (
        not isinstance(target, PooledConnectionProxy)
        and getattr(target, "autocommit", None) is not True
    ):
        raise ExternalEffectResolutionError("commit_required")
    account_id = _uuid(intent.get("account_id"))
    effect_id = _uuid(resolution_ref.get("effect_id"))
    project_id = str(intent.get("project_id") or "") or None
    with transaction_connection(db) as conn:
        repository = ExternalEffectResolutionRepository(conn)
        effect = repository.lock_effect(
            account_id=account_id, project_id=project_id, effect_id=effect_id
        )
        if effect is None:
            raise ExternalEffectResolutionError("not_found")
        command_id = str(resolution_ref.get("command_id") or "")
        receipt = repository.receipt(account_id=account_id, command_id=command_id)
        if (
            receipt is None
            or receipt["command_type"] != RESOLUTION_COMMAND
            or receipt["result"].get("resolution_ref") != dict(resolution_ref)
        ):
            raise ExternalEffectResolutionError("reference_invalid")
        if receipt["payload"].get("intent") != dict(intent) or effect_intent(effect) != dict(
            intent
        ):
            raise ExternalEffectResolutionError("intent_mismatch")
        action = receipt["payload"].get("action")
        if action not in {"reject", RETRY_AUTHORIZED}:
            raise ExternalEffectResolutionError("not_retryable")
        if action == RETRY_AUTHORIZED and resolution_ref.get("phase") != "issued":
            raise ExternalEffectResolutionError("reference_invalid")
        consumption_id = str(
            uuid5(NAMESPACE_URL, f"flow-steward:effect-resolution-consume:{command_id}")
        )
        if repository.receipt(account_id=account_id, command_id=consumption_id) is not None:
            return effect, False
        if (
            int(effect["claim_version"]) != receipt["payload"]["fenced_version"]
            or effect.get("status") != "timeout_unknown"
            or effect.get("status_reason")
            != (
                "approval_retry_authorized"
                if action == RETRY_AUTHORIZED
                else "operator_confirmed_no_effect"
            )
        ):
            raise ExternalEffectResolutionError("stale")
        claimed = repository.claim_retry(effect, expires_at=expires_at)
        repository.tombstone_replay(
            claimed,
            binding_hash=replay_binding_hash(external_effect_binding(claimed)),
            reason="operator_retry_authorized",
        )
        repository.insert_receipt(
            account_id=account_id,
            command_id=consumption_id,
            command_type=CONSUMPTION_COMMAND,
            actor_type="system_actor",
            actor_id="external_effect_claim",
            target={"effect_id": effect_id, "project_id": project_id},
            payload={
                "resolution_command_id": command_id,
                "fenced_version": receipt["payload"]["fenced_version"],
            },
            result={"effect_id": effect_id, "claim_version": claimed["claim_version"]},
        )
    return claimed, True
