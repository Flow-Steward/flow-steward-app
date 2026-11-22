"""The operator jobs list of a workflow: column values and filters (ADR 0075).

Column values come from what each job stored: the accepted inputs (the bounded,
display-safe projection is enough to show them), the final outputs of a
successful run and the author recorded at launch. A value the job does not have
is left out, not invented: no default, no type coercion, no current-contract guess.

Filters are accepted only by the id the admin configured, their values are
checked against the filter type, and they compare full values: the inputs kept in
``meta.filter_values`` at run acceptance and the outputs in
``meta.workflow_outputs``.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import date
from typing import Any

from .workflow_operator_form_model import (
    JOB_STATUS_OPTIONS,
    OperatorContract,
    OperatorListColumn,
    OperatorSettings,
    field_kind,
    resolve_schema_path,
)

#: A run's outputs are its result only once it succeeded; a failed or running job
#: shows its status and safe error instead of a partial result.
SUCCESS_STATUSES = frozenset({"completed", "succeeded", "success"})
MAX_FILTERS_PARAM_BYTES = 8 * 1024
MAX_SEARCH_CHARS = 500
MAX_CHOICES = 50
_SECRET_KEYS = frozenset({"secret_ref", "secret_id", "$secret", "$secret_ref", "catalog_ref"})
_FILE_KEYS = ("filename", "content_type", "mime_type", "size_bytes", "artifact_id")
_JOB_VALUES = {
    "job_id": "job_id",
    "status": "status",
    "created_at": "queued_at",
    "started_at": "started_at",
    "finished_at": "finished_at",
    "duration_ms": "duration_ms",
}
_MISSING = object()


# --- Column values -------------------------------------------------------------


def _public(value: Any) -> Any:
    """A stored value as the table may show it: file handles summarized, secrets masked."""
    if isinstance(value, dict):
        if _SECRET_KEYS.intersection(value) or value.get("__type__") == "secret_reference":
            return "[secret]"
        if (
            "artifact_id" in value
            or "storage_ref" in value
            or "object_key" in value
            or ("filename" in value and "sha256" in value)
        ):
            return {key: value[key] for key in _FILE_KEYS if key in value}
        return {key: _public(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_public(item) for item in value]
    return value


def _at_path(data: Any, path: str) -> Any:
    """The value at ``a.b`` or ``items[].sku``; a list step collects every item's value."""
    segments = path.split(".")
    node: Any = data
    for index, segment in enumerate(segments):
        name, into_items = (segment[:-2], True) if segment.endswith("[]") else (segment, False)
        if not isinstance(node, dict) or name not in node:
            return _MISSING
        node = node[name]
        if into_items:
            if not isinstance(node, list):
                return _MISSING
            rest = ".".join(segments[index + 1 :])
            if not rest:
                return node
            values = [_at_path(item, rest) for item in node]
            return [value for value in values if value is not _MISSING]
    return node


def _needs_artifact(value: Any) -> bool:
    return isinstance(value, dict) and "artifact_id" not in value and "filename" in value


def _with_input_artifact(value: Any, files: Any, input_name: str) -> Any:
    """Give each input file its own stored artifact, so it can be downloaded.

    The stored files of the run record the input each was sent for
    (``field_name``) in upload order, so the n-th file of an input is its n-th
    stored copy: two pages with the same filename still get their own
    artifacts. Older runs without ``field_name`` are matched by filename, each
    stored copy used once, in order.
    """
    stored = [
        row
        for row in (files if isinstance(files, list) else [])
        if isinstance(row, dict) and row.get("artifact_id")
    ]
    own = [row for row in stored if row.get("field_name") == input_name]
    pool = own or stored
    used: set[int] = set()

    def attach(item: Any, position: int) -> Any:
        if not _needs_artifact(item):
            return item
        chosen: int | None = None
        if (
            own
            and position < len(own)
            and position not in used
            and own[position].get("filename") == item.get("filename")
        ):
            chosen = position
        else:
            chosen = next(
                (
                    index
                    for index, row in enumerate(pool)
                    if index not in used and row.get("filename") == item.get("filename")
                ),
                None,
            )
        if chosen is None:
            return item
        used.add(chosen)
        return {**item, "artifact_id": pool[chosen]["artifact_id"]}

    if isinstance(value, list):
        return [attach(item, position) for position, item in enumerate(value)]
    return attach(value, 0)


def operator_row_values(
    columns: list[OperatorListColumn], row: dict[str, Any], payload: dict[str, Any] | None
) -> dict[str, Any]:
    """Column id -> value for one job; absent values are left out."""
    stored = payload or {}
    succeeded = str(row.get("status") or "").strip().lower() in SUCCESS_STATUSES
    values: dict[str, Any] = {}
    for column in columns:
        if column.source == "job":
            if column.path == "submitted_by":
                value = stored.get("submitted_by") or _MISSING
            else:
                value = row.get(_JOB_VALUES.get(column.path, ""), _MISSING)
        elif column.source == "input":
            value = _with_input_artifact(
                _at_path(stored.get("inputs") or {}, column.path),
                stored.get("input_files"),
                column.path.split(".", 1)[0].removesuffix("[]"),
            )
        elif succeeded:
            outputs = stored.get("outputs") or {}
            # A returned file is kept with the run's artifacts, not its variables.
            value = _at_path(outputs, column.path)
            if value is _MISSING and "." not in column.path:
                value = (stored.get("output_files") or {}).get(column.path, _MISSING)
        else:
            value = _MISSING
        if value is not _MISSING and value is not None:
            values[column.id] = _public(value)
    return values


def _timestamp(value: Any) -> str | None:
    """A stored time as ISO 8601 text, or None."""
    if value is None or value == "":
        return None
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


def operator_job_values(row: dict[str, Any], payload: dict[str, Any] | None) -> dict[str, Any]:
    """Everything one job submitted and, once it succeeded, returned, as the list shows values.

    Input files carry their own stored artifact; a returned file comes from the
    run's artifacts when it is not among the output variables. A running or
    failed run has no ``outputs``: a partial result is not its result.
    """
    stored = payload or {}
    status = str(row.get("status") or "").strip().lower()
    files = stored.get("input_files")
    raw_inputs = stored.get("inputs")
    inputs = raw_inputs if isinstance(raw_inputs, dict) else {}
    outputs: dict[str, Any] | None = None
    if status in SUCCESS_STATUSES:
        outputs = {
            str(name): _public(value)
            for name, value in (stored.get("outputs") or {}).items()
            if value is not None
        }
        for name, value in (stored.get("output_files") or {}).items():
            if value is not None:
                outputs.setdefault(str(name), _public(value))
    author = stored.get("submitted_by")
    duration = row.get("duration_ms")
    return {
        "status": status,
        "submitted_by": author if isinstance(author, dict) else None,
        # When it was sent and how long it ran, for the job page's header.
        "created_at": _timestamp(row.get("queued_at") or row.get("started_at")),
        "finished_at": _timestamp(row.get("finished_at")),
        "duration_ms": duration
        if isinstance(duration, int) and not isinstance(duration, bool)
        else None,
        "inputs": {
            str(name): _public(_with_input_artifact(value, files, str(name)))
            for name, value in inputs.items()
            if value is not None
        },
        "outputs": outputs,
    }


# --- Filters ---------------------------------------------------------------------


class OperatorFilterError(ValueError):
    """A jobs-list filter the workflow does not configure, or a value that does not fit."""

    def __init__(self, message: str, *, filter_id: str = "", code: str = "invalid_filter"):
        self.filter_id = filter_id
        self.code = code
        super().__init__(message)


@dataclass(frozen=True)
class JobValueFilter:
    """One configured filter with a checked value, ready for the jobs query.

    ``source`` is ``input`` (``meta.filter_values``), ``output``
    (``meta.workflow_outputs.outputs``) or ``job`` (the job row); ``key`` is the
    top-level name. ``value`` by type: ``search`` str; ``number_range`` and
    ``date_range`` a ``(low, high)`` pair with ``None`` for an open end;
    ``boolean`` bool; ``choice`` / ``choice_multi`` a tuple of strings.
    """

    id: str
    source: str
    key: str
    type: str
    value: Any


def _number(value: Any, filter_id: str) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise OperatorFilterError("A range bound must be a number", filter_id=filter_id)
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise OperatorFilterError("A range bound must be a number", filter_id=filter_id) from exc
    if not math.isfinite(number):
        raise OperatorFilterError("A range bound must be a finite number", filter_id=filter_id)
    return number


def _day(value: Any, filter_id: str) -> str | None:
    if value is None or value == "":
        return None
    text = str(value).strip()
    try:
        if len(text) == 10:
            return date.fromisoformat(text).isoformat()
    except ValueError:
        pass
    raise OperatorFilterError("A date bound must be YYYY-MM-DD", filter_id=filter_id)


def _bounds(raw: Any, keys: tuple[str, str], filter_id: str) -> tuple[Any, Any]:
    if isinstance(raw, dict):
        unknown = set(raw) - set(keys)
        if unknown:
            raise OperatorFilterError(
                f"A range takes {keys[0]!r} and {keys[1]!r}", filter_id=filter_id
            )
        return raw.get(keys[0]), raw.get(keys[1])
    if isinstance(raw, list) and len(raw) == 2:
        return raw[0], raw[1]
    raise OperatorFilterError(
        f"A range is an object with {keys[0]!r} and/or {keys[1]!r}", filter_id=filter_id
    )


def _choices(raw: Any, options: list[str], *, multi: bool, filter_id: str) -> tuple[str, ...]:
    values = raw if isinstance(raw, list) else [raw]
    if not values or not all(isinstance(item, str) and item for item in values):
        raise OperatorFilterError("A choice filter takes option values", filter_id=filter_id)
    if not multi and len(values) != 1:
        raise OperatorFilterError("This filter takes one option", filter_id=filter_id)
    if len(values) > MAX_CHOICES:
        raise OperatorFilterError(
            f"A choice filter takes at most {MAX_CHOICES} options", filter_id=filter_id
        )
    unknown = [item for item in values if options and item not in options]
    if unknown:
        raise OperatorFilterError(
            f"'{unknown[0]}' is not an option of this filter", filter_id=filter_id
        )
    return tuple(dict.fromkeys(values))


def _filter_value(filter_type: str, raw: Any, options: list[str], filter_id: str) -> Any:
    if filter_type == "search":
        if not isinstance(raw, str) or not raw.strip():
            raise OperatorFilterError("A search filter takes text", filter_id=filter_id)
        return " ".join(raw.split())[:MAX_SEARCH_CHARS]
    if filter_type == "number_range":
        low, high = _bounds(raw, ("min", "max"), filter_id)
        bounds = (_number(low, filter_id), _number(high, filter_id))
    elif filter_type == "date_range":
        low, high = _bounds(raw, ("from", "to"), filter_id)
        bounds = (_day(low, filter_id), _day(high, filter_id))
    elif filter_type == "boolean":
        if isinstance(raw, bool):
            return raw
        if raw in ("true", "false"):
            return raw == "true"
        raise OperatorFilterError("A yes/no filter takes true or false", filter_id=filter_id)
    else:
        return _choices(raw, options, multi=filter_type == "choice_multi", filter_id=filter_id)
    if bounds == (None, None):
        raise OperatorFilterError("A range needs at least one bound", filter_id=filter_id)
    return bounds


def _options(contract: OperatorContract, source: str, path: str) -> list[str]:
    if source == "job":
        return list(JOB_STATUS_OPTIONS) if path == "status" else []
    schema = contract.input_schema if source == "input" else contract.output_schema
    node = resolve_schema_path(schema, path) or {}
    kind = field_kind(node)
    raw = node.get("enum") if kind == "select" else (node.get("items") or {}).get("enum")
    return [str(item) for item in raw or []]


def parse_filters_param(raw: Any) -> dict[str, Any]:
    """The ``filters`` query parameter: a JSON object of filter id -> value."""
    if raw in (None, "", {}):
        return {}
    if isinstance(raw, dict):
        return raw
    text = str(raw)
    if len(text.encode("utf-8")) > MAX_FILTERS_PARAM_BYTES:
        raise OperatorFilterError("filters is too long")
    try:
        parsed = json.loads(text)
    except ValueError as exc:
        raise OperatorFilterError("filters must be a JSON object") from exc
    if not isinstance(parsed, dict):
        raise OperatorFilterError("filters must be a JSON object")
    return parsed


def operator_filters(
    requested: dict[str, Any],
    settings: OperatorSettings | None,
    contract: OperatorContract,
) -> list[JobValueFilter]:
    """The requested filters, each one configured for the workflow and checked by its type.

    A filter id the workflow does not configure is refused: the list never takes
    arbitrary JSON paths. ``null`` for a configured id means "not filtered".
    """
    configured = {row.id: row for row in (settings.list.filters if settings else [])}
    filters: list[JobValueFilter] = []
    for filter_id, raw in requested.items():
        row = configured.get(str(filter_id))
        if row is None:
            raise OperatorFilterError(
                f"'{filter_id}' is not a filter of this list",
                filter_id=str(filter_id),
                code="unknown_filter",
            )
        if raw is None:
            continue
        value = _filter_value(row.type, raw, _options(contract, row.source, row.path), row.id)
        filters.append(
            JobValueFilter(id=row.id, source=row.source, key=row.path, type=row.type, value=value)
        )
    return filters


__all__ = [
    "SUCCESS_STATUSES",
    "JobValueFilter",
    "OperatorFilterError",
    "operator_filters",
    "operator_job_values",
    "operator_row_values",
    "parse_filters_param",
]
