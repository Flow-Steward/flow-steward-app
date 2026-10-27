"""Application coordinator for extension-backed external-effect delivery."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Protocol

from core.application.extensions.pre_dispatch_delivery import (
    PreDispatchDeliveryError,
    execute_emits_pre_dispatch_kind,
    pre_dispatch_host_mutation_kinds,
    validate_prepare_invocation,
)


class DeliveryInvocationPort(Protocol):
    def __call__(self, request_payload: dict[str, Any]) -> dict[str, Any]: ...


class DeliveryPersistencePort(Protocol):
    def __call__(self, invocation: dict[str, Any]) -> None: ...


class MarkExternalEffectRunningPort(Protocol):
    def __call__(self, effect_id: str, claim_token: str) -> dict[str, Any] | None: ...


def _mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _failure(code: str) -> dict[str, Any]:
    return {
        "status": "failed",
        "detail": code,
        "response": {
            "ok": False,
            "error": code,
            "external_effect_status": "failed",
            "definitely_no_external_effect": True,
        },
    }


def provider_payload_for_effect_claim(
    extension: dict[str, Any],
    operation_id: str,
    provider_payload: dict[str, Any],
) -> dict[str, Any]:
    """Exclude secrets/config from a claim that precedes a prepare phase."""

    if not pre_dispatch_host_mutation_kinds(extension, operation_id):
        return provider_payload
    return {
        "provider_key": provider_payload.get("provider_key"),
        "integration_key": provider_payload.get("integration_key"),
        "project_id": provider_payload.get("project_id"),
    }


def coordinate_extension_delivery(
    *,
    extension: dict[str, Any],
    operation_id: str,
    runtime_connection: dict[str, Any],
    action_payload: dict[str, Any],
    provider_payload: dict[str, Any],
    event: dict[str, Any],
    route_payload: dict[str, Any],
    external_effect_id: str,
    external_effect_claim_token: str,
    invoke: DeliveryInvocationPort,
    persist: DeliveryPersistencePort,
    mark_running: MarkExternalEffectRunningPort,
    report_persistence_error: Callable[[str, Exception], None] | None = None,
) -> dict[str, Any]:
    """Run prepare/persist/lease/execute in the only safe external-effect order."""

    pre_dispatch_kinds = pre_dispatch_host_mutation_kinds(extension, operation_id)
    invocation: dict[str, Any] = {}
    prepared_delivery: dict[str, Any] | None = None

    if pre_dispatch_kinds and (not external_effect_id or not external_effect_claim_token):
        invocation = _failure("external_effect_claim_required")

    if pre_dispatch_kinds and not invocation:
        prepare_connection = {
            key: value
            for key, value in runtime_connection.items()
            if key not in {"secrets", "secret_refs"}
        }
        prepare_action_payload = {
            "action_id": operation_id,
            "target": ({"connection": prepare_connection} if prepare_connection else {}),
        }
        prepare_provider_payload = {
            "provider_key": provider_payload.get("provider_key"),
            "integration_key": provider_payload.get("integration_key"),
            "project_id": provider_payload.get("project_id"),
            "provider_config": _mapping(prepare_connection.get("config")),
            "connection_routes": list(prepare_connection.get("connection_routes") or []),
            "ingress_config": _mapping(prepare_connection.get("ingress_config")),
            "workflow_mappings": _mapping(prepare_connection.get("workflow_mappings")),
        }
        try:
            prepare_invocation = invoke(
                {
                    "action": operation_id,
                    "delivery_phase": "prepare",
                    "action_payload": prepare_action_payload,
                    "event": event,
                    "route": route_payload,
                    "provider": prepare_provider_payload,
                }
            )
        except Exception:
            prepare_invocation = _failure("pre_dispatch_prepare_failed")
        try:
            prepared = validate_prepare_invocation(
                prepare_invocation, allowed_kinds=pre_dispatch_kinds
            )
            persist(prepare_invocation)
            if prepare_invocation.get("status") != "succeeded":
                raise PreDispatchDeliveryError("pre_dispatch_persistence_failed")
            prepared_delivery = prepared.payload
        except PreDispatchDeliveryError as exc:
            invocation = _failure(exc.code)
        except Exception as exc:
            if report_persistence_error is not None:
                report_persistence_error("pre_dispatch", exc)
            invocation = _failure("pre_dispatch_persistence_failed")

    if not invocation and external_effect_id:
        try:
            running_effect = mark_running(
                external_effect_id,
                external_effect_claim_token,
            )
            if not running_effect:
                invocation = _failure("external_effect_lease_reclaimed")
        except Exception:
            invocation = _failure("external_effect_lease_unavailable")

    if not invocation:
        request_payload = {
            "action": operation_id,
            "action_payload": action_payload,
            "event": event,
            "route": route_payload,
            "provider": provider_payload,
        }
        if pre_dispatch_kinds:
            request_payload.update(
                {
                    "delivery_phase": "execute",
                    "prepared_delivery": prepared_delivery,
                }
            )
        try:
            invocation = invoke(request_payload)
        except Exception:
            if not external_effect_id:
                raise
            from core.application.extensions.external_effects import (
                external_effect_spawn_exception_invocation,
            )

            invocation = external_effect_spawn_exception_invocation()
        if pre_dispatch_kinds and execute_emits_pre_dispatch_kind(invocation, pre_dispatch_kinds):
            invocation = {
                **_mapping(invocation),
                "status": "failed",
                "detail": "execute_pre_dispatch_host_mutation_forbidden",
            }

    if invocation.get("status") == "succeeded":
        try:
            persist(invocation)
        except Exception as exc:
            if report_persistence_error is not None:
                report_persistence_error("execute", exc)
            invocation = {
                **_mapping(invocation),
                "status": "failed",
                "detail": "project_state_update_failed",
                "errors": [
                    *list(_mapping(invocation).get("errors") or []),
                    {"code": "project_state_update_failed", "message": str(exc)},
                ],
            }
    return invocation


__all__ = [
    "DeliveryInvocationPort",
    "DeliveryPersistencePort",
    "MarkExternalEffectRunningPort",
    "coordinate_extension_delivery",
    "provider_payload_for_effect_claim",
]
