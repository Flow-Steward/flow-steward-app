"""The first versions that understand a workflow's typed contract and review fields (ADR 0075).

A workflow that declares the shape of its contract fields (``items``,
``properties``, ``min_items``, ``max_items``, ``format``) or an Approval with
typed review fields (``override.approval.review``) means something an older
Core reads differently: it ignores the shape and the review, so the workflow
runs with unchecked inputs or an Approval without fields. A template or an
extension bundle that carries such a workflow therefore declares a minimum
that excludes those releases, and an import refuses one that does not:

* a catalog template: ``compatible_flow_steward.min`` (the product version)
  at least :data:`TYPED_CONTRACT_CORE_MIN`;
* an extension bundle: ``runtime.compatibility.platform_min`` (the extension
  host contract) at least ``TYPED_WORKFLOW_CONTRACT_PLATFORM_MIN``.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from typing import Any

from core.application.extensions.extension_contract_policies import (
    TYPED_WORKFLOW_CONTRACT_PLATFORM_MIN,
)
from core.application.extensions.semver import compare_semver

#: The first Flow Steward release with typed contract shapes and review fields.
#: 0.9.20 and 0.9.21 shipped without them, so the floor is the next release;
#: it moves to the release that actually carries them if that is a later one.
TYPED_CONTRACT_CORE_MIN = "0.9.22"
#: Contract field keys an older Core does not know.
TYPED_SHAPE_KEYS = ("items", "properties", "min_items", "max_items", "format")


def _mapping(value: Any) -> Mapping[str, Any]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return {}
    return value if isinstance(value, Mapping) else {}


def _rows(value: Any) -> list[Mapping[str, Any]]:
    return [row for row in value if isinstance(row, Mapping)] if isinstance(value, list) else []


def _field_uses_shape(row: Mapping[str, Any]) -> bool:
    return any(row.get(key) not in (None, "", [], {}) for key in TYPED_SHAPE_KEYS)


def workflow_uses_typed_contract(workflow: Mapping[str, Any]) -> bool:
    """Whether a workflow definition uses a contract shape key or typed review fields."""
    settings = _mapping(workflow.get("settings"))
    callable_settings = _mapping(settings.get("callable"))
    for key in ("expected_inputs", "expected_outputs"):
        if any(_field_uses_shape(row) for row in _rows(callable_settings.get(key))):
            return True
    for step in _rows(workflow.get("steps")):
        approval = _mapping(_mapping(step.get("override")).get("approval"))
        if approval.get("review") is not None:
            return True
    return False


def any_workflow_uses_typed_contract(workflows: Iterable[Mapping[str, Any]]) -> bool:
    return any(workflow_uses_typed_contract(workflow) for workflow in workflows)


def declares_at_least(declared: str, required: str) -> bool:
    """Whether a declared minimum version excludes every release before ``required``."""
    text = str(declared or "").strip()
    if not text:
        return False
    try:
        return compare_semver(text, required, field_name="minimum") >= 0
    except ValueError:
        return False


__all__ = [
    "TYPED_CONTRACT_CORE_MIN",
    "TYPED_SHAPE_KEYS",
    "TYPED_WORKFLOW_CONTRACT_PLATFORM_MIN",
    "any_workflow_uses_typed_contract",
    "declares_at_least",
    "workflow_uses_typed_contract",
]
