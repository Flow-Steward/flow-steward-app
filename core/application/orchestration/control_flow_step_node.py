"""Runtime execution node for first-class workflow control-flow primitives."""

from __future__ import annotations

import copy
import json
from typing import TYPE_CHECKING, Any

from core.application.orchestration import control_flow_step_helpers as _helpers
from core.application.orchestration.active_step import active_step
from core.application.orchestration.state_path_guards import is_reserved_state_target
from core.application.orchestration.step_io_preview import (
    referenced_state_values,
    step_io_preview,
)
from core.application.orchestration.workflow_state_selector_contract import (
    RUNTIME_ERROR_CODE,
    validate_step_state_selectors,
)
from core.application.workflows.execution_graph import (
    OUTCOME_CASE,
    OUTCOME_DEFAULT,
    OUTCOME_FALSE,
    OUTCOME_TRUE,
)
from core.application.workflows.workflow_step_kinds import (
    STEP_KIND_BATCH,
    STEP_KIND_BREAK,
    STEP_KIND_COLLECT,
    STEP_KIND_CONTINUE,
    STEP_KIND_IF,
    STEP_KIND_SET_VARIABLE,
    STEP_KIND_STOP_FAIL,
    STEP_KIND_STOP_SUCCESS,
    STEP_KIND_SWITCH,
    is_control_flow_step_kind,
    normalize_step_kind,
)

if TYPE_CHECKING:
    from core.application.orchestration.state import OrchestratorState

STATE_PREFIX = _helpers.STATE_PREFIX
_now_iso = _helpers._now_iso
_to_object = _helpers._to_object
_resolve_state_path = _helpers._resolve_state_path
_has_runtime_value = _helpers._has_runtime_value
_resolve_runtime_public_path = _helpers._resolve_runtime_public_path
_resolve_selector_value = _helpers._resolve_selector_value
_parse_literal = _helpers._parse_literal
_resolve_operand = _helpers._resolve_operand
_coerce_comparable_number = _helpers._coerce_comparable_number
_evaluate_operator_condition = _helpers._evaluate_operator_condition
_evaluate_condition = _helpers._evaluate_condition
_condition_reason = _helpers._condition_reason
_set_path = _helpers._set_path
_is_set_variable_source_reference = _helpers._is_set_variable_source_reference
_resolve_set_variable_value = _helpers._resolve_set_variable_value
_set_variable_path = _helpers._set_variable_path
_coerce_positive_int = _helpers._coerce_positive_int
_strict_positive_int = _helpers._strict_positive_int
_resolve_collection_source = _helpers._resolve_collection_source
_resolve_switch_case = _helpers._resolve_switch_case
_append_control_message = _helpers._append_control_message


def _stop_output(override: dict[str, Any], step_kind: str) -> dict[str, Any]:
    """How a stop step ended the run, as Job Details shows its outputs."""
    spec = _to_object(override.get(step_kind))
    output: dict[str, Any] = {
        "workflow": "completed" if step_kind == STEP_KIND_STOP_SUCCESS else "failed"
    }
    for key in ("message", "reason"):
        text = str(spec.get(key) or "").strip()
        if text:
            output[key] = text
    return output


def _stop_reason(override: dict[str, Any], step_kind: str) -> str:
    """The author's words for why a stop step ended the run."""
    output = _stop_output(override, step_kind)
    return str(output.get("message") or output.get("reason") or "").strip() or (
        "a stop step completed the run"
        if step_kind == STEP_KIND_STOP_SUCCESS
        else "a stop step ended the run"
    )


def _retry_dispatch_matches(retry_dispatch: dict[str, Any], current: Any) -> bool:
    """Whether the host's operation retry dispatch is for exactly this attempt."""
    if not retry_dispatch or current is None:
        return False
    try:
        attempt = int(retry_dispatch.get("attempt") or 0)
    except (TypeError, ValueError):
        return False
    return (
        str(retry_dispatch.get("step_execution_id") or "").strip() == current.step_execution_id
        and attempt == current.attempt
    )


def control_flow_step_node(state: OrchestratorState) -> dict[str, Any]:
    """Execute control-flow and data-shaping workflow step kinds.

    The node only reports the step's result: an if/switch names the outcome it
    took in ``artifacts["_outcome"]`` and a stop step reports how it ends the
    run. The hub accepts the result and follows the execution graph.
    """
    state_dict = dict(state or {})
    current = active_step(state_dict)
    identity = current.message_identity() if current is not None else {}
    step = dict(current.step) if current is not None else {}
    step_kind = normalize_step_kind(step.get("step_kind"))
    step_id = current.step_id if current is not None else ""
    override = _to_object(step.get("override"))
    message_artifacts: dict[str, Any] = {"step_kind": step_kind}
    if step_id:
        message_artifacts["step_id"] = step_id
    step_display_name = str(step.get("display_name") or "").strip()
    if step_display_name:
        message_artifacts["step_display_name"] = step_display_name
    summary = f"Executed control-flow step '{step_kind}'"
    status = "pass"
    updates: dict[str, Any] = {}
    retry_dispatch = state_dict.get("operation_retry_dispatch")
    retry_dispatch = retry_dispatch if isinstance(retry_dispatch, dict) else {}
    dispatch_matches = _retry_dispatch_matches(retry_dispatch, current)

    def _reply(
        reply_status: str, reply_summary: str, extra: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        message = _append_control_message(
            state=state_dict,
            identity=identity,
            status=reply_status,
            summary=reply_summary,
            artifacts=message_artifacts,
        )
        return {
            **dict(extra or {}),
            "agent_messages": [*(state_dict.get("agent_messages") or []), message],
        }

    selector_issues = validate_step_state_selectors(step)
    if selector_issues:
        issue = selector_issues[0]
        message_artifacts.update(
            {
                "error": RUNTIME_ERROR_CODE,
                "selector": issue["selector"],
                "path": issue["path"].removeprefix("step."),
            }
        )
        return _reply("fail", "Control-flow step has an invalid input selector")

    unresolved_failure = state_dict.get("unresolved_step_failure")
    if step_kind == STEP_KIND_STOP_SUCCESS and isinstance(unresolved_failure, dict):
        # A run with an unresolved failure never ends as a success.
        message_artifacts.update(
            {
                "error": "unresolved_step_failure",
                "unresolved_step_failure": copy.deepcopy(unresolved_failure),
            }
        )
        return _reply(
            "fail",
            str(
                unresolved_failure.get("message") or "Workflow has an unresolved step failure."
            ).strip(),
        )

    if not is_control_flow_step_kind(step_kind):
        status = "fail"
        summary = f"Unsupported control-flow step kind '{step_kind}'"
        message_artifacts["error"] = "unsupported_step_kind"
    elif step_kind == STEP_KIND_IF:
        spec = _to_object(override.get("if"))
        condition = spec.get("condition")
        result = _evaluate_condition(condition, state_dict)
        outcome = {"name": OUTCOME_TRUE if result else OUTCOME_FALSE}
        message_artifacts.update(
            {
                "result": result,
                "_outcome": outcome,
                "input_payload": step_io_preview(
                    {
                        "condition": condition,
                        "values": referenced_state_values(condition, state_dict),
                    }
                ),
                "output_payload": {"result": result, "outcome": outcome["name"]},
            }
        )
        summary = f"if condition evaluated to {outcome['name']}"
        message_artifacts["summary_key"] = "react.jobs.step_if_condition_evaluated"
        message_artifacts["summary_params"] = {"result": outcome["name"]}
    elif step_kind == STEP_KIND_SWITCH:
        spec = _to_object(override.get("switch"))
        case, selector_value = _resolve_switch_case(spec, state_dict)
        if case is None:
            outcome = {"name": OUTCOME_DEFAULT}
            taken = "the default case"
        else:
            case_id = str(case["case_id"]).strip()
            outcome = {"name": OUTCOME_CASE, "case_id": case_id}
            taken = f"case '{case_id}'"
        output_payload: dict[str, Any] = dict(outcome)
        label = str((case or {}).get("label") or "").strip()
        if label:
            output_payload["label"] = label
        message_artifacts.update(
            {
                "selector": selector_value,
                "_outcome": outcome,
                "input_payload": step_io_preview(
                    {"selector": spec.get("selector"), "value": selector_value}
                ),
                "output_payload": output_payload,
            }
        )
        if selector_value is None:
            summary = f"switch selector has no value: {taken}"
            summary_key = (
                "react.jobs.step_switch_no_value_default"
                if case is None
                else "react.jobs.step_switch_no_value_case"
            )
            summary_params: dict[str, Any] = {}
        else:
            display_value = json.dumps(selector_value, ensure_ascii=False, default=str)
            summary = f"switch selector resolved to {display_value}: {taken}"
            summary_key = (
                "react.jobs.step_switch_value_default"
                if case is None
                else "react.jobs.step_switch_value_case"
            )
            summary_params = {"value": display_value}
        if case is not None:
            summary_params["case_id"] = case_id
        message_artifacts["summary_key"] = summary_key
        message_artifacts["summary_params"] = summary_params
    elif step_kind == STEP_KIND_SET_VARIABLE:
        spec = _to_object(override.get("set_variable"))
        assignments = spec.get("assignments")
        result_targets: list[str] = []
        if isinstance(assignments, dict) and assignments:
            blocked_targets = [
                str(target) for target in assignments if is_reserved_state_target(str(target))
            ]
            if blocked_targets:
                status = "fail"
                summary = "set_variable target is reserved"
                message_artifacts["error"] = "set_variable_reserved_target"
                message_artifacts["blocked_targets"] = blocked_targets
            else:
                for target, value in assignments.items():
                    _set_variable_path(updates, state_dict, str(target), value)
                    result_targets.append(str(target))
        else:
            target = str(spec.get("target") or "")
            if is_reserved_state_target(target):
                status = "fail"
                summary = "set_variable target is reserved"
                message_artifacts["error"] = "set_variable_reserved_target"
                message_artifacts["blocked_targets"] = [target]
            else:
                _set_variable_path(
                    updates,
                    state_dict,
                    target,
                    spec.get("value"),
                )
                if target:
                    result_targets.append(target)
        message_artifacts["assigned"] = sorted(updates.keys())
        message_artifacts["result_targets"] = sorted(result_targets)
        # Only the values a step copies from state are inputs; its targets are
        # where it writes.
        assigned_values = (
            list(assignments.values())
            if isinstance(assignments, dict) and assignments
            else [spec.get("value")]
        )
        read_values = referenced_state_values(assigned_values, state_dict)
        if read_values:
            message_artifacts["input_payload"] = step_io_preview(read_values)
        if result_targets:
            from core.application.orchestration.step_templating import _resolve_path

            written = {**state_dict, **updates}
            message_artifacts["output_payload"] = step_io_preview(
                {target: _resolve_path(written, target) for target in result_targets}
            )
        if status != "fail":
            summary = f"set_variable wrote {len(updates)} top-level key(s)"
            message_artifacts["summary_key"] = "react.jobs.step_variables_written"
            message_artifacts["summary_params"] = {"count": len(updates)}
    elif step_kind == STEP_KIND_COLLECT:
        from core.application.orchestration.collect_step_runtime import (
            CollectError,
            collect_state_update,
        )

        try:
            collected = collect_state_update(_to_object(override.get("collect")), state_dict)
        except CollectError as exc:
            status = "fail"
            summary = str(exc)
            message_artifacts["error"] = exc.code
            message_artifacts["failure_class"] = "contract"
            message_artifacts["retryable"] = False
        else:
            updates.update(collected.updates)
            # History keeps the target, the operation and a bounded preview of
            # this one contribution — never the growing collection itself.
            message_artifacts["collect"] = collected.summary
            message_artifacts["result_targets"] = [collected.summary["target"]]
            message_artifacts["input_payload"] = step_io_preview(collected.contribution)
            message_artifacts["output_payload"] = collected.summary
            summary = (
                f"collect added to {collected.summary['target']} "
                f"({collected.summary['count']} entr{'y' if collected.summary['count'] == 1 else 'ies'})"
            )
            message_artifacts["summary_key"] = (
                "react.jobs.step_collect_added_one"
                if collected.summary["count"] == 1
                else "react.jobs.step_collect_added"
            )
            message_artifacts["summary_params"] = {
                "target": collected.summary["target"],
                "count": collected.summary["count"],
            }
    elif step_kind == STEP_KIND_BATCH:
        spec = _to_object(override.get("batch"))
        source_value = _resolve_collection_source(state_dict, spec.get("source_path"))
        if source_value is None:
            status = "fail"
            summary = "batch source_path must resolve to a bounded array or dataset result"
            message_artifacts["error"] = "batch_source_missing"
        else:
            # Core does not know a provider's per-call maximum and must not guess
            # one, so the author owns batch_size. Coercing an unusable value to 1
            # silently turned a 20-call run into a 1000-call one, so it is refused.
            batch_size = _strict_positive_int(spec.get("batch_size"))
            emit_partial = bool(spec.get("emit_partial_batch", True))
            result_target = str(spec.get("result_target") or "").strip()
            if batch_size is None:
                status = "fail"
                summary = "batch requires a positive integer batch_size"
                message_artifacts["error"] = "batch_size_invalid"
                message_artifacts["batch_size"] = spec.get("batch_size")
            elif not result_target:
                status = "fail"
                summary = "batch requires result_target"
                message_artifacts["error"] = "batch_result_target_missing"
            else:
                from core.application.orchestration.dataset_cursor_runtime import (
                    DatasetCursorError,
                    execute_batch_contract,
                )

                try:
                    batch_result = execute_batch_contract(
                        source_value,
                        batch_size=batch_size,
                        emit_partial_batch=emit_partial,
                        state=state_dict,
                    )
                except DatasetCursorError as exc:
                    status = "fail"
                    summary = "batch source violates the bounded collection contract"
                    message_artifacts["error"] = exc.code
                else:
                    _set_path(updates, state_dict, result_target, batch_result["value"])
                    message_artifacts.update(
                        {key: value for key, value in batch_result.items() if key != "value"}
                    )
                    message_artifacts.update(
                        {
                            "result_target": result_target,
                            "batch_size": batch_size,
                            "emit_partial_batch": emit_partial,
                            "input_payload": step_io_preview(
                                {
                                    "source": spec.get("source_path"),
                                    "items": batch_result.get("item_count"),
                                    "batch_size": batch_size,
                                }
                            ),
                            "output_payload": step_io_preview(
                                {
                                    "result_target": result_target,
                                    "batches": batch_result.get("batch_count"),
                                    "batch_sizes": [
                                        len(batch) if isinstance(batch, list) else 1
                                        for batch in (batch_result["value"] or [])[:20]
                                    ]
                                    if isinstance(batch_result["value"], list)
                                    else None,
                                }
                            ),
                        }
                    )
                    if batch_result.get("dataset_handle"):
                        summary = (
                            f"batch planned {batch_result['item_count']} dataset row(s) "
                            f"across {batch_result['batch_count']} page(s)"
                        )
                    else:
                        summary = (
                            f"batch chunked {batch_result['item_count']} item(s) "
                            f"into {batch_result['batch_count']} batch(es)"
                        )
    elif step_kind in {STEP_KIND_BREAK, STEP_KIND_CONTINUE}:
        # The hub ends the item (Continue) or the For each (Break); the step
        # only records that it was reached, for this item's history.
        frame = current.for_each_frame if current is not None else None
        position = frame.item_index if frame is not None else None
        message_artifacts.update(
            {
                "loop_control": step_kind,
                "output_payload": {"action": step_kind, "item_index": position},
            }
        )
        summary = (
            "Break: leave the For each without the remaining items"
            if step_kind == STEP_KIND_BREAK
            else "Continue: skip the rest of this item"
        )
        message_artifacts["summary_key"] = f"react.jobs.step_{step_kind}_reached"
    elif step_kind in {STEP_KIND_STOP_SUCCESS, STEP_KIND_STOP_FAIL}:
        # The hub records the terminal status when it accepts this result.
        succeeded = step_kind == STEP_KIND_STOP_SUCCESS
        message_artifacts.update(
            {
                "terminal": "pass" if succeeded else "fail",
                "reason": _stop_reason(override, step_kind),
                "output_payload": _stop_output(override, step_kind),
            }
        )
        summary = f"{step_kind} {'completed' if succeeded else 'failed'} the workflow"
        if succeeded:
            message_artifacts["summary_key"] = "react.jobs.step_success_finished"

    if status != "pass":
        return _reply(
            "fail", summary, {"operation_retry_dispatch": None} if dispatch_matches else None
        )
    if dispatch_matches:
        updates["operation_retry_dispatch"] = None
    return _reply("pass", summary, updates)
