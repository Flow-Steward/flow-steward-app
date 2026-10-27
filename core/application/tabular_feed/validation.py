"""Configuration validation for first-party ``tabular_feed`` workflow steps."""

from __future__ import annotations

from typing import Any

from core.infrastructure.rounding import (
    UnsupportedRoundingModeError,
    normalize_rounding_mode,
)

from .contracts import (
    CHECKSUM_OPERATION,
    CONDITION_KEYS,
    CONDITION_OPERATORS,
    INPUT_FIELDS_BY_OPERATION,
    LEGACY_RULE_FIELDS,
    OPERATION_ACTIONS,
    RELATIONAL_AGG_FIELDS,
    RELATIONAL_AGG_FUNCS,
    RELATIONAL_OP_FIELDS,
    SUPPORTED_OUTPUT_FORMATS,
    TABULAR_ACTION_FIELDS,
    TABULAR_ACTION_PARAMS,
    TABULAR_ACTION_REQUIRED_PARAMS,
    TOP_LEVEL_FIELDS,
    snapshot_mode,
)


def _as_dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _as_list(value: Any) -> list[Any]:
    return list(value) if isinstance(value, list) else []


def _text(value: Any) -> str:
    return str(value or "").strip()


def _error(code: str, message: str, path: str) -> dict[str, Any]:
    return {
        "code": f"workflow.step_tabular_feed_{code}",
        "message": message,
        "path": path,
        "severity": "error",
    }


def _value_from_config(config: dict[str, Any], input_cfg: dict[str, Any], key: str) -> Any:
    return input_cfg.get(key) if key in input_cfg else config.get(key)


def _is_rows_reference(value: Any) -> bool:
    """True when ``rows`` is a workflow state reference the step runtime resolves.

    ``rows`` is copied through ``PAYLOAD_COPY_FIELDS`` and rendered by
    ``_render_tabular_value`` before dispatch, so a ``state.*`` selector is a valid
    authoring-time value even though the engine ultimately needs a list.
    """
    return isinstance(value, str) and value.strip().startswith("state.")


def _rows_supplied(value: Any) -> bool:
    return isinstance(value, list) or _is_rows_reference(value)


def _require_text(
    errors: list[dict[str, Any]],
    config: dict[str, Any],
    input_cfg: dict[str, Any],
    key: str,
    path_prefix: str,
    *,
    input_field: bool = False,
) -> None:
    if _text(_value_from_config(config, input_cfg, key)):
        return
    path = (
        f"{path_prefix}.input.{key}" if input_field or key in input_cfg else f"{path_prefix}.{key}"
    )
    errors.append(_error("field_required", f"tabular_feed requires {key}", path))


def _validate_condition(
    condition: Any, *, path_prefix: str, allow_empty: bool = False
) -> list[dict[str, Any]]:
    errors: list[dict[str, Any]] = []
    if condition in (None, ""):
        if not allow_empty:
            errors.append(
                _error("condition_required", "tabular_feed condition is required", path_prefix)
            )
        return errors
    if not isinstance(condition, dict):
        return [
            _error("condition_invalid", "tabular_feed condition must be an object", path_prefix)
        ]
    if not condition:
        if not allow_empty:
            errors.append(
                _error("condition_required", "tabular_feed condition is required", path_prefix)
            )
        return errors
    for key in sorted(set(condition) - CONDITION_KEYS):
        errors.append(
            _error(
                "condition_field_unknown",
                f"tabular_feed condition field is not supported: {key}",
                f"{path_prefix}.{key}",
            )
        )
    composite_keys = [key for key in ("all", "any") if key in condition]
    if composite_keys:
        if len(composite_keys) > 1 or set(condition) - set(composite_keys):
            errors.append(
                _error(
                    "condition_invalid",
                    "tabular_feed composite condition must contain only all or any",
                    path_prefix,
                )
            )
        for key in composite_keys:
            items = condition.get(key)
            if not isinstance(items, list) or not items:
                errors.append(
                    _error(
                        "condition_invalid",
                        f"tabular_feed condition {key} must be a non-empty list",
                        f"{path_prefix}.{key}",
                    )
                )
                continue
            for index, item in enumerate(items):
                errors.extend(
                    _validate_condition(item, path_prefix=f"{path_prefix}.{key}[{index}]")
                )
        return errors
    if not _text(condition.get("column")):
        errors.append(
            _error(
                "condition_column_required",
                "tabular_feed condition requires column",
                f"{path_prefix}.column",
            )
        )
    operators = [key for key in CONDITION_OPERATORS if key in condition]
    if len(operators) != 1:
        errors.append(
            _error(
                "condition_operator_invalid",
                "tabular_feed condition requires exactly one supported operator",
                path_prefix,
            )
        )
    elif operators[0] in {"in", "not_in"} and not isinstance(condition.get(operators[0]), list):
        errors.append(
            _error(
                "condition_operator_invalid",
                f"tabular_feed condition {operators[0]} must be a list",
                f"{path_prefix}.{operators[0]}",
            )
        )
    elif operators[0] in {"is_empty", "is_not_empty"} and not isinstance(
        condition.get(operators[0]), bool
    ):
        errors.append(
            _error(
                "condition_operator_invalid",
                f"tabular_feed condition {operators[0]} must be boolean",
                f"{path_prefix}.{operators[0]}",
            )
        )
    return errors


def _validate_actions(actions: Any, *, path_prefix: str) -> list[dict[str, Any]]:
    errors: list[dict[str, Any]] = []
    if actions is None:
        return errors
    if not isinstance(actions, list):
        return [_error("actions_invalid", "tabular_feed actions must be a list", path_prefix)]
    for index, action in enumerate(actions):
        action_path = f"{path_prefix}[{index}]"
        if not isinstance(action, dict):
            errors.append(
                _error("action_invalid", "tabular_feed action must be an object", action_path)
            )
            continue
        for key in sorted(set(action) - TABULAR_ACTION_FIELDS):
            errors.append(
                _error(
                    "action_field_unknown",
                    f"tabular_feed action field is not supported: {key}",
                    f"{action_path}.{key}",
                )
            )
        action_id = _text(action.get("action"))
        allowed_params = TABULAR_ACTION_PARAMS.get(action_id)
        if allowed_params is None:
            errors.append(
                _error(
                    "action_unknown",
                    f"unknown tabular_feed action: {action_id or '<missing>'}",
                    f"{action_path}.action",
                )
            )
            continue
        if not _text(action.get("target")):
            errors.append(
                _error(
                    "action_target_required",
                    f"tabular_feed action {action_id} requires target",
                    f"{action_path}.target",
                )
            )
        params = action.get("params", {})
        if not isinstance(params, dict):
            errors.append(
                _error(
                    "action_params_invalid",
                    "tabular_feed action params must be an object",
                    f"{action_path}.params",
                )
            )
            continue
        for key in sorted(set(params) - allowed_params):
            errors.append(
                _error(
                    "action_param_unknown",
                    f"tabular_feed action {action_id} does not accept {key}",
                    f"{action_path}.params.{key}",
                )
            )
        if "mode" in params:
            try:
                normalize_rounding_mode(params.get("mode"))
            except UnsupportedRoundingModeError as exc:
                errors.append(
                    _error(
                        "action_param_value_invalid",
                        f"tabular_feed action {action_id} {exc}",
                        f"{action_path}.params.mode",
                    )
                )
        for key in sorted(TABULAR_ACTION_REQUIRED_PARAMS.get(action_id, set()) - set(params)):
            errors.append(
                _error(
                    "action_param_required",
                    f"tabular_feed action {action_id} requires {key}",
                    f"{action_path}.params.{key}",
                )
            )
        if "when" in action:
            errors.extend(
                _validate_condition(action.get("when"), path_prefix=f"{action_path}.when")
            )
    return errors


def _validate_legacy_rules(rules: Any, *, path_prefix: str) -> list[dict[str, Any]]:
    errors: list[dict[str, Any]] = []
    if rules is None:
        return errors
    if not isinstance(rules, list):
        return [_error("rules_invalid", "tabular_feed rules must be a list", path_prefix)]
    for index, rule in enumerate(rules):
        rule_path = f"{path_prefix}[{index}]"
        if not isinstance(rule, dict):
            errors.append(_error("rule_invalid", "tabular_feed rule must be an object", rule_path))
            continue
        for key in sorted(set(rule) - LEGACY_RULE_FIELDS):
            errors.append(
                _error(
                    "rule_field_unknown",
                    f"tabular_feed rule field is not supported: {key}",
                    f"{rule_path}.{key}",
                )
            )
        operations = list(set(rule) & (LEGACY_RULE_FIELDS - {"target", "when"}))
        if len(operations) != 1:
            errors.append(
                _error(
                    "rule_operation_invalid",
                    "tabular_feed rule must define exactly one operation",
                    rule_path,
                )
            )
        if "when" in rule:
            errors.extend(_validate_condition(rule.get("when"), path_prefix=f"{rule_path}.when"))
    return errors


def _validate_string_list(
    value: Any, *, path_prefix: str, code: str, message: str
) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not [_text(item) for item in value if _text(item)]:
        return [_error(code, message, path_prefix)]
    return []


def _validate_relational(relational: Any, *, path_prefix: str) -> list[dict[str, Any]]:
    errors: list[dict[str, Any]] = []
    if relational is None:
        return errors
    if not isinstance(relational, list):
        return [_error("relational_invalid", "tabular_feed relational must be a list", path_prefix)]
    for index, raw in enumerate(relational):
        op_path = f"{path_prefix}[{index}]"
        if not isinstance(raw, dict):
            errors.append(
                _error(
                    "relational_invalid", "tabular_feed relational op must be an object", op_path
                )
            )
            continue
        op = _text(raw.get("op")).lower()
        allowed_fields = RELATIONAL_OP_FIELDS.get(op)
        if allowed_fields is None:
            allowed = ", ".join(sorted(RELATIONAL_OP_FIELDS))
            errors.append(
                _error(
                    "relational_op_invalid",
                    f"tabular_feed relational op must be one of: {allowed}",
                    f"{op_path}.op",
                )
            )
            continue
        for key in sorted(set(raw) - allowed_fields):
            errors.append(
                _error(
                    "relational_field_unknown",
                    f"tabular_feed relational {op} field is not supported: {key}",
                    f"{op_path}.{key}",
                )
            )
        if op == "filter":
            if "when" in raw and "condition" in raw:
                errors.append(
                    _error(
                        "relational_condition_ambiguous",
                        "tabular_feed filter accepts either when or condition, not both",
                        op_path,
                    )
                )
            condition = raw.get("when") if "when" in raw else raw.get("condition")
            errors.extend(_validate_condition(condition, path_prefix=f"{op_path}.when"))
        elif op == "dedupe_by":
            errors.extend(
                _validate_string_list(
                    raw.get("keys"),
                    path_prefix=f"{op_path}.keys",
                    code="relational_keys_required",
                    message="tabular_feed dedupe_by requires non-empty keys",
                )
            )
            keep = _text(raw.get("keep") or "first").lower()
            if keep not in {"first", "last"}:
                errors.append(
                    _error(
                        "relational_keep_invalid",
                        "tabular_feed dedupe_by keep must be first or last",
                        f"{op_path}.keep",
                    )
                )
        elif op == "sort":
            if "by" in raw and "columns" in raw:
                errors.append(
                    _error(
                        "relational_sort_ambiguous",
                        "tabular_feed sort accepts either by or columns, not both",
                        op_path,
                    )
                )
            by = raw.get("by") if "by" in raw else raw.get("columns")
            if not isinstance(by, list) or not by:
                errors.append(
                    _error(
                        "relational_sort_required",
                        "tabular_feed sort requires non-empty by or columns",
                        f"{op_path}.by",
                    )
                )
            else:
                for sort_index, item in enumerate(by):
                    item_path = f"{op_path}.by[{sort_index}]"
                    if isinstance(item, dict):
                        for key in sorted(set(item) - {"column", "name", "direction"}):
                            errors.append(
                                _error(
                                    "relational_sort_field_unknown",
                                    f"tabular_feed sort field is not supported: {key}",
                                    f"{item_path}.{key}",
                                )
                            )
                        if not _text(item.get("column") or item.get("name")):
                            errors.append(
                                _error(
                                    "relational_sort_required",
                                    "tabular_feed sort item requires column",
                                    item_path,
                                )
                            )
                        direction = _text(item.get("direction") or "asc").lower()
                        if direction not in {"asc", "desc"}:
                            errors.append(
                                _error(
                                    "relational_sort_direction_invalid",
                                    "tabular_feed sort direction must be asc or desc",
                                    f"{item_path}.direction",
                                )
                            )
                    elif not _text(item):
                        errors.append(
                            _error(
                                "relational_sort_required",
                                "tabular_feed sort item requires column",
                                item_path,
                            )
                        )
            if "descending" in raw and not isinstance(raw.get("descending"), bool):
                errors.append(
                    _error(
                        "relational_sort_direction_invalid",
                        "tabular_feed sort descending must be boolean",
                        f"{op_path}.descending",
                    )
                )
            if "nulls_last" in raw and not isinstance(raw.get("nulls_last"), bool):
                errors.append(
                    _error(
                        "relational_sort_nulls_invalid",
                        "tabular_feed sort nulls_last must be boolean",
                        f"{op_path}.nulls_last",
                    )
                )
        elif op == "group_by":
            errors.extend(
                _validate_string_list(
                    raw.get("keys"),
                    path_prefix=f"{op_path}.keys",
                    code="relational_keys_required",
                    message="tabular_feed group_by requires non-empty keys",
                )
            )
            aggregations = raw.get("aggregations")
            if not isinstance(aggregations, list) or not aggregations:
                errors.append(
                    _error(
                        "relational_aggregations_required",
                        "tabular_feed group_by requires aggregations",
                        f"{op_path}.aggregations",
                    )
                )
            else:
                for agg_index, aggregation in enumerate(aggregations):
                    agg_path = f"{op_path}.aggregations[{agg_index}]"
                    if not isinstance(aggregation, dict):
                        errors.append(
                            _error(
                                "relational_aggregation_invalid",
                                "tabular_feed group_by aggregation must be an object",
                                agg_path,
                            )
                        )
                        continue
                    for key in sorted(set(aggregation) - RELATIONAL_AGG_FIELDS):
                        errors.append(
                            _error(
                                "relational_aggregation_field_unknown",
                                f"tabular_feed group_by aggregation field is not supported: {key}",
                                f"{agg_path}.{key}",
                            )
                        )
                    func = _text(aggregation.get("agg") or aggregation.get("function")).lower()
                    if func not in RELATIONAL_AGG_FUNCS:
                        errors.append(
                            _error(
                                "relational_aggregation_invalid",
                                "tabular_feed group_by aggregation function is not supported",
                                f"{agg_path}.agg",
                            )
                        )
                    if func != "count" and not _text(aggregation.get("column")):
                        errors.append(
                            _error(
                                "relational_aggregation_column_required",
                                "tabular_feed group_by aggregation requires column",
                                f"{agg_path}.column",
                            )
                        )
        elif op in {"explode", "unnest"} and not _text(raw.get("column")):
            errors.append(
                _error(
                    "relational_column_required",
                    f"tabular_feed {op} requires column",
                    f"{op_path}.column",
                )
            )
    return errors


def validate_tabular_feed_config(
    config: dict[str, Any],
    *,
    path_prefix: str = "override.tabular_feed",
) -> list[dict[str, Any]]:
    errors: list[dict[str, Any]] = []
    if not isinstance(config, dict) or not config:
        return [
            _error(
                "config_missing",
                "tabular_feed requires override.tabular_feed configuration",
                path_prefix,
            )
        ]
    for key in sorted(set(config) - TOP_LEVEL_FIELDS):
        errors.append(
            _error(
                "field_unknown",
                f"tabular_feed field is not supported: {key}",
                f"{path_prefix}.{key}",
            )
        )
    operation = _text(config.get("operation")).lower()
    if operation not in OPERATION_ACTIONS:
        allowed = ", ".join(sorted(OPERATION_ACTIONS))
        errors.append(
            _error(
                "operation_invalid",
                f"tabular_feed operation must be one of: {allowed}",
                f"{path_prefix}.operation",
            )
        )
    input_cfg = config.get("input")
    normalized_input = _as_dict(input_cfg)
    if input_cfg is not None and not isinstance(input_cfg, dict):
        errors.append(
            _error(
                "input_invalid",
                "tabular_feed input must be an object when provided",
                f"{path_prefix}.input",
            )
        )
    elif operation in INPUT_FIELDS_BY_OPERATION:
        allowed_input = INPUT_FIELDS_BY_OPERATION[operation]
        for key in sorted(set(normalized_input) - allowed_input):
            errors.append(
                _error(
                    "field_unknown",
                    f"tabular_feed input field is not supported for {operation}: {key}",
                    f"{path_prefix}.input.{key}",
                )
            )
    result_target = _text(config.get("result_target"))
    if not result_target:
        errors.append(
            _error(
                "result_target_missing",
                "tabular_feed requires result_target",
                f"{path_prefix}.result_target",
            )
        )
    elif result_target != "state" and not result_target.startswith("state."):
        errors.append(
            _error(
                "result_target_invalid",
                "tabular_feed result_target must be state or state.*",
                f"{path_prefix}.result_target",
            )
        )

    if operation == CHECKSUM_OPERATION:
        _require_text(errors, config, normalized_input, "profile_key", path_prefix)
        _require_text(errors, config, normalized_input, "raw_sha256", path_prefix)
    elif operation in {"inspect", "read"}:
        _require_text(errors, config, normalized_input, "artifact_handle", path_prefix)
    elif operation == "transform":
        _require_text(errors, config, normalized_input, "dataset_handle", path_prefix)
        if (
            not _as_list(config.get("actions"))
            and not _as_list(config.get("rules"))
            and not _as_list(config.get("relational"))
        ):
            errors.append(
                _error(
                    "actions_required",
                    "tabular_feed transform requires actions, rules, or relational operations",
                    f"{path_prefix}.actions",
                )
            )
        errors.extend(
            _validate_actions(config.get("actions"), path_prefix=f"{path_prefix}.actions")
        )
        errors.extend(
            _validate_legacy_rules(config.get("rules"), path_prefix=f"{path_prefix}.rules")
        )
        errors.extend(
            _validate_relational(config.get("relational"), path_prefix=f"{path_prefix}.relational")
        )
    elif operation == "concat":
        handles = _as_list(_value_from_config(config, normalized_input, "dataset_handles"))
        if len([item for item in handles if _text(item)]) < 2:
            errors.append(
                _error(
                    "dataset_handles_invalid",
                    "tabular_feed concat requires at least two dataset_handles",
                    f"{path_prefix}.input.dataset_handles",
                )
            )
        errors.extend(
            _validate_relational(config.get("relational"), path_prefix=f"{path_prefix}.relational")
        )
    elif operation == "write":
        _require_text(errors, config, normalized_input, "dataset_handle", path_prefix)
        fmt = _text(config.get("format")).lower()
        if not fmt or fmt not in SUPPORTED_OUTPUT_FORMATS:
            errors.append(
                _error(
                    "format_invalid",
                    "tabular_feed write requires a supported format",
                    f"{path_prefix}.format",
                )
            )
    elif operation == "validate":
        _require_text(errors, config, normalized_input, "dataset_handle", path_prefix)
        if not _as_dict(config.get("checks")):
            errors.append(
                _error(
                    "checks_required",
                    "tabular_feed validate requires checks",
                    f"{path_prefix}.checks",
                )
            )
    elif operation == "diff":
        _require_text(
            errors, config, normalized_input, "dataset_handle", path_prefix, input_field=True
        )
        _require_text(
            errors,
            config,
            normalized_input,
            "baseline_dataset_handle",
            path_prefix,
            input_field=True,
        )
        _require_text(errors, config, normalized_input, "profile_key", path_prefix)
        _require_text(errors, config, normalized_input, "key_column", path_prefix)
        ref = _value_from_config(config, normalized_input, "expected_snapshot_ref")
        try:
            snapshot_mode(config.get("snapshot_mode"), has_reference=ref is not None)
        except ValueError:
            errors.append(
                _error(
                    "snapshot_mode_invalid",
                    "Choose compare or publish; comparisons cannot retain a publication reference",
                    f"{path_prefix}.snapshot_mode",
                )
            )
        if ref is not None:
            from core.application.tabular_feed.snapshot_models import SnapshotRef

            selector = _is_rows_reference(ref) or (
                isinstance(ref, dict)
                and set(ref) == {"source"}
                and _is_rows_reference(ref["source"])
            )
            if not selector:
                try:
                    if not isinstance(ref, dict) or set(ref) != {
                        "revision",
                        "snapshot_artifact_id",
                    }:
                        raise ValueError("invalid_snapshot_ref")
                    SnapshotRef(ref["revision"], ref["snapshot_artifact_id"])
                except ValueError:
                    errors.append(
                        _error(
                            "snapshot_ref_invalid",
                            "Expected snapshot reference must be a complete reference or state selector",
                            f"{path_prefix}.input.expected_snapshot_ref",
                        )
                    )
    elif operation == "commit_snapshot":
        _require_text(errors, config, normalized_input, "profile_key", path_prefix)
        _require_text(errors, config, normalized_input, "snapshot_artifact_id", path_prefix)
    elif operation == "read_rows":
        _require_text(errors, config, normalized_input, "dataset_handle", path_prefix)
        limit = config.get("limit")
        if limit is not None:
            try:
                limit_int = int(limit)
            except (TypeError, ValueError):
                limit_int = 0
            if limit_int < 1 or limit_int > 1000:
                errors.append(
                    _error(
                        "limit_invalid",
                        "tabular_feed read_rows limit must be between 1 and 1000",
                        f"{path_prefix}.limit",
                    )
                )
    elif operation == "merge_rows":
        _require_text(errors, config, normalized_input, "dataset_handle", path_prefix)
        _require_text(errors, config, normalized_input, "key_column", path_prefix)
        if not _rows_supplied(config.get("rows")):
            errors.append(
                _error(
                    "rows_required", "tabular_feed merge_rows requires rows", f"{path_prefix}.rows"
                )
            )
    elif operation == "create_dataset" and not _rows_supplied(config.get("rows")):
        errors.append(
            _error(
                "rows_required", "tabular_feed create_dataset requires rows", f"{path_prefix}.rows"
            )
        )
    return errors
