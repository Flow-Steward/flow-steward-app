"""Plain-text summaries and the bounded, model-safe preview of a report.

``summary_text`` is what a client without a report viewer reads (schema v1
sections 4.1 and 4.2). ``report_preview`` is the only form of a report that may
appear in terminal events, step messages, previews and model-visible
projections (ADR 0059 decision 11): kind, version, title, summary text and
counts, never datasets, rows or component values.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from core.application.workflow_documents.report_formats import plain_value
from core.application.workflow_documents.report_issues import describe_type
from core.application.workflow_documents.report_tree import iter_components

REPORT_KIND = "fs.report"
SUMMARY_MAX_CHARS = 4_000
#: Room kept under the summary limit for the closing ``[K more items not shown]`` line.
_SUMMARY_LINE_BUDGET = SUMMARY_MAX_CHARS - 40
_PREVIEW_TITLE_MAX = 200
_PREVIEW_ID_MAX = 64
_PREVIEW_DATASETS_MAX = 10


def render_segments(segments: Sequence[Any], time_zone: str) -> str:
    """Concatenate resolved segments in the plain rendering of section 4.1."""
    parts: list[str] = []
    for segment in segments:
        if not isinstance(segment, Mapping):
            continue
        if "text" in segment:
            parts.append(str(segment["text"]))
        else:
            parts.append(plain_value(segment.get("value"), segment.get("format") or {}, time_zone))
    return "".join(parts)


def fallback_summary(
    title: str,
    components: Sequence[Any],
    row_counts: Mapping[str, int],
    time_zone: str,
) -> str:
    """The generated summary of section 4.2, bounded at 4,000 characters."""
    lines = _summary_lines(components, row_counts, time_zone)
    text = title
    for index, line in enumerate(lines):
        if len(text) + 1 + len(line) > _SUMMARY_LINE_BUDGET:
            return f"{text}\n[{len(lines) - index} more items not shown]"
        text = f"{text}\n{line}"
    return text


def _summary_lines(
    components: Sequence[Any], row_counts: Mapping[str, int], time_zone: str
) -> list[str]:
    lines: list[str] = []
    for component, _path, _depth in iter_components(components):
        kind = component.get("type")
        if kind == "metric":
            lines.append(_metric_line(component, time_zone))
        elif kind == "table":
            rows = row_counts.get(str(component.get("dataset")), 0)
            lines.append(f"{component.get('caption')}: {rows} rows")
        elif kind == "chart":
            rows = row_counts.get(str(component.get("dataset")), 0)
            lines.append(f"{component.get('title')}: {rows} points")
    return lines


def _metric_line(component: Mapping[str, Any], time_zone: str) -> str:
    value = plain_value(component.get("value"), component.get("format") or {}, time_zone)
    line = f"{component.get('label')}: {value}"
    comparison = component.get("comparison")
    if isinstance(comparison, Mapping):
        compared = plain_value(comparison.get("value"), comparison.get("format") or {}, time_zone)
        line += f" ({comparison.get('label')}: {compared})"
    return line


def report_preview(value: Any) -> dict[str, Any]:
    """A bounded, model-safe description of a report output, valid or not.

    The result never holds datasets, rows or component values. Strings longer
    than their schema limit are left out rather than cut, so a broken value
    cannot smuggle data through the preview.
    """
    if not isinstance(value, Mapping):
        return {"report_omitted": True, "value_type": describe_type(value)}
    components = value.get("components")
    datasets = value.get("datasets")
    return {
        "kind": REPORT_KIND if value.get("kind") == REPORT_KIND else None,
        "schema_version": _small_int(value.get("schema_version")),
        "title": _bounded_text(value.get("title"), _PREVIEW_TITLE_MAX),
        "summary_text": _bounded_text(value.get("summary_text"), SUMMARY_MAX_CHARS),
        "component_count": (
            sum(1 for _ in iter_components(components)) if isinstance(components, list) else 0
        ),
        "datasets": _dataset_counts(datasets),
    }


def _small_int(value: Any) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool) and 0 <= value < 1000:
        return value
    return None


def _bounded_text(value: Any, limit: int) -> str | None:
    if isinstance(value, str) and len(value) <= limit:
        return value
    return None


def _dataset_counts(datasets: Any) -> list[dict[str, Any]]:
    counts: list[dict[str, Any]] = []
    for dataset in datasets[:_PREVIEW_DATASETS_MAX] if isinstance(datasets, list) else []:
        if not isinstance(dataset, Mapping):
            continue
        rows = dataset.get("rows")
        counts.append(
            {
                "id": _bounded_text(dataset.get("id"), _PREVIEW_ID_MAX),
                "row_count": len(rows) if isinstance(rows, list) else 0,
            }
        )
    return counts


def replace_report_output(outputs: Any, report_output: str | None) -> Any:
    """``outputs`` with the selected report replaced by its preview.

    Anything that is not a mapping, or a selection that names no present output,
    comes back unchanged; the canonical outputs are never modified in place.
    """
    if not report_output or not isinstance(outputs, Mapping) or report_output not in outputs:
        return outputs
    return {**outputs, report_output: report_preview(outputs[report_output])}


__all__ = [
    "REPORT_KIND",
    "SUMMARY_MAX_CHARS",
    "fallback_summary",
    "render_segments",
    "replace_report_output",
    "report_preview",
]
