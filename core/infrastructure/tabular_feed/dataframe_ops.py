"""DataFrame-level helpers shared by tabular operations."""

from __future__ import annotations

from typing import Any

from core.infrastructure.tabular_feed.common import _text


def _column_names(frame: Any) -> list[str]:
    collect_schema = getattr(frame, "collect_schema", None)
    if callable(collect_schema):
        return list(collect_schema().names())
    return list(frame.columns)


def _apply_column_map(
    rows: list[dict[str, Any]], column_map: dict[str, Any]
) -> list[dict[str, Any]]:
    mapping = {
        _text(src): _text(dst) for src, dst in column_map.items() if _text(src) and _text(dst)
    }
    if not mapping:
        return [dict(row) for row in rows]
    return [{mapping.get(key, key): value for key, value in row.items()} for row in rows]


def _apply_column_map_df(df: Any, column_map: dict[str, Any]) -> Any:
    mapping = {
        _text(src): _text(dst) for src, dst in column_map.items() if _text(src) and _text(dst)
    }
    if not mapping:
        return df
    _validate_columns_exist(_column_names(df), mapping.keys(), "column_map")
    return df.rename(mapping)


def _validate_columns_exist(existing: list[str], required: Any, path: str) -> None:
    existing_set = set(existing)
    for column in required:
        column_name = _text(column)
        if column_name and column_name not in existing_set:
            raise ValueError(f"{path} references missing column: {column_name}")
