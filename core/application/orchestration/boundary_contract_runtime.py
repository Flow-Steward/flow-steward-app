"""Boundary contracts at run time: entry inputs before, exit outputs after.

A step or real group may carry ``boundary_contract = {entry?, exit?, exit_on?}``
(groups: ``entry``/``exit``). ``entry`` rows are the former phase
``expected_inputs`` and are resolved and materialized into
``materialized_inputs`` before the executor runs; ``exit`` names the former
phase ``required_outputs`` and is checked after an accepted successful result.
For a step, ``exit_on`` limits the check to the outcomes that leave the former
phase; without it every non-error outcome is checked. The checks are the
existing ones moved here from the phase hub and supervisor; nothing in a
contract changes the route.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from core.application.orchestration.expected_input_sources import (
    resolve_expected_input_source,
    resolve_runtime_public_value,
)
from core.application.workflows.contract_sanitation import safe_contract_rows
from core.application.workflows.execution_graph import OUTCOME_ERROR, Outcome


@dataclass
class EntryCheck:
    missing: list[str] = field(default_factory=list)
    updates: dict[str, Any] = field(default_factory=dict)
    selector_failure: dict[str, Any] = field(default_factory=dict)


def _has_value(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, tuple, set, dict)):
        return len(value) > 0
    return True


def _contract(owner: Mapping[str, Any]) -> Mapping[str, Any]:
    contract = owner.get("boundary_contract")
    return contract if isinstance(contract, Mapping) else {}


def _entry_selector_failure(
    owner: Mapping[str, Any], owner_path: str
) -> tuple[list[str], dict[str, Any]]:
    from core.application.orchestration.workflow_state_selector_contract import (
        RUNTIME_ERROR_CODE,
        _boundary_entry_selectors,
        validate_selector_references,
    )

    issues = validate_selector_references(_boundary_entry_selectors(owner, owner_path))
    if not issues:
        return [], {}
    raw_entry = _contract(owner).get("entry")
    rows = raw_entry if isinstance(raw_entry, list) else []
    invalid: list[str] = []
    failure: dict[str, Any] = {}
    for issue in issues:
        index_text = issue["path"].partition("entry[")[2].partition("]")[0]
        try:
            row = rows[int(index_text)]
        except (IndexError, TypeError, ValueError):
            row = {}
        name = str(row.get("name") or "input") if isinstance(row, Mapping) else "input"
        invalid.append(f"{name} ({RUNTIME_ERROR_CODE} at {issue['path']}: {issue['selector']})")
        if not failure:
            failure = {
                "code": RUNTIME_ERROR_CODE,
                "selector": issue["selector"],
                "path": issue["path"],
                "input_name": name,
            }
    return invalid, failure


def check_entry(
    owner: Mapping[str, Any], state: Mapping[str, Any], *, owner_path: str
) -> EntryCheck:
    """Resolve the owner's entry rows; report the required ones that are missing."""
    invalid, selector_failure = _entry_selector_failure(owner, owner_path)
    existing = state.get("materialized_inputs")
    materialized = dict(existing) if isinstance(existing, Mapping) else {}
    if invalid:
        return EntryCheck(missing=invalid, selector_failure=selector_failure)
    missing: list[str] = []
    for row in safe_contract_rows(_contract(owner).get("entry") or []):
        if not isinstance(row, Mapping):
            continue
        name = str(row.get("name") or "")
        if not name:
            continue
        source = str(row.get("source") or "").strip()
        required = bool(row.get("required", False))
        source_error = ""
        if source:
            value, source_error = resolve_expected_input_source(dict(state), source, name)
        else:
            # Source-less rows are shorthand for public runtime values.
            value = resolve_runtime_public_value(dict(state), name)
        if source_error:
            if required:
                missing.append(source_error)
            continue
        # Optional inputs are materialized too: downstream contracts read them.
        if _has_value(value):
            materialized[name] = value
        if required and not _has_value(value):
            missing.append(name)
    updates = {"materialized_inputs": materialized} if materialized else {}
    return EntryCheck(missing=missing, updates=updates)


def missing_context_inject(step: Mapping[str, Any], state: Mapping[str, Any]) -> list[str]:
    """Required ``context_inject`` inputs of a step that no message or selector provides."""
    from core.application.orchestration.agent_messages import get_state_messages

    spec_map = step.get("context_inject")
    if not isinstance(spec_map, Mapping):
        return []
    missing: list[str] = []
    messages: list[dict] | None = None
    for key, spec in spec_map.items():
        if not isinstance(spec, Mapping) or not bool(spec.get("required", True)):
            continue
        selector = str(spec.get("source") or "").strip()
        if selector:
            value, source_error = resolve_expected_input_source(
                dict(state), selector, f"context_inject.{key}"
            )
            if source_error or not _has_value(value):
                missing.append(source_error or f"context_inject.{key}")
            continue
        message_type = str(spec.get("message_type") or spec.get("skill") or "").strip()
        producer = str(spec.get("producer") or spec.get("from_agent") or "").strip()
        step_filter = str(spec.get("step_id") or "").strip()
        if not message_type:
            missing.append(f"context_inject.{key} (missing message_type/skill selector)")
            continue
        if messages is None:
            messages = get_state_messages(state)
        found = any(
            isinstance(msg, dict)
            and str(msg.get("skill") or "") == message_type
            and (not producer or str(msg.get("from_agent") or "") == producer)
            and (not step_filter or str(msg.get("step_id") or "") == step_filter)
            for msg in messages
        )
        if not found:
            missing.append(
                f"context_inject.{key} (missing message skill='{message_type}'"
                f"{f', producer={producer}' if producer else ''}"
                f"{f', step_id={step_filter}' if step_filter else ''})"
            )
    return missing


def exit_applies(owner: Mapping[str, Any], outcome: Outcome) -> bool:
    """Whether a successful *outcome* leaves the boundary and must meet its exit."""
    contract = _contract(owner)
    if not contract.get("exit"):
        return False
    exit_on = contract.get("exit_on")
    if isinstance(exit_on, list) and exit_on:
        names = {
            str(item.get("name") if isinstance(item, Mapping) else item or "").strip()
            for item in exit_on
        }
        return outcome.name in names
    return outcome.name != OUTCOME_ERROR


def _resolve_dotted(artifacts: Mapping[str, Any], path: str) -> tuple[bool, Any]:
    current: Any = artifacts
    for part in path.split("."):
        if not isinstance(current, Mapping) or part not in current:
            return False, None
        current = current[part]
    return True, current


def missing_exit_outputs(
    owner: Mapping[str, Any], state: Mapping[str, Any], messages: Sequence[Mapping[str, Any]]
) -> list[str]:
    """Exit outputs the owner's results and declared state targets do not provide."""
    from core.application.orchestration.state_paths import (
        resolve_state_path_with_workflow_data,
        strip_state_prefix,
    )

    missing: list[str] = []
    for raw_key in _contract(owner).get("exit") or []:
        output_key = str(raw_key or "").strip()
        if not output_key:
            continue
        found = False
        if output_key.startswith("message."):
            from core.application.orchestration.agent_messages import resolve_message_selector

            try:
                value = resolve_message_selector(output_key, state)
                found = _has_value(value) or isinstance(value, (list, tuple, set, dict))
            except Exception:
                found = False
        else:
            output_selector = strip_state_prefix(output_key)
            for message in messages:
                artifacts = message.get("artifacts")
                artifacts = artifacts if isinstance(artifacts, Mapping) else {}
                exists, value = (
                    _resolve_dotted(artifacts, output_key)
                    if "." in output_key
                    else (output_key in artifacts, artifacts.get(output_key))
                )
                if _has_value(value) or (exists and isinstance(value, (list, tuple, set, dict))):
                    found = True
                    break
                targets = [artifacts.get("result_target")]
                raw_targets = artifacts.get("result_targets")
                if isinstance(raw_targets, list):
                    targets.extend(raw_targets)
                for target in targets:
                    target_selector = strip_state_prefix(str(target or "").strip())
                    declares = target_selector == output_selector or (
                        bool(output_selector)
                        and target_selector.startswith(
                            (f"{output_selector}.", f"{output_selector}[")
                        )
                    )
                    if not target_selector or not declares:
                        continue
                    value, exists = resolve_state_path_with_workflow_data(
                        dict(state), output_selector
                    )
                    if _has_value(value) or (
                        exists and isinstance(value, (list, tuple, set, dict))
                    ):
                        found = True
                        break
                if found:
                    break
        if not found:
            missing.append(output_key)
    return missing


__all__ = [
    "EntryCheck",
    "check_entry",
    "exit_applies",
    "missing_context_inject",
    "missing_exit_outputs",
]
