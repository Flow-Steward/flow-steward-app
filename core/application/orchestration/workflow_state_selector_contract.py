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
    return phase_scope_issues + validate_selector_references(references)


__all__ = [
    "AUTHORING_ERROR_CODE",
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
