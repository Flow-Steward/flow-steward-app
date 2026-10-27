"""Provider-neutral contract for two-phase external-effect delivery."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from typing import Any

from core.application.extensions.response_envelope import response_host_mutations

MAX_PREPARED_DELIVERY_BYTES = 8192
MAX_PREPARED_DELIVERY_DEPTH = 8
_SECRET_KEY_MARKERS = (
    "api_key",
    "apikey",
    "authorization",
    "access_key",
    "credential",
    "password",
    "private_key",
    "secret",
    "token",
)


class PreDispatchDeliveryError(ValueError):
    """Raised when a two-phase delivery response violates the host contract."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class PreparedDelivery:
    """Validated opaque state that the host may pass to the execute phase."""

    payload: dict[str, Any]


def _mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def pre_dispatch_host_mutation_kinds(
    extension: dict[str, Any], operation_id: str
) -> tuple[str, ...]:
    """Return the manifest-declared pre-dispatch mutation capability."""

    compiled_manifest = _mapping(extension.get("compiled_manifest"))
    action_definitions = extension.get("action_definitions") or compiled_manifest.get(
        "action_definitions"
    )
    for raw_action in action_definitions or []:
        action = _mapping(raw_action)
        if str(action.get("action_id") or "").strip() != operation_id:
            continue
        handler = _mapping(action.get("handler"))
        config = _mapping(handler.get("config"))
        declared = config.get("pre_dispatch_host_mutations")
        if not isinstance(declared, list):
            return ()
        return tuple(item.strip() for item in declared if isinstance(item, str) and item.strip())
    return ()


def _mutation_kinds(invocation: dict[str, Any]) -> tuple[str, ...]:
    try:
        raw_mutations = response_host_mutations(invocation, strict_types=True)
    except ValueError as exc:
        raise PreDispatchDeliveryError("prepare_host_mutation_forbidden") from exc
    if not raw_mutations:
        return ()
    if not isinstance(raw_mutations, list):
        raise PreDispatchDeliveryError("prepare_host_mutation_forbidden")
    kinds: list[str] = []
    for raw_mutation in raw_mutations:
        mutation = _mapping(raw_mutation)
        kind = str(mutation.get("kind") or "").strip()
        if not kind:
            raise PreDispatchDeliveryError("prepare_host_mutation_forbidden")
        kinds.append(kind)
    return tuple(kinds)


def _validate_json_value(value: Any, *, depth: int = 1) -> None:
    if depth > MAX_PREPARED_DELIVERY_DEPTH:
        raise PreDispatchDeliveryError("prepared_delivery_too_deep")
    if value is None or isinstance(value, (str, bool, int)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise PreDispatchDeliveryError("prepared_delivery_invalid_json")
        return
    if isinstance(value, list):
        for item in value:
            _validate_json_value(item, depth=depth + 1)
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise PreDispatchDeliveryError("prepared_delivery_invalid_json")
            normalized_key = key.strip().lower().replace("-", "_")
            if any(marker in normalized_key for marker in _SECRET_KEY_MARKERS):
                raise PreDispatchDeliveryError("prepared_delivery_secret_key_forbidden")
            _validate_json_value(item, depth=depth + 1)
        return
    raise PreDispatchDeliveryError("prepared_delivery_invalid_json")


def validate_prepare_invocation(
    invocation: dict[str, Any], *, allowed_kinds: tuple[str, ...]
) -> PreparedDelivery:
    """Validate a prepare response before any host mutation is persisted."""

    if str(invocation.get("status") or "").strip().lower() != "succeeded":
        raise PreDispatchDeliveryError("prepare_invocation_failed")
    response = _mapping(invocation.get("response"))
    if response.get("ok") is not True:
        raise PreDispatchDeliveryError("prepare_response_not_ok")
    if response.get("delivered") not in (None, False) or any(
        field in response for field in ("provider_message_id", "message_id")
    ):
        raise PreDispatchDeliveryError("prepare_external_effect_forbidden")

    allowed = set(allowed_kinds)
    if any(kind not in allowed for kind in _mutation_kinds(invocation)):
        raise PreDispatchDeliveryError("prepare_host_mutation_forbidden")

    payload = response.get("prepared_delivery")
    if not isinstance(payload, dict):
        raise PreDispatchDeliveryError("prepared_delivery_invalid_json")
    _validate_json_value(payload)
    try:
        encoded = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise PreDispatchDeliveryError("prepared_delivery_invalid_json") from exc
    if len(encoded) > MAX_PREPARED_DELIVERY_BYTES:
        raise PreDispatchDeliveryError("prepared_delivery_too_large")
    return PreparedDelivery(payload=json.loads(encoded))


def execute_emits_pre_dispatch_kind(invocation: dict[str, Any], kinds: tuple[str, ...]) -> bool:
    """Return whether execute tried to emit a mutation reserved for prepare."""

    if not kinds:
        return False
    try:
        return any(kind in set(kinds) for kind in _mutation_kinds(invocation))
    except PreDispatchDeliveryError:
        return True
