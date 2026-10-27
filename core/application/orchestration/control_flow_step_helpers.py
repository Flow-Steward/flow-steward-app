# mypy: ignore-errors
"""Helper routines for first-class workflow control-flow steps."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from typing import Any

from core.application.orchestration.state_path_guards import is_reserved_state_target
from core.application.orchestration.state_paths import (
    path_segments,
    resolve_state_path_with_workflow_data,
)
from core.application.orchestration.step_templating import set_state_path

STATE_PREFIX = "state."


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _to_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return {}
        try:
            parsed = json.loads(text)
        except Exception:
            return {}
        return dict(parsed) if isinstance(parsed, dict) else {}
    return {}


def _resolve_state_path(state: dict[str, Any], path: str) -> Any:
    parts = path_segments(path)

    resolved, found = resolve_state_path_with_workflow_data(state, path)
    if found or not parts or parts[0] == "workflow_data":
        return resolved
    return _resolve_runtime_public_path(state, parts)


def _has_runtime_value(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, tuple, set, dict)):
        return len(value) > 0
    return True


def _resolve_runtime_public_path(state: dict[str, Any], parts: list[str]) -> Any:
    if not parts:
        return None
    root_field = parts[0]

    def _walk(root: Any) -> Any:
        cursor: Any = root
        for part in parts:
            if not isinstance(cursor, dict):
                return None
            cursor = cursor.get(part)
            if cursor is None:
                return None
        return cursor

    def _walk_from_field(root: Any) -> Any:
        cursor: Any = root
        for part in parts[1:]:
            if not isinstance(cursor, dict):
                return None
            cursor = cursor.get(part)
            if cursor is None:
                return None
        return cursor

    for container_name in ("outputs", "artifacts"):
        container = state.get(container_name)
        if isinstance(container, dict):
            direct = _walk(container)
            if _has_runtime_value(direct):
                return direct
            if root_field in container:
                nested = _walk_from_field(container.get(root_field))
                if _has_runtime_value(nested):
                    return nested

    workflow_data = state.get("workflow_data")
    if isinstance(workflow_data, dict):
        for container_name in ("outputs", "artifacts"):
            container = workflow_data.get(container_name)
            if isinstance(container, dict):
                direct = _walk(container)
                if _has_runtime_value(direct):
                    return direct
                if root_field in container:
                    nested = _walk_from_field(container.get(root_field))
                    if _has_runtime_value(nested):
                        return nested

    messages = state.get("agent_messages")
    for msg in reversed(messages if isinstance(messages, list) else []):
        if not isinstance(msg, dict):
            continue
        artifacts = msg.get("artifacts")
        artifacts = artifacts if isinstance(artifacts, dict) else {}
        direct = _walk(artifacts)
        if _has_runtime_value(direct):
            return direct
        if root_field in artifacts:
            nested = _walk_from_field(artifacts.get(root_field))
            if _has_runtime_value(nested):
                return nested

        state_patch = artifacts.get("_state_patch")
        state_patch = state_patch if isinstance(state_patch, dict) else {}
        direct = _walk(state_patch)
        if _has_runtime_value(direct):
            return direct
        if root_field in state_patch:
            nested = _walk_from_field(state_patch.get(root_field))
            if _has_runtime_value(nested):
                return nested

        workflow_outputs = artifacts.get("workflow_outputs")
        workflow_outputs = workflow_outputs if isinstance(workflow_outputs, dict) else {}
        for container_name in ("outputs", "artifacts"):
            container = workflow_outputs.get(container_name)
            if isinstance(container, dict):
                direct = _walk(container)
                if _has_runtime_value(direct):
                    return direct
                if root_field in container:
                    nested = _walk_from_field(container.get(root_field))
                    if _has_runtime_value(nested):
                        return nested

        workflow_terminal = artifacts.get("workflow_terminal")
        workflow_terminal = workflow_terminal if isinstance(workflow_terminal, dict) else {}
        for container_name in ("outputs", "artifacts"):
            container = workflow_terminal.get(container_name)
            if isinstance(container, dict):
                direct = _walk(container)
                if _has_runtime_value(direct):
                    return direct
                if root_field in container:
                    nested = _walk_from_field(container.get(root_field))
                    if _has_runtime_value(nested):
                        return nested

    return None


def _resolve_selector_value(selector: Any, state: dict[str, Any]) -> Any:
    if isinstance(selector, str):
        token = selector.strip()
        if token.startswith(STATE_PREFIX):
            return _resolve_state_path(state, token[len(STATE_PREFIX) :])
        if token == "state":
            return state
    return selector


def _parse_literal(token: str) -> Any:
    text = token.strip()
    if not text:
        return ""
    try:
        return json.loads(text)
    except Exception:
        return text.strip("'\"")


def _resolve_operand(token: str, state: dict[str, Any]) -> Any:
    text = token.strip()
    if text.startswith(STATE_PREFIX):
        return _resolve_state_path(state, text[len(STATE_PREFIX) :])
    if text == "state":
        return state
    return _parse_literal(text)


def _coerce_comparable_number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


def _evaluate_operator_condition(left: Any, operator: str, right: Any = None) -> bool:
    normalized = str(operator or "").strip().lower()
    if normalized in {"eq", "equals", "=="}:
        return left == right
    if normalized in {"ne", "not_equals", "!="}:
        return left != right
    if normalized == "exists":
        return left is not None
    if normalized == "truthy":
        return bool(left)
    if normalized in {"always_true", "constant_true"}:
        return True
    if normalized in {"always_false", "constant_false"}:
        return False
    if normalized in {"gt", "gte", "lt", "lte"}:
        left_number = _coerce_comparable_number(left)
        right_number = _coerce_comparable_number(right)
        if left_number is None or right_number is None:
            return False
        if normalized == "gt":
            return left_number > right_number
        if normalized == "gte":
            return left_number >= right_number
        if normalized == "lt":
            return left_number < right_number
        return left_number <= right_number
    if normalized == "contains":
        if left is None:
            return False
        if isinstance(left, dict):
            return right in left
        if isinstance(left, str):
            return str(right) in left
        try:
            return right in left
        except TypeError:
            return False
    if normalized == "in":
        if right is None:
            return False
        if isinstance(right, dict):
            return left in right
        if isinstance(right, str):
            return str(left) in right
        try:
            return left in right
        except TypeError:
            return False
    if normalized == "not_in":
        return not _evaluate_operator_condition(left, "in", right)
    return False


def _evaluate_condition(condition: Any, state: dict[str, Any]) -> bool:
    condition = _condition_expression(condition)
    if isinstance(condition, bool):
        return condition
    if condition in (None, ""):
        return False
    if isinstance(condition, (int, float)):
        return bool(condition)
    if isinstance(condition, dict):
        if "selector" in condition or "operator" in condition:
            selector = condition.get("selector")
            operator = str(condition.get("operator") or "truthy").strip().lower()
            left = _resolve_selector_value(selector, state)
            right = _resolve_selector_value(condition.get("value"), state)
            return _evaluate_operator_condition(left, operator, right)
        if "equals" in condition and isinstance(condition.get("equals"), dict):
            eq = condition.get("equals") or {}
            return _evaluate_operator_condition(
                _resolve_selector_value(eq.get("left"), state),
                "equals",
                _resolve_selector_value(eq.get("right"), state),
            )
        if "not_equals" in condition and isinstance(condition.get("not_equals"), dict):
            ne = condition.get("not_equals") or {}
            return _evaluate_operator_condition(
                _resolve_selector_value(ne.get("left"), state),
                "not_equals",
                _resolve_selector_value(ne.get("right"), state),
            )
        for operator in ("gt", "gte", "lt", "lte", "contains", "in", "not_in"):
            comparison = condition.get(operator)
            if isinstance(comparison, dict):
                return _evaluate_operator_condition(
                    _resolve_selector_value(comparison.get("left"), state),
                    operator,
                    _resolve_selector_value(comparison.get("right"), state),
                )
        if "exists" in condition:
            return _evaluate_operator_condition(
                _resolve_selector_value(condition.get("exists"), state), "exists"
            )
        if "truthy" in condition:
            return _evaluate_operator_condition(
                _resolve_selector_value(condition.get("truthy"), state), "truthy"
            )
    text = str(condition).strip()
    if not text:
        return False
    if "==" in text:
        left, right = text.split("==", 1)
        return _resolve_operand(left, state) == _resolve_operand(right, state)
    if "!=" in text:
        left, right = text.split("!=", 1)
        return _resolve_operand(left, state) != _resolve_operand(right, state)
    if text.startswith("!state."):
        return not bool(_resolve_state_path(state, text[len("!state.") :]))
    if text.startswith(STATE_PREFIX):
        return bool(_resolve_state_path(state, text[len(STATE_PREFIX) :]))
    return bool(_parse_literal(text))


def _condition_expression(condition: Any) -> Any:
    if isinstance(condition, dict) and "condition" in condition:
        return condition.get("condition")
    return condition


def _condition_reason(condition: Any, default: str) -> str:
    if isinstance(condition, dict):
        for key in ("reason", "message", "label"):
            value = str(condition.get(key) or "").strip()
            if value:
                return value
    return default


def _set_path(update: dict[str, Any], state: dict[str, Any], target: str, value: Any) -> None:
    set_state_path(update, state, target, value)


def _is_set_variable_source_reference(value: Any) -> bool:
    if not isinstance(value, dict) or set(value.keys()) != {"source"}:
        return False
    source = value.get("source")
    if not isinstance(source, str):
        return False
    normalized = source.strip()
    return normalized == "state" or normalized.startswith(STATE_PREFIX)


def _resolve_set_variable_value(value: Any, state: dict[str, Any]) -> Any:
    if _is_set_variable_source_reference(value):
        return _resolve_selector_value(str(value.get("source") or "").strip(), state)
    return value


def _set_variable_path(
    update: dict[str, Any],
    state: dict[str, Any],
    target: str,
    value: Any,
) -> None:
    if is_reserved_state_target(target):
        raise ValueError("set_variable_reserved_target")
    set_state_path(update, state, target, _resolve_set_variable_value(value, state))


def _strict_positive_int(value: Any) -> int | None:
    """Return a positive whole number, or None when the value cannot be one.

    Unlike ``_coerce_positive_int`` this does not substitute a default: for a
    batch size, silently falling back changes how many provider calls a run makes.
    """
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if isinstance(value, float):
        return int(value) if value.is_integer() and value > 0 else None
    raw = str(value).strip()
    if not raw.isdigit():
        return None
    parsed = int(raw)
    return parsed if parsed > 0 else None


def _coerce_positive_int(value: Any, default: int) -> int:
    try:
        parsed = int(value)
        if parsed > 0:
            return parsed
    except (TypeError, ValueError):
        pass
    return default


def _resolve_array_source(state: dict[str, Any], source_path: Any) -> list[Any] | None:
    path = str(source_path or "").strip()
    if not path:
        return None
    selector = path if path.startswith(STATE_PREFIX) else f"state.{path}"
    value = _resolve_selector_value(selector, state)
    return value if isinstance(value, list) else None


def _resolve_collection_source(state: dict[str, Any], source_path: Any) -> Any:
    path = str(source_path or "").strip()
    if not path:
        return None
    selector = path if path.startswith(STATE_PREFIX) else f"state.{path}"
    return _resolve_selector_value(selector, state)


def _chunk_items(items: list[Any], batch_size: int, emit_partial_batch: bool) -> list[list[Any]]:
    if batch_size <= 0:
        return []
    chunks = [items[idx : idx + batch_size] for idx in range(0, len(items), batch_size)]
    if chunks and not emit_partial_batch and len(chunks[-1]) < batch_size:
        chunks = chunks[:-1]
    return chunks


def _switch_value_key(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _resolve_switch_case(
    spec: dict[str, Any], state: dict[str, Any]
) -> tuple[dict[str, Any] | None, Any]:
    """The first case whose ``when`` equals the selector value, or None for default.

    The case only names an outcome; where it leads is the execution graph's edge
    for that case id.
    """
    selector_value = _resolve_selector_value(spec.get("selector"), state)
    selected_key = _switch_value_key(selector_value)
    cases = spec.get("cases")
    for case in cases if isinstance(cases, list) else []:
        if not isinstance(case, dict) or not str(case.get("case_id") or "").strip():
            continue
        if _switch_value_key(case.get("when")) == selected_key:
            return case, selector_value
    return None, selector_value


def _append_control_message(
    *,
    state: dict[str, Any],
    identity: dict[str, Any],
    status: str,
    summary: str,
    artifacts: dict[str, Any],
) -> dict[str, Any]:
    from core.application.orchestration.agent_messages import build_agent_message
    from core.application.orchestration.agents.common import publish_agent_message

    timestamp = _now_iso()
    message = build_agent_message(
        from_agent="control_flow_step",
        to_agent="orchestrator",
        skill="control_flow",
        status=status,
        correlation_id=uuid.uuid4().hex,
        caused_by=[],
        artifacts=artifacts,
        summary=summary,
        errors=[] if status == "pass" else [summary],
        started_at=timestamp,
        completed_at=timestamp,
        published_at=timestamp,
        **identity,
    )
    publish_agent_message(state, message)
    return message
