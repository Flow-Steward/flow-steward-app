"""Validation of a ReportSnapshot (schema v1 section 5.1).

Runs when the compiler finishes and again at the workflow boundary for the
selected ``report_output``, whatever step produced the value. Dispatch is on
(``kind``, ``schema_version``); an unknown version is an error, never a
best-effort read.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping, Sequence
from itertools import islice
from typing import Any

from core.application.workflow_documents.report_formats import value_problem
from core.application.workflow_documents.report_issues import Issue, issue
from core.application.workflow_documents.report_rules import (
    format_issues,
    locale_issues,
    size_issue,
    version_issue,
)
from core.application.workflow_documents.report_structure import structure_issues
from core.application.workflow_documents.report_summary import REPORT_KIND
from core.application.workflow_documents.report_tree import (
    MAX_TEXT_CHARS,
    FieldUse,
    dataset_field_uses,
    dataset_reference_issues,
    iter_components,
    iter_value_slots,
    tree_issues,
)

SNAPSHOT_MAX_BYTES = 1_048_576
#: A broken 1,000-row dataset could yield tens of thousands of cell errors;
#: past this many the list ends with one ``report.more_issues`` entry.
MAX_ISSUES = 100


def capped(issues: Iterable[Issue], limit: int = MAX_ISSUES) -> list[Issue]:
    """At most ``limit`` issues, then one entry that says more were found."""
    iterator = iter(issues)
    kept = list(islice(iterator, limit))
    if next(iterator, None) is not None:
        kept.append(issue("more_issues", f"More than {limit} problems were found; fix these first"))
    return kept


def validate_report_snapshot(value: Any) -> list[Issue]:
    """Every problem of ``value`` as a ReportSnapshot; empty means valid."""
    if not isinstance(value, Mapping):
        return [
            issue(
                "snapshot_not_object",
                f"A report must be a JSON object, not {_type_word(value)}",
            )
        ]
    if value.get("kind") != REPORT_KIND:
        return [issue("kind_invalid", f"A report has kind '{REPORT_KIND}'", ("kind",))]
    version = version_issue(value)
    if version is not None:
        return [version]
    oversize = size_issue(value, SNAPSHOT_MAX_BYTES, "snapshot_too_large", "report")
    if oversize is not None:
        return [oversize]
    structural = structure_issues(value, "snapshot")
    if structural:
        return capped(structural)
    return capped(_semantic_issues(value))


def _type_word(value: Any) -> str:
    return "text" if isinstance(value, str) else type(value).__name__


def _semantic_issues(snapshot: Mapping[str, Any]) -> Iterator[Issue]:
    datasets = [row for row in snapshot.get("datasets") or [] if isinstance(row, Mapping)]
    yield from tree_issues(snapshot)
    yield from locale_issues(snapshot)
    yield from format_issues(snapshot)
    yield from dataset_reference_issues(
        snapshot,
        [str(row.get("id")) for row in datasets],
        [("datasets", index, "id") for index in range(len(datasets))],
    )
    uses = dataset_field_uses(snapshot)
    for index, dataset in enumerate(datasets):
        yield from _dataset_issues(index, dataset, uses.get(str(dataset.get("id")), []))
    yield from _unknown_field_issues(datasets, uses)
    for slot in iter_value_slots(snapshot):
        problem = value_problem(slot.holder.get(slot.key), slot.format)
        if problem is not None:
            yield issue(problem[0], problem[1], slot.path)
    yield from _text_length_issues(snapshot)


def _dataset_issues(
    index: int, dataset: Mapping[str, Any], uses: Sequence[FieldUse]
) -> Iterator[Issue]:
    fields = [str(name) for name in dataset.get("fields") or []]
    read = {use.field for use in uses}
    for position, name in enumerate(fields):
        if name not in read:
            yield issue(
                "dataset_field_unused",
                f"Field '{name}' is stored but no component reads it",
                ("datasets", index, "fields", position),
            )
    formats: dict[int, list[Mapping[str, Any]]] = {}
    for use in uses:
        if use.field in fields:
            formats.setdefault(fields.index(use.field), []).append(use.format)
    for row_index, row in enumerate(dataset.get("rows") or []):
        path = ("datasets", index, "rows", row_index)
        if not isinstance(row, list) or len(row) != len(fields):
            yield issue("row_shape_invalid", f"Each row holds exactly {len(fields)} cells", path)
            continue
        for column, fmts in formats.items():
            yield from _cell_issues(row[column], fmts, (*path, column))


def _cell_issues(
    cell: Any, formats: Sequence[Mapping[str, Any]], path: tuple[Any, ...]
) -> Iterator[Issue]:
    for fmt in formats:
        problem = value_problem(cell, fmt)
        if problem is not None:
            yield issue(problem[0], problem[1], path)
            return


def _unknown_field_issues(
    datasets: Sequence[Mapping[str, Any]], uses: Mapping[str, Sequence[FieldUse]]
) -> Iterator[Issue]:
    stored = {
        str(row.get("id")): {str(name) for name in row.get("fields") or []} for row in datasets
    }
    for dataset_id, dataset_uses in uses.items():
        if dataset_id not in stored:
            continue
        for use in dataset_uses:
            if use.field not in stored[dataset_id]:
                yield issue(
                    "dataset_field_unknown",
                    f"Dataset '{dataset_id}' stores no field '{use.field}'",
                    use.path,
                )


def _text_length_issues(snapshot: Mapping[str, Any]) -> Iterator[Issue]:
    for component, path, _depth in iter_components(snapshot.get("components") or []):
        if component.get("type") != "text":
            continue
        total = 0
        for segment in component.get("segments") or []:
            if not isinstance(segment, Mapping):
                continue
            part = segment.get("text", segment.get("value"))
            if isinstance(part, str):
                total += len(part)
        if total > MAX_TEXT_CHARS:
            yield issue(
                "text_too_long",
                f"A text component holds at most {MAX_TEXT_CHARS} characters",
                path,
            )


__all__ = ["MAX_ISSUES", "SNAPSHOT_MAX_BYTES", "capped", "validate_report_snapshot"]
