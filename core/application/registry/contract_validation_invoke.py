# ruff: noqa: F405, I001, RUF100
# mypy: ignore-errors
from __future__ import annotations

from .state_selector_shape import StateSelectorShape
from .workflow_lookup import get_workflow_row

from .contract_validation_workflow import *  # noqa: F403,F401  # NOSONAR


def validate_invoke_workflow_step(
    db,
    *,
    workflow_id: str,
    step: dict,
    path_prefix: str,
) -> dict:
    """Validate invoke-workflow step payload stored in step.override.invoke_workflow."""
    errors: list[dict] = []
    warnings: list[dict] = []
    spec = _extract_invoke_workflow_spec(step)
    if not isinstance(spec, dict):
        # Readiness/runtime parity: the runtime reads the child + mappings from
        # override.invoke_workflow, so a step declared as invoke_workflow WITHOUT that override
        # (e.g. a hand-authored / API / canvas step carrying only a top-level workflow_id column)
        # would save clean but never invoke anything. Reject it here instead of at run time.
        if str(step.get("step_kind") or "").strip().lower() == "invoke_workflow":
            return {
                "valid": False,
                "errors": [
                    _issue(
                        code="invoke.workflow_id_required",
                        message=(
                            "invoke_workflow step requires override.invoke_workflow.workflow_id "
                            "(with input/output mappings); a top-level workflow_id column alone is "
                            "not honored by the runtime."
                        ),
                        path=f"{path_prefix}.override.invoke_workflow.workflow_id",
                    )
                ],
                "warnings": [],
            }
        return {"valid": True, "errors": [], "warnings": []}

    from core.application.workflows.workflow_hook_contract import (
        HOOK_CONFIG_KEY,
        invoke_config_issues,
    )

    if HOOK_CONFIG_KEY in spec:
        # A hook names no child here: each project's binding does, and the
        # binding save checks the child, its mappings and the chain (§9.2).
        hook_issues = invoke_config_issues(spec)
        return {
            "valid": not hook_issues,
            "errors": [
                _issue(
                    code=issue["code"],
                    message=issue["message"],
                    path=f"{path_prefix}.{issue['path']}",
                )
                for issue in hook_issues
            ],
            "warnings": [],
        }

    child_workflow_id = str(spec.get("workflow_id") or "").strip()
    if not child_workflow_id:
        errors.append(
            _issue(
                code="invoke.workflow_id_required",
                message="invoke_workflow.workflow_id is required",
                path=f"{path_prefix}.override.invoke_workflow.workflow_id",
            )
        )
        return {"valid": False, "errors": errors, "warnings": warnings}

    if child_workflow_id == str(workflow_id or "").strip():
        errors.append(
            _issue(
                code="invoke.self_call_forbidden",
                message="invoke_workflow cannot call the same parent workflow (direct recursion is forbidden)",
                path=f"{path_prefix}.override.invoke_workflow.workflow_id",
            )
        )

    child_workflow = get_workflow_row(db, child_workflow_id)
    if not isinstance(child_workflow, dict):
        errors.append(
            _issue(
                code="invoke.child_workflow_missing",
                message=f"invoke_workflow references unknown workflow '{child_workflow_id}'",
                path=f"{path_prefix}.override.invoke_workflow.workflow_id",
            )
        )
        return {"valid": False, "errors": errors, "warnings": warnings}

    if _workflow_reaches_target_via_invoke(
        db,
        current_workflow_id=child_workflow_id,
        target_workflow_id=workflow_id,
        seen=set(),
        cache={},
    ):
        errors.append(
            _issue(
                code="invoke.indirect_recursion_forbidden",
                message=(
                    f"invoke_workflow '{workflow_id}' -> '{child_workflow_id}' creates a recursive cycle "
                    f"back to '{workflow_id}'"
                ),
                path=f"{path_prefix}.override.invoke_workflow.workflow_id",
            )
        )
    if not bool(child_workflow.get("enabled", True)):
        errors.append(
            _issue(
                code="invoke.child_workflow_disabled",
                message=f"invoke_workflow references disabled workflow '{child_workflow_id}'",
                path=f"{path_prefix}.override.invoke_workflow.workflow_id",
            )
        )

    child_interface = _workflow_interface_from_definition(child_workflow)
    callable_enabled = bool(child_interface.enabled)
    child_inputs_raw = [row.dump() for row in child_interface.expected_inputs]
    child_outputs_raw = [row.dump() for row in child_interface.expected_outputs]
    child_artifacts_raw = [row.dump() for row in child_interface.returned_files]
    if not callable_enabled:
        errors.append(
            _issue(
                code="invoke.child_not_callable",
                message=(
                    f"Workflow '{child_workflow_id}' is not callable. "
                    "Enable callable mode and declare its public interface first."
                ),
                path=f"{path_prefix}.override.invoke_workflow.workflow_id",
            )
        )
    elif "parent_workflow" not in set(child_interface.trigger_channels):
        errors.append(
            _issue(
                code="invoke.child_not_parent_workflow_triggerable",
                message=(
                    f"Workflow '{child_workflow_id}' does not allow parent_workflow triggers. "
                    "Add parent_workflow to settings.callable.trigger_channels before using it as a child workflow."
                ),
                path=f"{path_prefix}.override.invoke_workflow.workflow_id",
            )
        )
    _append_issues_by_severity(
        issues=child_interface.definition_issues(
            path_prefix=f"{path_prefix}.override.invoke_workflow.child.callable"
        ),
        errors=errors,
        warnings=warnings,
    )

    child_inputs, child_input_issues = validate_contract_items(
        child_inputs_raw,
        path_prefix=f"{path_prefix}.override.invoke_workflow.child.expected_inputs",
        default_required=False,
        allow_secret_input=True,
    )
    child_outputs, child_output_issues = validate_contract_items(
        child_outputs_raw,
        path_prefix=f"{path_prefix}.override.invoke_workflow.child.expected_outputs",
        default_required=True,
    )
    child_artifacts, child_artifact_issues = validate_contract_items(
        child_artifacts_raw,
        path_prefix=f"{path_prefix}.override.invoke_workflow.child.returned_files",
        default_required=True,
    )
    errors.extend(child_input_issues)
    errors.extend(child_output_issues)
    errors.extend(child_artifact_issues)
    if child_interface.variable_outputs_required() and child_outputs:
        errors.extend(
            _validate_callable_outputs_produced(
                db=db,
                workflow=child_workflow,
                callable_outputs=child_outputs,
                path_prefix=f"{path_prefix}.override.invoke_workflow.child.expected_outputs",
                code_prefix="invoke.child",
                warnings=warnings,
            )
        )

    input_mapping = spec.get("input_mapping") or {}
    output_mapping = spec.get("output_mapping") or {}
    artifact_mapping = spec.get("artifact_mapping") or {}
    if not isinstance(input_mapping, dict):
        errors.append(
            _issue(
                code="invoke.input_mapping_not_object",
                message="invoke_workflow.input_mapping must be an object: child_input -> parent_source",
                path=f"{path_prefix}.override.invoke_workflow.input_mapping",
            )
        )
        input_mapping = {}
    if not isinstance(output_mapping, dict):
        errors.append(
            _issue(
                code="invoke.output_mapping_not_object",
                message="invoke_workflow.output_mapping must be an object: child_output -> parent_target",
                path=f"{path_prefix}.override.invoke_workflow.output_mapping",
            )
        )
        output_mapping = {}
    if not isinstance(artifact_mapping, dict):
        errors.append(
            _issue(
                code="invoke.artifact_mapping_not_object",
                message="invoke_workflow.artifact_mapping must be an object: child_artifact -> parent_target",
                path=f"{path_prefix}.override.invoke_workflow.artifact_mapping",
            )
        )
        artifact_mapping = {}

    normalized_input_mapping: dict[str, str] = {}
    for child_input, source in (input_mapping or {}).items():
        clean_child_input = str(child_input or "").strip()
        clean_source = str(source or "").strip()
        if not clean_child_input:
            errors.append(
                _issue(
                    code="invoke.input_mapping_key_required",
                    message="invoke_workflow.input_mapping contains an empty child input key",
                    path=f"{path_prefix}.override.invoke_workflow.input_mapping",
                )
            )
            continue
        if not clean_source:
            errors.append(
                _issue(
                    code="invoke.input_mapping_value_required",
                    message=f"invoke_workflow input '{clean_child_input}' must map to a parent source",
                    path=f"{path_prefix}.override.invoke_workflow.input_mapping.{clean_child_input}",
                )
            )
            continue
        normalized_input_mapping[clean_child_input] = clean_source

    normalized_output_mapping: dict[str, str] = {}
    for child_output, target in (output_mapping or {}).items():
        clean_child_output = str(child_output or "").strip()
        clean_target = str(target or "").strip()
        if not clean_child_output:
            errors.append(
                _issue(
                    code="invoke.output_mapping_key_required",
                    message="invoke_workflow.output_mapping contains an empty child output key",
                    path=f"{path_prefix}.override.invoke_workflow.output_mapping",
                )
            )
            continue
        if not clean_target:
            errors.append(
                _issue(
                    code="invoke.output_mapping_value_required",
                    message=f"invoke_workflow output '{clean_child_output}' must map to a parent target",
                    path=f"{path_prefix}.override.invoke_workflow.output_mapping.{clean_child_output}",
                )
            )
            continue
        if is_reserved_state_target(clean_target):
            errors.append(
                _issue(
                    code="invoke.output_mapping_target_reserved",
                    message="invoke_workflow output mapping cannot write security-sensitive runtime state",
                    path=f"{path_prefix}.override.invoke_workflow.output_mapping.{clean_child_output}",
                )
            )
            continue
        normalized_output_mapping[clean_child_output] = clean_target

    normalized_artifact_mapping: dict[str, str] = {}
    for child_artifact, target in (artifact_mapping or {}).items():
        clean_child_artifact = str(child_artifact or "").strip()
        clean_target = str(target or "").strip()
        if not clean_child_artifact:
            errors.append(
                _issue(
                    code="invoke.artifact_mapping_key_required",
                    message="invoke_workflow.artifact_mapping contains an empty child artifact key",
                    path=f"{path_prefix}.override.invoke_workflow.artifact_mapping",
                )
            )
            continue
        if not clean_target:
            errors.append(
                _issue(
                    code="invoke.artifact_mapping_value_required",
                    message=f"invoke_workflow artifact '{clean_child_artifact}' must map to a parent target",
                    path=f"{path_prefix}.override.invoke_workflow.artifact_mapping.{clean_child_artifact}",
                )
            )
            continue
        if is_reserved_state_target(clean_target):
            errors.append(
                _issue(
                    code="invoke.artifact_mapping_target_reserved",
                    message="invoke_workflow artifact mapping cannot write security-sensitive runtime state",
                    path=f"{path_prefix}.override.invoke_workflow.artifact_mapping.{clean_child_artifact}",
                )
            )
            continue
        normalized_artifact_mapping[clean_child_artifact] = clean_target

    child_input_names = {
        str(item.get("name") or "").strip()
        for item in child_inputs
        if str(item.get("name") or "").strip()
    }
    child_input_types = {
        str(item.get("name") or "").strip(): _normalize_type(item.get("type"))
        for item in child_inputs
        if str(item.get("name") or "").strip()
    }
    child_required_input_names = {
        str(item.get("name") or "").strip()
        for item in child_inputs
        if str(item.get("name") or "").strip() and bool(item.get("required", False))
    }
    child_output_names = {
        str(item.get("name") or "").strip()
        for item in child_outputs
        if str(item.get("name") or "").strip()
    }
    child_required_output_names = {
        str(item.get("name") or "").strip()
        for item in child_outputs
        if str(item.get("name") or "").strip() and bool(item.get("required", True))
    }
    child_artifact_names = {
        str(item.get("name") or "").strip()
        for item in child_artifacts
        if str(item.get("name") or "").strip()
    }
    child_required_artifact_names = {
        str(item.get("name") or "").strip()
        for item in child_artifacts
        if str(item.get("name") or "").strip() and bool(item.get("required", True))
    }

    available_type_map: dict[str, str] = {}
    resolver_for_types = IOResolver(db)
    try:
        available_type_map = resolver_for_types.get_available_type_map_for_step(
            workflow_id, str(step.get("step_id") or "").strip()
        )
    except Exception as resolver_err:
        warnings.append(
            _issue(
                code="invoke.input_mapping_source_validation_skipped",
                message=(
                    "Could not validate invoke_workflow input mapping sources against parent step variables: "
                    f"{resolver_err}"
                ),
                path=f"{path_prefix}.override.invoke_workflow.input_mapping",
                severity="warning",
            )
        )
        available_type_map = {}

    selector_shape = StateSelectorShape(resolver_for_types, get_workflow_row(db, workflow_id) or {})

    for child_input_name in sorted(child_required_input_names):
        if child_input_name not in normalized_input_mapping:
            issue = _issue(
                code="invoke.required_input_unmapped",
                message=(
                    f"Required child input '{child_input_name}' is not mapped in invoke_workflow.input_mapping"
                ),
                path=f"{path_prefix}.override.invoke_workflow.input_mapping",
            )
            issue["child_workflow_id"] = child_workflow_id
            issue["input_name"] = child_input_name
            issue["issue_type"] = "invoke_required_input_unmapped"
            errors.append(issue)
    for child_input_name in sorted(normalized_input_mapping):
        if child_input_names and child_input_name not in child_input_names:
            issue = _issue(
                code="invoke.input_mapping_unknown_key",
                message=(
                    f"invoke_workflow input mapping key '{child_input_name}' "
                    f"is not declared by child workflow '{child_workflow_id}'"
                ),
                path=f"{path_prefix}.override.invoke_workflow.input_mapping.{child_input_name}",
                severity="warning",
            )
            issue["child_workflow_id"] = child_workflow_id
            issue["input_name"] = child_input_name
            issue["issue_type"] = "invoke_input_mapping_unknown_key"
            warnings.append(issue)

    for child_input_name, source_selector in sorted(normalized_input_mapping.items()):
        if child_input_names and child_input_name not in child_input_names:
            continue
        source_type, resolve_error = _resolve_invoke_mapping_source_type(
            source_selector=source_selector,
            child_input_name=child_input_name,
            available_type_map=available_type_map,
        )
        # Bare dotted paths are not state selectors at the Invoke runtime boundary.
        if "." in source_selector and not source_selector.startswith(
            ("state.", "message.", LITERAL_PREFIX)
        ):
            source_type, resolve_error = None, "source_not_available"
        if resolve_error == "source_not_available" and source_selector.startswith("state."):
            problem = selector_shape.problem(str(step.get("step_id") or ""), source_selector)
            if problem:
                errors.append(
                    {
                        **_issue(
                            code="workflow.state_selector_shape_invalid",
                            message=problem,
                            path=f"{path_prefix}.override.invoke_workflow.input_mapping.{child_input_name}",
                        ),
                        "selector": source_selector,
                    }
                )
                continue
            if selector_shape.loop_member(str(step.get("step_id") or ""), source_selector):
                warnings.append(
                    _issue(
                        code="invoke.input_mapping_state_member_unchecked",
                        message=(
                            f"invoke_workflow input '{child_input_name}' reads '{source_selector}' from the current loop item. "
                            "This member's existence and type are unknown; the child validates the actual value at runtime."
                        ),
                        path=f"{path_prefix}.override.invoke_workflow.input_mapping.{child_input_name}",
                        severity="warning",
                    )
                )
                continue
        if resolve_error == "dynamic_message_selector":
            warnings.append(
                _issue(
                    code="invoke.input_mapping_dynamic_selector_unchecked",
                    message=(
                        f"invoke_workflow input '{child_input_name}' uses dynamic message selector "
                        f"'{source_selector}' which cannot be fully validated at save-time"
                    ),
                    path=f"{path_prefix}.override.invoke_workflow.input_mapping.{child_input_name}",
                    severity="warning",
                )
            )
            continue
        if resolve_error and resolve_error != "missing_source_selector":
            issue = _issue(
                code="invoke.input_mapping_source_unavailable",
                message=(
                    f"invoke_workflow input '{child_input_name}' maps to '{source_selector}', "
                    "but this source is not available from parent state/upstream steps"
                ),
                path=f"{path_prefix}.override.invoke_workflow.input_mapping.{child_input_name}",
            )
            source_parts = str(source_selector or "").strip().split(".")
            issue["child_workflow_id"] = child_workflow_id
            issue["input_name"] = child_input_name
            issue["issue_type"] = "invoke_input_mapping_source_unavailable"
            issue["source"] = source_selector
            if (
                len(source_parts) >= 5
                and source_parts[0] == "message"
                and source_parts[1] == "step"
            ):
                issue["source_step_id"] = source_parts[2]
                if source_parts[3] == "artifacts":
                    issue["source_name"] = ".".join(source_parts[4:])
                else:
                    issue["source_name"] = ".".join(source_parts[3:])
            errors.append(issue)
            continue
        if source_type == "json":
            source_type = selector_shape.source_type(
                str(step.get("step_id") or ""), source_selector
            )
        expected_type = child_input_types.get(child_input_name, "text")
        if source_type and not IOResolver._check_type_compat(expected_type, source_type):
            errors.append(
                _issue(
                    code="invoke.input_mapping_type_mismatch",
                    message=(
                        f"invoke_workflow input '{child_input_name}' expects type '{expected_type}' "
                        f"but source '{source_selector}' resolves as '{source_type}'"
                    ),
                    path=f"{path_prefix}.override.invoke_workflow.input_mapping.{child_input_name}",
                )
            )

    if child_interface.variable_outputs_required():
        for child_output_name in sorted(child_required_output_names):
            if child_output_name not in normalized_output_mapping:
                errors.append(
                    _issue(
                        code="invoke.required_output_unmapped",
                        message=(
                            f"Child output '{child_output_name}' is not mapped in "
                            "invoke_workflow.output_mapping"
                        ),
                        path=f"{path_prefix}.override.invoke_workflow.output_mapping",
                    )
                )
        for child_output_name in sorted(normalized_output_mapping):
            if child_output_names and child_output_name not in child_output_names:
                warnings.append(
                    _issue(
                        code="invoke.output_mapping_unknown_key",
                        message=(
                            f"invoke_workflow output mapping key '{child_output_name}' "
                            f"is not declared by child workflow '{child_workflow_id}'"
                        ),
                        path=f"{path_prefix}.override.invoke_workflow.output_mapping.{child_output_name}",
                        severity="warning",
                    )
                )
    elif normalized_output_mapping:
        warnings.append(
            _issue(
                code="invoke.output_mapping_unused",
                message=(
                    f"Child workflow '{child_workflow_id}' output_mode is "
                    f"'{child_interface.output_mode}', so output_mapping will not be required"
                ),
                path=f"{path_prefix}.override.invoke_workflow.output_mapping",
                severity="warning",
            )
        )

    if child_interface.artifacts_required():
        for child_artifact_name in sorted(child_required_artifact_names):
            if child_artifact_name not in normalized_artifact_mapping:
                errors.append(
                    _issue(
                        code="invoke.required_artifact_unmapped",
                        message=(
                            f"Child artifact '{child_artifact_name}' is not mapped in "
                            "invoke_workflow.artifact_mapping"
                        ),
                        path=f"{path_prefix}.override.invoke_workflow.artifact_mapping",
                    )
                )
        for child_artifact_name in sorted(normalized_artifact_mapping):
            if child_artifact_names and child_artifact_name not in child_artifact_names:
                warnings.append(
                    _issue(
                        code="invoke.artifact_mapping_unknown_key",
                        message=(
                            f"invoke_workflow artifact mapping key '{child_artifact_name}' "
                            f"is not declared by child workflow '{child_workflow_id}'"
                        ),
                        path=f"{path_prefix}.override.invoke_workflow.artifact_mapping.{child_artifact_name}",
                        severity="warning",
                    )
                )
    elif normalized_artifact_mapping:
        warnings.append(
            _issue(
                code="invoke.artifact_mapping_unused",
                message=(
                    f"Child workflow '{child_workflow_id}' output_mode is "
                    f"'{child_interface.output_mode}', so artifact_mapping will not be required"
                ),
                path=f"{path_prefix}.override.invoke_workflow.artifact_mapping",
                severity="warning",
            )
        )

    return {"valid": not errors, "errors": errors, "warnings": warnings}


__all__ = [name for name in globals() if not (name.startswith("__") and name.endswith("__"))]
