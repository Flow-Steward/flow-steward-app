"""Relational tabular operation compilation and execution."""

from __future__ import annotations

from typing import Any

from core.infrastructure.tabular_feed.common import _as_dict, _as_list, _pl, _text
from core.infrastructure.tabular_feed.conditions import _condition_columns, _when_expr
from core.infrastructure.tabular_feed.constants import RELATIONAL_OPS
from core.infrastructure.tabular_feed.dataframe_ops import (
    _column_names,
    _validate_columns_exist,
)


def _apply_relational(df: Any, operations: list[Any]) -> Any:
    pl = _pl()
    result = df
    for index, raw in enumerate(operations):
        op = _as_dict(raw)
        op_name = _text(op.get("op")).lower()
        if op_name not in RELATIONAL_OPS:
            raise ValueError(f"relational {index} unsupported op: {op_name}")
        if op_name == "filter":
            _validate_columns_exist(
                _column_names(result),
                _condition_columns(op.get("when") or op.get("condition")),
                f"relational[{index}].filter",
            )
            result = result.filter(_when_expr(op.get("when") or op.get("condition")))
        elif op_name == "dedupe_by":
            keys = [_text(item) for item in _as_list(op.get("keys")) if _text(item)]
            if not keys:
                raise ValueError("dedupe_by requires keys")
            _validate_columns_exist(_column_names(result), keys, f"relational[{index}].keys")
            keep = _text(op.get("keep"), "first").lower()
            result = result.unique(subset=keys, keep="last" if keep == "last" else "first")
        elif op_name == "sort":
            by = _as_list(op.get("by")) or _as_list(op.get("columns"))
            columns: list[str] = []
            descending: list[bool] = []
            for item in by:
                if isinstance(item, dict):
                    columns.append(_text(item.get("column") or item.get("name")))
                    descending.append(_text(item.get("direction"), "asc").lower() == "desc")
                elif _text(item):
                    columns.append(_text(item))
                    descending.append(False)
            if not columns:
                raise ValueError("sort requires columns")
            _validate_columns_exist(_column_names(result), columns, f"relational[{index}].sort")
            if "descending" in op:
                descending = [bool(op.get("descending"))] * len(columns)
            result = result.sort(
                columns,
                descending=descending,
                nulls_last=bool(op.get("nulls_last", True)),
            )
        elif op_name == "group_by":
            keys = [_text(item) for item in _as_list(op.get("keys")) if _text(item)]
            aggregations = _as_list(op.get("aggregations"))
            if not keys or not aggregations:
                raise ValueError("group_by requires keys and aggregations")
            _validate_columns_exist(_column_names(result), keys, f"relational[{index}].keys")
            exprs = []
            for agg in aggregations:
                row = _as_dict(agg)
                column = _text(row.get("column"))
                func = _text(row.get("agg") or row.get("function")).lower()
                alias = _text(row.get("as") or row.get("alias")) or f"{column}_{func}"
                if func == "count":
                    exprs.append(pl.len().alias(alias))
                elif func == "sum":
                    _validate_columns_exist(
                        _column_names(result), [column], f"relational[{index}].aggregations"
                    )
                    exprs.append(pl.col(column).cast(pl.Float64, strict=False).sum().alias(alias))
                elif func == "min":
                    _validate_columns_exist(
                        _column_names(result), [column], f"relational[{index}].aggregations"
                    )
                    exprs.append(pl.col(column).min().alias(alias))
                elif func == "max":
                    _validate_columns_exist(
                        _column_names(result), [column], f"relational[{index}].aggregations"
                    )
                    exprs.append(pl.col(column).max().alias(alias))
                elif func == "first":
                    _validate_columns_exist(
                        _column_names(result), [column], f"relational[{index}].aggregations"
                    )
                    exprs.append(pl.col(column).first().alias(alias))
                elif func == "last":
                    _validate_columns_exist(
                        _column_names(result), [column], f"relational[{index}].aggregations"
                    )
                    exprs.append(pl.col(column).last().alias(alias))
                else:
                    raise ValueError(f"group_by unsupported aggregation: {func}")
            result = result.group_by(keys).agg(exprs)
        elif op_name == "explode":
            column = _text(op.get("column"))
            if not column:
                raise ValueError("explode requires column")
            _validate_columns_exist(_column_names(result), [column], f"relational[{index}].column")
            result = result.explode(column)
        elif op_name == "unnest":
            column = _text(op.get("column"))
            prefix = _text(op.get("prefix"))
            if not column:
                raise ValueError("unnest requires column")
            _validate_columns_exist(_column_names(result), [column], f"relational[{index}].column")
            schema = result.collect_schema()
            dtype = schema[column]
            if isinstance(dtype, pl.Struct):
                names = [f"{prefix}{field.name}" for field in dtype.fields]
                outer_columns = set(schema.names()) - {column}
                collisions = sorted(outer_columns.intersection(names))
                if collisions:
                    raise ValueError(
                        f"relational[{index}].unnest has conflicting columns: "
                        f"{', '.join(collisions)}; use a nonconflicting prefix"
                    )
                if prefix:
                    result = result.with_columns(pl.col(column).struct.rename_fields(names))
            result = result.unnest(column)
    return result
