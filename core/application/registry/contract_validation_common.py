# ruff: noqa: F401
# mypy: ignore-errors
"""
Contract validation helpers for registry interconnection points.

Scope:
- Structured contract item sanity checks (name/type/required/source)
- Enable-time readiness checks for skills, agents, and workflows
- Workflow contract checks used by simulate/invoke guards
"""

from __future__ import annotations

import json
from typing import Any

from core.application.registry.io_resolver import IOResolver
from core.application.registry.workflow_contract import (
    SECRET_FIELD_TYPES,
    WorkflowInterface,
    interface_from_definition,
    normalize_field_type,
)
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
    STEP_KIND_STOP_FAIL,
    STEP_KIND_STOP_SUCCESS,
    STEP_KIND_SWITCH,
    STEP_KIND_TABULAR_FEED,
    STEP_KIND_TEMPLATE,
    STEP_KIND_TRANSFORM,
    SUPPORTED_STEP_KINDS,
    is_control_flow_step_kind,
    normalize_step_kind,
)

from .contract_item_validation import (
    ALLOWED_CONTRACT_TYPES,
    _issue,
    _normalize_name,
    _normalize_type,
)

STATE_PREFIX = "state."
WORKFLOW_CALLABLE_EXPECTED_INPUTS_VALUE = "workflow.callable.expected_inputs"
WORKFLOW_STEPS_VALUE = "workflow.steps"
WORKFLOW_STEP_IF_CONDITION_INVALID_VALUE = "workflow.step_if_condition_invalid"


LITERAL_PREFIX = "literal:"
PATH_TOOL_IMPLEMENTATION = "tool.implementation"
PATH_AGENT_SKILLS = "agent.skills"
PATH_AGENT_TOOLS = "agent.tools"
PATH_WORKFLOW_EXPECTED_OUTPUTS = "workflow.callable.expected_outputs"
PATH_WORKFLOW_RETURNED_FILES = "workflow.callable.returned_files"
IF_CONDITION_OPERATORS = {
    "equals",
    "not_equals",
    "exists",
    "truthy",
    "gt",
    "gte",
    "lt",
    "lte",
    "contains",
    "in",
    "not_in",
    "always_true",
    "always_false",
}
IF_CONDITION_CONSTANT_OPERATORS = {"always_true", "always_false"}
IF_CONDITION_VALUE_OPERATORS = {
    "equals",
    "not_equals",
    "gt",
    "gte",
    "lt",
    "lte",
    "contains",
    "in",
    "not_in",
}
REAL_EXECUTION_STEP_KINDS = {
    STEP_KIND_AGENT,
    STEP_KIND_CONNECTOR,
    STEP_KIND_INVOKE_WORKFLOW,
    STEP_KIND_HTTP_REQUEST,
    STEP_KIND_DATASET_QUERY,
    STEP_KIND_TRANSFORM,
    STEP_KIND_TABULAR_FEED,
    STEP_KIND_FILE_FROM_BASE64,
    STEP_KIND_TEMPLATE,
}


def _looks_like_template_string(value: Any) -> bool:
    text = str(value or "").strip()
    return bool(text) and "{{" in text and "}}" in text


def _is_source_reference(value: Any) -> bool:
    return (
        isinstance(value, dict)
        and isinstance(value.get("source"), str)
        and bool(str(value.get("source") or "").strip())
    )


def _is_valid_http_method_config(value: Any) -> bool:
    if _is_source_reference(value) or _looks_like_template_string(value):
        return True
    return str(value or "").strip().upper() in {
        "GET",
        "POST",
        "PUT",
        "PATCH",
        "DELETE",
        "HEAD",
        "OPTIONS",
    }


def _indexed_path(prefix: str, index: int, suffix: str = "") -> str:
    base = f"{prefix}[{index}]"
    if not suffix:
        return base
    return f"{base}.{suffix}".strip(".")


def _is_raw_mcp_connector_id(value: Any) -> bool:
    normalized = str(value or "").strip()
    return bool(normalized) and normalized.startswith("mcp_")


def _to_object(value: Any) -> dict:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            return {}
        try:
            parsed = json.loads(raw)
        except Exception:
            return {}
        if isinstance(parsed, dict):
            return parsed
    return {}


def _is_supported_if_condition_expression(value: str) -> bool:
    text = value.strip()
    if not text:
        return False
    lowered = text.lower()
    if lowered in {"true", "false", "null", "none", "yes", "no", "on", "off"}:
        return False
    try:
        parsed = json.loads(text)
    except Exception:
        parsed = None
    else:
        if isinstance(parsed, (bool, int, float)) or parsed is None:
            return False
    if text.startswith((STATE_PREFIX, "!state.")):
        return True
    return "==" in text or "!=" in text


def _if_condition_issues(condition: Any, path: str) -> list[dict]:
    if condition in (None, ""):
        return [
            _issue(
                code="workflow.step_if_condition_missing",
                message="if step requires override.if.condition",
                path=path,
            )
        ]
    if isinstance(condition, (bool, int, float)):
        return [
            _issue(
                code=WORKFLOW_STEP_IF_CONDITION_INVALID_VALUE,
                message=(
                    "if step condition must be a selector/operator object or "
                    "a supported state expression"
                ),
                path=path,
            )
        ]
    if isinstance(condition, str):
        if not condition.strip():
            return [
                _issue(
                    code="workflow.step_if_condition_missing",
                    message="if step requires override.if.condition",
                    path=path,
                )
            ]
        if _is_supported_if_condition_expression(condition):
            return []
        return [
            _issue(
                code=WORKFLOW_STEP_IF_CONDITION_INVALID_VALUE,
                message=(
                    "if step string condition must reference state or use a "
                    "supported comparison expression"
                ),
                path=path,
            )
        ]
    if not isinstance(condition, dict):
        return [
            _issue(
                code=WORKFLOW_STEP_IF_CONDITION_INVALID_VALUE,
                message="if step condition must be a supported expression or condition object",
                path=path,
            )
        ]

    if "selector" in condition or "operator" in condition:
        selector = str(condition.get("selector") or "").strip()
        operator = str(condition.get("operator") or "").strip().lower()
        issues: list[dict] = []
        if operator not in IF_CONDITION_CONSTANT_OPERATORS and not selector:
            issues.append(
                _issue(
                    code="workflow.step_if_condition_selector_missing",
                    message="if step condition requires selector",
                    path=f"{path}.selector",
                )
            )
        if operator not in IF_CONDITION_OPERATORS:
            issues.append(
                _issue(
                    code="workflow.step_if_condition_operator_invalid",
                    message="if step condition operator is not supported",
                    path=f"{path}.operator",
                )
            )
        if operator in IF_CONDITION_VALUE_OPERATORS and "value" not in condition:
            issues.append(
                _issue(
                    code="workflow.step_if_condition_value_missing",
                    message="if step condition operator requires value",
                    path=f"{path}.value",
                )
            )
        return issues

    matched_keys = [key for key in IF_CONDITION_OPERATORS if key in condition]
    if len(matched_keys) != 1:
        return [
            _issue(
                code=WORKFLOW_STEP_IF_CONDITION_INVALID_VALUE,
                message="if step condition must use one supported operator",
                path=path,
            )
        ]
    operator = matched_keys[0]
    operand = condition.get(operator)
    if operator in IF_CONDITION_CONSTANT_OPERATORS:
        return []
    if operator in {"exists", "truthy"}:
        if str(operand or "").strip():
            return []
        return [
            _issue(
                code="workflow.step_if_condition_selector_missing",
                message="if step condition requires selector",
                path=f"{path}.{operator}",
            )
        ]
    comparison = _to_object(operand)
    issues = []
    if not str(comparison.get("left") or "").strip():
        issues.append(
            _issue(
                code="workflow.step_if_condition_left_missing",
                message="if step condition comparison requires left operand",
                path=f"{path}.{operator}.left",
            )
        )
    if "right" not in comparison:
        issues.append(
            _issue(
                code="workflow.step_if_condition_right_missing",
                message="if step condition comparison requires right operand",
                path=f"{path}.{operator}.right",
            )
        )
    return issues


def _is_state_path(value: Any) -> bool:
    text = str(value or "").strip()
    return text == "state" or text.startswith(STATE_PREFIX)


def _extract_workflow_callable_contract(
    settings: Any,
) -> tuple[bool, list[dict] | None, list[dict] | None]:
    """Extract callable workflow contract rows from workflow.settings.

    Supported settings form:
    - {"callable": {"enabled": true, "expected_inputs": [...], "expected_outputs": [...]} }
    """
    interface = WorkflowInterface.from_settings(settings)
    return (
        bool(interface.enabled),
        [row.dump() for row in interface.expected_inputs],
        [row.dump() for row in interface.expected_outputs],
    )


def _workflow_interface_from_settings(settings: Any) -> WorkflowInterface:
    return WorkflowInterface.from_settings(settings)


def _workflow_interface_from_definition(workflow: Any) -> WorkflowInterface:
    """The interface including the files the workflow's steps return."""
    return interface_from_definition(workflow if isinstance(workflow, dict) else {})


def _append_issues_by_severity(
    *,
    issues: list[dict],
    errors: list[dict],
    warnings: list[dict],
) -> None:
    for issue in issues:
        if not isinstance(issue, dict):
            continue
        if str(issue.get("severity") or "error").strip().lower() == "warning":
            warnings.append(issue)
        else:
            errors.append(issue)


def _extract_invoke_workflow_spec(step: Any) -> dict | None:
    if not isinstance(step, dict):
        return None
    override = _to_object(step.get("override"))
    spec = override.get("invoke_workflow")
    if isinstance(spec, dict):
        return dict(spec)
    return None


def _extract_step_published_block_key(step: Any) -> str:
    if not isinstance(step, dict):
        return ""
    override = _to_object(step.get("override"))
    for key in ("published_block_key", "block_key"):
        value = str(override.get(key) or "").strip()
        if value:
            return value
    return ""


def _validate_step_published_block_binding(
    db,
    *,
    step: dict,
    step_path: str,
    agent_id: str,
    skill_id: str = "",
) -> list[dict]:
    """Validate that a step's published block matches the resolved runtime agent."""
    block_key = _extract_step_published_block_key(step)
    if not block_key:
        return []

    get_block = getattr(db, "get_published_block_contract", None)
    if not callable(get_block):
        return []

    try:
        block = get_block(block_key)
    except Exception:
        block = None
    if not isinstance(block, dict):
        return [
            _issue(
                code="workflow.step_published_block_missing",
                message=f"Step references missing published block '{block_key}'",
                path=f"{step_path}.override.published_block_key",
            )
        ]

    issues: list[dict] = []
    if not bool(block.get("enabled", True)):
        issues.append(
            _issue(
                code="workflow.step_published_block_disabled",
                message=f"Step references disabled published block '{block_key}'",
                path=f"{step_path}.override.published_block_key",
            )
        )

    source_kind = str(block.get("source_kind") or "").strip().lower()
    source_ref = str(block.get("source_ref") or "").strip()
    if source_kind != "agent":
        issues.append(
            _issue(
                code="workflow.step_published_block_source_kind_invalid",
                message=(
                    f"Step references block '{block_key}' with source_kind='{source_kind}', "
                    "but workflow steps require agent-backed blocks"
                ),
                path=f"{step_path}.override.published_block_key",
            )
        )
        return issues

    if not agent_id:
        return issues

    if "::" in source_ref:
        src_agent, src_skill = source_ref.split("::", 1)
        src_agent = str(src_agent or "").strip()
        src_skill = str(src_skill or "").strip()
        if src_agent != agent_id or (skill_id and src_skill != skill_id):
            issues.append(
                _issue(
                    code="workflow.step_published_block_pair_mismatch",
                    message=(
                        f"Step agent '{agent_id}' mismatches block '{block_key}' "
                        f"source_ref '{source_ref}'"
                    ),
                    path=f"{step_path}.override.published_block_key",
                )
            )
        return issues

    if source_ref and source_ref != agent_id:
        issues.append(
            _issue(
                code="workflow.step_published_block_pair_mismatch",
                message=(
                    f"Step agent '{agent_id}' mismatches block '{block_key}' "
                    f"source_ref '{source_ref}'"
                ),
                path=f"{step_path}.override.published_block_key",
            )
        )
    return issues


__all__ = [name for name in globals() if not (name.startswith("__") and name.endswith("__"))]
