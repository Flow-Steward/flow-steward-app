"""Runtime and validation for ``step_kind=transform``."""

from __future__ import annotations

import copy
import json
from typing import Any

from core.application.orchestration.step_templating import _resolve_path, set_state_path
from core.application.orchestration.transform_action_contract import TransformAction
from core.application.orchestration.transform_actions import (
    _ACTIONS,
)
from core.application.orchestration.transform_actions import (
    TRANSFORM_ACTION_CATALOG_VERSION as TRANSFORM_ACTION_CATALOG_VERSION,
)
from core.application.orchestration.transform_actions import (
    get_transform_action_contract as get_transform_action_contract,
)
from core.application.orchestration.transform_actions import (
    list_transform_action_contracts as list_transform_action_contracts,
)
from core.application.orchestration.transform_config_validation import (
    _is_param_source_ref,
    _validate_output_path,
    _validate_state_target_path,
)
from core.application.orchestration.transform_config_validation import (
    validate_transform_config as _validate_transform_config,
)
from core.application.orchestration.transform_selectors import (
    MISSING as _MISSING,
)
from core.application.orchestration.transform_selectors import (
    parse_transform_selector as parse_transform_selector,
)
from core.application.orchestration.transform_selectors import (
    select_value as _select_value,
)
from core.application.orchestration.transform_selectors import (
    selector_has_wildcard as selector_has_wildcard,
)
from core.application.orchestration.transform_selectors import (
    transform_state_selector_error,
)
from core.application.orchestration.transform_values import (
    TRANSFORM_VALUE_TYPES as TRANSFORM_VALUE_TYPES,
)
from core.application.orchestration.transform_values import (
    _coerce_value,
    _normalize_type,
)
from core.infrastructure.config.runtime_env import runtime_env as _runtime_env


class TransformRuntimeFailure(Exception):
    def __init__(self, errors: list[dict[str, Any]]):
        super().__init__("transform step failed")
        self.errors = errors


def _runtime_error(
    *,
    reason: str,
    field_index: int | None = None,
    field_to: str = "",
    selector: str = "",
    action_index: int | None = None,
    action: str = "",
    element_index: int | None = None,
    detail: str = "",
) -> dict[str, Any]:
    payload: dict[str, Any] = {"reason": reason}
    if field_index is not None:
        payload["field_index"] = field_index
    if field_to:
        payload["field_to"] = field_to
    if selector:
        payload["selector"] = selector
    if action_index is not None:
        payload["action_index"] = action_index
    if action:
        payload["action"] = action
    if element_index is not None:
        payload["element_index"] = element_index
    if detail:
        payload["detail"] = detail
    return payload


def _env_int(name: str, default: int) -> int:
    raw = str(_runtime_env.get(name, "") or "").strip()
    if not raw:
        return default
    try:
        parsed = int(raw)
    except ValueError:
        return default
    return parsed if parsed > 0 else default


def _guard_limits() -> dict[str, int]:
    return {
        "max_input_bytes": _env_int("FS_TRANSFORM_MAX_INPUT_BYTES", 5 * 1024 * 1024),
        "max_list_length": _env_int("FS_TRANSFORM_MAX_LIST_LENGTH", 10_000),
        "max_fields": _env_int("FS_TRANSFORM_MAX_FIELDS", 200),
        "max_actions_per_field": _env_int("FS_TRANSFORM_MAX_ACTIONS_PER_FIELD", 20),
        "max_output_bytes": _env_int("FS_TRANSFORM_MAX_OUTPUT_BYTES", 5 * 1024 * 1024),
    }


def _serialized_size(value: Any) -> int:
    try:
        payload = json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)
    except Exception:
        payload = str(value)
    return len(payload.encode("utf-8"))


def _set_output_path(target: dict[str, Any], path: str, value: Any) -> None:
    normalized = _validate_output_path(path)
    parts = [part for part in normalized.split(".") if part]
    cursor = target
    for part in parts[:-1]:
        child = cursor.get(part)
        if not isinstance(child, dict):
            child = {}
            cursor[part] = child
        cursor = child
    cursor[parts[-1]] = value


def _resolve_param_value(
    raw_value: Any,
    *,
    action: TransformAction,
    param_name: str,
    current_value: Any,
    selector_root: Any,
) -> Any:
    if _is_param_source_ref(raw_value):
        if param_name not in action.param_sources:
            raise ValueError(f"param '{param_name}' does not allow source references")
        if "literal" in raw_value:
            return raw_value.get("literal")
        if "current" in raw_value:
            return current_value
        selector = str(raw_value.get("selector") or "").strip()
        selected, missing = _select_value(selector_root, selector)
        if (missing or selected is None) and "default" in raw_value:
            return raw_value.get("default")
        if missing or selected is None:
            raise ValueError(f"selector param '{param_name}' is missing")
        return selected
    return raw_value


def _execute_action_pipeline(
    value: Any,
    actions: list[Any],
    *,
    field_index: int,
    field_to: str,
    selector: str,
    field_type: str,
    selector_root: Any,
    element_index: int | None,
) -> Any:
    if not actions:
        try:
            return _coerce_value(value, field_type)
        except ValueError as exc:
            raise TransformRuntimeFailure(
                [
                    _runtime_error(
                        reason="field_type_mismatch",
                        field_index=field_index,
                        field_to=field_to,
                        selector=selector,
                        element_index=element_index,
                        detail=str(exc),
                    )
                ]
            ) from exc
    current = _initial_action_value(value, actions, field_index, field_to, selector, element_index)
    for action_index, row in enumerate(actions):
        action_id = str((row or {}).get("action") or "").strip() if isinstance(row, dict) else ""
        action = _ACTIONS.get(action_id)
        if not action:
            raise TransformRuntimeFailure(
                [
                    _runtime_error(
                        reason="unknown_action",
                        field_index=field_index,
                        field_to=field_to,
                        selector=selector,
                        action_index=action_index,
                        action=action_id,
                        element_index=element_index,
                    )
                ]
            )
        params = _resolve_action_params(
            row,
            action,
            current,
            selector_root,
            error_context={
                "field_index": field_index,
                "field_to": field_to,
                "selector": selector,
                "action_index": action_index,
                "element_index": element_index,
            },
        )
        try:
            current = _coerce_value(current, action.input_type)
            current = action.func(current, params)
            current = _coerce_value(current, action.output_type)
        except Exception as exc:
            raise TransformRuntimeFailure(
                [
                    _runtime_error(
                        reason="action_execution_failed",
                        field_index=field_index,
                        field_to=field_to,
                        selector=selector,
                        action_index=action_index,
                        action=action.action_id,
                        element_index=element_index,
                        detail=str(exc),
                    )
                ]
            ) from exc
    try:
        return _coerce_value(current, field_type)
    except Exception as exc:
        raise TransformRuntimeFailure(
            [
                _runtime_error(
                    reason="field_type_mismatch",
                    field_index=field_index,
                    field_to=field_to,
                    selector=selector,
                    element_index=element_index,
                    detail=str(exc),
                )
            ]
        ) from exc


def _condition_matches(condition: dict[str, Any], item: Any) -> bool:
    selector = str(condition.get("selector") or "").strip()
    operator = str(condition.get("operator") or "").strip()
    selected, missing = _select_value(item, selector)
    expected = condition.get("value")
    if operator == "exists":
        return not missing and selected is not None
    if operator == "truthy":
        return bool(selected) if not missing else False
    if missing:
        return False
    return _compare_condition_values(operator, selected, expected)


def _field_source_value(field: dict[str, Any], item: Any) -> tuple[Any, bool]:
    if "value" in field:
        literal = field.get("value")
        return (literal or {}).get("literal") if isinstance(literal, dict) else None, False
    selected, missing = _select_value(item, str(field.get("from") or "").strip())
    if missing or selected is None:
        if "default" in field:
            return field.get("default"), False
        if bool(field.get("required")):
            return None, True
        return _MISSING, False
    return selected, False


def _execute_field(
    field: dict[str, Any],
    item: Any,
    output: dict[str, Any],
    *,
    field_index: int,
    element_index: int | None,
) -> None:
    field_to = str(field.get("to") or "").strip()
    selector = str(field.get("from") or "").strip()
    value, required_missing = _field_source_value(field, item)
    if required_missing:
        raise TransformRuntimeFailure(
            [
                _runtime_error(
                    reason="required_source_missing",
                    field_index=field_index,
                    field_to=field_to,
                    selector=selector,
                    element_index=element_index,
                )
            ]
        )
    if value is _MISSING:
        return
    field_type = _normalize_type(field.get("type")) or "json"
    actions_each = field.get("actions_each")
    if isinstance(actions_each, list):
        if not isinstance(value, list):
            raise TransformRuntimeFailure(
                [
                    _runtime_error(
                        reason="wildcard_source_not_array",
                        field_index=field_index,
                        field_to=field_to,
                        selector=selector,
                        element_index=element_index,
                    )
                ]
            )
        item_type = _normalize_type(field.get("item_type")) or "json"
        mapped_items = [
            _execute_action_pipeline(
                child,
                actions_each,
                field_index=field_index,
                field_to=field_to,
                selector=selector,
                field_type=item_type,
                selector_root=item,
                element_index=element_index,
            )
            for child in value
        ]
        _set_output_path(output, field_to, mapped_items)
        return
    raw_actions = field.get("actions")
    actions: list[Any] = raw_actions if isinstance(raw_actions, list) else []
    final_value = _execute_action_pipeline(
        value,
        actions,
        field_index=field_index,
        field_to=field_to,
        selector=selector,
        field_type=field_type,
        selector_root=item,
        element_index=element_index,
    )
    _set_output_path(output, field_to, final_value)


def _execute_object_mapping(config: dict[str, Any], item: Any, *, element_index: int | None) -> Any:
    if not isinstance(item, dict):
        raise TransformRuntimeFailure(
            [
                _runtime_error(
                    reason="input_item_not_object",
                    selector=str(((config.get("input") or {}).get("source")) or "").strip(),
                    element_index=element_index,
                )
            ]
        )
    output_mode = str(config.get("output_mode") or "mapped_only").strip().lower()
    output = copy.deepcopy(item) if output_mode == "merge_with_input" else {}
    for field_index, field in enumerate(config.get("fields") or []):
        if not isinstance(field, dict):
            continue
        _execute_field(field, item, output, field_index=field_index, element_index=element_index)
    return output


def validate_transform_config(
    config: Any, *, path_prefix: str = "override.transform"
) -> list[dict[str, Any]]:
    """Validate against the same configured guard limits used by execution."""
    return _validate_transform_config(config, path_prefix=path_prefix, limits=_guard_limits())


def execute_transform_step(
    *,
    config: dict[str, Any],
    state: dict[str, Any] | None = None,
) -> dict[str, Any]:
    selector_error = transform_state_selector_error(config)
    if selector_error:
        return selector_error
    errors = validate_transform_config(config)
    if errors:
        return {"ok": False, "errors": errors, "state_patch": {}, "detail": "validation_failed"}

    runtime_state = dict(state or {})
    limits = _guard_limits()
    source_path = str(((config.get("input") or {}).get("source")) or "").strip()
    source_value = _resolve_path(runtime_state, source_path)
    if source_value is None:
        return {
            "ok": False,
            "errors": [_runtime_error(reason="input_source_missing", selector=source_path)],
            "state_patch": {},
            "detail": "input_source_missing",
        }
    if _serialized_size(source_value) > limits["max_input_bytes"]:
        return {
            "ok": False,
            "errors": [_runtime_error(reason="input_too_large", selector=source_path)],
            "state_patch": {},
            "detail": "input_too_large",
        }
    try:
        output_value = _execute_transform_mapping(config, source_value, limits)
        if _serialized_size(output_value) > limits["max_output_bytes"]:
            raise TransformRuntimeFailure([_runtime_error(reason="output_too_large")])
    except TransformRuntimeFailure as exc:
        return {"ok": False, "errors": exc.errors, "state_patch": {}, "detail": "runtime_failed"}

    state_patch: dict[str, Any] = {}
    result_target = str(config.get("result_target") or "").strip()
    try:
        _validate_state_target_path(result_target)
        set_state_path(state_patch, runtime_state, result_target, output_value)
    except Exception as exc:
        return {
            "ok": False,
            "errors": [
                _runtime_error(
                    reason="result_target_invalid",
                    selector=result_target,
                    detail=str(exc),
                )
            ],
            "state_patch": {},
            "detail": "runtime_failed",
        }
    return {
        "ok": True,
        "errors": [],
        "output": output_value,
        "result_target": result_target,
        "state_patch": state_patch,
        "detail": "transform step executed",
    }


def _execute_transform_mapping(
    config: dict[str, Any], source_value: Any, limits: dict[str, int]
) -> Any:
    mode = str(config.get("mode") or "object").strip().lower()
    if mode == "list":
        if not isinstance(source_value, list):
            raise TransformRuntimeFailure([_runtime_error(reason="input_source_not_array")])
        if len(source_value) > limits["max_list_length"]:
            raise TransformRuntimeFailure([_runtime_error(reason="list_too_large")])
        row_filter = config.get("row_filter")
        output_value = []
        for element_index, item in enumerate(source_value):
            if not _row_filter_matches(row_filter, item, element_index):
                continue
            output_value.append(_execute_object_mapping(config, item, element_index=element_index))
    else:
        output_value = _execute_object_mapping(config, source_value, element_index=None)
    return output_value


def _row_filter_matches(row_filter: Any, item: Any, element_index: int | None) -> bool:
    if isinstance(row_filter, dict):
        try:
            if not _condition_matches(row_filter, item):
                return False
        except Exception as exc:
            raise TransformRuntimeFailure(
                [
                    _runtime_error(
                        reason="row_filter_failed",
                        selector=str(row_filter.get("selector") or "").strip(),
                        element_index=element_index,
                        detail=str(exc),
                    )
                ]
            ) from exc
    return True


def _resolve_action_params(
    row: Any,
    action: TransformAction,
    current: Any,
    selector_root: Any,
    *,
    error_context: dict[str, Any],
) -> dict[str, Any]:
    params_raw = row.get("params") if isinstance(row, dict) else {}
    params_raw = params_raw if isinstance(params_raw, dict) else {}
    params: dict[str, Any] = {}
    for key, raw_param in params_raw.items():
        try:
            params[key] = _resolve_param_value(
                raw_param,
                action=action,
                param_name=str(key),
                current_value=current,
                selector_root=selector_root,
            )
        except Exception as exc:
            raise TransformRuntimeFailure(
                [
                    _runtime_error(
                        reason="action_param_resolution_failed",
                        **error_context,
                        action=action.action_id,
                        detail=str(exc),
                    )
                ]
            ) from exc
    return params


def _initial_action_value(
    value: Any,
    actions: list[Any],
    field_index: int,
    field_to: str,
    selector: str,
    element_index: int | None,
) -> Any:
    first = actions[0] if isinstance(actions[0], dict) else {}
    first_action = _ACTIONS.get(str(first.get("action") or "").strip())
    current = value
    if first_action:
        try:
            current = _coerce_value(current, first_action.input_type)
        except ValueError as exc:
            raise TransformRuntimeFailure(
                [
                    _runtime_error(
                        reason="action_type_mismatch",
                        field_index=field_index,
                        field_to=field_to,
                        selector=selector,
                        action_index=0,
                        action=first_action.action_id,
                        element_index=element_index,
                        detail=str(exc),
                    )
                ]
            ) from exc
    return current


def _compare_condition_values(operator: str, selected: Any, expected: Any) -> bool:
    if operator == "equals":
        return selected == expected
    if operator == "not_equals":
        return selected != expected
    if operator in {"gt", "gte", "lt", "lte"}:
        left = _coerce_value(selected, "number")
        right = _coerce_value(expected, "number")
        return {
            "gt": left > right,
            "gte": left >= right,
            "lt": left < right,
            "lte": left <= right,
        }[operator]
    if operator == "contains":
        return isinstance(selected, (list, str, dict)) and expected in selected
    if operator == "in":
        return isinstance(expected, list) and selected in expected
    if operator == "not_in":
        return isinstance(expected, list) and selected not in expected
    return False
