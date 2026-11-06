"""Compile a ReportDefinition and resolved Template inputs into a ReportSnapshot.

A pure function (schema v1 section 4): no I/O, clock, model call or randomness,
and the same inputs always give byte-identical JSON. Nothing is truncated,
sampled or coerced; any problem raises ``ReportError`` with every issue found,
paths relative to the report.
"""

from __future__ import annotations

import copy
from collections.abc import Iterator, Mapping, Sequence
from typing import Any, cast

from core.application.orchestration.state_paths import StatePathError, resolve_state_path
from core.application.workflow_documents.report_definition import validate_report_definition
from core.application.workflow_documents.report_formats import value_problem
from core.application.workflow_documents.report_issues import Issue, PathPart, ReportError, issue
from core.application.workflow_documents.report_snapshot import capped, validate_report_snapshot
from core.application.workflow_documents.report_summary import (
    REPORT_KIND,
    SUMMARY_MAX_CHARS,
    fallback_summary,
    render_segments,
)
from core.application.workflow_documents.report_tree import (
    FieldUse,
    dataset_field_uses,
    distinct_fields,
    iter_value_slots,
)

#: Rows per dataset; checked before projection so a huge array costs nothing.
MAX_ROWS = 1_000
_MISSING = object()


def compile_report(
    definition: Mapping[str, Any],
    declared_inputs: Sequence[Mapping[str, Any]],
    context: Mapping[str, Any],
) -> dict[str, Any]:
    """The snapshot for ``definition`` given the Template ``context``.

    ``context`` holds the values of the declared inputs that were supplied; an
    optional input that was not supplied is absent, which is *missing*, while a
    present ``None`` is a known empty value.
    """
    problems = validate_report_definition(definition, declared_inputs)
    if problems:
        raise ReportError(problems)
    datasets, dataset_problems = _project_datasets(definition, context)
    components = copy.deepcopy(list(definition["components"]))
    summary = copy.deepcopy(definition.get("summary"))
    value_problems = list(_resolve_values({"components": components, "summary": summary}, context))
    problems = capped([*dataset_problems, *value_problems])
    if problems:
        raise ReportError(problems)
    snapshot: dict[str, Any] = {
        "kind": REPORT_KIND,
        "schema_version": 1,
        "title": definition["title"],
        "locale": definition["locale"],
        "time_zone": definition["time_zone"],
        "summary_text": _summary_text(definition, summary, components, datasets),
        "datasets": datasets,
        "components": components,
    }
    problems = validate_report_snapshot(snapshot)
    if problems:
        raise ReportError(problems)
    return snapshot


def _summary_text(
    definition: Mapping[str, Any],
    summary: Any,
    components: Sequence[Any],
    datasets: Sequence[Mapping[str, Any]],
) -> str:
    time_zone = str(definition["time_zone"])
    if summary is None:
        rows = {str(row["id"]): len(row["rows"]) for row in datasets}
        return fallback_summary(str(definition["title"]), components, rows, time_zone)
    text = render_segments(summary, time_zone)
    if len(text) > SUMMARY_MAX_CHARS:
        raise ReportError(
            [
                issue(
                    "summary_too_long",
                    f"The summary renders to {len(text)} characters; the limit is "
                    f"{SUMMARY_MAX_CHARS}",
                    ("summary",),
                )
            ]
        )
    return text


def _lookup(binding: Mapping[str, Any], context: Mapping[str, Any]) -> Any:
    """The bound value, or ``_MISSING`` (schema v1 section 2.3)."""
    name = str(binding.get("input"))
    if name not in context:
        return _MISSING
    root = context[name]
    if "path" not in binding:
        return root
    try:
        value, found = resolve_state_path(root, str(binding["path"]))
    except StatePathError:
        return _MISSING
    return value if found else _MISSING


def _describe(binding: Mapping[str, Any]) -> str:
    name = str(binding.get("input"))
    return f"{name}.{binding['path']}" if "path" in binding else name


def _resolve_values(document: dict[str, Any], context: Mapping[str, Any]) -> Iterator[Issue]:
    for slot in iter_value_slots(document):
        binding = slot.holder[slot.key]
        value = _lookup(binding, context)
        holder = cast("dict[str, Any]", slot.holder)
        if value is _MISSING:
            if "default" not in binding:
                yield issue(
                    "binding_missing",
                    f"{_describe(binding)} is missing and has no default",
                    slot.path,
                )
                holder[slot.key] = None
                continue
            value = binding["default"]
        if isinstance(value, (dict, list)):
            yield issue(
                "binding_not_scalar",
                f"{_describe(binding)} holds an object or array, not one value",
                slot.path,
            )
            holder[slot.key] = None
            continue
        holder[slot.key] = value
        problem = value_problem(value, slot.format)
        if problem is not None:
            yield issue(problem[0], problem[1], slot.path)


def _project_datasets(
    definition: Mapping[str, Any], context: Mapping[str, Any]
) -> tuple[list[dict[str, Any]], list[Issue]]:
    uses = dataset_field_uses(definition)
    datasets: list[dict[str, Any]] = []
    problems: list[Issue] = []
    for index, dataset in enumerate(definition.get("datasets") or []):
        dataset_id = str(dataset["id"])
        dataset_uses = uses.get(dataset_id, [])
        rows, found = _dataset_rows(dataset["source"], context, index)
        problems.extend(found)
        projected = list(_project_rows(rows, dataset_uses, index, problems)) if rows else []
        datasets.append(
            {"id": dataset_id, "fields": distinct_fields(dataset_uses), "rows": projected}
        )
    return datasets, problems


def _dataset_rows(
    binding: Mapping[str, Any], context: Mapping[str, Any], index: int
) -> tuple[list[Any], list[Issue]]:
    path: tuple[PathPart, ...] = ("datasets", index, "source")
    value = _lookup(binding, context)
    if value is _MISSING:
        if "default" in binding:
            return [], []
        return [], [
            issue("binding_missing", f"{_describe(binding)} is missing and has no default", path)
        ]
    if not isinstance(value, list):
        return [], [
            issue("dataset_not_array", f"{_describe(binding)} is not an array of rows", path)
        ]
    if len(value) > MAX_ROWS:
        return [], [
            issue(
                "rows_too_many",
                f"{_describe(binding)} has {len(value)} rows; the limit is {MAX_ROWS}",
                ("datasets", index, "rows"),
            )
        ]
    return value, []


def _project_rows(
    rows: Sequence[Any], uses: Sequence[FieldUse], index: int, problems: list[Issue]
) -> Iterator[list[Any]]:
    fields = distinct_fields(uses)
    for row_index, row in enumerate(rows):
        path: tuple[PathPart, ...] = ("datasets", index, "rows", row_index)
        if not isinstance(row, Mapping):
            problems.append(issue("dataset_row_not_object", "Each row must be an object", path))
            continue
        problems.extend(_row_issues(row, uses, path))
        yield [row.get(name) for name in fields]


def _row_issues(
    row: Mapping[str, Any], uses: Sequence[FieldUse], path: tuple[PathPart, ...]
) -> Iterator[Issue]:
    reported: set[str] = set()
    for use in uses:
        if use.field in reported:
            continue
        cell_path = (*path, use.field)
        if use.field not in row:
            reported.add(use.field)
            yield issue("dataset_field_missing", f"The row has no field '{use.field}'", cell_path)
            continue
        cell = row[use.field]
        if isinstance(cell, (dict, list)):
            reported.add(use.field)
            yield issue(
                "dataset_cell_not_scalar",
                f"Field '{use.field}' holds an object or array, not one value",
                cell_path,
            )
            continue
        problem = value_problem(cell, use.format)
        if problem is not None:
            reported.add(use.field)
            yield issue(problem[0], f"Field '{use.field}': {problem[1]}", cell_path)


__all__ = ["MAX_ROWS", "compile_report"]
