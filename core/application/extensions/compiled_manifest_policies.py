"""Domain policies for extension effects, action execution and provider contracts."""

from __future__ import annotations

from typing import Any

from core.application.extensions.runtime_host_grants import own_runtime_grants
from core.infrastructure.extension_runtime.extension_runtime_validation import (
    _as_dict,
    _as_list,
    _issue,
)

_EXTERNAL_EFFECT_IDEMPOTENCY_VALUES = {"required"}
#: Only this kind renders one route form from a single target kind.
NOTIFICATION_PROVIDER_KIND = "notification_provider"
_EXTERNAL_EFFECT_TEST_MODE_VALUES = {"suppress"}
_ACTION_CONTRACT_FIELDS = {"background", "serialization"}
_SERIALIZATION_FIELDS = {"key_fields", "resource"}
_BACKGROUND_POLL_FIELDS = {
    "interval_field",
    "kind",
    "mode_field",
    "transport_policy",
}


def _validate_action_runtime_contract(
    value: Any,
    *,
    issues: list[dict],
    manifest_path: str,
    field_path: str,
) -> None:
    if value in (None, ""):
        return
    if not isinstance(value, dict) or not set(value) <= _ACTION_CONTRACT_FIELDS:
        issues.append(
            _issue(
                severity="error",
                code="invalid_action_runtime_contract",
                message="action contract contains an unknown field or is not an object",
                file_path=manifest_path,
                field_path=field_path,
            )
        )
        return
    serialization = value.get("serialization")
    if serialization is not None:
        if not isinstance(serialization, dict) or not set(serialization) <= _SERIALIZATION_FIELDS:
            issues.append(
                _issue(
                    severity="error",
                    code="invalid_action_serialization",
                    message="contract.serialization supports only resource and key_fields",
                    file_path=manifest_path,
                    field_path=f"{field_path}.serialization",
                )
            )
        else:
            resource = serialization.get("resource")
            key_fields = serialization.get("key_fields", [])
            valid_resource = resource is None or (
                isinstance(resource, str) and 0 < len(resource.strip()) <= 128
            )
            valid_keys = (
                isinstance(key_fields, list)
                and len(key_fields) <= 8
                and all(
                    isinstance(item, str) and 0 < len(item.strip()) <= 128 for item in key_fields
                )
                and len(set(key_fields)) == len(key_fields)
            )
            if not valid_resource or not valid_keys or (resource is None and not key_fields):
                issues.append(
                    _issue(
                        severity="error",
                        code="invalid_action_serialization",
                        message=(
                            "contract.serialization requires a non-empty resource or "
                            "one to eight unique key_fields"
                        ),
                        file_path=manifest_path,
                        field_path=f"{field_path}.serialization",
                    )
                )
    background = value.get("background")
    if background is None:
        return
    if (
        not isinstance(background, dict)
        or set(background) != _BACKGROUND_POLL_FIELDS
        or background.get("kind") != "connection_poll"
        or background.get("transport_policy") != "webhook_preferred"
        or not all(
            isinstance(background.get(name), str) and str(background[name]).strip()
            for name in ("interval_field", "mode_field")
        )
        or serialization is None
    ):
        issues.append(
            _issue(
                severity="error",
                code="invalid_background_action_contract",
                message=(
                    "connection_poll background actions require the closed interval, mode, "
                    "transport, and serialization contract"
                ),
                file_path=manifest_path,
                field_path=f"{field_path}.background",
            )
        )


def _validate_external_effect_observability(
    value: Any,
    *,
    issues: list[dict],
    manifest_path: str,
    field_path: str,
) -> dict[str, Any]:
    if value in (None, ""):
        return {}
    if not isinstance(value, dict):
        issues.append(
            _issue(
                severity="error",
                code="invalid_external_effect_observability",
                message="external_effects[].observability must be an object",
                file_path=manifest_path,
                field_path=field_path,
            )
        )
        return {}

    declaration = _as_dict(value)
    normalized: dict[str, Any] = {}
    raw_target_fields = declaration.get("target_fields")
    if raw_target_fields not in (None, ""):
        if not isinstance(raw_target_fields, list):
            issues.append(
                _issue(
                    severity="error",
                    code="invalid_external_effect_target_fields",
                    message="external_effects[].observability.target_fields must be a list",
                    file_path=manifest_path,
                    field_path=f"{field_path}.target_fields",
                )
            )
        else:
            target_fields = [str(item or "").strip() for item in raw_target_fields]
            if any(not item for item in target_fields) or len(set(target_fields)) != len(
                target_fields
            ):
                issues.append(
                    _issue(
                        severity="error",
                        code="invalid_external_effect_target_fields",
                        message=(
                            "external_effects[].observability.target_fields must contain "
                            "unique non-empty field names"
                        ),
                        file_path=manifest_path,
                        field_path=f"{field_path}.target_fields",
                    )
                )
            else:
                normalized["target_fields"] = target_fields

    for key in ("target_kind", "target_label", "artifact_field", "payload_size_field"):
        raw_value = declaration.get(key)
        if raw_value in (None, ""):
            continue
        normalized_value = str(raw_value).strip()
        if not normalized_value:
            issues.append(
                _issue(
                    severity="error",
                    code="invalid_external_effect_observability_field",
                    message=f"external_effects[].observability.{key} must be a non-empty string",
                    file_path=manifest_path,
                    field_path=f"{field_path}.{key}",
                )
            )
            continue
        normalized[key] = normalized_value
    return normalized


def _validate_project_state_declaration(
    value: Any,
    *,
    issues: list[dict],
    manifest_path: str,
) -> dict[str, list[dict[str, Any]]]:
    if value in (None, ""):
        return {}
    if not isinstance(value, dict):
        issues.append(
            _issue(
                severity="error",
                code="invalid_project_state",
                message="project_state must be an object",
                file_path=manifest_path,
                field_path="project_state",
            )
        )
        return {}
    declaration = _as_dict(value)
    if not declaration:
        return {}

    normalized: dict[str, list[dict[str, Any]]] = {}
    for group in ("reads", "writes"):
        raw_rows = declaration.get(group)
        if raw_rows in (None, ""):
            continue
        if isinstance(raw_rows, dict):
            raw_rows = [raw_rows]
        if not isinstance(raw_rows, list):
            issues.append(
                _issue(
                    severity="error",
                    code=f"invalid_project_state_{group}",
                    message=f"project_state.{group} must be a list",
                    file_path=manifest_path,
                    field_path=f"project_state.{group}",
                )
            )
            continue
        rows: list[dict[str, Any]] = []
        for idx, raw_item in enumerate(raw_rows):
            field_path = f"project_state.{group}[{idx}]"
            item = _as_dict(raw_item)
            if not item:
                issues.append(
                    _issue(
                        severity="error",
                        code=f"invalid_project_state_{group}_entry",
                        message=f"project_state.{group} entries must be objects",
                        file_path=manifest_path,
                        field_path=field_path,
                    )
                )
                continue
            namespace = str(item.get("namespace") or "").strip()
            state_key = str(item.get("state_key") or item.get("key") or "").strip()
            state_key_from_input = str(item.get("state_key_from_input") or "").strip()
            state_key_prefix = str(item.get("state_key_prefix") or "").strip()
            try:
                max_items = int(item.get("max_items") or 0)
            except (TypeError, ValueError):
                max_items = 0
            if not namespace:
                issues.append(
                    _issue(
                        severity="error",
                        code=f"missing_project_state_{group}_namespace",
                        message=f"project_state.{group}[].namespace is required",
                        file_path=manifest_path,
                        field_path=f"{field_path}.namespace",
                    )
                )
            if group == "writes" and not state_key:
                issues.append(
                    _issue(
                        severity="error",
                        code="missing_project_state_write_key",
                        message="project_state.writes[].state_key is required",
                        file_path=manifest_path,
                        field_path=f"{field_path}.state_key",
                    )
                )
            if group == "reads" and not (state_key or state_key_from_input or max_items > 0):
                issues.append(
                    _issue(
                        severity="error",
                        code="missing_project_state_read_selector",
                        message=(
                            "project_state.reads[] requires state_key, "
                            "state_key_from_input, or an explicit max_items"
                        ),
                        file_path=manifest_path,
                        field_path=field_path,
                    )
                )
            if group == "reads" and state_key and state_key_from_input:
                issues.append(
                    _issue(
                        severity="error",
                        code="ambiguous_project_state_read_selector",
                        message="project_state.reads[] may declare only one state key selector",
                        file_path=manifest_path,
                        field_path=field_path,
                    )
                )
            if group == "reads" and state_key_prefix and not state_key_from_input:
                issues.append(
                    _issue(
                        severity="error",
                        code="invalid_project_state_read_prefix",
                        message="project_state.reads[].state_key_prefix requires state_key_from_input",
                        file_path=manifest_path,
                        field_path=f"{field_path}.state_key_prefix",
                    )
                )
            if group == "reads" and max_items and not (1 <= max_items <= 1000):
                issues.append(
                    _issue(
                        severity="error",
                        code="invalid_project_state_read_max_items",
                        message="project_state.reads[].max_items must be between 1 and 1000",
                        file_path=manifest_path,
                        field_path=f"{field_path}.max_items",
                    )
                )
            row: dict[str, str] = {"namespace": namespace}
            if state_key:
                row["state_key"] = state_key
            if state_key_from_input:
                row["state_key_from_input"] = state_key_from_input
            if state_key_prefix:
                row["state_key_prefix"] = state_key_prefix
            if max_items:
                row["max_items"] = max_items
            description = str(item.get("description") or "").strip()
            if description:
                row["description"] = description
            rows.append(row)
        if rows:
            normalized[group] = rows
    return normalized


def _validate_external_effects(
    value: Any,
    *,
    issues: list[dict],
    manifest_path: str,
    action_ids: set[str],
    query_ids: set[str],
    manifest_kind: str = "",
) -> list[dict]:
    if value in (None, ""):
        return []
    if not isinstance(value, list):
        issues.append(
            _issue(
                severity="error",
                code="invalid_external_effects",
                message="external_effects must be a list",
                file_path=manifest_path,
                field_path="external_effects",
            )
        )
        return []

    normalized: list[dict] = []
    seen_operations: set[str] = set()
    declared_operations = set(action_ids) | set(query_ids)
    for idx, raw_item in enumerate(value):
        field_path = f"external_effects[{idx}]"
        item = _as_dict(raw_item)
        if not item:
            issues.append(
                _issue(
                    severity="error",
                    code="invalid_external_effect",
                    message="external_effects[] entries must be objects",
                    file_path=manifest_path,
                    field_path=field_path,
                )
            )
            continue
        operation_id = str(item.get("operation_id") or "").strip()
        effect_kind = str(item.get("effect_kind") or "").strip()
        channel = str(item.get("channel") or "").strip()
        idempotency = str(item.get("idempotency") or "").strip()
        test_mode_behavior = str(item.get("test_mode_behavior") or "").strip()
        rate_limit = _as_dict(item.get("rate_limit"))
        observability = _validate_external_effect_observability(
            item.get("observability"),
            issues=issues,
            manifest_path=manifest_path,
            field_path=f"{field_path}.observability",
        )
        if not operation_id:
            issues.append(
                _issue(
                    severity="error",
                    code="missing_external_effect_operation_id",
                    message="external_effects[].operation_id is required",
                    file_path=manifest_path,
                    field_path=f"{field_path}.operation_id",
                )
            )
        elif operation_id in seen_operations:
            issues.append(
                _issue(
                    severity="error",
                    code="duplicate_external_effect_operation",
                    message=f"Only one external effect may be declared for operation '{operation_id}'",
                    file_path=manifest_path,
                    field_path=f"{field_path}.operation_id",
                )
            )
        elif declared_operations and operation_id not in declared_operations:
            issues.append(
                _issue(
                    severity="error",
                    code="unknown_external_effect_operation",
                    message=f"external_effects operation_id '{operation_id}' is not declared",
                    file_path=manifest_path,
                    field_path=f"{field_path}.operation_id",
                )
            )
        seen_operations.add(operation_id)
        if not effect_kind:
            issues.append(
                _issue(
                    severity="error",
                    code="missing_external_effect_kind",
                    message="external_effects[].effect_kind is required",
                    file_path=manifest_path,
                    field_path=f"{field_path}.effect_kind",
                )
            )
        if not channel:
            issues.append(
                _issue(
                    severity="error",
                    code="missing_external_effect_channel",
                    message="external_effects[].channel is required",
                    file_path=manifest_path,
                    field_path=f"{field_path}.channel",
                )
            )
        if idempotency not in _EXTERNAL_EFFECT_IDEMPOTENCY_VALUES:
            issues.append(
                _issue(
                    severity="error",
                    code="invalid_external_effect_idempotency",
                    message="external_effects[].idempotency must be 'required'",
                    file_path=manifest_path,
                    field_path=f"{field_path}.idempotency",
                )
            )
        if test_mode_behavior not in _EXTERNAL_EFFECT_TEST_MODE_VALUES:
            issues.append(
                _issue(
                    severity="error",
                    code="invalid_external_effect_test_mode_behavior",
                    message="external_effects[].test_mode_behavior must be 'suppress'",
                    file_path=manifest_path,
                    field_path=f"{field_path}.test_mode_behavior",
                )
            )
        normalized_rate_limit: dict[str, int] = {}
        if rate_limit:
            for field in ("window_seconds", "account_max", "project_max", "connection_max"):
                raw_value = rate_limit.get(field)
                try:
                    normalized_value = int(raw_value) if raw_value is not None else 0
                except (TypeError, ValueError):
                    normalized_value = 0
                if normalized_value <= 0:
                    issues.append(
                        _issue(
                            severity="error",
                            code="invalid_external_effect_rate_limit",
                            message=(
                                "external_effects[].rate_limit values must be positive integers"
                            ),
                            file_path=manifest_path,
                            field_path=f"{field_path}.rate_limit.{field}",
                        )
                    )
                    continue
                if field == "window_seconds" and normalized_value > 86400:
                    issues.append(
                        _issue(
                            severity="error",
                            code="external_effect_rate_window_too_large",
                            message=(
                                "external_effects[].rate_limit.window_seconds must be at most 86400"
                            ),
                            file_path=manifest_path,
                            field_path=f"{field_path}.rate_limit.{field}",
                        )
                    )
                    continue
                normalized_rate_limit[field] = normalized_value
        row = {
            "operation_id": operation_id,
            "effect_kind": effect_kind,
            "channel": channel,
            "idempotency": idempotency,
            "test_mode_behavior": test_mode_behavior,
        }
        if normalized_rate_limit:
            row["rate_limit"] = normalized_rate_limit
        if observability:
            row["observability"] = observability
        normalized.append(row)

    _validate_single_notification_target_kind(
        normalized, issues=issues, manifest_path=manifest_path, manifest_kind=manifest_kind
    )
    return normalized


def _validate_single_notification_target_kind(
    rows: list[dict],
    *,
    issues: list[dict],
    manifest_path: str,
    manifest_kind: str = "",
) -> None:
    """A notification provider must address one kind of target, or say so at build time.

    The platform asks a *notification provider* for one answer — does a route name
    a channel the provider offers, or a recipient the operator types — and renders
    one form from it. A provider whose effects disagree has no single answer, and
    guessing means showing a form that cannot deliver.

    Only that kind. A tool provider's operations legitimately touch different kinds
    of target — a file here, a record there — and nothing renders one form from
    them, so requiring agreement would reject a correct manifest.
    """
    if manifest_kind != NOTIFICATION_PROVIDER_KIND:
        return
    declared = {
        kind
        for row in rows
        if isinstance(row, dict)
        for kind in [str((row.get("observability") or {}).get("target_kind") or "").strip()]
        if kind
    }
    if len(declared) <= 1:
        return
    issues.append(
        _issue(
            severity="error",
            code="ambiguous_external_effect_target_kind",
            message=(
                "external_effects declare more than one observability.target_kind "
                f"({', '.join(sorted(declared))}); an extension must address one kind "
                "of target so the platform can render one route form"
            ),
            file_path=manifest_path,
            field_path="external_effects[].observability.target_kind",
        )
    )


def _validate_workflow_action_operation_parity(
    compiled_manifest: dict[str, Any],
    *,
    contract_present: bool,
) -> list[dict[str, Any]]:
    """Require final resolved workflow actions and V2 operations to match."""
    manifest_path = str(compiled_manifest.get("manifest_path") or "")
    release_stage = str(compiled_manifest.get("release_stage") or "active").strip().lower()
    actions = [_as_dict(row) for row in _as_list(compiled_manifest.get("action_definitions"))]
    operations = [_as_dict(row) for row in _as_list(compiled_manifest.get("operation_manifest"))]
    workflow_action_ids = {
        str(row.get("action_id") or "").strip()
        for row in actions
        if row.get("workflow_visible") is True and str(row.get("action_id") or "").strip()
    }
    operation_ids = {
        str(row.get("operation_id") or "").strip()
        for row in operations
        if row.get("workflow_visible") is not False and str(row.get("operation_id") or "").strip()
    }
    issues: list[dict[str, Any]] = []
    if release_stage != "placeholder" and workflow_action_ids and not contract_present:
        issues.append(
            _issue(
                severity="error",
                code="workflow_action_requires_contract_v2",
                message="Active extensions with workflow-visible actions must declare Contract V2",
                file_path=manifest_path,
                field_path="runtime.extension_contract_v2",
            )
        )
    for action_id in sorted(workflow_action_ids - operation_ids):
        issues.append(
            _issue(
                severity="error",
                code="workflow_action_operation_mismatch",
                message=(
                    f"Workflow-visible action '{action_id}' requires one same-ID Contract V2 operation"
                ),
                file_path=manifest_path,
                field_path="action_definitions",
            )
        )
    for operation_id in sorted(operation_ids - workflow_action_ids):
        issues.append(
            _issue(
                severity="error",
                code="workflow_operation_action_mismatch",
                message=(
                    f"Contract V2 operation '{operation_id}' requires one same-ID workflow-visible action"
                ),
                file_path=manifest_path,
                field_path="operation_manifest",
            )
        )
    return issues


def _validate_external_effect_pre_save_actions(
    compiled_manifest: dict[str, Any],
    *,
    issues: list[dict],
    manifest_path: str,
) -> None:
    effect_ids = {
        str(row.get("operation_id") or "").strip()
        for row in _as_list(compiled_manifest.get("external_effects"))
        if isinstance(row, dict)
    }
    for page_idx, page_payload in enumerate(
        _as_list(_as_dict(compiled_manifest.get("ui_manifest")).get("pages"))
    ):
        for component_idx, component_payload in enumerate(
            _as_list(_as_dict(page_payload).get("components"))
        ):
            component = _as_dict(component_payload)
            if str(component.get("type") or "").strip() != "connection_form":
                continue
            for action_idx, action_payload in enumerate(
                _as_list(_as_dict(component.get("data")).get("pre_save_actions"))
            ):
                action_id = str(_as_dict(action_payload).get("action_id") or "").strip()
                if action_id not in effect_ids:
                    continue
                issues.append(
                    _issue(
                        severity="error",
                        code="external_effect_pre_save_action_forbidden",
                        message=(
                            "Connection-form preflight actions must be read-only; "
                            f"external-effect action '{action_id}' is forbidden"
                        ),
                        file_path=manifest_path,
                        field_path=(
                            f"ui.pages[{page_idx}].components[{component_idx}]."
                            f"data.pre_save_actions[{action_idx}].action_id"
                        ),
                    )
                )


def _validate_notification_provider_delivery_contract(
    compiled_manifest: dict[str, Any],
    *,
    issues: list[dict],
    manifest_path: str,
) -> None:
    if str(compiled_manifest.get("kind") or "").strip().lower() != NOTIFICATION_PROVIDER_KIND:
        return
    if str(compiled_manifest.get("release_stage") or "active").strip().lower() != "active":
        return
    delivery_actions = [
        _as_dict(row)
        for row in _as_list(compiled_manifest.get("action_definitions"))
        if str(_as_dict(row).get("action_id") or "").strip() == "deliver_notification"
    ]
    delivery_operations = [
        _as_dict(row)
        for row in _as_list(compiled_manifest.get("operation_manifest"))
        if str(_as_dict(row).get("operation_id") or "").strip() == "deliver_notification"
    ]
    delivery_effects = [
        _as_dict(row)
        for row in _as_list(compiled_manifest.get("external_effects"))
        if str(_as_dict(row).get("operation_id") or "").strip() == "deliver_notification"
    ]
    action_parameters = {
        str(row.get("name") or "").strip(): row
        for row in _as_list(delivery_actions[0].get("parameters") if delivery_actions else [])
        if isinstance(row, dict)
    }
    operation_inputs = {
        str(row.get("name") or "").strip(): row
        for row in _as_list(delivery_operations[0].get("inputs") if delivery_operations else [])
        if isinstance(row, dict)
    }
    operation_outputs = {
        str(row.get("name") or "").strip(): row
        for row in _as_list(delivery_operations[0].get("outputs") if delivery_operations else [])
        if isinstance(row, dict)
    }
    action_valid = len(delivery_actions) == 1 and (
        str(_as_dict(delivery_actions[0].get("handler")).get("mode") or "").strip().lower()
        == "extension"
        and delivery_actions[0].get("workflow_visible") is False
        and set(action_parameters) == {"event", "route"}
        and all(
            action_parameters[name].get("required") is True
            and str(action_parameters[name].get("type") or "").strip().lower() == "object"
            for name in ("event", "route")
        )
    )
    operation_valid = len(delivery_operations) == 1 and (
        str(delivery_operations[0].get("operation_kind") or "").strip().lower() == "action"
        and delivery_operations[0].get("workflow_visible") is False
        and set(operation_inputs) == {"event", "route"}
        and all(
            operation_inputs[name].get("required") is True
            and str(operation_inputs[name].get("value_type") or "").strip().lower() == "object"
            for name in ("event", "route")
        )
        and {"delivered", "provider_message_id"}.issubset(operation_outputs)
        and str(operation_outputs["delivered"].get("value_type") or "").strip().lower() == "boolean"
        and str(operation_outputs["provider_message_id"].get("value_type") or "").strip().lower()
        == "text"
    )
    effect_valid = len(delivery_effects) == 1 and (
        delivery_effects[0].get("idempotency") == "required"
        and delivery_effects[0].get("test_mode_behavior") == "suppress"
        and delivery_effects[0].get("effect_kind") == "message_delivery"
        and bool(str(delivery_effects[0].get("channel") or "").strip())
    )
    if action_valid and operation_valid and effect_valid:
        return
    issues.append(
        _issue(
            severity="error",
            code="invalid_notification_provider_delivery_contract",
            message=(
                "Active notification providers must reserve deliver_notification "
                "as a non-workflow extension action and V2 action operation with non-empty "
                "required event/route inputs, delivered/provider_message_id outputs, and a "
                "suppressed, idempotent message-delivery external effect"
            ),
            file_path=manifest_path,
            field_path="deliver_notification",
        )
    )


def _validate_runtime_grants(
    declared: Any,
    *,
    extension_id: str,
    issues: list[dict],
    manifest_path: str,
) -> dict[str, Any]:
    """Keep only grants over the extension's own namespace; report every other one.

    A manifest that asks for another extension's secrets, routes or deliveries
    (or any grant the host does not know) fails validation and cannot install.
    """
    accepted, problems = own_runtime_grants(declared, extension_id)
    for problem in problems:
        issues.append(
            _issue(
                severity="error",
                code=str(problem.get("code") or "runtime_grant_forbidden"),
                message=str(problem.get("message") or "Runtime grant is not allowed."),
                file_path=manifest_path,
                field_path=str(problem.get("field_path") or "runtime_grants"),
                hint=(
                    "Declare runtime_grants only over this extension's own namespace: "
                    "scope owning_extension and provider_key equal to the extension id."
                ),
            )
        )
    return accepted
