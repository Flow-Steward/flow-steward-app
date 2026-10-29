"""Catalog action validation and Polars expression compilation for tabular feeds."""

from __future__ import annotations

import math
from collections.abc import Mapping
from decimal import Decimal
from typing import Any

from core.infrastructure import standard_money, standard_numbers
from core.infrastructure.date_types import DateLexicon
from core.infrastructure.html_text import html_to_text
from core.infrastructure.rounding import quantize_decimal
from core.infrastructure.tabular_feed.common import _as_dict, _as_list, _pl, _text
from core.infrastructure.tabular_feed.conditions import (
    _coerce_number,
    _condition_columns,
    _condition_matches,
    _empty,
    _when_expr,
)
from core.infrastructure.tabular_feed.constants import RULE_KEYS, TABULAR_ACTION_CATALOG
from core.infrastructure.tabular_feed.dataframe_ops import (
    _column_names,
    _validate_columns_exist,
)
from core.infrastructure.tabular_feed.date_actions import DATE_ACTION_IDS as _TABULAR_DATE_ACTIONS
from core.infrastructure.tabular_feed.date_actions import (
    _apply_date_rule_expression,
    _is_date_rule,
    apply_date_rule_to_row,
)


def _validate_rule(rule: Any, index: int) -> dict[str, Any]:
    if not isinstance(rule, dict):
        raise ValueError(f"rule {index} must be an object")
    unknown = set(rule) - RULE_KEYS
    if unknown:
        raise ValueError(f"rule {index} unknown key: {min(unknown)}")
    ops = [key for key in RULE_KEYS if key not in {"target", "when"} and key in rule]
    if len(ops) != 1:
        raise ValueError(f"rule {index} must define exactly one operation")
    op = ops[0]
    if op in {
        "set",
        "multiply",
        "multiply_by_column",
        "round",
        "money_multiply",
        "money_round",
        "discount_percent",
        "floor_multiply",
    } and not _text(rule.get("target")):
        raise ValueError(f"rule {index} requires target")
    if op == "date_operation":
        action_id = _text(_as_dict(rule.get(op)).get("action_id"))
        if action_id not in _TABULAR_DATE_ACTIONS:
            raise ValueError(f"rule {index} has unsupported date action")
        if not _text(rule.get("target")):
            raise ValueError(f"rule {index} requires target")
    return rule


def _shared_action_to_legacy_rule(action: dict[str, Any]) -> dict[str, Any]:
    action_id = _text(action.get("action")).lower()
    target = _text(action.get("target"))
    params = _as_dict(action.get("params"))
    catalog = TABULAR_ACTION_CATALOG.get(action_id)
    if catalog is None:
        raise ValueError(f"unsupported action: {action_id}")
    unknown = set(params) - catalog["params"]
    if unknown:
        raise ValueError(f"action {action_id} unknown param: {min(unknown)}")
    required = set(catalog.get("required", set()))
    if missing := required - set(params):
        raise ValueError(f"action {action_id} requires param: {min(missing)}")
    if not target:
        raise ValueError(f"action {action_id} requires target")
    rule: dict[str, Any] = {"target": target} if target else {}
    if action.get("when") is not None:
        rule["when"] = action.get("when")
    if action_id in _TABULAR_DATE_ACTIONS:
        rule["date_operation"] = {"action_id": action_id, "params": params}
        return rule
    for translate in (_numeric_action_rule, _text_action_rule, _value_action_rule):
        if translate(action_id, target, params, rule):
            return rule
    raise ValueError(f"unsupported action: {action_id}")


def _normalize_actions(payload: dict[str, Any]) -> list[dict[str, Any]]:
    raw_rules = _as_list(payload.get("rules"))
    raw_actions = _as_list(payload.get("actions"))
    if raw_rules:
        return raw_rules
    return [
        _shared_action_to_legacy_rule(action) for action in raw_actions if isinstance(action, dict)
    ]


def _numeric_expr(column: str):
    pl = _pl()
    return pl.col(column).cast(pl.Float64, strict=False)


def _money_expr(column: str):
    pl = _pl()
    return pl.col(column).cast(pl.String).cast(pl.Decimal(precision=38, scale=6), strict=False)


def _money_literal(value: Any):
    pl = _pl()
    return pl.lit(str(value)).cast(pl.Decimal(precision=38, scale=6), strict=False)


_POLARS_ROUNDING_MODES = {"half_up": "half_away_from_zero", "half_even": "half_to_even"}


def _rounded_expr(expr: Any, decimals: int, mode: Any):
    """Round a Polars expression with the declared mode.

    Polars rounds to a decimal place directly for the half-* modes. Ceiling and
    floor have no decimals argument, so they are expressed by scaling: on the
    Decimal money column that scaling is exact, which is the whole point of having
    a ceiling that does not go through floats.
    """
    from core.infrastructure.rounding import normalize_rounding_mode

    normalized = normalize_rounding_mode(mode)
    places = max(0, int(decimals))
    polars_mode = _POLARS_ROUNDING_MODES.get(normalized)
    if polars_mode is not None:
        return expr.round(places, mode=polars_mode)
    scale = 10**places
    scaled = expr * scale if places else expr
    directed = scaled.ceil() if normalized == "ceil" else scaled.floor()
    return directed / scale if places else directed


def _rule_target(rule: dict[str, Any]) -> str:
    if "default" in rule:
        return _text(_as_dict(rule.get("default")).get("target"))
    if "coerce" in rule:
        return _text(_as_dict(rule.get("coerce")).get("column"))
    if "map_values" in rule:
        return _text(_as_dict(rule.get("map_values")).get("column"))
    return _text(rule.get("target"))


def _rule_required_columns(rule: dict[str, Any]) -> set[str]:
    required = set(_condition_columns(rule.get("when")))
    target = _rule_target(rule)
    if target and "set" not in rule and "default" not in rule:
        required.add(target)
    if "multiply_by_column" in rule:
        required.add(_text(rule.get("multiply_by_column")))
    if "rename" in rule:
        required.update(_text(item) for item in _as_dict(rule.get("rename")))
    if "drop" in rule:
        required.update(_text(item) for item in _as_list(rule.get("drop")))
    return {column for column in required if column}


def _empty_expr(column: str):
    pl = _pl()
    value = pl.col(column)
    return value.is_null() | (value.cast(pl.String) == "")


def _map_values_expr(column: str, mapping: dict[str, Any], keep_unmapped: bool):
    pl = _pl()
    source = pl.col(column)
    text_source = source.cast(pl.String)
    result = source if keep_unmapped else pl.lit("")
    for raw_key, raw_value in reversed(list(mapping.items())):
        result = pl.when(text_source == _text(raw_key)).then(pl.lit(raw_value)).otherwise(result)
    return result.alias(column)


_SIMPLE_TEXT_ACTIONS = ("trim", "upper", "lower", "title_case", "length", "html_to_text")


def _simple_text_expression(target: str, rule: dict[str, Any]):
    pl = _pl()
    source = pl.col(target).cast(pl.String)
    if "trim" in rule:
        return source.str.strip_chars()
    if "upper" in rule:
        return source.str.to_uppercase()
    if "lower" in rule:
        return source.str.to_lowercase()
    if "title_case" in rule:
        return source.str.to_titlecase()
    if "length" in rule:
        return source.str.len_chars()
    return source.map_elements(html_to_text, return_dtype=pl.String)


def _rule_expression(
    df: Any, rule: dict[str, Any], *, date_lexicons: Mapping[str, DateLexicon] | None = None
):
    pl = _pl()
    target = _rule_target(rule)
    condition = rule.get("when")
    if _is_date_rule(rule):
        return _apply_date_rule_expression(
            df,
            rule,
            condition,
            lexicons=date_lexicons or {},
        )
    expr = None
    for build in (
        _numeric_rule_expression,
        _money_rule_expression,
        _text_rule_expression,
        _default_rule_expression,
        _coerce_rule_expression,
    ):
        expr = build(df, rule, target)
        if expr is not None:
            break
    if expr is None:
        if "map_values" in rule:
            payload = _as_dict(rule.get("map_values"))
            return _map_values_expr(
                target, _as_dict(payload.get("mapping")), bool(payload.get("keep_unmapped", True))
            )
        raise ValueError("rule cannot be expressed as a column operation")
    if condition not in (None, "", {}):
        original = pl.col(target) if target in _column_names(df) else pl.lit(None)
        expr = pl.when(_when_expr(condition)).then(expr).otherwise(original)
    return expr.alias(target)


def _apply_rule_expressions(
    df: Any,
    rules: list[dict[str, Any]],
    *,
    row_count: int | None = None,
    date_lexicons: Mapping[str, DateLexicon] | None = None,
) -> tuple[Any, dict[str, int]]:
    result = df
    report: dict[str, int] = {str(index): 0 for index in range(len(rules))}
    for index, rule in enumerate(rules):
        _validate_columns_exist(
            _column_names(result), _rule_required_columns(rule), f"rules[{index}]"
        )
        condition = rule.get("when")
        report[str(index)] = _matching_row_count(result, condition, row_count)
        result = _apply_frame_rule(result, rule, condition, index, date_lexicons=date_lexicons)
    return result, report


def _round_number(value: float, decimals: int, mode: Any) -> float:
    """Round in exact decimal arithmetic so the declared mode is what is applied."""
    return standard_numbers.round_number(value, decimals, mode)


def _money_quantize(value: Decimal, decimals: int, mode: Any) -> str:
    if value.is_finite():
        return standard_money.round_money(value, decimals, mode)
    return format(quantize_decimal(value, decimals=decimals, mode=mode), "f")


def _coerce_value(value: Any, target_type: str) -> Any:
    if target_type in {"int", "integer"}:
        return int(_coerce_number(value))
    if target_type == "number":
        return _coerce_number(value)
    if target_type == "money":
        return format(Decimal(_text(value) or "0"), "f")
    if target_type == "boolean":
        return _text(value).lower() in {"1", "true", "yes", "on"}
    if target_type == "text":
        return _text(value)
    return value


def _apply_rule(
    row: dict[str, Any],
    rule: dict[str, Any],
    *,
    date_lexicons: Mapping[str, DateLexicon] | None = None,
) -> bool:
    if not _condition_matches(row, rule.get("when")):
        return False
    if "rename" in rule:
        return _apply_row_rename(row, rule)
    if "drop" in rule:
        return _apply_row_drop(row, rule)
    if "default" in rule:
        return _apply_row_default(row, rule)
    if "coerce" in rule:
        return _apply_row_coerce(row, rule)
    if "map_values" in rule:
        return _apply_row_map_values(row, rule)
    if _is_date_rule(rule):
        return apply_date_rule_to_row(row, rule, lexicons=date_lexicons or {})
    return _apply_row_numeric(row, rule)


def _apply_row_numeric(row: dict[str, Any], rule: dict[str, Any]) -> bool:
    target = _text(rule.get("target"))
    if "set" in rule:
        row[target] = rule.get("set")
    elif "multiply" in rule:
        row[target] = _coerce_number(row.get(target)) * _coerce_number(rule.get("multiply"))
    elif "multiply_by_column" in rule:
        row[target] = _coerce_number(row.get(target)) * _coerce_number(
            row.get(_text(rule.get("multiply_by_column")))
        )
    elif "money_multiply" in rule:
        row[target] = format(
            Decimal(_text(row.get(target)) or "0") * Decimal(str(rule.get("money_multiply"))),
            "f",
        )
    elif "round" in rule:
        payload = _as_dict(rule.get("round"))
        row[target] = _round_number(
            _coerce_number(row.get(target)),
            int(payload.get("decimals") or 0),
            payload.get("mode"),
        )
    elif "money_round" in rule:
        payload = _as_dict(rule.get("money_round"))
        row[target] = _money_quantize(
            Decimal(_text(row.get(target)) or "0"),
            int(payload.get("decimals") or 2),
            payload.get("mode"),
        )
    elif "discount_percent" in rule:
        row[target] = _coerce_number(row.get(target)) * (
            1 - _coerce_number(rule.get("discount_percent")) / 100
        )
    elif "floor_multiply" in rule:
        row[target] = math.floor(
            _coerce_number(row.get(target)) * _coerce_number(rule.get("floor_multiply"))
        )
    return True


def _numeric_action_rule(
    action_id: str, target: str, params: dict[str, Any], rule: dict[str, Any]
) -> bool:
    simple = {
        "number.multiply": ("multiply", "factor"),
        "number.add": ("add", "operand"),
        "number.subtract": ("subtract", "operand"),
        "number.divide": ("divide", "divisor"),
        "money.multiply": ("money_multiply", "factor"),
        "money.add": ("money_add", "operand"),
        "money.subtract": ("money_subtract", "operand"),
    }
    if action_id in simple:
        operation, parameter = simple[action_id]
        rule[operation] = params.get(parameter)
    elif action_id in {"number.floor"}:
        rule["floor_multiply"] = 1
    elif action_id == "number.ceil":
        rule["ceil"] = True
    elif action_id == "number.round":
        rule["round"] = {
            "decimals": int(params.get("decimals") or 0),
            "mode": _text(params.get("mode"), "half_up"),
        }
    elif action_id == "money.round":
        rule["money_round"] = {
            "decimals": int(params["decimals"]) if "decimals" in params else 2,
            "mode": _text(params.get("mode"), "half_up"),
        }
    else:
        return False
    return True


def _text_action_rule(
    action_id: str, target: str, params: dict[str, Any], rule: dict[str, Any]
) -> bool:
    if action_id in {
        "text.trim",
        "text.upper",
        "text.lower",
        "text.title_case",
        "text.length",
        "text.html_to_text",
    }:
        rule[action_id.split(".", 1)[1]] = True
    elif action_id == "text.replace":
        rule["text_replace"] = params
    elif action_id == "text.append":
        rule["text_append"] = params.get("append_value")
    elif action_id == "text.pad":
        rule["text_pad"] = params
    else:
        return False
    return True


def _value_action_rule(
    action_id: str, target: str, params: dict[str, Any], rule: dict[str, Any]
) -> bool:
    if action_id in {"text.default", "value.default"}:
        rule.pop("target", None)
        rule["default"] = {"target": target, "set": params.get("value")}
    elif action_id in {"text.set", "value.set"}:
        rule["set"] = params.get("value")
    elif action_id in {"boolean.coerce", "number.coerce", "text.coerce", "money.coerce"}:
        rule.pop("target", None)
        rule["coerce"] = {"column": target, "type": action_id.split(".", 1)[0]}
    elif action_id in {"map.values", "value.map"}:
        rule.pop("target", None)
        rule["map_values"] = {
            "column": target,
            "mapping": _as_dict(params.get("mapping")),
            "keep_unmapped": bool(params.get("keep_unmapped", True)),
        }
    else:
        return False
    return True


def _numeric_rule_expression(df: Any, rule: dict[str, Any], target: str) -> Any:
    pl = _pl()
    if "set" in rule:
        expr = pl.lit(rule.get("set"))
    elif "multiply" in rule:
        expr = _numeric_expr(target) * float(rule.get("multiply") or 0)
    elif "add" in rule:
        expr = _numeric_expr(target) + float(rule.get("add") or 0)
    elif "subtract" in rule:
        expr = _numeric_expr(target) - float(rule.get("subtract") or 0)
    elif "divide" in rule:
        divisor = float(rule.get("divide") or 0)
        if divisor == 0:
            raise ValueError("number.divide divisor must not be zero")
        expr = _numeric_expr(target) / divisor
    elif "multiply_by_column" in rule:
        expr = _numeric_expr(target) * _numeric_expr(_text(rule.get("multiply_by_column")))
    elif "round" in rule:
        payload = _as_dict(rule.get("round"))
        expr = _rounded_expr(
            _numeric_expr(target),
            int(payload.get("decimals") or 0),
            payload.get("mode"),
        )
    else:
        return None
    return expr


def _money_rule_expression(df: Any, rule: dict[str, Any], target: str) -> Any:
    pl = _pl()
    if "money_multiply" in rule:
        expr = _money_expr(target) * _money_literal(rule.get("money_multiply"))
    elif "money_add" in rule:
        expr = _money_expr(target) + _money_literal(rule.get("money_add"))
    elif "money_subtract" in rule:
        expr = _money_expr(target) - _money_literal(rule.get("money_subtract"))
    elif "money_round" in rule:
        payload = _as_dict(rule.get("money_round"))
        decimals = int(payload["decimals"]) if "decimals" in payload else 2
        expr = (
            _rounded_expr(_money_expr(target), decimals, payload.get("mode"))
            .cast(pl.Decimal(precision=38, scale=decimals))
            .cast(pl.String)
        )
    else:
        return None
    return expr


def _text_rule_expression(df: Any, rule: dict[str, Any], target: str) -> Any:
    pl = _pl()
    if "ceil" in rule:
        expr = _numeric_expr(target).ceil()
    elif any(action in rule for action in _SIMPLE_TEXT_ACTIONS):
        expr = _simple_text_expression(target, rule)
    elif "text_replace" in rule:
        payload = _as_dict(rule.get("text_replace"))
        expr = (
            pl.col(target)
            .cast(pl.String)
            .str.replace_all(
                _text(payload.get("find")), _text(payload.get("replace_with")), literal=True
            )
        )
    elif "text_append" in rule:
        expr = pl.concat_str(
            [pl.col(target).cast(pl.String), pl.lit(_text(rule.get("text_append")))]
        )
    elif "text_pad" in rule:
        payload = _as_dict(rule.get("text_pad"))
        width = int(payload.get("width") or 0)
        fill = _text(payload.get("fill_char"), " ") or " "
        source = pl.col(target).cast(pl.String)
        expr = (
            source.str.pad_start(width, fill)
            if _text(payload.get("side"), "left") == "left"
            else source.str.pad_end(width, fill)
        )
    else:
        return None
    return expr


def _default_rule_expression(df: Any, rule: dict[str, Any], target: str) -> Any:
    pl = _pl()
    if "discount_percent" in rule:
        expr = _numeric_expr(target) * (1 - float(rule.get("discount_percent") or 0) / 100)
    elif "floor_multiply" in rule:
        expr = (_numeric_expr(target) * float(rule.get("floor_multiply") or 0)).floor()
    elif "default" in rule:
        payload = _as_dict(rule.get("default"))
        target = _text(payload.get("target"))
        base = pl.col(target) if target in _column_names(df) else pl.lit(None)
        expr = (
            pl.when(base.is_null() | (base.cast(pl.String) == ""))
            .then(pl.lit(payload.get("set")))
            .otherwise(base)
        )
    else:
        return None
    return expr


def _coerce_rule_expression(df: Any, rule: dict[str, Any], target: str) -> Any:
    pl = _pl()
    if "coerce" in rule:
        payload = _as_dict(rule.get("coerce"))
        target_type = _text(payload.get("type"), "text")
        source = pl.col(target)
        if target_type in {"int", "integer"}:
            expr = source.cast(pl.Int64, strict=False)
        elif target_type == "number":
            expr = source.cast(pl.Float64, strict=False)
        elif target_type == "money":
            expr = source.cast(pl.String).cast(pl.Decimal(precision=38, scale=6), strict=False)
        elif target_type == "boolean":
            expr = source.cast(pl.String).str.to_lowercase().is_in(["1", "true", "yes", "on"])
        else:
            expr = source.cast(pl.String)
    else:
        return None
    return expr


def _matching_row_count(result: Any, condition: Any, row_count: int | None) -> int:
    if condition in (None, "", {}):
        if row_count is not None:
            return int(row_count)
        return int(result.height)
    count_frame = result.select(_when_expr(condition).sum())
    collect = getattr(count_frame, "collect", None)
    if callable(collect):
        count_frame = collect(engine="streaming")
    return int(count_frame.item() or 0)


def _apply_frame_rule(
    result: Any,
    rule: dict[str, Any],
    condition: Any,
    index: int,
    *,
    date_lexicons: Mapping[str, DateLexicon] | None = None,
) -> Any:
    if "rename" in rule:
        if condition not in (None, "", {}):
            raise ValueError(f"rules[{index}].rename does not support when")
        mapping = {
            _text(old): _text(new)
            for old, new in _as_dict(rule.get("rename")).items()
            if _text(old) and _text(new)
        }
        result = result.rename(mapping)
    elif "drop" in rule:
        if condition not in (None, "", {}):
            raise ValueError(f"rules[{index}].drop does not support when")
        result = result.drop(
            [_text(column) for column in _as_list(rule.get("drop")) if _text(column)]
        )
    else:
        result = result.with_columns(_rule_expression(result, rule, date_lexicons=date_lexicons))
    return result


def _apply_row_rename(row: dict[str, Any], rule: dict[str, Any]) -> bool:
    for old, new in _as_dict(rule.get("rename")).items():
        old_name = _text(old)
        new_name = _text(new)
        if old_name in row and new_name:
            row[new_name] = row.pop(old_name)
    return True


def _apply_row_drop(row: dict[str, Any], rule: dict[str, Any]) -> bool:
    for column in _as_list(rule.get("drop")):
        row.pop(_text(column), None)
    return True


def _apply_row_default(row: dict[str, Any], rule: dict[str, Any]) -> bool:
    payload = _as_dict(rule.get("default"))
    target = _text(payload.get("target"))
    if target and _empty(row.get(target)):
        row[target] = payload.get("set")
        return True
    return False


def _apply_row_coerce(row: dict[str, Any], rule: dict[str, Any]) -> bool:
    payload = _as_dict(rule.get("coerce"))
    column = _text(payload.get("column"))
    if column:
        row[column] = _coerce_value(row.get(column), _text(payload.get("type"), "text"))
        return True
    return False


def _apply_row_map_values(row: dict[str, Any], rule: dict[str, Any]) -> bool:
    payload = _as_dict(rule.get("map_values"))
    column = _text(payload.get("column"))
    mapping = _as_dict(payload.get("mapping"))
    if column in row:
        raw = row.get(column)
        if raw in mapping:
            row[column] = mapping[raw]
            return True
        text = _text(raw)
        if text in mapping:
            row[column] = mapping[text]
            return True
        if not bool(payload.get("keep_unmapped", True)):
            row[column] = ""
            return True
    return False
