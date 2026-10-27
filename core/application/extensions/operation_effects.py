from __future__ import annotations

import hashlib
from typing import Any

from core.application.management.command_models import (
    ExtensionActionResultView,
)
from core.application.management.errors import (
    ServiceError,
    ServiceValidationError,
)

from .extension_descriptor import is_llm_secret_field_name
from .operation_context import _ActionExecutionContext
from .operation_projection import _client_action_invocation

_DIRECT_INTENT_MAX_NODES = 65_536


_DIRECT_INTENT_MAX_DEPTH = 64


_DIRECT_INTENT_OMIT_KEYS = frozenset(
    {
        "authorization",
        "config",
        "connection",
        "connection_config",
        "cookie",
        "cookies",
        "credential",
        "credentials",
        "headers",
        "password",
        "project_state",
        "secret",
        "secret_refs",
        "secrets",
        "session",
        "token",
    }
)


_DIRECT_INTENT_VOLATILE_KEYS = frozenset(
    {
        "attempt",
        "attempt_id",
        "attempt_no",
        "attempt_number",
        "invocation_attempt_id",
        "retry_attempt",
        "retry_attempt_number",
        "runtime_token",
        "runtime_tokens",
    }
)


def _direct_action_intent_value(value: Any) -> Any:
    nodes = 0

    def visit(item: Any, depth: int, *, manifest_input: bool = False) -> Any:
        nonlocal nodes
        nodes += 1
        if nodes > _DIRECT_INTENT_MAX_NODES or depth > _DIRECT_INTENT_MAX_DEPTH:
            raise ServiceValidationError("Direct action intent exceeds the host identity budget")
        if item is None or type(item) in {str, int, float, bool}:
            return item
        if isinstance(item, dict):
            projected: dict[str, Any] = {}
            for raw_key, raw_value in item.items():
                key = str(raw_key)
                normalized_key = key.strip().lower()
                if not manifest_input and normalized_key in _DIRECT_INTENT_VOLATILE_KEYS:
                    raise ServiceValidationError(
                        "External-effect direct actions reject caller-supplied "
                        "retry and host runtime fields"
                    )
                if not manifest_input and normalized_key == "connection":
                    raise ServiceValidationError(
                        "External-effect direct actions accept only target.connection_id; "
                        "caller-supplied connection material is forbidden"
                    )
                if not manifest_input and (
                    normalized_key in _DIRECT_INTENT_OMIT_KEYS
                    or is_llm_secret_field_name(normalized_key)
                ):
                    raise ServiceValidationError(
                        "External-effect direct actions reject caller-supplied "
                        "credentials and host runtime fields"
                    )
                projected[key] = visit(
                    raw_value,
                    depth + 1,
                    manifest_input=manifest_input or (depth == 0 and normalized_key == "input"),
                )
            return projected
        if isinstance(item, (list, tuple)):
            return [visit(child, depth + 1, manifest_input=manifest_input) for child in item]
        raise ServiceValidationError("Direct action intent contains a non-JSON value")

    return visit(value, 0)


def _claim_action_external_effect(
    context: _ActionExecutionContext,
) -> ExtensionActionResultView | None:
    from core.application.extensions import external_effects

    declaration = external_effects.external_effect_declaration_for_operation(
        context.extension,
        context.action_id,
    )
    direct_action_key = context.direct_action_idempotency_key
    if declaration and not direct_action_key:
        raise ServiceValidationError(
            "Idempotency-Key is required for external-effect extension actions"
        )
    if declaration and (
        len(direct_action_key) > 255
        or any(ord(character) < 32 or ord(character) == 127 for character in direct_action_key)
    ):
        raise ServiceValidationError(
            "Idempotency-Key must be an opaque value of at most 255 characters"
        )
    intent_hash = (
        hashlib.sha256(direct_action_key.encode("utf-8")).hexdigest() if direct_action_key else ""
    )
    request_payload = dict(context.input_payload or {})
    if context.direct_action_intent:
        request_payload["__direct_action_intent"] = context.direct_action_intent

    source_ref = {
        "source": "extension_binding_service.execute_action",
        "actor_id": context.audit.get("actor_id"),
        "project_id": context.audit.get("project_id"),
        "direct_action_intent_hash": intent_hash,
    }
    from contextlib import nullcontext

    from .direct_effect_recovery import prepare_direct_effect_recovery

    preparation = (
        prepare_direct_effect_recovery(
            context, source_ref=source_ref, request_payload=request_payload
        )
        if declaration
        else nullcontext(
            {"source_ref": source_ref, "idempotency_key_override": intent_hash or None}
        )
    )
    with preparation as claim_identity:
        try:
            effect_claim = external_effects.claim_external_effect_for_action(
                context.bl,
                extension=context.extension,
                account_id=str(context.audit.get("account_id") or "").strip(),
                project_id=str(context.audit.get("project_id") or "").strip() or None,
                extension_id=context.extension_id,
                extension_install_id=str(
                    context.extension.get("extension_install_id")
                    or context.extension.get("install_id")
                    or ""
                ).strip()
                or None,
                connection_id=str(context.target.get("connection_id") or "").strip() or None,
                operation_id=context.action_id,
                source_type="api_action",
                **claim_identity,
                request_payload=request_payload,
                test_mode=bool(
                    context.body.context.get("test_mode") or context.target.get("test_mode")
                ),
            )
        except Exception as exc:
            raise ServiceError(
                "External effect claim failed",
                status_code=502,
                payload={
                    "extension_id": context.extension_id,
                    "action_id": context.action_id,
                    "detail": str(exc),
                },
            ) from exc

    if effect_claim.get("declared") and not effect_claim.get("should_spawn"):
        from core.application.extensions.external_effect_replay import (
            hydrate_external_effect_claim,
        )

        effect_claim = hydrate_external_effect_claim(context.bl, effect_claim)

    raw_effect = effect_claim.get("effect")
    context.external_effect = dict(raw_effect) if isinstance(raw_effect, dict) else {}
    context.external_effect_id = str(context.external_effect.get("effect_id") or "").strip()
    context.external_effect_claim_token = str(
        context.external_effect.get("claim_token") or ""
    ).strip()
    if not effect_claim.get("declared") or effect_claim.get("should_spawn"):
        return None
    if effect_claim.get("error"):
        error_code = str(effect_claim.get("error") or "external_effect_claim_failed")
        raise ServiceError(
            error_code,
            status_code=(409 if error_code == "external_effect_idempotency_conflict" else 502),
            payload={
                "extension_id": context.extension_id,
                "action_id": context.action_id,
                "code": error_code,
                "external_effect": context.external_effect,
            },
        )
    effect_status = str(context.external_effect.get("status") or "").lower()
    replay_invocation = effect_claim.get("replay_invocation")
    if effect_status == "succeeded" and isinstance(replay_invocation, dict):
        return ExtensionActionResultView(
            extension_id=context.extension_id,
            action_id=context.action_id,
            action=_client_action_invocation(replay_invocation),
        )
    return ExtensionActionResultView(
        extension_id=context.extension_id,
        action_id=context.action_id,
        action={
            "status": ("succeeded" if effect_status in {"succeeded", "suppressed"} else "failed"),
            "detail": (f"external_effect_{context.external_effect.get('status') or 'deduped'}"),
            "response": {"external_effect": context.external_effect},
        },
    )


def _mark_action_external_effect_running(
    context: _ActionExecutionContext,
) -> None:
    if not context.external_effect_id:
        return
    context.runtime_ctx["external_effect_id"] = context.external_effect_id
    try:
        from core.application.extensions.external_effects import (
            mark_external_effect_running,
        )

        running_effect = mark_external_effect_running(
            context.bl,
            effect_id=context.external_effect_id,
            claim_token=context.external_effect_claim_token,
        )
        if not running_effect:
            raise ServiceError(
                "External effect lease was reclaimed before invocation started",
                status_code=409,
            )
    except ServiceError:
        raise
    except Exception as exc:
        raise ServiceError(
            "External effect lease could not be confirmed before invocation",
            status_code=503,
        ) from exc
