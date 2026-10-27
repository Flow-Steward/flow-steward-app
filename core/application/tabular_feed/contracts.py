"""First-party ``tabular_feed`` operation and action contracts."""

from __future__ import annotations

from typing import Any

from core.application.workflows.workflow_step_config_contracts import TABULAR_FEED_CONFIG_KEYS

STEP_KIND_TABULAR_FEED = "tabular_feed"

CHECKSUM_OPERATION = "checksum"

OPERATION_ACTIONS: dict[str, str] = {
    CHECKSUM_OPERATION: CHECKSUM_OPERATION,
    "inspect": "inspect",
    "read": "read",
    "transform": "transform",
    "concat": "concat",
    "write": "write",
    "validate": "validate",
    "diff": "diff",
    "commit_snapshot": "commit_snapshot",
    "read_rows": "read_rows",
    "merge_rows": "merge_rows",
    "create_dataset": "create_dataset",
}

TOP_LEVEL_FIELDS: set[str] = set(TABULAR_FEED_CONFIG_KEYS)

INPUT_FIELDS_BY_OPERATION: dict[str, set[str]] = {
    CHECKSUM_OPERATION: {"profile_key", "raw_sha256"},
    "inspect": {"artifact_handle"},
    "read": {"artifact_handle"},
    "transform": {"dataset_handle"},
    "concat": {"dataset_handles"},
    "write": {"dataset_handle"},
    "validate": {"dataset_handle"},
    "diff": {"dataset_handle", "baseline_dataset_handle", "expected_snapshot_ref"},
    "commit_snapshot": {"profile_key", "snapshot_artifact_id"},
    "read_rows": {"dataset_handle"},
    "merge_rows": {"dataset_handle"},
    "create_dataset": set(),
}

SUPPORTED_OUTPUT_FORMATS = {"csv", "tsv", "json", "jsonl", "ndjson", "parquet", "xlsx"}

TABULAR_ACTION_PARAMS: dict[str, set[str]] = {
    "text.trim": set(),
    "text.html_to_text": set(),
    "text.upper": set(),
    "text.lower": set(),
    "text.title_case": set(),
    "text.replace": {"find", "replace_with"},
    "text.append": {"append_value"},
    "text.length": set(),
    "text.pad": {"width", "fill_char", "side"},
    "number.coerce": set(),
    "number.add": {"operand"},
    "number.subtract": {"operand"},
    "number.multiply": {"factor"},
    "number.divide": {"divisor"},
    "number.round": {"decimals", "mode"},
    "number.floor": set(),
    "number.ceil": set(),
    "money.coerce": set(),
    "money.add": {"operand"},
    "money.subtract": {"operand"},
    "money.multiply": {"factor"},
    "money.round": {"decimals", "mode"},
    "boolean.coerce": set(),
    "map.values": {"mapping", "keep_unmapped"},
}

TABULAR_ACTION_REQUIRED_PARAMS: dict[str, set[str]] = {
    "text.replace": {"find", "replace_with"},
    "text.append": {"append_value"},
    "text.pad": {"width"},
    "number.add": {"operand"},
    "number.subtract": {"operand"},
    "number.multiply": {"factor"},
    "number.divide": {"divisor"},
    "money.add": {"operand"},
    "money.subtract": {"operand"},
    "money.multiply": {"factor"},
    "map.values": {"mapping"},
}

TABULAR_ACTION_FIELDS = {"action", "target", "params", "when"}
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
CONDITION_OPERATORS = CONDITION_KEYS - {"all", "any", "column"}

RELATIONAL_OP_FIELDS: dict[str, set[str]] = {
    "filter": {"op", "when", "condition"},
    "dedupe_by": {"op", "keys", "keep"},
    "sort": {"op", "by", "columns", "descending", "nulls_last"},
    "group_by": {"op", "keys", "aggregations"},
    "explode": {"op", "column"},
    "unnest": {"op", "column", "prefix"},
}
RELATIONAL_AGG_FIELDS = {"column", "agg", "function", "as", "alias"}
RELATIONAL_AGG_FUNCS = {"count", "sum", "min", "max", "first", "last"}

LEGACY_RULE_FIELDS = {
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

PAYLOAD_COPY_FIELDS: tuple[str, ...] = tuple(
    field for field in TOP_LEVEL_FIELDS if field not in {"operation", "input", "result_target"}
)


def operation_action(operation: Any) -> str:
    """Return the canonical core action id for a serialized operation name."""
    normalized = str(operation or "").strip().lower()
    return OPERATION_ACTIONS.get(normalized, normalized)


def snapshot_mode(value: Any, *, has_reference: bool) -> str:
    """Resolve the typed purpose, including an existing hidden explicit reference."""
    if value is None:
        return "publish" if has_reference else "compare"
    if not isinstance(value, str) or value not in {"compare", "publish"}:
        raise ValueError("invalid_snapshot_mode")
    if value == "compare" and has_reference:
        raise ValueError("invalid_snapshot_mode")
    return value
