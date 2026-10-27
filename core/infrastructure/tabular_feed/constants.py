"""Shared constants for the core tabular feed infrastructure."""

from __future__ import annotations

from typing import Any

RUNTIME_ID = "tabular_feed"
TEXT_CSV_VALUE = "text/csv"
PARQUET_CONTENT_TYPE = "application/vnd.apache.parquet"
DEFAULT_MAX_FEED_BYTES = 1024 * 1024 * 1024
DEFAULT_MAX_ROWS = 2_000_000
DEFAULT_MAX_MATERIALIZE_BYTES = 128 * 1024 * 1024
DEFAULT_READ_ROWS_SCAN_MAX_ROWS = 100_000
DEFAULT_READ_ROWS_SCAN_MAX_BYTES = 64 * 1024 * 1024
DEFAULT_MAX_ROW_ERROR_RATIO = 0.2
READ_ROWS_LIMIT_MAX = 1000
READ_ROWS_CELL_BUDGET = 50_000
PREVIEW_LIMIT = 10
STORAGE_CHUNK_SIZE = 1024 * 1024

CONDITION_KEYS = {
    "all",
    "any",
    "column",
    "equals",
    "not_equals",
    "in",
    "not_in",
    "gt",
    "gte",
    "lt",
    "lte",
    "is_empty",
    "is_not_empty",
}

RULE_KEYS = {
    "target",
    "when",
    "set",
    "multiply",
    "multiply_by_column",
    "round",
    "money_multiply",
    "money_round",
    "discount_percent",
    "floor_multiply",
    "rename",
    "drop",
    "default",
    "coerce",
    "map_values",
    "add",
    "subtract",
    "divide",
    "ceil",
    "money_add",
    "money_subtract",
    "trim",
    "html_to_text",
    "upper",
    "lower",
    "title_case",
    "length",
    "text_replace",
    "text_append",
    "text_pad",
}

RELATIONAL_OPS = {"filter", "dedupe_by", "sort", "group_by", "explode", "unnest"}

CATALOG_VERSION = 1
TABULAR_ACTION_CATALOG: dict[str, dict[str, Any]] = {
    "text.trim": {"params": set()},
    "text.html_to_text": {"params": set()},
    "text.upper": {"params": set()},
    "text.lower": {"params": set()},
    "text.title_case": {"params": set()},
    "text.replace": {"params": {"find", "replace_with"}, "required": {"find", "replace_with"}},
    "text.append": {"params": {"append_value"}, "required": {"append_value"}},
    "text.length": {"params": set()},
    "text.pad": {"params": {"width", "fill_char", "side"}, "required": {"width"}},
    "number.coerce": {"params": set()},
    "number.add": {"params": {"operand"}, "required": {"operand"}},
    "number.subtract": {"params": {"operand"}, "required": {"operand"}},
    "number.multiply": {"params": {"factor"}, "required": {"factor"}},
    "number.divide": {"params": {"divisor"}, "required": {"divisor"}},
    "number.round": {"params": {"decimals", "mode"}},
    "number.floor": {"params": set()},
    "number.ceil": {"params": set()},
    "money.coerce": {"params": set()},
    "money.add": {"params": {"operand"}, "required": {"operand"}},
    "money.subtract": {"params": {"operand"}, "required": {"operand"}},
    "money.multiply": {"params": {"factor"}, "required": {"factor"}},
    "money.round": {"params": {"decimals", "mode"}},
    "boolean.coerce": {"params": set()},
    "map.values": {"params": {"mapping", "keep_unmapped"}, "required": {"mapping"}},
}
