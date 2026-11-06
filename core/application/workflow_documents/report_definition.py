"""Authoring-time validation of a ReportDefinition v1 (schema v1 section 3).

The definition is checked against the Template step's declared inputs: every
binding must name one of them, parse with the canonical state path grammar and
be possible for the input's declared type. Whether a path really yields a
scalar or an array is a run-time question for the compiler.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from core.application.orchestration.state_paths import StatePathError, parse_state_path
from core.application.workflow_documents.report_formats import value_problem
from core.application.workflow_documents.report_issues import Issue, PathPart, issue
from core.application.workflow_documents.report_rules import (
    format_issues,
    locale_issues,
    size_issue,
    version_issue,
)
from core.application.workflow_documents.report_structure import structure_issues
from core.application.workflow_documents.report_tree import (
    MAX_TEXT_CHARS,
    dataset_reference_issues,
    iter_components,
    iter_value_slots,
    tree_issues,
)

DEFINITION_MAX_BYTES = 64 * 1024

#: Input types a binding may read, with and without a relative ``path``
#: (schema v1 section 3.2).
_VALUE_WHOLE = frozenset({"text", "number", "boolean", "json"})
_VALUE_PATH = frozenset({"object", "array", "json"})
_DATASET_WHOLE = frozenset({"array", "json"})
_DATASET_PATH = frozenset({"object", "array", "json"})


def validate_report_definition(
    definition: Any, declared_inputs: Sequence[Mapping[str, Any]]
) -> list[Issue]:
    """Every problem of ``definition``; paths are relative to the definition."""
    if not isinstance(definition, Mapping):
        return [issue("type_invalid", "The report definition must be an object")]
    version = version_issue(definition)
    if version is not None:
        return [version]
    oversize = size_issue(
        definition, DEFINITION_MAX_BYTES, "definition_too_large", "report definition"
    )
    if oversize is not None:
        return [oversize]
    structural = structure_issues(definition, "definition")
    if structural:
        return structural
    input_types = {
        str(row.get("name")): str(row.get("type") or "text")
        for row in declared_inputs
        if isinstance(row, Mapping)
    }
    datasets = list(definition.get("datasets") or [])
    return [
        *tree_issues(definition),
        *locale_issues(definition),
        *format_issues(definition),
        *_binding_issues(definition, input_types),
        *dataset_reference_issues(
            definition,
            [str(row.get("id")) for row in datasets],
            [("datasets", index, "id") for index in range(len(datasets))],
        ),
        *_literal_text_issues(definition),
    ]


def _binding_issues(definition: Mapping[str, Any], input_types: Mapping[str, str]) -> list[Issue]:
    problems: list[Issue] = []
    for slot in iter_value_slots(definition):
        binding = slot.holder.get(slot.key) or {}
        problems.extend(_one_binding(binding, slot.path, input_types, dataset=False))
        if "default" in binding:
            problem = value_problem(binding["default"], slot.format)
            if problem is not None:
                problems.append(issue(problem[0], problem[1], (*slot.path, "default")))
    for index, dataset in enumerate(definition.get("datasets") or []):
        path: tuple[PathPart, ...] = ("datasets", index, "source")
        problems.extend(_one_binding(dataset.get("source") or {}, path, input_types, dataset=True))
    return problems


def _one_binding(
    binding: Mapping[str, Any],
    path: tuple[PathPart, ...],
    input_types: Mapping[str, str],
    *,
    dataset: bool,
) -> list[Issue]:
    name = str(binding.get("input"))
    if name not in input_types:
        return [
            issue(
                "binding_input_unknown",
                f"'{name}' is not a declared input of this Template step",
                (*path, "input"),
            )
        ]
    relative = binding.get("path")
    if relative is not None:
        problem = path_problem(str(relative))
        if problem:
            return [issue("binding_path_invalid", problem, (*path, "path"))]
    allowed = _allowed_types(dataset=dataset, has_path=relative is not None)
    input_type = input_types[name]
    if input_type in allowed:
        return []
    what = "an array of rows" if dataset else "one value"
    how = "with a path" if relative is not None else "as a whole"
    return [
        issue(
            "binding_type_impossible",
            f"Input '{name}' is declared {input_type}; it cannot give {what} {how}",
            path,
        )
    ]


def _allowed_types(*, dataset: bool, has_path: bool) -> frozenset[str]:
    if dataset:
        return _DATASET_PATH if has_path else _DATASET_WHOLE
    return _VALUE_PATH if has_path else _VALUE_WHOLE


def path_problem(relative: str) -> str:
    """Why ``relative`` is not a usable binding path, or an empty string."""
    text = relative.strip()
    if text != relative or not text:
        return "The path must not be empty or padded with spaces"
    if text == "state" or text.startswith(("state.", "state[")):
        return "Bindings read inside a declared input; do not start the path with state"
    try:
        parse_state_path(text)
    except StatePathError:
        return "The path does not parse: use a.b, a[0].b or a.0.b without wildcards"
    return ""


def _literal_text_issues(definition: Mapping[str, Any]) -> list[Issue]:
    problems: list[Issue] = []
    for component, path, _depth in iter_components(definition.get("components") or []):
        if component.get("type") != "text":
            continue
        total = sum(
            len(segment.get("text", ""))
            for segment in component.get("segments") or []
            if isinstance(segment, Mapping)
        )
        if total > MAX_TEXT_CHARS:
            problems.append(
                issue(
                    "text_too_long",
                    f"A text component holds at most {MAX_TEXT_CHARS} characters",
                    path,
                )
            )
    return problems


__all__ = ["DEFINITION_MAX_BYTES", "path_problem", "validate_report_definition"]
