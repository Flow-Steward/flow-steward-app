"""Walks over the component tree shared by ReportDefinition and ReportSnapshot.

Both documents have the same tree; only the leaves differ (a binding in a
definition, a resolved scalar in a snapshot). These helpers assume the document
already passed structural validation.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from core.application.workflow_documents.report_issues import Issue, PathPart, issue

MAX_DEPTH = 4
MAX_COMPONENTS = 100
MAX_DATASET_FIELDS = 30
MAX_TEXT_CHARS = 10_000

Path = tuple[PathPart, ...]


@dataclass(frozen=True)
class ValueSlot:
    """A place that holds one bound scalar: ``holder[key]`` read with ``format``."""

    holder: Mapping[str, Any]
    key: str
    format: Mapping[str, Any]
    path: Path


@dataclass(frozen=True)
class FieldUse:
    """One component reading ``field`` of a dataset with ``format``."""

    field: str
    format: Mapping[str, Any]
    path: Path


def iter_components(
    components: Sequence[Any], base: Path = ("components",), depth: int = 1
) -> Iterator[tuple[Mapping[str, Any], Path, int]]:
    """Depth-first, document order: ``(component, path, depth)``."""
    for index, component in enumerate(components):
        if not isinstance(component, Mapping):
            continue
        path = (*base, index)
        yield component, path, depth
        if component.get("type") == "section":
            children = component.get("children")
            if isinstance(children, list):
                yield from iter_components(children, (*path, "children"), depth + 1)


def tree_issues(document: Mapping[str, Any]) -> list[Issue]:
    """Unique IDs, depth and component count (schema v1 sections 2.1 and 3.2)."""
    problems: list[Issue] = []
    seen: set[str] = set()
    count = 0
    for component, path, depth in iter_components(document.get("components") or []):
        count += 1
        if depth > MAX_DEPTH:
            problems.append(
                issue("depth_exceeded", f"Sections nest at most {MAX_DEPTH} deep", path)
            )
        component_id = str(component.get("id"))
        if component_id in seen:
            problems.append(
                issue("id_duplicate", f"Component id '{component_id}' is used twice", (*path, "id"))
            )
        seen.add(component_id)
        problems.extend(_member_id_issues(component, path))
    if count > MAX_COMPONENTS:
        problems.append(
            issue(
                "components_too_many",
                f"A report has at most {MAX_COMPONENTS} components at all depths",
                ("components",),
            )
        )
    problems.extend(_unique(document.get("datasets") or [], ("datasets",), "Dataset"))
    return problems


def _member_id_issues(component: Mapping[str, Any], path: Path) -> list[Issue]:
    if component.get("type") == "table":
        return _unique(component.get("columns") or [], (*path, "columns"), "Column")
    if component.get("type") == "chart":
        return _unique(component.get("series") or [], (*path, "series"), "Series")
    return []


def _unique(rows: Sequence[Any], base: Path, label: str) -> list[Issue]:
    problems: list[Issue] = []
    seen: set[str] = set()
    for index, row in enumerate(rows):
        row_id = str(row.get("id")) if isinstance(row, Mapping) else ""
        if row_id in seen:
            problems.append(
                issue("id_duplicate", f"{label} id '{row_id}' is used twice", (*base, index, "id"))
            )
        seen.add(row_id)
    return problems


def iter_value_slots(document: Mapping[str, Any]) -> Iterator[ValueSlot]:
    """Every bound scalar: summary and text segments, metric values and comparisons."""
    yield from _segment_slots(document.get("summary"), ("summary",))
    for component, path, _depth in iter_components(document.get("components") or []):
        kind = component.get("type")
        if kind == "text":
            yield from _segment_slots(component.get("segments"), (*path, "segments"))
        elif kind == "metric":
            yield ValueSlot(component, "value", component.get("format") or {}, (*path, "value"))
            comparison = component.get("comparison")
            if isinstance(comparison, Mapping):
                yield ValueSlot(
                    comparison,
                    "value",
                    comparison.get("format") or {},
                    (*path, "comparison", "value"),
                )


def _segment_slots(segments: Any, base: Path) -> Iterator[ValueSlot]:
    for index, segment in enumerate(segments if isinstance(segments, list) else []):
        if isinstance(segment, Mapping) and "value" in segment:
            yield ValueSlot(segment, "value", segment.get("format") or {}, (*base, index, "value"))


def iter_formats(document: Mapping[str, Any]) -> Iterator[tuple[Mapping[str, Any], Path]]:
    """Every format object with its path."""
    for slot in iter_value_slots(document):
        yield slot.format, (*slot.path[:-1], "format")
    for component, path, _depth in iter_components(document.get("components") or []):
        for use in _component_field_uses(component, path):
            yield use.format, (*use.path[:-1], "format")


def _component_field_uses(component: Mapping[str, Any], path: Path) -> Iterator[FieldUse]:
    kind = component.get("type")
    if kind == "table":
        for index, column in enumerate(component.get("columns") or []):
            yield FieldUse(
                str(column.get("field")),
                column.get("format") or {},
                (*path, "columns", index, "field"),
            )
    elif kind == "chart":
        category = component.get("category") or {}
        yield FieldUse(
            str(category.get("field")), category.get("format") or {}, (*path, "category", "field")
        )
        for index, series in enumerate(component.get("series") or []):
            yield FieldUse(
                str(series.get("field")),
                series.get("format") or {},
                (*path, "series", index, "field"),
            )


def dataset_references(document: Mapping[str, Any]) -> Iterator[tuple[str, Path]]:
    """``(dataset id, path of the reference)`` for every table and chart."""
    for component, path, _depth in iter_components(document.get("components") or []):
        if component.get("type") in {"table", "chart"}:
            yield str(component.get("dataset")), (*path, "dataset")


def dataset_field_uses(document: Mapping[str, Any]) -> dict[str, list[FieldUse]]:
    """Field reads per dataset id, in first-reference order (schema v1 section 4)."""
    uses: dict[str, list[FieldUse]] = {}
    for component, path, _depth in iter_components(document.get("components") or []):
        if component.get("type") in {"table", "chart"}:
            bucket = uses.setdefault(str(component.get("dataset")), [])
            bucket.extend(_component_field_uses(component, path))
    return uses


def distinct_fields(uses: Sequence[FieldUse]) -> list[str]:
    return list(dict.fromkeys(use.field for use in uses))


def dataset_reference_issues(
    document: Mapping[str, Any], declared: Sequence[str], dataset_paths: Sequence[Path]
) -> list[Issue]:
    """Unknown and unused datasets, and too many fields read from one dataset."""
    problems: list[Issue] = []
    referenced: set[str] = set()
    for dataset_id, path in dataset_references(document):
        referenced.add(dataset_id)
        if dataset_id not in declared:
            problems.append(
                issue("dataset_unknown", f"No dataset '{dataset_id}' is declared", path)
            )
    uses = dataset_field_uses(document)
    for dataset_id, path in zip(declared, dataset_paths, strict=True):
        if dataset_id not in referenced:
            problems.append(
                issue(
                    "dataset_unused", f"Dataset '{dataset_id}' is not used by any component", path
                )
            )
        if len(distinct_fields(uses.get(dataset_id, []))) > MAX_DATASET_FIELDS:
            problems.append(
                issue(
                    "dataset_fields_too_many",
                    f"Dataset '{dataset_id}' is read through more than {MAX_DATASET_FIELDS} fields",
                    path,
                )
            )
    return problems


__all__ = [
    "MAX_COMPONENTS",
    "MAX_DATASET_FIELDS",
    "MAX_DEPTH",
    "MAX_TEXT_CHARS",
    "FieldUse",
    "ValueSlot",
    "dataset_field_uses",
    "dataset_reference_issues",
    "dataset_references",
    "distinct_fields",
    "iter_components",
    "iter_formats",
    "iter_value_slots",
    "tree_issues",
]
