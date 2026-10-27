"""Validate transform configuration against selector and action contracts."""

from __future__ import annotations

import re
from typing import Any

from core.application.orchestration.transform_action_contract import TransformAction
from core.application.orchestration.transform_actions import _ACTIONS
from core.application.orchestration.transform_selectors import (
    parse_transform_selector,
)
from core.application.orchestration.transform_values import (
    _coerce_value,
    _money_decimal,
    _normalize_type,
    _types_compatible,
)

STATE_PREFIX = "state."


TRANSFORM_MODES = {"object", "list"}


TRANSFORM_OUTPUT_MODES = {"mapped_only", "merge_with_input"}


TRANSFORM_CONDITION_OPERATORS = {
    "equals",
    "not_equals",
    "exists",
    "truthy",
    "gt",
    "gte",
    "lt",
    "lte",
    "contains",
    "in",
    "not_in",
}


_TARGET_SEGMENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]*$")


def _issue(
    *, code: str, message: str, path: str = "", severity: str = "error", message_key: str = ""
) -> dict:
    issue: dict[str, Any] = {
        "code": str(code or "").strip(),
        "message": str(message or "").strip(),
        "path": str(path or "").strip(),
        "severity": str(severity or "error").strip().lower(),
    }
    if message_key:
        issue.update(message_key=message_key, message_params={})
    return issue


def _validate_output_path(path: str) -> str:
    text = str(path or "").strip()
    if not text:
        raise ValueError("path is required")
    if "[" in text or "]" in text:
        raise ValueError("array indexes are not allowed in target paths")
    parts = [part.strip() for part in text.split(".")]
    if not parts or any(not part for part in parts):
        raise ValueError("path contains an empty segment")
    for part in parts:
        if not _TARGET_SEGMENT_RE.match(part):
            raise ValueError("path segment must be an identifier")
    return text


def _is_state_path(path: Any) -> bool:
    return str(path or "").strip().startswith(STATE_PREFIX)


def _validate_state_target_path(path: str) -> str:
    text = str(path or "").strip()
    if not text.startswith(STATE_PREFIX):
        raise ValueError("state target must start with state.")
    suffix = text[len(STATE_PREFIX) :]
    _validate_output_path(suffix)
    return text


def _is_param_source_ref(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    source_keys = {"literal", "selector", "current"} & set(value.keys())
    return len(source_keys) == 1 and set(value.keys()).issubset(
        {"literal", "selector", "current", "default"}
    )


def _param_matches_schema(value: Any, schema: dict[str, Any]) -> bool:
    expected = str((schema or {}).get("type") or "json").strip().lower()
    if expected in {"json", ""}:
        return True
    if expected == "string":
        return isinstance(value, str)
    if expected == "number":
        if isinstance(value, bool):
            return False
        try:
            _coerce_value(value, "number")
        except ValueError:
            return False
        return True
    if expected == "money":
        try:
            _money_decimal(value)
        except ValueError:
            return False
        return True
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "object":
        return isinstance(value, dict)
    if expected == "array":
        return isinstance(value, list)
    return True


def _validate_action_params(
    action: TransformAction,
    params: Any,
    *,
    path: str,
) -> list[dict[str, Any]]:
    errors: list[dict[str, Any]] = []
    params_obj = params if isinstance(params, dict) else {}
    if params not in (None, "") and not isinstance(params, dict):
        errors.append(
            _issue(
                code="workflow.step_transform_action_params_invalid",
                message=f"Transform action '{action.action_id}' params must be an object",
                path=path,
            )
        )
        return errors
    properties = action.params_schema.get("properties") or {}
    for key in params_obj:
        _validate_action_param(action, key, params_obj.get(key), properties, path, errors)
    for key in action.required_params:
        if key not in params_obj:
            errors.append(
                _issue(
                    code="workflow.step_transform_action_param_missing",
                    message=f"Transform action '{action.action_id}' requires param '{key}'",
                    path=f"{path}.{key}",
                )
            )
    return errors


def _validate_action_pipeline(
    actions: Any,
    *,
    field_type: str,
    path: str,
    max_actions: int,
) -> tuple[list[dict[str, Any]], str]:
    errors: list[dict[str, Any]] = []
    if actions in (None, ""):
        return errors, field_type
    if not isinstance(actions, list):
        return [
            _issue(
                code="workflow.step_transform_actions_invalid",
                message="Transform actions must be a list",
                path=path,
            )
        ], field_type
    if len(actions) > max_actions:
        errors.append(
            _issue(
                code="workflow.step_transform_actions_too_many",
                message=f"Transform fields support at most {max_actions} actions",
                path=path,
            )
        )
    current_type = ""
    for idx, row in enumerate(actions):
        current_type = _validate_pipeline_action(row, idx, path, current_type, errors)
    return errors, current_type or field_type


def validate_transform_config(
    config: Any,
    *,
    limits: dict[str, int],
    path_prefix: str = "override.transform",
) -> list[dict[str, Any]]:
    errors: list[dict[str, Any]] = []
    if not isinstance(config, dict):
        return [
            _issue(
                code="workflow.step_transform_config_missing",
                message="transform step requires override.transform configuration",
                path=path_prefix,
            )
        ]
    allowed_keys = {"input", "mode", "result_target", "output_mode", "row_filter", "fields"}
    for key in config:
        if key not in allowed_keys:
            errors.append(
                _issue(
                    code="workflow.step_transform_key_unknown",
                    message=f"Unknown transform config key '{key}'",
                    path=f"{path_prefix}.{key}",
                )
            )
    _validate_transform_input(config, path_prefix, errors)
    mode = str(config.get("mode") or "object").strip().lower()
    if mode not in TRANSFORM_MODES:
        errors.append(
            _issue(
                code="workflow.step_transform_mode_invalid",
                message="transform mode must be object or list",
                path=f"{path_prefix}.mode",
            )
        )
    _validate_transform_target(config, path_prefix, errors)
    output_mode = str(config.get("output_mode") or "mapped_only").strip().lower()
    if output_mode not in TRANSFORM_OUTPUT_MODES:
        errors.append(
            _issue(
                code="workflow.step_transform_output_mode_invalid",
                message="transform output_mode must be mapped_only or merge_with_input",
                path=f"{path_prefix}.output_mode",
            )
        )
    if config.get("row_filter") not in (None, ""):
        if mode != "list":
            errors.append(
                _issue(
                    code="workflow.step_transform_row_filter_mode_invalid",
                    message="transform row_filter is valid only in list mode",
                    path=f"{path_prefix}.row_filter",
                )
            )
        errors.extend(_validate_condition(config.get("row_filter"), f"{path_prefix}.row_filter"))
    fields = config.get("fields")
    if not isinstance(fields, list) or not fields:
        errors.append(
            _issue(
                code="workflow.step_transform_fields_missing",
                message="transform requires at least one field mapping",
                path=f"{path_prefix}.fields",
            )
        )
        return errors
    if len(fields) > limits["max_fields"]:
        errors.append(
            _issue(
                code="workflow.step_transform_fields_too_many",
                message=f"transform supports at most {limits['max_fields']} fields",
                path=f"{path_prefix}.fields",
            )
        )
    for idx, field in enumerate(fields):
        errors.extend(
            _validate_transform_field(
                field,
                path=f"{path_prefix}.fields[{idx}]",
                max_actions=limits["max_actions_per_field"],
            )
        )
    return errors


def _validate_condition(condition: Any, path: str) -> list[dict[str, Any]]:
    if not isinstance(condition, dict):
        return [
            _issue(
                code="workflow.step_transform_condition_invalid",
                message="transform condition must be an object",
                path=path,
            )
        ]
    errors: list[dict[str, Any]] = []
    selector = str(condition.get("selector") or "").strip()
    if not selector:
        errors.append(
            _issue(
                code="workflow.step_transform_condition_selector_missing",
                message="transform condition requires selector",
                path=f"{path}.selector",
            )
        )
    else:
        try:
            parse_transform_selector(selector, allow_wildcard=False)
        except ValueError as exc:
            errors.append(
                _issue(
                    code="workflow.step_transform_condition_selector_invalid",
                    message=str(exc),
                    path=f"{path}.selector",
                )
            )
    operator = str(condition.get("operator") or "").strip()
    if operator not in TRANSFORM_CONDITION_OPERATORS:
        errors.append(
            _issue(
                code="workflow.step_transform_condition_operator_invalid",
                message="transform condition operator is unsupported",
                path=f"{path}.operator",
            )
        )
    return errors


def _validate_transform_field(field: Any, *, path: str, max_actions: int) -> list[dict[str, Any]]:
    errors: list[dict[str, Any]] = []
    if not isinstance(field, dict):
        return [
            _issue(
                code="workflow.step_transform_field_invalid",
                message="transform field mapping must be an object",
                path=path,
            )
        ]
    has_from = _validate_field_source(field, path, errors)
    field_type = _normalize_type(field.get("type"))
    if not field_type:
        errors.append(
            _issue(
                code="workflow.step_transform_field_type_invalid",
                message="transform field type is unsupported",
                path=f"{path}.type",
            )
        )
        field_type = "json"
    if "item_type" in field and field_type != "array":
        errors.append(
            _issue(
                code="workflow.step_transform_item_type_invalid",
                message="item_type is valid only when type is array",
                path=f"{path}.item_type",
            )
        )
    item_type = _normalize_type(field.get("item_type")) or "json"
    _validate_field_target(field, path, errors)
    wildcard = _validate_field_selector(field, path, field_type, has_from, errors)
    has_actions_each = _validate_field_action_mode(
        field, path, field_type, has_from, wildcard, errors
    )
    pipeline_errors, final_type = _validate_action_pipeline(
        field.get("actions_each") if has_actions_each else field.get("actions"),
        field_type=item_type if has_actions_each else field_type,
        path=f"{path}.{'actions_each' if has_actions_each else 'actions'}",
        max_actions=max_actions,
    )
    errors.extend(pipeline_errors)
    expected_final = item_type if has_actions_each else field_type
    if final_type and not _types_compatible(expected_final, final_type):
        errors.append(
            _issue(
                code="workflow.step_transform_final_type_mismatch",
                message=f"transform action pipeline returns {final_type}, not {expected_final}",
                path=path,
            )
        )
    return errors


def _validate_action_param(
    action: TransformAction,
    key: Any,
    value: Any,
    properties: dict[str, Any],
    path: str,
    errors: list[dict[str, Any]],
) -> None:
    if key not in properties:
        errors.append(
            _issue(
                code="workflow.step_transform_action_param_unknown",
                message=f"Transform action '{action.action_id}' does not accept param '{key}'",
                path=f"{path}.{key}",
            )
        )
        return
    if _is_param_source_ref(value):
        _validate_param_source(action, key, value, path, errors)
        return
    if not _param_matches_schema(value, properties.get(key) or {}):
        errors.append(
            _issue(
                code="workflow.step_transform_action_param_type_invalid",
                message=f"Transform action '{action.action_id}' param '{key}' has invalid type",
                path=f"{path}.{key}",
            )
        )


def _validate_param_source(
    action: TransformAction, key: Any, value: Any, path: str, errors: list[dict[str, Any]]
) -> None:
    value_ref = value if isinstance(value, dict) else {}
    if key not in action.param_sources:
        errors.append(
            _issue(
                code="workflow.step_transform_action_param_source_forbidden",
                message=f"Transform action '{action.action_id}' param '{key}' does not allow source references",
                path=f"{path}.{key}",
            )
        )
    elif "selector" in value_ref:
        selector = str(value_ref.get("selector") or "").strip()
        if selector == "state" or selector.startswith("state."):
            errors.append(
                _issue(
                    code="workflow.step_transform_action_param_selector_scope_invalid",
                    message="Transform action param selectors are relative to the current input object, not workflow state",
                    path=f"{path}.{key}.selector",
                )
            )
            return
        try:
            parse_transform_selector(selector)
        except ValueError as exc:
            errors.append(
                _issue(
                    code="workflow.step_transform_action_param_selector_invalid",
                    message=str(exc),
                    path=f"{path}.{key}.selector",
                )
            )


def _validate_pipeline_action(
    row: Any, idx: int, path: str, current_type: str, errors: list[dict[str, Any]]
) -> str:
    action_path = f"{path}[{idx}]"
    if not isinstance(row, dict):
        errors.append(
            _issue(
                code="workflow.step_transform_action_invalid",
                message=f"Transform action #{idx + 1} must be an object",
                path=action_path,
            )
        )
        return current_type
    action_id = str(row.get("action") or "").strip()
    action = _ACTIONS.get(action_id)
    if not action:
        errors.append(
            _issue(
                code="workflow.step_transform_action_unknown",
                message=f"Unknown transform action '{action_id or '<missing>'}'",
                path=f"{action_path}.action",
            )
        )
        return current_type
    if not (action.transform_safe and action.pure and action.deterministic) or action.io_allowed:
        errors.append(
            _issue(
                code="workflow.step_transform_action_unsafe",
                message=f"Transform action '{action_id}' is not transform-safe",
                path=f"{action_path}.action",
            )
        )
    if current_type and not _types_compatible(action.input_type, current_type):
        errors.append(
            _issue(
                code="workflow.step_transform_action_type_mismatch",
                message=(
                    f"Transform action '{action_id}' expects {action.input_type} "
                    f"after previous output {current_type}"
                ),
                path=f"{action_path}.action",
            )
        )
    current_type = action.output_type
    errors.extend(_validate_action_params(action, row.get("params"), path=f"{action_path}.params"))
    return current_type


def _validate_transform_input(
    config: dict[str, Any], path_prefix: str, errors: list[dict[str, Any]]
) -> None:
    input_cfg = config.get("input")
    if not isinstance(input_cfg, dict):
        errors.append(
            _issue(
                code="workflow.step_transform_input_missing",
                message="transform requires input.source",
                message_key="react.workflows.transform_input_source_required",
                path=f"{path_prefix}.input",
            )
        )
    else:
        source = str(input_cfg.get("source") or "").strip()
        if not source:
            errors.append(
                _issue(
                    code="workflow.step_transform_input_source_missing",
                    message="transform requires input.source",
                    message_key="react.workflows.transform_input_source_required",
                    path=f"{path_prefix}.input.source",
                )
            )
        elif not _is_state_path(source):
            errors.append(
                _issue(
                    code="workflow.step_transform_input_source_invalid",
                    message="transform input.source must be a state path",
                    path=f"{path_prefix}.input.source",
                )
            )


def _validate_transform_target(
    config: dict[str, Any], path_prefix: str, errors: list[dict[str, Any]]
) -> None:
    result_target = str(config.get("result_target") or "").strip()
    if not result_target:
        errors.append(
            _issue(
                code="workflow.step_transform_result_target_missing",
                message="transform requires result_target",
                path=f"{path_prefix}.result_target",
            )
        )
    else:
        try:
            _validate_state_target_path(result_target)
        except ValueError as exc:
            errors.append(
                _issue(
                    code="workflow.step_transform_result_target_invalid",
                    message=str(exc),
                    path=f"{path_prefix}.result_target",
                )
            )


def _validate_field_source(field: dict[str, Any], path: str, errors: list[dict[str, Any]]) -> bool:
    allowed_keys = {
        "from",
        "value",
        "to",
        "type",
        "item_type",
        "default",
        "required",
        "actions",
        "actions_each",
    }
    for key in field:
        if key not in allowed_keys:
            errors.append(
                _issue(
                    code="workflow.step_transform_field_key_unknown",
                    message=f"Unknown transform field key '{key}'",
                    path=f"{path}.{key}",
                )
            )
    has_from = str(field.get("from") or "").strip() != ""
    has_value = "value" in field
    if has_from == has_value:
        errors.append(
            _issue(
                code="workflow.step_transform_field_source_invalid",
                message="transform field must define exactly one of from or value",
                path=path,
            )
        )
    if has_value:
        value = field.get("value")
        if not isinstance(value, dict) or "literal" not in value:
            errors.append(
                _issue(
                    code="workflow.step_transform_field_literal_invalid",
                    message="literal fields must use value.literal",
                    path=f"{path}.value",
                )
            )
        if "default" in field or "required" in field:
            errors.append(
                _issue(
                    code="workflow.step_transform_literal_default_required_invalid",
                    message="literal fields cannot define default or required",
                    path=path,
                )
            )
    return has_from


def _validate_field_target(field: dict[str, Any], path: str, errors: list[dict[str, Any]]) -> None:
    if not str(field.get("to") or "").strip():
        errors.append(
            _issue(
                code="workflow.step_transform_field_to_missing",
                message="transform field requires to",
                path=f"{path}.to",
            )
        )
    else:
        try:
            _validate_output_path(str(field.get("to") or ""))
        except ValueError as exc:
            errors.append(
                _issue(
                    code="workflow.step_transform_field_to_invalid",
                    message=str(exc),
                    path=f"{path}.to",
                )
            )


def _validate_field_selector(
    field: dict[str, Any], path: str, field_type: str, has_from: bool, errors: list[dict[str, Any]]
) -> bool:
    wildcard = False
    if has_from:
        selector = str(field.get("from") or "").strip()
        try:
            tokens = parse_transform_selector(selector)
            wildcard = any(token.kind == "wildcard" for token in tokens)
        except ValueError as exc:
            errors.append(
                _issue(
                    code="workflow.step_transform_selector_invalid",
                    message=str(exc),
                    path=f"{path}.from",
                )
            )
        if wildcard and field_type != "array":
            errors.append(
                _issue(
                    code="workflow.step_transform_wildcard_requires_array",
                    message="wildcard selectors require field type array",
                    path=f"{path}.type",
                )
            )
    return wildcard


def _validate_field_action_mode(
    field: dict[str, Any],
    path: str,
    field_type: str,
    has_from: bool,
    wildcard: bool,
    errors: list[dict[str, Any]],
) -> bool:
    has_actions = "actions" in field
    has_actions_each = "actions_each" in field
    if has_actions and has_actions_each:
        errors.append(
            _issue(
                code="workflow.step_transform_actions_conflict",
                message="transform field cannot define both actions and actions_each",
                path=path,
            )
        )
    if has_actions_each and (not has_from or not wildcard or field_type != "array"):
        errors.append(
            _issue(
                code="workflow.step_transform_actions_each_invalid",
                message="actions_each requires a wildcard from selector and type array",
                path=f"{path}.actions_each",
            )
        )
    if wildcard and has_actions:
        errors.append(
            _issue(
                code="workflow.step_transform_wildcard_actions_invalid",
                message="wildcard selectors must use actions_each, not actions",
                path=f"{path}.actions",
            )
        )
    return has_actions_each
