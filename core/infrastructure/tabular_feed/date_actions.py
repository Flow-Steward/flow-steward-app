"""Date-action adapters for row and Polars tabular-feed transforms."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from core.infrastructure.date_types import DateLexicon
from core.infrastructure.tabular_feed.common import _as_dict, _pl, _text
from core.infrastructure.tabular_feed.conditions import _when_expr

DATE_ACTION_IDS = frozenset(
    {
        "date.parse",
        "date.to_utc",
        "date.to_timezone",
        "date.format",
        "date.add",
        "date.part",
        "date.start_of",
        "date.end_of",
    }
)


def _operation(rule: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    operation = _as_dict(rule.get("date_operation"))
    action_id = _text(operation.get("action_id"))
    if action_id not in DATE_ACTION_IDS:
        raise ValueError("unsupported tabular date action")
    return action_id, _as_dict(operation.get("params"))


def _execute(
    action_id: str,
    value: object,
    params: Mapping[str, object],
    lexicons: Mapping[str, DateLexicon],
) -> object:
    from core.infrastructure.date_operations import execute_date_operation

    return execute_date_operation(action_id, value, params, lexicons=lexicons)


def apply_date_rule_to_row(
    row: dict[str, Any], rule: dict[str, Any], *, lexicons: Mapping[str, DateLexicon]
) -> bool:
    target = _text(rule.get("target"))
    if not target or target not in row or row[target] is None:
        return False
    action_id, params = _operation(rule)
    row[target] = _execute(action_id, row[target], params, lexicons)
    return True


def _collect_values(frame: Any, target: str, condition: Any) -> list[object]:
    pl = _pl()
    selected = frame
    if condition not in (None, "", {}):
        selected = selected.filter(_when_expr(condition))
    values_frame = selected.select(pl.col(target).drop_nulls().unique(maintain_order=True))
    collect = getattr(values_frame, "collect", None)
    if callable(collect):
        values_frame = collect(engine="streaming")
    return values_frame.get_column(target).to_list()


def _has_nonmatching_nonnull_value(frame: Any, target: str, condition: Any) -> bool:
    pl = _pl()
    selected = frame.filter(~(_when_expr(condition).fill_null(False)))
    probe = selected.select(pl.col(target).drop_nulls().head(1))
    collect = getattr(probe, "collect", None)
    if callable(collect):
        probe = collect(engine="streaming")
    return probe.height > 0


def _return_dtype(action_id: str):
    pl = _pl()
    return pl.Int64 if action_id == "date.part" else pl.String


def _mapped_expression(
    target: str,
    values: list[object],
    action_id: str,
    params: dict[str, Any],
    lexicons: Mapping[str, DateLexicon],
):
    pl = _pl()
    source = pl.col(target)
    return_dtype = _return_dtype(action_id)
    if not values:
        return pl.lit(None, dtype=return_dtype)
    outputs = [_execute(action_id, value, params, lexicons) for value in values]
    return source.replace_strict(
        values,
        outputs,
        default=None,
        return_dtype=return_dtype,
    )


def _apply_date_rule_expression(
    frame: Any,
    rule: dict[str, Any],
    condition: Any,
    *,
    lexicons: Mapping[str, DateLexicon],
):
    pl = _pl()
    target = _text(rule.get("target"))
    action_id, params = _operation(rule)
    values = _collect_values(frame, target, condition)
    if action_id == "date.part" and condition not in (None, "", {}):
        if not values:
            return pl.col(target).alias(target)
        if _has_nonmatching_nonnull_value(frame, target, condition):
            raise ValueError(
                "date.part with when cannot mix numeric results with unmatched non-null text values; "
                "apply it to every non-null row or use a separate target column"
            )
    result = _mapped_expression(target, values, action_id, params, lexicons)
    if condition not in (None, "", {}):
        otherwise = pl.lit(None, dtype=pl.Int64) if action_id == "date.part" else pl.col(target)
        result = pl.when(_when_expr(condition)).then(result).otherwise(otherwise)
    return result.alias(target)


def _is_date_rule(rule: dict[str, Any]) -> bool:
    return "date_operation" in rule
