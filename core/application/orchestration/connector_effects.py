"""Connector external-effect action identity and durable claim policy."""

from __future__ import annotations

from typing import Any

from core.application.orchestration.connector_mapping import (
    _TOKEN_EXTENSION,
    CONNECTOR_PREFIX,
    _to_object,
)


def _derive_extension_action(
    *,
    connector_id: str,
    extension_id: str,
    action_id: str,
    operation_id: str,
) -> tuple[str, str]:
    ext = str(extension_id or "").strip()
    action = str(action_id or operation_id or "").strip()
    normalized_connector_id = str(connector_id or "").strip()
    if ext and action:
        return ext, action
    if normalized_connector_id.startswith(CONNECTOR_PREFIX):
        return ext, action
    if normalized_connector_id.startswith(_TOKEN_EXTENSION):
        if not ext:
            ext = normalized_connector_id.removeprefix(_TOKEN_EXTENSION).strip()
        return ext, action
    if "." not in normalized_connector_id:
        return ext, action
    inferred_ext, _, inferred_action = normalized_connector_id.rpartition(".")
    if not ext:
        ext = inferred_ext.strip()
    if not action:
        action = inferred_action.strip()
    return ext, action


def _source_ref_from_runtime_context(runtime_context: dict[str, Any]) -> dict[str, Any]:
    source_ref = {
        "workflow_id": runtime_context.get("workflow_id"),
        "job_id": runtime_context.get("job_id"),
        "step_id": runtime_context.get("step_id"),
    }
    group_id = str(runtime_context.get("group_id") or "").strip()
    if group_id:
        source_ref["group_id"] = group_id
    for_each_id = str(runtime_context.get("for_each_id") or "").strip()
    if for_each_id:
        source_ref["for_each_id"] = for_each_id
    loop = _to_object(runtime_context.get("loop"))
    if loop:
        source_ref["loop"] = loop
    return source_ref


def _external_effect_runtime_identity_error(
    *,
    extension: dict[str, Any],
    operation_id: str,
    runtime_context: dict[str, Any],
) -> dict[str, Any] | None:
    from core.application.extensions.external_effects import (
        external_effect_declaration_for_operation,
    )

    if not external_effect_declaration_for_operation(extension, operation_id):
        return None
    required_fields = (
        "job_id",
        "workflow_id",
        "account_id",
        "project_id",
        "workspace_id",
        "step_id",
    )
    missing = [
        field for field in required_fields if not str(runtime_context.get(field) or "").strip()
    ]
    if not missing:
        return None
    field_list = ", ".join(missing)
    return {
        "code": "external_effect_runtime_identity_missing",
        "missing_fields": missing,
        "message": f"External effect requires trusted runtime identity: {field_list}",
    }


def _claim_external_effect_for_action(
    bl: Any,
    *,
    extension: dict[str, Any],
    extension_id: str,
    extension_install_id: str,
    connection_id: str,
    action_id: str,
    input_payload: dict[str, Any],
    runtime_context: dict[str, Any],
) -> dict[str, Any]:
    from core.application.extensions import external_effects

    with_context = dict(runtime_context or {})
    ref = with_context.get("external_effect_resolution_ref")
    if (
        ref is not None
        and _resolved_install_id(extension) != str(extension_install_id or "").strip()
    ):
        return {
            "declared": bool(
                external_effects.external_effect_declaration_for_operation(extension, action_id)
            ),
            "effect": {},
            "should_spawn": False,
            "error": "external_effect_resolution_install_mismatch",
        }
    if ref is not None and (not isinstance(ref, dict) or not ref):
        raise ValueError("external_effect_resolution_reference_invalid")
    source_ref = _source_ref_from_runtime_context(with_context)
    intent_key = external_effects.external_effect_identity_hmac(
        purpose="workflow-intent",
        value={
            "account_id": str(with_context.get("account_id") or "").strip(),
            "project_id": str(with_context.get("project_id") or "").strip(),
            "extension_id": extension_id,
            "extension_install_id": extension_install_id,
            "connection_id": connection_id,
            "operation_id": action_id,
            "workflow_id": source_ref.get("workflow_id"),
            "job_id": source_ref.get("job_id"),
            # The real group scope, never its execution id: a retry or a resumed
            # run of the same step and loop item must claim the same effect.
            "group_id": source_ref.get("group_id"),
            "step_id": source_ref.get("step_id"),
            "loop": source_ref.get("loop"),
            "test_mode": bool(with_context.get("test_mode")),
        },
    )
    return external_effects.claim_external_effect_for_action(
        bl,
        extension=extension,
        account_id=str(with_context.get("account_id") or "").strip(),
        project_id=str(with_context.get("project_id") or "").strip() or None,
        extension_id=extension_id,
        extension_install_id=extension_install_id or None,
        connection_id=connection_id or None,
        operation_id=action_id,
        source_type="workflow_step",
        source_ref=source_ref,
        request_payload=dict(input_payload or {}),
        idempotency_key_override=intent_key,
        test_mode=bool(with_context.get("test_mode")),
        resolution_ref=ref,
    )


def _resolved_install_id(extension: dict[str, Any]) -> str:
    """The install the runtime resolved for a step saved without connector_install_id.

    Such a step still names its extension, and an account holds at most one install
    of it. Adopting that install keeps the test-mode connection check and the
    external-effect claim on the same identity the connection was bound to.
    """
    return str(extension.get("extension_install_id") or extension.get("install_id") or "").strip()
