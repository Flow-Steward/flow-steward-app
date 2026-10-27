"""Step and group boundary-contract checks at workflow authoring boundaries.

A step or group may declare ``boundary_contract={entry?, exit?, exit_on?}``:
``entry`` rows use the expected-input row format (name, type, required, source),
``exit`` lists the output keys the owner must produce. Shape is enforced by the
definition validator; this module owns the row-level checks and the read
projection that keeps malformed rows visible for repair.
"""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from typing import Any

from .contract_item_validation import validate_contract_items

_OWNER_COLLECTIONS = ("steps", "groups")


def _boundary(owner: Mapping[str, Any]) -> Mapping[str, Any]:
    boundary = owner.get("boundary_contract")
    return boundary if isinstance(boundary, Mapping) else {}


def boundary_exit_keys(owner: Mapping[str, Any]) -> list[str]:
    """Required output keys of a step or group, in authored order."""
    raw = _boundary(owner).get("exit")
    keys: list[str] = []
    for item in raw if isinstance(raw, list) else []:
        key = str(item or "").strip()
        if key and key not in keys:
            keys.append(key)
    return keys


def _owner_id(owner: dict[str, Any], index: int) -> str:
    """The id-based path segment validation uses everywhere, with index as fallback."""
    owner_id = str(owner.get("step_id") or owner.get("group_id") or "").strip()
    return owner_id or f"[{index}]"


def validate_workflow_boundary_contracts(
    workflow: dict[str, Any], *, workflow_path: str = "workflow"
) -> list[dict[str, Any]]:
    """Row-level issues of every step and group ``boundary_contract.entry``."""
    issues: list[dict[str, Any]] = []
    for collection in _OWNER_COLLECTIONS:
        owners = workflow.get(collection)
        for index, owner in enumerate(owners if isinstance(owners, list) else []):
            if not isinstance(owner, dict) or "entry" not in _boundary(owner):
                continue
            _, errors = validate_contract_items(
                _boundary(owner).get("entry"),
                path_prefix=f"{workflow_path}.{collection}.{_owner_id(owner, index)}.boundary_contract.entry",
                default_required=False,
                allow_source=True,
            )
            issues.extend(errors)
    return issues


def project_boundary_entry(owner: Mapping[str, Any], *, path_prefix: str) -> dict[str, Any]:
    """Keep invalid authoring evidence separate from the typed read projection."""
    raw = _boundary(owner).get("entry")
    normalized, issues = validate_contract_items(
        raw, path_prefix=path_prefix, default_required=False, allow_source=True
    )
    if not issues:
        return {"expected_inputs": deepcopy(raw) if isinstance(raw, list) else []}
    raw_rows = raw if isinstance(raw, list) else []
    return {
        "expected_inputs": [
            deepcopy(
                next(
                    row
                    for row in raw_rows
                    if isinstance(row, dict) and str(row.get("name") or "").strip() == item["name"]
                )
            )
            for item in normalized
        ],
        "raw_expected_inputs": deepcopy(raw),
        "expected_input_issues": issues,
    }


def _project_owner(owner: dict[str, Any], *, path_prefix: str) -> dict[str, Any]:
    boundary = _boundary(owner)
    if "entry" not in boundary:
        return owner
    view = project_boundary_entry(owner, path_prefix=path_prefix)
    # Valid rows stay in the contract; malformed evidence sits beside it.
    projected = {**owner, "boundary_contract": {**boundary, "entry": view.pop("expected_inputs")}}
    projected.update(view)
    return projected


def project_workflow_boundary_contracts(workflow: dict[str, Any]) -> dict[str, Any]:
    """Expose canonical repair issues on detail reads without changing stored rows."""
    result = deepcopy(workflow)
    for collection in _OWNER_COLLECTIONS:
        owners = result.get(collection)
        if not isinstance(owners, list):
            continue
        result[collection] = [
            _project_owner(
                owner,
                path_prefix=(
                    f"workflow.{collection}.{_owner_id(owner, index)}.boundary_contract.entry"
                ),
            )
            if isinstance(owner, dict)
            else owner
            for index, owner in enumerate(owners)
        ]
    return result


def boundary_entry_inspection(workflow: dict[str, Any], step: dict[str, Any]) -> dict[str, Any]:
    """The step's boundary entry inspection, under its id-based field path."""
    del workflow
    step_id = str(step.get("step_id") or "").strip()
    return project_boundary_entry(
        step, path_prefix=f"workflow.steps.{step_id}.boundary_contract.entry"
    )
