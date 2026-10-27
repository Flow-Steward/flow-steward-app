"""Materialize builder Test Run input bindings into workflow state."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from core.application.orchestration.state_path_guards import canonical_state_write_parts
from core.application.orchestration.step_templating import STATE_PREFIX, set_state_path
from core.application.registry.workflow_contract import WorkflowContractField, WorkflowInterface
from core.application.registry.workflow_contract_service import (
    _contains_secret_reference,
    _resolved_default,
    coerce_contract_value,
)


def _binding_policy_path(source: Any) -> tuple[str, ...] | None:
    parts = canonical_state_write_parts(source)
    if not parts:
        return None
    # Explicit mirror writes address the same public value as their canonical path.
    return parts[1:] if parts and parts[0] == "workflow_data" else parts


def _prefer_policy(
    policies: dict[tuple[str, ...], WorkflowContractField],
    path: tuple[str, ...],
    field: WorkflowContractField,
) -> None:
    existing = policies.get(path)
    if existing is None or (not existing.has_default and field.has_default):
        policies[path] = field


def _materialize_protected_value(
    value: Any,
    path: tuple[str, ...],
    field: WorkflowContractField,
    *,
    present: bool = True,
) -> tuple[Any, str, str]:
    if not path:
        if field.has_default:
            if present:
                return (
                    value,
                    "workflow.input.override_forbidden",
                    "Configured defaults cannot be supplied by the caller",
                )
            ok, normalized, message = _resolved_default(field)
            return normalized, "" if ok else "workflow.interface.default_invalid", message
        from core.infrastructure.security.secret_reference import is_secret_reference

        # Only this helper can have created a reference: external references were rejected.
        if is_secret_reference(value):
            return value, "", ""
        ok, normalized, message = coerce_contract_value(value, field)
        return normalized, "" if ok else "workflow.input.invalid_type", message
    if not present:
        value = {}
    if not isinstance(value, dict):
        if field.has_default:
            return (
                value,
                "workflow.input.override_forbidden",
                "Configured defaults cannot be replaced by a parent value",
            )
        return value, "", ""
    key, *remaining = path
    if key not in value and not field.has_default:
        return value, "", ""
    normalized, code, message = _materialize_protected_value(
        value.get(key), tuple(remaining), field, present=key in value
    )
    if not code:
        value[key] = normalized
    return value, code, message


def materialize_step_input_values(
    envelope: dict[str, Any],
    inputs: dict[str, Any],
    interface: WorkflowInterface,
    contract_preview: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Park step aliases after protecting every overlapping server-owned input path."""
    metadata = envelope.get("metadata")
    metadata = dict(metadata) if isinstance(metadata, Mapping) else {}
    if "input_binding_values" in metadata or _contains_secret_reference(envelope):
        return [
            {
                "code": "workflow.input.invalid_type",
                "message": "Test-run inputs must not contain internal binding values or secret references",
                "path": "inputs",
            }
        ]
    raw_bindings = metadata.get("input_bindings")
    bindings = (
        [row for row in raw_bindings if isinstance(row, Mapping)]
        if isinstance(raw_bindings, list)
        else []
    )
    preview_rows = [
        row
        for group in ("locked_required", "unresolved_required", "optional", "custom")
        for row in (contract_preview or {}).get(group, [])
        if isinstance(row, Mapping)
    ]
    # Declared step inputs use the existing binding transport. Values without
    # a state source go to the canonical input container for isolated execution.
    bound_names = {str(row.get("name") or "").strip() for row in bindings}
    for row in preview_rows:
        name = str(row.get("name") or "").strip()
        if name in inputs and name not in bound_names:
            source = str(row.get("source") or "")
            bindings.append(
                {
                    "name": name,
                    "source": source
                    if source.startswith(STATE_PREFIX)
                    else f"state.materialized_inputs[{json.dumps(name)}]",
                }
            )
            bound_names.add(name)
    if bindings:
        metadata["input_bindings"] = bindings
        envelope["metadata"] = metadata

    protected: dict[tuple[str, ...], WorkflowContractField] = {
        ("inputs", field.name): field
        for field in interface.expected_inputs
        if field.is_secret or field.has_default
    }
    for row in preview_rows:
        path = _binding_policy_path(row.get("source"))
        if path and row.get("type") == "secret":
            _prefer_policy(
                protected,
                path,
                WorkflowContractField(name=str(row.get("name") or "value"), type="secret"),
            )
    values: dict[str, Any] = {}
    errors: list[dict[str, Any]] = []
    names = {str(row.get("name") or "").strip() for row in bindings}
    for name in sorted(names - {""}):
        if name not in inputs:
            continue
        matched = [row for row in bindings if str(row.get("name") or "").strip() == name]
        authoritative = [row for row in preview_rows if str(row.get("name") or "").strip() == name]
        policies: dict[tuple[str, ...], WorkflowContractField] = {}
        # Supplying a step alias with the public input's own name must not
        # turn its configured default into a caller override.
        for field in interface.expected_inputs:
            if authoritative and field.name == name and field.has_default:
                _prefer_policy(policies, (), field)
        for row in [*matched, *authoritative]:
            target = _binding_policy_path(row.get("source"))
            if target is None:
                continue
            for path, field in protected.items():
                if path[: len(target)] == target:
                    _prefer_policy(policies, path[len(target) :], field)
                elif target[: len(path)] == path:
                    errors.append(
                        {
                            "code": "workflow.input.override_forbidden"
                            if field.has_default
                            else "workflow.input.invalid_type",
                            "message": "Step inputs cannot write inside a protected input value",
                            "path": f"inputs.{name}",
                        }
                    )
        for row in authoritative:
            if row.get("type") == "secret" and not _binding_policy_path(row.get("source")):
                _prefer_policy(policies, (), WorkflowContractField(name=name, type="secret"))
        value = inputs[name]
        for path, field in sorted(policies.items()):
            value, code, message = _materialize_protected_value(value, path, field)
            if code:
                errors.append({"code": code, "message": message, "path": f"inputs.{name}"})
                break
        # The authoritative preview, not the caller's binding type, owns coercion.
        # Reuse the same secret/file/default/type owner as workflow inputs.
        for row in authoritative:
            field = WorkflowContractField.model_validate(
                {**dict(row), "name": name, "required": False}
            )
            value, code, message = _materialize_protected_value(value, (), field)
            if code:
                errors.append({"code": code, "message": message, "path": f"inputs.{name}"})
                break
        values[name] = value
    if errors:
        return errors
    for name in values:
        inputs.pop(name)
    if values:
        metadata["input_binding_values"] = values
        envelope["metadata"] = metadata
    return []


def apply_test_run_input_bindings(
    state: Mapping[str, Any],
    task_envelope: Mapping[str, Any],
) -> dict[str, Any]:
    """Return the safe state patch described by Test Run input bindings."""

    metadata = task_envelope.get("metadata")
    metadata = dict(metadata) if isinstance(metadata, Mapping) else {}
    raw_bindings = metadata.get("input_bindings")
    bindings = list(raw_bindings) if isinstance(raw_bindings, list) else []
    raw_inputs = task_envelope.get("inputs")
    inputs = dict(raw_inputs) if isinstance(raw_inputs, Mapping) else {}
    # A step run keeps its step-local values apart from the workflow inputs.
    step_values = metadata.get("input_binding_values")
    if isinstance(step_values, Mapping):
        inputs = {**inputs, **dict(step_values)}
    if not bindings:
        return {}

    source_state = dict(state)
    update: dict[str, Any] = {}
    for row in bindings:
        if not isinstance(row, Mapping):
            continue
        input_name = str(row.get("name") or "").strip()
        source = str(row.get("source") or "").strip()
        if not input_name or not source.startswith(STATE_PREFIX):
            continue
        if input_name not in inputs:
            continue
        set_state_path(update, source_state, source, inputs.get(input_name))
    return update


__all__ = ["apply_test_run_input_bindings", "materialize_step_input_values"]
