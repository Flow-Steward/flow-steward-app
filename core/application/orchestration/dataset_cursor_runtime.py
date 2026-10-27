"""Bounded control-plane contract for iterating immutable tabular datasets."""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping
from typing import Any

from core.application.orchestration.dataset_cursor_contracts import MAX_DATASET_PAGE_SIZE

DATASET_CURSOR_KIND = "dataset_cursor_v1"
MAX_CONTROL_PLANE_ARRAY_ITEMS = 1000
MAX_CONTROL_PLANE_ARRAY_BYTES = 256 * 1024
MAX_DATASET_DIAGNOSTIC_BYTES = 16 * 1024
MAX_DATASET_ACTIONABLE_ERRORS = 10
_JSON_STRING_WINDOW_CHARS = 4096


class DatasetCursorError(ValueError):
    """Stable runtime failure raised while constructing a dataset cursor."""

    def __init__(self, code: str) -> None:
        self.code = str(code or "dataset_cursor_invalid")
        super().__init__(self.code)


def serialized_size(value: Any, *, max_bytes: int | None = None) -> int:
    total = 0

    class _LimitReached(Exception):
        pass

    def add(size: int) -> None:
        nonlocal total
        total += size
        if max_bytes is not None and total > max_bytes:
            raise _LimitReached

    def add_string(raw: str) -> None:
        add(2)
        for start in range(0, len(raw), _JSON_STRING_WINDOW_CHARS):
            window = raw[start : start + _JSON_STRING_WINDOW_CHARS]
            encoded = json.dumps(
                window,
                ensure_ascii=False,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
            add(len(encoded) - 2)

    active: set[int] = set()

    def visit(item: Any) -> None:
        if item is None or item is True:
            add(4)
        elif item is False:
            add(5)
        elif isinstance(item, str):
            add_string(item)
        elif isinstance(item, int):
            add(len(str(item).encode("ascii")))
        elif isinstance(item, float):
            encoded = json.dumps(
                item,
                ensure_ascii=False,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("ascii")
            add(len(encoded))
        elif isinstance(item, (list, tuple)):
            object_id = id(item)
            if object_id in active:
                raise ValueError("circular reference")
            active.add(object_id)
            try:
                add(1)
                for index, nested in enumerate(item):
                    if index:
                        add(1)
                    visit(nested)
                add(1)
            finally:
                active.discard(object_id)
        elif isinstance(item, dict):
            object_id = id(item)
            if object_id in active:
                raise ValueError("circular reference")
            active.add(object_id)
            try:
                add(1)
                for index, (key, nested) in enumerate(item.items()):
                    if index:
                        add(1)
                    if isinstance(key, str):
                        add_string(key)
                    elif key is None:
                        add_string("null")
                    elif isinstance(key, bool):
                        add_string("true" if key else "false")
                    elif isinstance(key, int):
                        add_string(str(key))
                    elif isinstance(key, float):
                        add_string(json.dumps(key, allow_nan=False))
                    else:
                        raise TypeError("keys must be str, int, float, bool or None")
                    add(1)
                    visit(nested)
                add(1)
            finally:
                active.discard(object_id)
        else:
            raise TypeError(f"Object of type {type(item).__name__} is not JSON serializable")

    try:
        visit(value)
    except _LimitReached:
        return (max_bytes or 0) + 1
    except (TypeError, ValueError, OverflowError, RecursionError) as exc:
        raise DatasetCursorError("control_plane_collection_not_serializable") from exc
    return total


def _normalized_dataset_handle(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    handle = value.strip()
    if handle.startswith("dataset:"):
        handle = handle.removeprefix("dataset:").strip()
    return handle if handle.startswith("dst_") else ""


def _dataset_descriptor(value: Any) -> dict[str, Any] | None:
    if isinstance(value, str):
        handle = _normalized_dataset_handle(value)
        return {"dataset_handle": handle} if handle else None
    if not isinstance(value, Mapping):
        return None
    direct_handle = _normalized_dataset_handle(
        value.get("dataset_handle") or value.get("dataset_id")
    )
    if direct_handle:
        descriptor = dict(value)
        descriptor["dataset_handle"] = direct_handle
        if "row_count" not in descriptor:
            nested_candidates: list[Any] = []
            produced = value.get("produced_datasets")
            if isinstance(produced, list):
                nested_candidates.extend(produced)
            nested_candidates.extend(value.get(key) for key in ("connector_result", "result"))
            for nested in nested_candidates:
                candidate = _dataset_descriptor(nested)
                if (
                    candidate is not None
                    and _normalized_dataset_handle(candidate.get("dataset_handle")) == direct_handle
                    and "row_count" in candidate
                ):
                    descriptor["row_count"] = candidate.get("row_count")
                    break
        return descriptor
    for key in ("connector_result", "result"):
        nested = value.get(key)
        if isinstance(nested, Mapping):
            descriptor = _dataset_descriptor(nested)
            if descriptor is not None:
                return descriptor
    produced = value.get("produced_datasets")
    if isinstance(produced, list) and len(produced) == 1:
        descriptor = _dataset_descriptor(produced[0])
        if descriptor is not None:
            return descriptor
    return None


def is_dataset_cursor(value: Any) -> bool:
    return isinstance(value, dict) and value.get("kind") == DATASET_CURSOR_KIND


def is_dataset_result(value: Any) -> bool:
    return not is_dataset_cursor(value) and _dataset_descriptor(value) is not None


def _validate_dataset_cursor(cursor: Any) -> dict[str, Any]:
    if not is_dataset_cursor(cursor):
        raise DatasetCursorError("dataset_cursor_invalid")
    handle = _normalized_dataset_handle(cursor.get("dataset_handle"))
    row_count = cursor.get("row_count")
    page_size = cursor.get("page_size")
    emit_partial_batch = cursor.get("emit_partial_batch")
    if not handle:
        raise DatasetCursorError("dataset_cursor_invalid")
    if isinstance(row_count, bool) or not isinstance(row_count, int) or row_count < 0:
        raise DatasetCursorError("dataset_cursor_invalid")
    if (
        isinstance(page_size, bool)
        or not isinstance(page_size, int)
        or page_size <= 0
        or page_size > MAX_DATASET_PAGE_SIZE
    ):
        raise DatasetCursorError("dataset_cursor_page_size_invalid")
    if not isinstance(emit_partial_batch, bool):
        raise DatasetCursorError("dataset_cursor_invalid")
    return {
        "dataset_handle": handle,
        "row_count": row_count,
        "page_size": page_size,
        "emit_partial_batch": emit_partial_batch,
    }


def normalize_dataset_result(
    source: Any,
    *,
    state: dict[str, Any] | None = None,
) -> dict[str, Any]:
    descriptor = _dataset_descriptor(source)
    if descriptor is None:
        raise DatasetCursorError("dataset_cursor_handle_invalid")
    handle = _normalized_dataset_handle(descriptor.get("dataset_handle"))
    if not handle:
        raise DatasetCursorError("dataset_cursor_handle_invalid")

    if "row_count" in descriptor:
        row_count = descriptor.get("row_count")
    else:
        try:
            from core.infrastructure.tabular_feed.storage import plane_from_state_or_env

            metadata = plane_from_state_or_env(state or {}).get_dataset(handle)
        except Exception as exc:
            raise DatasetCursorError("dataset_cursor_handle_unavailable") from exc
        if not isinstance(metadata, dict):
            raise DatasetCursorError("dataset_cursor_handle_unavailable")
        row_count = metadata.get("row_count")

    if isinstance(row_count, bool) or not isinstance(row_count, int) or row_count < 0:
        raise DatasetCursorError("dataset_cursor_row_count_invalid")
    return {"dataset_handle": handle, "row_count": row_count}


def assert_bounded_control_plane_array(value: Any) -> list[Any]:
    if not isinstance(value, list):
        raise DatasetCursorError("control_plane_collection_not_array")
    if (
        len(value) > MAX_CONTROL_PLANE_ARRAY_ITEMS
        or serialized_size(value, max_bytes=MAX_CONTROL_PLANE_ARRAY_BYTES)
        > MAX_CONTROL_PLANE_ARRAY_BYTES
    ):
        raise DatasetCursorError("control_plane_collection_too_large")
    return value


def dataset_cursor_page_count(cursor: dict[str, Any]) -> int:
    validated = _validate_dataset_cursor(cursor)
    row_count = validated["row_count"]
    page_size = validated["page_size"]
    if validated["emit_partial_batch"]:
        return math.ceil(row_count / page_size) if row_count else 0
    return row_count // page_size


def dataset_page_digest(rows: list[dict[str, Any]]) -> str:
    payload = json.dumps(
        rows,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def dataset_page_context_from_state(state: Mapping[str, Any]) -> dict[str, Any] | None:
    """The dataset page the active step's item is: never, since ADR 0038.

    A For each walks one element of a ready collection per item and refuses a
    dataset cursor, so no step runs for a dataset page any more. The page
    context of the extension envelope is removed with the final execution scope.
    """
    del state
    return None


def _count_projection(*values: Any) -> dict[str, int]:
    from core.infrastructure.security.bounded_audit import project_count_fields

    return project_count_fields(*values)


def compact_dataset_connector_result(result: Mapping[str, Any]) -> dict[str, Any]:
    from core.infrastructure.security.bounded_audit import (
        hashed_audit_identifier,
        project_actionable_errors,
        safe_audit_code,
    )

    nested_result = result.get("result")
    nested_result = nested_result if isinstance(nested_result, Mapping) else {}
    invocation = result.get("invocation")
    invocation = invocation if isinstance(invocation, Mapping) else {}
    response = invocation.get("response")
    response = response if isinstance(response, Mapping) else {}
    response_result = response.get("result")
    response_result = response_result if isinstance(response_result, Mapping) else {}
    compact: dict[str, Any] = {
        "ok": bool(result.get("ok")),
        "runtime": safe_audit_code(result.get("runtime")) or "unresolved",
    }
    detail = safe_audit_code(result.get("detail"))
    if detail:
        compact["detail"] = detail
    counts = _count_projection(
        result,
        nested_result,
        response,
        response_result,
        result.get("state_patch"),
    )
    if counts:
        compact["counts"] = counts
    error_candidates = result.get("errors")
    if not isinstance(error_candidates, list):
        error_candidates = nested_result.get("errors")
    if not isinstance(error_candidates, list):
        error_candidates = response.get("errors")
    errors = project_actionable_errors(
        error_candidates,
        limit=MAX_DATASET_ACTIONABLE_ERRORS,
    )
    if errors:
        compact["errors"] = errors
    external_effect = result.get("external_effect")
    if isinstance(external_effect, Mapping):
        effect: dict[str, Any] = {}
        status = safe_audit_code(external_effect.get("status"))
        if status:
            effect["status"] = status
        receipt_hash = hashed_audit_identifier(external_effect.get("effect_id"))
        if receipt_hash:
            effect["receipt_hash"] = receipt_hash
        if effect:
            compact["external_effect"] = effect
    if serialized_size(compact, max_bytes=MAX_DATASET_DIAGNOSTIC_BYTES) > (
        MAX_DATASET_DIAGNOSTIC_BYTES
    ):
        return {
            "ok": bool(result.get("ok")),
            "runtime": safe_audit_code(result.get("runtime")) or "unresolved",
            "detail": "dataset_page_diagnostic_truncated",
        }
    return compact


def _contains_unbounded_dataset_output(value: Any, *, parent_key: str = "") -> bool:
    if isinstance(value, Mapping):
        for key, nested in value.items():
            normalized_key = str(key or "").strip().lower()
            if normalized_key in {"row", "rows", "items"} or normalized_key.endswith("_rows"):
                return True
            if normalized_key == "id" or normalized_key.endswith("_id"):
                return True
            if _contains_unbounded_dataset_output(nested, parent_key=normalized_key):
                return True
        return False
    if isinstance(value, list):
        return len(value) > MAX_DATASET_ACTIONABLE_ERRORS or any(
            _contains_unbounded_dataset_output(item, parent_key=parent_key) for item in value
        )
    return False


def assert_compact_dataset_page_output(value: Any) -> None:
    try:
        too_large = (
            serialized_size(value, max_bytes=MAX_DATASET_DIAGNOSTIC_BYTES)
            > MAX_DATASET_DIAGNOSTIC_BYTES
        )
    except DatasetCursorError as exc:
        raise DatasetCursorError("dataset_page_output_too_large") from exc
    if too_large or _contains_unbounded_dataset_output(value):
        raise DatasetCursorError("dataset_page_output_too_large")


def read_dataset_cursor_page(
    cursor: dict[str, Any],
    *,
    page_ordinal: int,
    state: dict[str, Any],
    step_id: str,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    from core.application.tabular_feed.context import context_from_state_and_step
    from core.application.tabular_feed.service import TabularFeedService
    from core.infrastructure.tabular_feed.storage import plane_from_state_or_env

    validated = _validate_dataset_cursor(cursor)
    page_size = validated["page_size"]
    ordinal = max(0, int(page_ordinal))
    handle = validated["dataset_handle"]
    payload = {
        "action_id": "read_rows",
        "input": {
            "dataset_handle": handle,
            "offset": ordinal * page_size,
            "limit": page_size,
        },
    }
    service = TabularFeedService(plane=plane_from_state_or_env(state))
    dispatch = service.execute(
        operation="read_rows",
        payload=payload,
        context=context_from_state_and_step(
            state=state,
            step={"step_id": step_id, "step_kind": "dataset_cursor_page"},
        ),
    )
    if not bool(dispatch.get("ok")):
        errors = dispatch.get("errors")
        first = errors[0] if isinstance(errors, list) and errors else {}
        code = str(first.get("code") or "dataset_cursor_page_read_failed")
        raise DatasetCursorError(code)
    result = dispatch.get("result")
    result = result if isinstance(result, dict) else {}
    rows = result.get("rows")
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise DatasetCursorError("dataset_cursor_page_invalid")
    if len(rows) > page_size or serialized_size(rows) > MAX_CONTROL_PLANE_ARRAY_BYTES:
        raise DatasetCursorError("dataset_cursor_page_too_large")
    return [dict(row) for row in rows], {
        "returned": len(rows),
        "total_matching": int(result.get("total_matching") or 0),
        "next_offset": result.get("next_offset"),
        "digest": dataset_page_digest(rows),
    }


def build_dataset_cursor(
    source: Any,
    *,
    page_size: int,
    emit_partial_batch: bool,
    state: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if page_size <= 0:
        raise DatasetCursorError("batch_size_invalid")
    if page_size > MAX_DATASET_PAGE_SIZE:
        raise DatasetCursorError("dataset_cursor_page_size_too_large")
    normalized = normalize_dataset_result(source, state=state)
    return {
        "kind": DATASET_CURSOR_KIND,
        "dataset_handle": normalized["dataset_handle"],
        "row_count": normalized["row_count"],
        "page_size": page_size,
        "emit_partial_batch": bool(emit_partial_batch),
    }


def execute_batch_contract(
    source: Any,
    *,
    batch_size: int,
    emit_partial_batch: bool,
    state: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Execute the one bounded batch contract used by top-level and inline steps."""
    if isinstance(batch_size, bool) or not isinstance(batch_size, int) or batch_size <= 0:
        raise DatasetCursorError("batch_size_invalid")
    if is_dataset_result(source):
        value = build_dataset_cursor(
            source,
            page_size=batch_size,
            emit_partial_batch=emit_partial_batch,
            state=state,
        )
        return {
            "value": value,
            "dataset_handle": value["dataset_handle"],
            "item_count": value["row_count"],
            "batch_count": dataset_cursor_page_count(value),
        }
    items = assert_bounded_control_plane_array(source)
    chunks = [items[index : index + batch_size] for index in range(0, len(items), batch_size)]
    if chunks and not emit_partial_batch and len(chunks[-1]) < batch_size:
        chunks.pop()
    return {
        "value": chunks,
        "item_count": len(items),
        "batch_count": len(chunks),
    }


__all__ = [
    "DATASET_CURSOR_KIND",
    "MAX_CONTROL_PLANE_ARRAY_BYTES",
    "MAX_CONTROL_PLANE_ARRAY_ITEMS",
    "MAX_DATASET_ACTIONABLE_ERRORS",
    "MAX_DATASET_DIAGNOSTIC_BYTES",
    "MAX_DATASET_PAGE_SIZE",
    "DatasetCursorError",
    "assert_bounded_control_plane_array",
    "assert_compact_dataset_page_output",
    "build_dataset_cursor",
    "compact_dataset_connector_result",
    "dataset_cursor_page_count",
    "dataset_page_context_from_state",
    "dataset_page_digest",
    "execute_batch_contract",
    "is_dataset_cursor",
    "is_dataset_result",
    "normalize_dataset_result",
    "read_dataset_cursor_page",
    "serialized_size",
]
