"""Pure shape rules of the standalone For each container (ADR 0038).

A For each has its own ``for_each_id``, an optional parent sequence Group,
``over``/``item_name``/``index_name`` and the same sequence or lanes body as a
Group. These rules only parse; the whole-definition validator composes them.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from core.application.workflows.execution_graph import (
    ROOT_SCOPE,
    NodeRef,
    Scope,
    for_each_scope,
    group_scope,
)
from core.application.workflows.workflow_definition_rules import (
    BODY_KIND_SEQUENCE,
    _boundary_shape_issues,
    _error_policy_token,
    _Group,
    _issue,
    _item_path,
    _parse_body,
    _text,
)

#: Item and index names of a For each are plain variable names.
VARIABLE_NAME_PATTERN = re.compile(r"^[A-Za-z_]\w*$", re.ASCII)


@dataclass(frozen=True)
class _ForEach:
    for_each_id: str
    group_id: str | None
    body_kind: str
    sequence_entry: NodeRef | None
    lanes: dict[str, str]
    error_policy: str | None
    path: str

    @property
    def scope(self) -> Scope:
        """Where the For each node itself sits: the root or its sequence Group."""
        if self.group_id is None:
            return ROOT_SCOPE
        return group_scope(self.group_id, None)

    def body_scope(self, lane_id: str | None) -> Scope:
        return for_each_scope(self.for_each_id, lane_id)


def _variable_name_issue(value: Any, *, code: str, what: str, path: str) -> dict[str, str] | None:
    if isinstance(value, str) and VARIABLE_NAME_PATTERN.match(value):
        return None
    return _issue(code, f"{what} must be a variable name (letters, digits, underscore)", path)


def _collect_for_each(
    raw_items: Any, groups: Mapping[str, _Group], issues: list[dict[str, str]]
) -> dict[str, _ForEach]:
    containers: dict[str, _ForEach] = {}
    if raw_items is None:
        return containers
    if not isinstance(raw_items, list):
        issues.append(_issue("workflow.for_each.invalid", "for_each must be a list", "for_each"))
        return containers
    for index, raw in enumerate(raw_items):
        path = _item_path("for_each", index, raw, "for_each_id")
        if not isinstance(raw, Mapping):
            issues.append(_issue("workflow.for_each.invalid", "For each must be an object", path))
            continue
        for_each_id = _text(raw.get("for_each_id"))
        if for_each_id is None:
            issues.append(
                _issue(
                    "workflow.for_each.id_missing",
                    "For each needs a for_each_id",
                    f"{path}.for_each_id",
                )
            )
            continue
        if for_each_id in containers:
            issues.append(
                _issue(
                    "workflow.for_each.id_duplicate",
                    f"For each id '{for_each_id}' is used twice",
                    f"{path}.for_each_id",
                )
            )
            continue
        group_id = raw.get("group_id")
        if group_id is not None:
            group_id = _text(group_id)
            group = groups.get(group_id or "")
            if group is None or group.body_kind != BODY_KIND_SEQUENCE:
                issues.append(
                    _issue(
                        "workflow.for_each.parent_group_invalid",
                        "A For each can sit only at the root or directly in a sequence Group",
                        f"{path}.group_id",
                    )
                )
                continue
        if _text(raw.get("over")) is None:
            issues.append(
                _issue(
                    "workflow.for_each.over_missing",
                    "For each needs the collection it walks (over)",
                    f"{path}.over",
                )
            )
        item_issue = _variable_name_issue(
            raw.get("item_name"),
            code="workflow.for_each.item_name_invalid",
            what="item_name",
            path=f"{path}.item_name",
        )
        if item_issue:
            issues.append(item_issue)
        index_name = raw.get("index_name")
        if index_name is not None:
            index_issue = _variable_name_issue(
                index_name,
                code="workflow.for_each.index_name_invalid",
                what="index_name",
                path=f"{path}.index_name",
            )
            if index_issue:
                issues.append(index_issue)
            elif index_name == raw.get("item_name"):
                issues.append(
                    _issue(
                        "workflow.for_each.names_conflict",
                        "item_name and index_name must differ",
                        f"{path}.index_name",
                    )
                )
        issues.extend(
            _boundary_shape_issues(
                raw.get("boundary_contract"), f"{path}.boundary_contract", group=True
            )
        )
        body_kind, sequence_entry, lanes = _parse_body(
            raw.get("body"), owner="for_each", path=f"{path}.body", issues=issues
        )
        containers[for_each_id] = _ForEach(
            for_each_id=for_each_id,
            group_id=group_id,
            body_kind=body_kind,
            sequence_entry=sequence_entry,
            lanes=lanes,
            error_policy=_error_policy_token(
                raw.get("error_policy"), f"{path}.error_policy", issues
            ),
            path=path,
        )
    return containers


__all__ = ["VARIABLE_NAME_PATTERN", "_ForEach", "_collect_for_each"]
