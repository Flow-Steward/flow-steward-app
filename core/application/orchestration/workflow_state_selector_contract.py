"""Discovery and syntax preflight for authored workflow state selectors.

This module owns no selector grammar. It discovers only fields whose runtime
contract treats them as state reads, then delegates syntax to ``state_paths``.
Write targets remain under their existing target/reserved-path validators.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from core.application.orchestration.state_paths import StatePathError, parse_state_path
from core.application.orchestration.step_templating import _split_default_filter
from core.application.workflows.workflow_step_kinds import (
    STEP_KIND_AGENT,
    STEP_KIND_BATCH,
    STEP_KIND_COLLECT,
    STEP_KIND_CONNECTOR,
    STEP_KIND_DATASET_QUERY,
    STEP_KIND_FILE_FROM_BASE64,
    STEP_KIND_HTTP_REQUEST,
    STEP_KIND_IF,
    STEP_KIND_INVOKE_WORKFLOW,
    STEP_KIND_SET_VARIABLE,
    STEP_KIND_SWITCH,
    STEP_KIND_TABULAR_FEED,
    STEP_KIND_TEMPLATE,
    STEP_KIND_TRANSFORM,
    STEP_KIND_WAIT,
    normalize_step_kind,
)

AUTHORING_ERROR_CODE = "workflow.state_selector_invalid"
IMPOSSIBLE_SELECTOR_CODE = "workflow.state_selector_impossible"
RUNTIME_ERROR_CODE = "invalid_input_selector"


@dataclass(frozen=True)
class StateSelectorReference:
    selector: str
    path: str


def _text(value: Any) -> str:
    return str(value or "").strip()


def _object(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _normalized_state_selector(value: Any, *, allow_bare: bool = False) -> str:
    text = _text(value)
    if text == "state" or text.startswith("state."):
        return text
    if allow_bare and text:
        return f"state.{text}"
    return ""


def _selector_reference(
    value: Any,
    path: str,
    *,
    allow_bare: bool = False,
    split_default: bool = False,
) -> list[StateSelectorReference]:
    text = _text(value)
    if split_default:
        text, _default, _has_default = _split_default_filter(text)
    selector = _normalized_state_selector(text, allow_bare=allow_bare)
    return [StateSelectorReference(selector, path)] if selector else []


def _template_references(value: Any, path: str) -> list[StateSelectorReference]:
    from core.application.orchestration.step_templating import _TEMPLATE_RE

    references: list[StateSelectorReference] = []
    if isinstance(value, str):
        for match in _TEMPLATE_RE.finditer(value):
            references.extend(
                _selector_reference(match.group(1), path, allow_bare=True, split_default=True)
            )
        return references
    if isinstance(value, list):
        for index, item in enumerate(value):
            references.extend(_template_references(item, f"{path}[{index}]"))
        return references
    if not isinstance(value, Mapping):
        return references
    if set(value) == {"source"}:
        references.extend(
            _selector_reference(value.get("source"), f"{path}.source", split_default=True)
        )
        return references
    for key, item in value.items():
        references.extend(_template_references(item, f"{path}.{key}"))
    return references


def _tabular_value_references(value: Any, path: str) -> list[StateSelectorReference]:
    references = _template_references(value, path)
    if isinstance(value, str):
        references.extend(_selector_reference(value, path))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            references.extend(_tabular_value_references(item, f"{path}[{index}]"))
    elif isinstance(value, Mapping) and set(value) != {"source"}:
        for key, item in value.items():
            references.extend(_tabular_value_references(item, f"{path}.{key}"))
    return references


def _condition_references(condition: Any, path: str) -> list[StateSelectorReference]:
    if isinstance(condition, Mapping) and "condition" in condition:
        return _condition_references(condition.get("condition"), path)
    if isinstance(condition, str):
        text = condition.strip()
        if "==" in text:
            left, right = text.split("==", 1)
            return [
                *_selector_reference(left, path),
                *_selector_reference(right, path),
            ]
        if "!=" in text:
            left, right = text.split("!=", 1)
            return [
                *_selector_reference(left, path),
                *_selector_reference(right, path),
            ]
        if text.startswith("!state."):
            text = text[1:]
        return _selector_reference(text, path)
    if not isinstance(condition, Mapping):
        return []
    references: list[StateSelectorReference] = []
    if "selector" in condition:
        references.extend(_selector_reference(condition.get("selector"), f"{path}.selector"))
        references.extend(_selector_reference(condition.get("value"), f"{path}.value"))
        return references
    for operator in (
        "equals",
        "not_equals",
        "gt",
        "gte",
        "lt",
        "lte",
        "contains",
        "in",
        "not_in",
    ):
        comparison = condition.get(operator)
        if isinstance(comparison, Mapping):
            references.extend(
                _selector_reference(comparison.get("left"), f"{path}.{operator}.left")
            )
            references.extend(
                _selector_reference(comparison.get("right"), f"{path}.{operator}.right")
            )
    for operator in ("exists", "truthy"):
        if operator in condition:
            references.extend(_selector_reference(condition.get(operator), f"{path}.{operator}"))
    return references


def _mapping_references(
    value: Any,
    path: str,
    *,
    split_default: bool = False,
) -> list[StateSelectorReference]:
    if not isinstance(value, Mapping):
        return []
    references: list[StateSelectorReference] = []
    for key, selector in value.items():
        references.extend(
            _selector_reference(selector, f"{path}.{key}", split_default=split_default)
        )
    return references


def discover_step_state_selectors(
    step: Mapping[str, Any], *, step_path: str = "step"
) -> list[StateSelectorReference]:
    kind = normalize_step_kind(step.get("step_kind") or step.get("kind"), default="")
    override = _object(step.get("override"))
    config = _object(override.get(kind))
    references: list[StateSelectorReference] = []

    if kind in {STEP_KIND_AGENT, STEP_KIND_CONNECTOR}:
        split_default = kind == STEP_KIND_CONNECTOR
        for field in ("input_mapping", "input_mapping_jsonb"):
            if field in step:
                references.extend(
                    _mapping_references(
                        step.get(field),
                        f"{step_path}.{field}",
                        split_default=split_default,
                    )
                )
            if field in config:
                references.extend(
                    _mapping_references(
                        config.get(field),
                        f"{step_path}.override.{kind}.{field}",
                        split_default=split_default,
                    )
                )
    elif kind == STEP_KIND_INVOKE_WORKFLOW:
        references.extend(
            _mapping_references(
                config.get("input_mapping"),
                f"{step_path}.override.invoke_workflow.input_mapping",
            )
        )
    elif kind == STEP_KIND_WAIT:
        duration = _object(config.get("input_mapping")).get("duration_ms")
        duration_path = f"{step_path}.override.wait.input_mapping.duration_ms"
        references.extend(_selector_reference(duration, duration_path))
        references.extend(_template_references(duration, duration_path))
    elif kind == STEP_KIND_IF:
        references.extend(
            _condition_references(config.get("condition"), f"{step_path}.override.if.condition")
        )
    elif kind == STEP_KIND_SWITCH:
        references.extend(
            _selector_reference(config.get("selector"), f"{step_path}.override.switch.selector")
        )
    elif kind == STEP_KIND_SET_VARIABLE:
        values: list[tuple[Any, str]] = [
            (config.get("value"), f"{step_path}.override.set_variable.value")
        ]
        assignments = config.get("assignments")
        if isinstance(assignments, Mapping):
            values.extend(
                (value, f"{step_path}.override.set_variable.assignments.{target}")
                for target, value in assignments.items()
            )
        for value, path in values:
            if isinstance(value, Mapping) and set(value) == {"source"}:
                references.extend(_selector_reference(value.get("source"), f"{path}.source"))
    elif kind == STEP_KIND_COLLECT:
        references.extend(
            _selector_reference(
                config.get("source"), f"{step_path}.override.collect.source", allow_bare=True
            )
        )
        key = config.get("key")
        if isinstance(key, Mapping) and set(key) == {"source"}:
            references.extend(
                _selector_reference(key.get("source"), f"{step_path}.override.collect.key.source")
            )
    elif kind == STEP_KIND_BATCH:
        references.extend(
            _selector_reference(
                config.get("source_path"),
                f"{step_path}.override.{kind}.source_path",
                allow_bare=True,
            )
        )
        steps = config.get("steps")
        if isinstance(steps, list):
            for index, nested in enumerate(steps):
                if isinstance(nested, Mapping):
                    references.extend(
                        discover_step_state_selectors(
                            nested,
                            step_path=f"{step_path}.override.{kind}.steps[{index}]",
                        )
                    )
    elif kind == STEP_KIND_HTTP_REQUEST:
        for field, value in config.items():
            if field not in {"response_target"}:
                references.extend(
                    _template_references(value, f"{step_path}.override.http_request.{field}")
                )
    elif kind == STEP_KIND_DATASET_QUERY:
        references.extend(
            _mapping_references(
                config.get("params"),
                f"{step_path}.override.dataset_query.params",
            )
        )
    elif kind == STEP_KIND_TRANSFORM:
        input_config = _object(config.get("input"))
        references.extend(
            _selector_reference(
                input_config.get("source"),
                f"{step_path}.override.transform.input.source",
            )
        )
    elif kind == STEP_KIND_TABULAR_FEED:
        for field, value in config.items():
            if field == "result_target":
                continue
            field_path = f"{step_path}.override.tabular_feed.{field}"
            references.extend(_tabular_value_references(value, field_path))
    elif kind == STEP_KIND_FILE_FROM_BASE64:
        field_root = f"{step_path}.override.{STEP_KIND_FILE_FROM_BASE64}"
        references.extend(_selector_reference(config.get("source"), f"{field_root}.source"))
        for field in ("filename", "content_type"):
            references.extend(_template_references(config.get(field), f"{field_root}.{field}"))
    elif kind == STEP_KIND_TEMPLATE:
        # The template text names local inputs, not state paths: only the
        # declared sources are workflow selectors.
        rows = config.get("inputs")
        for index, row in enumerate(rows if isinstance(rows, list) else []):
            if isinstance(row, Mapping):
                references.extend(
                    _selector_reference(
                        row.get("source"),
                        f"{step_path}.override.template.inputs[{index}].source",
                    )
                )
    return references


def discover_group_state_selectors(
    group: Mapping[str, Any], *, group_path: str = "group"
) -> list[StateSelectorReference]:
    return _boundary_entry_selectors(group, group_path)


def discover_for_each_state_selectors(
    item: Mapping[str, Any], *, for_each_path: str = "for_each"
) -> list[StateSelectorReference]:
    """The collection a For each walks, and its entry contract sources."""
    references: list[StateSelectorReference] = []
    if item.get("over") not in (None, ""):
        references.extend(
            _selector_reference(item.get("over"), f"{for_each_path}.over", allow_bare=True)
        )
    references.extend(_boundary_entry_selectors(item, for_each_path))
    return references


def _boundary_entry_selectors(
    owner: Mapping[str, Any], owner_path: str
) -> list[StateSelectorReference]:
    contract = _object(owner.get("boundary_contract"))
    entry = contract.get("entry")
    references: list[StateSelectorReference] = []
    if isinstance(entry, list):
        for index, row in enumerate(entry):
            if isinstance(row, Mapping):
                references.extend(
                    _selector_reference(
                        row.get("source"),
                        f"{owner_path}.boundary_contract.entry[{index}].source",
                    )
                )
    return references


def validate_selector_references(
    references: list[StateSelectorReference],
) -> list[dict[str, str]]:
    issues: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for reference in references:
        marker = (reference.selector, reference.path)
        if marker in seen:
            continue
        seen.add(marker)
        try:
            parse_state_path(reference.selector)
        except StatePathError as exc:
            issues.append(
                {
                    "code": AUTHORING_ERROR_CODE,
                    "message": f"State selector '{reference.selector}' is invalid: {exc}",
                    "path": reference.path,
                    "severity": "error",
                    "selector": reference.selector,
                }
            )
    return issues


def _target_paths_overlap(left: str, right: str) -> bool:
    """Whether either state write can replace or reshape the other's target."""
    return left == right or any(
        left.startswith(right + separator) or right.startswith(left + separator)
        for separator in (".", "[")
    )


def _closed_target_shapes(workflow: Mapping[str, Any]) -> dict[str, tuple[str, tuple[str, int]]]:
    """Find targets with a locally provable closed shape, without guessing unknown inputs."""
    shapes: dict[str, tuple[str, tuple[str, int]]] = {}
    known_array_paths: set[str] = set()
    array_path_producer: dict[str, str] = {}
    predecessors = _direct_predecessors(workflow)
    for step in workflow.get("steps", []) if isinstance(workflow.get("steps"), list) else []:
        if not isinstance(step, Mapping):
            continue
        if step.get("group_id") or step.get("for_each_id") or step.get("lane_id"):
            continue
        step_id = _text(step.get("step_id"))
        kind = normalize_step_kind(step.get("step_kind") or step.get("kind"), default="")
        override = _object(step.get("override"))
        config = _object(override.get(kind))
        assignments = config.get("assignments")
        assignments_are_active = isinstance(assignments, Mapping) and bool(assignments)
        target_fields = ("result_target", "response_target")
        if not (kind == STEP_KIND_SET_VARIABLE and assignments_are_active):
            target_fields = ("target", *target_fields)
        written_paths = {
            _text(config.get(field))
            for field in target_fields
            if _text(config.get(field)).startswith("state.")
        }
        if isinstance(assignments, Mapping):
            written_paths.update(
                _text(path) for path in assignments if _text(path).startswith("state.")
            )
        invalidated = {
            path
            for path in known_array_paths
            if any(_target_paths_overlap(path, written) for written in written_paths)
        }
        known_array_paths.difference_update(invalidated)
        for path in invalidated:
            array_path_producer.pop(path, None)
        if kind == STEP_KIND_SET_VARIABLE:
            if assignments_are_active and isinstance(assignments, Mapping):
                assignment_paths = [_text(path) for path in assignments]
                for path, value in assignments.items():
                    candidate = _text(path)
                    overlaps_other_write = any(
                        other != candidate and _target_paths_overlap(candidate, other)
                        for other in assignment_paths
                    )
                    if (
                        candidate.startswith("state.")
                        and isinstance(value, list)
                        and not overlaps_other_write
                    ):
                        known_array_paths.add(candidate)
                        array_path_producer[candidate] = step_id
            else:
                target = _text(config.get("target"))
                if target.startswith("state.") and isinstance(config.get("value"), list):
                    known_array_paths.add(target)
                    array_path_producer[target] = step_id
            continue
        if kind == STEP_KIND_BATCH:
            target = _text(config.get("result_target"))
            source = _text(config.get("source_path"))
            source = source if source.startswith("state.") else f"state.{source}"
            if source not in known_array_paths or array_path_producer.get(
                source
            ) != predecessors.get(step_id):
                continue
            shape = ("array", 2)
            if target.startswith("state."):
                known_array_paths.add(target)
                array_path_producer[target] = step_id
        elif kind == STEP_KIND_HTTP_REQUEST:
            if _text(config.get("response_mode")).lower() != "artifact":
                continue
            target = _text(config.get("response_target"))
            shape = ("scalar", 0)
        else:
            continue
        if target.startswith("state.") and target != "state":
            shapes[step_id] = (target, shape)
    return shapes


def _direct_predecessors(workflow: Mapping[str, Any]) -> dict[str, str]:
    """Return only graph edges that uniquely feed an ordinary root step."""
    settings = _object(workflow.get("settings"))
    graph = _object(settings.get("execution_graph"))
    edges = graph.get("edges")
    if not isinstance(edges, list):
        return {}
    incoming: dict[str, list[tuple[str, str, str]]] = {}
    for edge in edges:
        if not isinstance(edge, Mapping):
            continue
        source = _object(edge.get("source"))
        target = _object(edge.get("target"))
        if target.get("kind") != "step":
            continue
        source_kind = _text(source.get("kind"))
        source_id = _text(source.get("id"))
        target_id = _text(target.get("id"))
        if source_kind and source_id and target_id:
            outcome = _text(_object(edge.get("outcome")).get("name"))
            incoming.setdefault(target_id, []).append((source_kind, source_id, outcome))
    return {
        target_id: sources[0][1]
        for target_id, sources in incoming.items()
        if len(sources) == 1 and sources[0][0] == "step" and sources[0][2] == "success"
    }


def _shape_path_problem(
    selector: str,
    *,
    producer_target: str,
    shape: tuple[str, int],
) -> str:
    """Return why a known closed value cannot satisfy this selector suffix."""
    try:
        selector_tokens = parse_state_path(selector)
        target_tokens = parse_state_path(producer_target)
    except StatePathError:
        return ""
    if len(selector_tokens) <= len(target_tokens):
        return ""
    if selector_tokens[: len(target_tokens)] != target_tokens:
        return ""
    kind, array_depth = shape
    for token in selector_tokens[len(target_tokens) :]:
        if kind == "scalar":
            return "the preceding step writes a scalar value at this path"
        if kind != "array":
            return ""
        is_index = token.kind == "index" or (token.kind == "key" and str(token.value).isdigit())
        if not is_index:
            return "the preceding step writes an array at this path; select an item by index"
        array_depth -= 1
        if array_depth <= 0:
            kind = "unknown"
    return ""


def _closed_shape_selector_issues(
    workflow: Mapping[str, Any], *, workflow_path: str
) -> list[dict[str, str]]:
    shapes = _closed_target_shapes(workflow)
    if not shapes:
        return []
    predecessors = _direct_predecessors(workflow)
    if not predecessors:
        return []
    steps = workflow.get("steps")
    if not isinstance(steps, list):
        return []
    step_by_id = {
        _text(step.get("step_id")): step
        for step in steps
        if isinstance(step, Mapping) and _text(step.get("step_id"))
    }
    issues: list[dict[str, str]] = []
    for step_id, step in step_by_id.items():
        if step.get("group_id") or step.get("for_each_id") or step.get("lane_id"):
            continue
        producer_id = predecessors.get(step_id)
        if producer_id not in step_by_id:
            continue
        shaped_target = shapes.get(producer_id or "")
        if shaped_target is None:
            continue
        target, shape = shaped_target
        step_path = f"{workflow_path}.steps.{step_id}"
        for reference in discover_step_state_selectors(step, step_path=step_path):
            problem = _shape_path_problem(
                reference.selector,
                producer_target=target,
                shape=shape,
            )
            if problem:
                issues.append(
                    {
                        "code": IMPOSSIBLE_SELECTOR_CODE,
                        "message": (
                            f"State selector '{reference.selector}' is impossible: {problem}"
                        ),
                        "path": reference.path,
                        "severity": "error",
                        "selector": reference.selector,
                    }
                )
    return issues


def validate_step_state_selectors(
    step: Mapping[str, Any], *, step_path: str = "step"
) -> list[dict[str, str]]:
    return validate_selector_references(discover_step_state_selectors(step, step_path=step_path))


PHASE_MESSAGE_SELECTOR_CODE = "workflow.selector.phase_scope_removed"
_PHASE_MESSAGE_PREFIX = "message.phase."


def _phase_message_selector_issues(value: Any, path: str) -> list[dict[str, str]]:
    """Name the replacement for a selector that still addresses a phase.

    Workflows have no phases, so such a selector would otherwise fail later as
    an unexplained "not found". A step's latest result is ``message.step.<id>``;
    a real group's latest step result is ``message.group.<id>``.
    """
    if isinstance(value, str):
        if _PHASE_MESSAGE_PREFIX not in value:
            return []
        return [
            {
                "code": PHASE_MESSAGE_SELECTOR_CODE,
                "message": (
                    "Selector addresses a phase, and workflows have no phases. Read a step's "
                    "result with message.step.<step_id>.<field> or a group's latest step result "
                    "with message.group.<group_id>.<field>."
                ),
                "path": path,
                "severity": "error",
                "selector": value,
            }
        ]
    if isinstance(value, Mapping):
        issues: list[dict[str, str]] = []
        for key, item in value.items():
            issues.extend(_phase_message_selector_issues(item, f"{path}.{key}"))
        return issues
    if isinstance(value, list):
        issues = []
        for index, item in enumerate(value):
            issues.extend(_phase_message_selector_issues(item, f"{path}[{index}]"))
        return issues
    return []


def validate_workflow_state_selectors(
    workflow: Mapping[str, Any], *, workflow_path: str = "workflow"
) -> list[dict[str, str]]:
    phase_scope_issues: list[dict[str, str]] = []
    for collection, id_key, fields in (
        (
            "steps",
            "step_id",
            (
                "override",
                "input_mapping_jsonb",
                "output_mapping_jsonb",
                "context_inject",
                "boundary_contract",
            ),
        ),
        ("groups", "group_id", ("boundary_contract",)),
        ("for_each", "for_each_id", ("over", "boundary_contract")),
    ):
        owners = workflow.get(collection)
        for owner in owners if isinstance(owners, list) else []:
            if not isinstance(owner, Mapping):
                continue
            owner_path = f"{workflow_path}.{collection}.{str(owner.get(id_key) or '').strip()}"
            for field in fields:
                phase_scope_issues.extend(
                    _phase_message_selector_issues(owner.get(field), f"{owner_path}.{field}")
                )
    references: list[StateSelectorReference] = []
    groups = workflow.get("groups")
    for group in groups if isinstance(groups, list) else []:
        if isinstance(group, Mapping):
            group_id = str(group.get("group_id") or "").strip()
            references.extend(
                discover_group_state_selectors(
                    group, group_path=f"{workflow_path}.groups.{group_id}"
                )
            )
    containers = workflow.get("for_each")
    for item in containers if isinstance(containers, list) else []:
        if isinstance(item, Mapping):
            for_each_id = str(item.get("for_each_id") or "").strip()
            references.extend(
                discover_for_each_state_selectors(
                    item, for_each_path=f"{workflow_path}.for_each.{for_each_id}"
                )
            )
    steps = workflow.get("steps")
    for step in steps if isinstance(steps, list) else []:
        if isinstance(step, Mapping):
            step_path = f"{workflow_path}.steps.{str(step.get('step_id') or '').strip()}"
            references.extend(discover_step_state_selectors(step, step_path=step_path))
            references.extend(_boundary_entry_selectors(step, step_path))
    return (
        phase_scope_issues
        + validate_selector_references(references)
        + _closed_shape_selector_issues(workflow, workflow_path=workflow_path)
    )


__all__ = [
    "AUTHORING_ERROR_CODE",
    "IMPOSSIBLE_SELECTOR_CODE",
    "PHASE_MESSAGE_SELECTOR_CODE",
    "RUNTIME_ERROR_CODE",
    "StateSelectorReference",
    "discover_for_each_state_selectors",
    "discover_group_state_selectors",
    "discover_step_state_selectors",
    "validate_selector_references",
    "validate_step_state_selectors",
    "validate_workflow_state_selectors",
]
