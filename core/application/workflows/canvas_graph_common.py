from __future__ import annotations

import json
from typing import Any

from core.application.workflows.workflow_step_kinds import normalize_step_kind


def _step_config_summary_metadata(step: dict[str, Any], *, workflow_id: str) -> dict[str, Any]:
    step_kind = normalize_step_kind(step.get("step_kind"), preserve_unknown=True)
    if step_kind == "agent":
        agent_id = str(step.get("agent_id") or "").strip()
        participant_key = str(step.get("participant_key") or "").strip()
        label = participant_key or str(step.get("agent_display_name") or "").strip() or agent_id
        return (
            {
                "config_summary_key": "react.workflows.agent_summary",
                "config_summary_params": {"label": label},
            }
            if label
            else {"config_summary_key": "react.workflows.agent_default_summary"}
        )
    if step_kind == "invoke_workflow":
        config = _to_object(_to_object(step.get("override")).get("invoke_workflow"))
        hook = _to_object(config.get("hook"))
        if hook:
            # A hook names its place by the author's business name; the project picks the child.
            return {
                "config_summary_key": "react.workflows.invoke_hook_summary",
                "config_summary_params": {
                    "hook": str(hook.get("display_name") or hook.get("hook_id") or "").strip()
                },
            }
        child_workflow_id = str(config.get("workflow_id") or step.get("workflow_id") or "").strip()
        return {
            "config_summary_key": "react.workflows.invoke_workflow_summary"
            if child_workflow_id
            else "react.workflows.invoke_workflow_default_summary",
            "config_summary_params": {"workflow_id": child_workflow_id or workflow_id},
        }
    if step_kind == "batch":
        config = _to_object(_to_object(step.get("override")).get("batch"))
        source = str(config.get("source_path") or "").strip()
        batch_size = int(config.get("batch_size") or 1)
        return {
            "config_summary_key": "react.workflows.batch_summary"
            if source
            else "react.workflows.batch_default_summary",
            "config_summary_params": {"source": source, "batch_size": batch_size},
        }
    if step_kind == "collect":
        config = _to_object(_to_object(step.get("override")).get("collect"))
        target = str(config.get("target") or "").strip()
        source = str(config.get("source") or "").strip()
        set_key = config.get("mode") == "set_key"
        if not source and not target:
            return {
                "config_summary_key": "react.workflows.step_not_set_up_summary",
                "config_summary_params": {},
            }
        return {
            "config_summary_key": "react.workflows.collect_summary_set_key"
            if set_key
            else "react.workflows.collect_summary_append",
            "config_summary_params": {"source": source, "target": target},
        }
    if step_kind == "break":
        return {"config_summary_key": "react.workflows.break_summary"}
    if step_kind == "continue":
        return {"config_summary_key": "react.workflows.continue_summary"}
    if step_kind == "set_variable":
        config = _to_object(_to_object(step.get("override")).get("set_variable"))
        assignments = config.get("assignments")
        if isinstance(assignments, dict) and assignments:
            if len(assignments) <= 3:
                return {}
            parts = [
                f"{target} = {_summarize_value(value)}"
                for target, value in list(assignments.items())[:3]
            ]
            return {
                "config_summary_key": "react.workflows.set_variable_more_summary",
                "config_summary_params": {
                    "assignments": "; ".join(parts),
                    "extra_count": len(assignments) - 3,
                },
            }
        target = str(config.get("target") or "").strip()
        if not target and config.get("value") is None:
            return {
                "config_summary_key": "react.workflows.step_not_set_up_summary",
                "config_summary_params": {},
            }
        return (
            {}
            if target
            else {
                "config_summary_key": "react.workflows.set_variable_default_summary",
                "config_summary_params": {"value": _summarize_value(config.get("value"))},
            }
        )
    if step_kind == "file_from_base64":
        config = _to_object(_to_object(step.get("override")).get("file_from_base64"))
        source = str(config.get("source") or "").strip()
        filename = str(config.get("filename") or "").strip()
        return {
            "config_summary_key": "react.workflows.base64_file_named_summary"
            if filename
            else "react.workflows.base64_file_summary",
            "config_summary_params": {"source": source or "base64", "filename": filename},
        }
    if step_kind == "template":
        config = _to_object(_to_object(step.get("override")).get("template"))
        target = str(config.get("result_target") or "").strip()
        return {
            "config_summary_key": "react.workflows.template_summary"
            if target
            else "react.workflows.template_default_target_summary",
            "config_summary_params": {
                "format": str(config.get("format") or "text"),
                "target": target,
            },
        }
    if step_kind == "stop_success":
        return {"config_summary_key": "react.workflows.stop_workflow_success_summary"}
    if step_kind == "stop_fail":
        return {"config_summary_key": "react.workflows.stop_workflow_failure_summary"}
    if step_kind == "if":
        config = _to_object(_to_object(step.get("override")).get("if"))
        return (
            {}
            if _if_condition_summary(config)
            else {"config_summary_key": "react.workflows.if_default_summary"}
        )
    if step_kind == "switch":
        config = _to_object(_to_object(step.get("override")).get("switch"))
        selector = str(config.get("selector") or "").strip()
        cases = config.get("cases")
        case_count = len(cases) if isinstance(cases, (list, dict)) else 0
        return {
            "config_summary_key": "react.workflows.switch_selector_summary"
            if selector
            else "react.workflows.switch_cases_summary",
            "config_summary_params": {"selector": selector, "case_count": case_count},
        }
    if step_kind == "transform":
        config = _to_object(_to_object(step.get("override")).get("transform"))
        source = str(_to_object(config.get("input")).get("source") or "").strip()
        target = str(config.get("result_target") or "").strip()
        fields = config.get("fields")
        field_count = len(fields) if isinstance(fields, list) else 0
        if source and target:
            key = "react.workflows.transform_summary"
        elif target:
            key = "react.workflows.transform_summary_default_source"
        elif source:
            key = "react.workflows.transform_summary_default_target"
        else:
            key = "react.workflows.transform_summary_defaults"
        return {
            "config_summary_key": key,
            "config_summary_params": {
                "source": source,
                "target": target,
                "field_count": field_count,
            },
        }
    return {}


def _if_condition_summary(config: dict[str, Any]) -> str | None:
    condition = config.get("condition")
    if condition is None or condition == "":
        selector = config.get("selector")
        return selector.strip() if isinstance(selector, str) and selector.strip() else None
    if isinstance(condition, str):
        return condition.strip() or None
    if not isinstance(condition, dict):
        return None

    operators = {
        "equals": "=",
        "not_equals": "≠",
        "gt": ">",
        "gte": "≥",
        "lt": "<",
        "lte": "≤",
        "contains": "contains",
        "in": "in",
        "not_in": "not in",
    }

    def literal(value: Any) -> str | None:
        if isinstance(value, str):
            selector = value.strip()
            if selector == "state" or selector.startswith("state."):
                return selector
        try:
            return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        except (TypeError, ValueError):
            return None

    if "selector" in condition or "operator" in condition:
        operator = condition.get("operator")
        if not isinstance(operator, str):
            return None
        operator = operator.strip().lower()
        selector = condition.get("selector")
        if operator in {"always_true", "always_false"}:
            if set(condition) == {"operator"} or (
                set(condition) == {"selector", "operator"}
                and isinstance(selector, str)
                and selector.strip()
            ):
                return "always true" if operator == "always_true" else "always false"
            return None
        if not isinstance(selector, str) or not selector.strip():
            return None
        selector = selector.strip()
        if operator in {"exists", "truthy"}:
            return f"{selector} {operator}" if set(condition) == {"selector", "operator"} else None
        token = operators.get(operator)
        if not token or set(condition) != {"selector", "operator", "value"}:
            return None
        value = literal(condition["value"])
        return f"{selector} {token} {value}" if value is not None else None

    if len(condition) != 1:
        return None
    operator, operand = next(iter(condition.items()))
    if operator in {"exists", "truthy"}:
        return (
            f"{operand.strip()} {operator}"
            if isinstance(operand, str) and operand.strip()
            else None
        )
    token = operators.get(operator)
    if not token or not isinstance(operand, dict) or set(operand) != {"left", "right"}:
        return None
    left = operand.get("left")
    right = literal(operand["right"])
    return (
        f"{left.strip()} {token} {right}"
        if isinstance(left, str) and left.strip() and right is not None
        else None
    )


def _step_config_summary(step: dict[str, Any], *, workflow_id: str) -> str:
    step_kind = normalize_step_kind(step.get("step_kind"), preserve_unknown=True)
    override = _to_object(step.get("override"))
    if step_kind == "agent":
        agent_id = str(step.get("agent_id") or "").strip()
        participant_key = str(step.get("participant_key") or "").strip()
        label = participant_key or str(step.get("agent_display_name") or "").strip() or agent_id
        return f"agent: {label}" if label else "agent step"
    if step_kind == "connector":
        cfg = _to_object(override.get("connector"))
        connector_id = str(cfg.get("connector_id") or step.get("connector_id") or "").strip()
        operation_id = str(cfg.get("operation_id") or cfg.get("function_name") or "").strip()
        summary = connector_id or "connector"
        if operation_id:
            summary = f"{summary} -> {operation_id}"
        return summary
    if step_kind == "invoke_workflow":
        cfg = _to_object(override.get("invoke_workflow"))
        hook = _to_object(cfg.get("hook"))
        if hook:
            return f"Hook: {str(hook.get('display_name') or hook.get('hook_id') or '').strip()}"
        child_workflow_id = str(cfg.get("workflow_id") or step.get("workflow_id") or "").strip()
        return f"invoke {child_workflow_id}" if child_workflow_id else f"invoke from {workflow_id}"
    if step_kind == "if":
        cfg = _to_object(override.get("if"))
        return _if_condition_summary(cfg) or "conditional branch"
    if step_kind == "switch":
        cfg = _to_object(override.get("switch"))
        selector = str(cfg.get("selector") or "").strip()
        cases = cfg.get("cases")
        case_count = len(cases) if isinstance(cases, (list, dict)) else 0
        if selector:
            return f"{selector} ({case_count} cases)"
        return f"{case_count} switch cases"
    if step_kind == "collect":
        cfg = _to_object(override.get("collect"))
        source = str(cfg.get("source") or "").strip()
        target = str(cfg.get("target") or "").strip()
        if not source and not target:
            # A placeholder like "value -> variable" reads as configuration.
            return "Not set up yet"
        source, target = source or "value", target or "variable"
        if cfg.get("mode") == "set_key":
            return f"{target}[key] = {source}"
        return f"{source} → {target}"
    if step_kind == "break":
        return "Leave the For each"
    if step_kind == "continue":
        return "Next item"
    if step_kind == "batch":
        cfg = _to_object(override.get("batch"))
        source_path = str(cfg.get("source_path") or "").strip()
        batch_size = int(cfg.get("batch_size") or 1)
        return f"{source_path or 'items'} in batches of {batch_size}"
    if step_kind == "set_variable":
        cfg = _to_object(override.get("set_variable"))
        assignments = cfg.get("assignments")
        if isinstance(assignments, dict) and assignments:
            parts = [
                f"{target} = {_summarize_value(value)}"
                for target, value in list(assignments.items())[:3]
            ]
            if len(assignments) > 3:
                parts.append(f"+{len(assignments) - 3} more")
            return "; ".join(parts)
        target = str(cfg.get("target") or "").strip()
        value = cfg.get("value")
        if not target and value is None:
            # A new step has no assignment yet; "variable = null" read as one.
            return "Not set up yet"
        return f"{target or 'variable'} = {_summarize_value(value)}"
    if step_kind == "http_request":
        cfg = _to_object(override.get("http_request"))
        method = str(cfg.get("method") or "GET").strip().upper()
        url = str(cfg.get("url") or cfg.get("endpoint") or "").strip()
        return f"{method} {url}".strip()
    if step_kind == "transform":
        cfg = _to_object(override.get("transform"))
        source = str(_to_object(cfg.get("input")).get("source") or "").strip()
        target = str(cfg.get("result_target") or "").strip()
        field_count = len(cfg.get("fields") or []) if isinstance(cfg.get("fields"), list) else 0
        summary = f"{source or 'source'} -> {target or 'result'}"
        return f"{summary} ({field_count} fields)"
    if step_kind == "file_from_base64":
        cfg = _to_object(override.get("file_from_base64"))
        source = str(cfg.get("source") or "").strip()
        name = str(cfg.get("filename") or "").strip()
        return f"{source or 'base64'} -> file {name}".strip()
    if step_kind == "template":
        cfg = _to_object(override.get("template"))
        target = str(cfg.get("result_target") or "").strip()
        return f"{cfg.get('format') or 'text'} template -> {target or 'text'}"
    if step_kind == "stop_success":
        return "stop workflow (success)"
    if step_kind == "stop_fail":
        return "stop workflow (failure)"
    return step_kind or "step"


def _summarize_value(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        return value.strip() or '""'
    if isinstance(value, list):
        items = [_summarize_value(item) for item in value[:3]]
        suffix = ", ..." if len(value) > 3 else ""
        return f"[{', '.join(items)}{suffix}]"
    if isinstance(value, dict):
        pairs = [f"{key}: {_summarize_value(item)}" for key, item in list(value.items())[:3]]
        suffix = ", ..." if len(value) > 3 else ""
        return f"{{{', '.join(pairs)}{suffix}}}"
    return str(value).strip() or "value"


def _to_object(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _empty_scope_summary() -> dict[str, Any]:
    return {
        "issue_count": 0,
        "error_count": 0,
        "warning_count": 0,
        "rule_count": 0,
        "enabled_count": 0,
        "event_types": [],
        "action_types": [],
    }
