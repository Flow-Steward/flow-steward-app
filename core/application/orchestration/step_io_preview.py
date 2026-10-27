"""What a step received and produced, shaped for reading in Job Details.

A run is debuggable only when each step shows the parameters it actually ran
with (after templating: the URL it called, the filters it sent, the branch it
took) and what it produced. The values are copied into the step's message, so
they are kept bounded: secrets are masked, long strings and lists are cut with
a note of their full size, and deep nesting is folded. The structure stays
readable instead of collapsing to "value too large".
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from core.infrastructure.security.prompt_safety import redact_sensitive_structure

#: Bytes one preview may take in the step's message. Every step message is
#: copied into each checkpoint of the run, so a preview stays small; the full
#: value remains in the run's state and files.
MAX_PREVIEW_BYTES = 1_500
#: (items kept of one list, characters kept of one string), tried in order
#: until the preview fits its budget.
_SHAPES = ((10, 500), (3, 120))
#: Keys kept of one mapping.
MAX_MAPPING_KEYS = 40
#: Nesting kept before a value is folded.
MAX_DEPTH = 6
#: Kept for callers that size their own previews.
MAX_LIST_ITEMS, MAX_STRING_CHARS = _SHAPES[0]

_STATE_ROOTS = ("state.", "inputs.", "outputs.", "workflow_data.", "variables.")


def _shape(value: Any, depth: int, items: int, chars: int) -> Any:
    if depth > MAX_DEPTH:
        return f"… ({type(value).__name__} nested deeper than {MAX_DEPTH} levels)"
    if isinstance(value, str):
        if len(value) <= chars:
            return value
        return f"{value[:chars]}… ({len(value)} characters in total)"
    if isinstance(value, bytes):
        return f"({len(value)} bytes of binary data)"
    if isinstance(value, Mapping):
        entries = list(value.items())
        shaped = {
            str(key): _shape(item, depth + 1, items, chars)
            for key, item in entries[:MAX_MAPPING_KEYS]
        }
        if len(entries) > MAX_MAPPING_KEYS:
            shaped["…"] = f"{len(entries) - MAX_MAPPING_KEYS} more keys"
        return shaped
    if isinstance(value, (list, tuple, set, frozenset)):
        values = list(value)
        shaped_list = [_shape(item, depth + 1, items, chars) for item in values[:items]]
        if len(values) > items:
            shaped_list.append(f"… {len(values) - items} more items ({len(values)} in total)")
        return shaped_list
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(value)


def _size(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=False, default=str, separators=(",", ":")))


def step_io_preview(value: Any) -> Any:
    """A redacted copy of a step input or output, within its byte budget."""
    try:
        redacted = redact_sensitive_structure(value)
    except Exception:
        redacted = value
    shaped: Any = None
    for items, chars in _SHAPES:
        shaped = _shape(redacted, 0, items, chars)
        if _size(shaped) <= MAX_PREVIEW_BYTES:
            return shaped
    text = json.dumps(shaped, ensure_ascii=False, default=str)
    return f"{text[:MAX_PREVIEW_BYTES]}… ({_size(redacted)} bytes in total)"


#: Rows a table preview shows.
TABLE_PREVIEW_ROWS = 10


def table_preview(
    rows: Any = None,
    *,
    total: int | None = None,
    columns: Any = None,
) -> dict[str, Any]:
    """The columns, the first rows and how many rows there are in all.

    A table can hold a million rows; the run view needs its shape and a few
    rows to see whether the step read or wrote the right thing.
    """
    sample = list(rows)[:TABLE_PREVIEW_ROWS] if isinstance(rows, (list, tuple)) else []
    if total is None and isinstance(rows, (list, tuple)):
        total = len(rows)
    names: list[str] = []
    if isinstance(columns, Mapping):
        columns = columns.get("columns")
    for column in columns if isinstance(columns, (list, tuple)) else []:
        name = column.get("name") if isinstance(column, Mapping) else column
        if name not in (None, ""):
            names.append(str(name))
    if not names:
        for row in sample:
            if isinstance(row, Mapping):
                names.extend(str(key) for key in row if str(key) not in names)
    preview: dict[str, Any] = {"columns": names[:MAX_MAPPING_KEYS]}
    if total is not None:
        preview["rows"] = total
        preview["shown"] = f"{len(sample)} of {total}"
    preview["preview"] = step_io_preview(sample)
    return preview


def step_io_payload(value: Any) -> dict[str, Any]:
    """A preview the run view shows as fields: a mapping, or ``{"value": ...}``."""
    preview = step_io_preview(value)
    if isinstance(preview, dict):
        return preview
    return {} if preview is None else {"value": preview}


def referenced_state_values(spec: Any, state: Mapping[str, Any]) -> dict[str, Any]:
    """The current value of every state path a step's configuration names.

    An ``if`` that compares ``state.outputs.weather.city`` to "Vilnius" is only
    debuggable when the run view shows what that path held at the time.
    """
    from core.application.orchestration.step_templating import _resolve_path

    paths: list[str] = []

    def collect(node: Any) -> None:
        if isinstance(node, str):
            text = node.strip()
            if text.startswith(_STATE_ROOTS) and " " not in text and text not in paths:
                paths.append(text)
        elif isinstance(node, Mapping):
            for item in node.values():
                collect(item)
        elif isinstance(node, (list, tuple)):
            for item in node:
                collect(item)

    collect(spec)
    values: dict[str, Any] = {}
    for path in paths[:20]:
        try:
            values[path] = _resolve_path(dict(state), path)
        except Exception:
            values[path] = None
    return values


__all__ = ["referenced_state_values", "step_io_payload", "step_io_preview", "table_preview"]
