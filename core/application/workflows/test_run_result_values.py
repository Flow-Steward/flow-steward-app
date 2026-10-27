"""Materialize workflow and step Test Run output values for result presentation."""

from __future__ import annotations

import copy
from collections.abc import Mapping
from typing import Any

from core.application.orchestration.state_paths import resolve_state_path_with_workflow_data
from core.application.orchestration.step_templating import set_state_path
from core.application.workflows.platform_test_run_common import STATE_PREFIX, _as_dict, _text


def _materialize_workflow_outputs(
    final_state: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    workflow_outputs = _as_dict(final_state.get("workflow_outputs"))
    outputs = _as_dict(workflow_outputs.get("outputs")) or _as_dict(final_state.get("outputs"))
    artifacts = _as_dict(workflow_outputs.get("artifacts")) or _as_dict(
        final_state.get("artifacts")
    )
    if (outputs or artifacts) and not workflow_outputs:
        workflow_outputs = {}
    if outputs and not isinstance(workflow_outputs.get("outputs"), dict):
        workflow_outputs["outputs"] = copy.deepcopy(outputs)
    if artifacts and not isinstance(workflow_outputs.get("artifacts"), dict):
        workflow_outputs["artifacts"] = copy.deepcopy(artifacts)
    return workflow_outputs, outputs, artifacts


def _step_test_result_artifacts(
    final_state: Mapping[str, Any], artifacts: Mapping[str, Any]
) -> dict[str, Any]:
    """Rehydrate compact deterministic-step output for the test-run drawer."""
    result = copy.deepcopy(dict(artifacts))
    if _text(result.get("step_kind")) == "template":
        # The drawer previews the whole text; the message may keep only a cut.
        template = _as_dict(result.get("template"))
        target = _text(template.get("result_target"))
        if template.get("ok") and target:
            value, found = resolve_state_path_with_workflow_data(
                dict(final_state), target.removeprefix(STATE_PREFIX)
            )
            if found:
                template["output"] = copy.deepcopy(value)
        result["template"] = template
    if isinstance(result.get("_state_patch"), dict):
        return result

    step_kind = _text(result.get("step_kind"))
    if step_kind in {"batch", "collect", "set_variable"}:
        targets: list[str] = []
        if step_kind == "batch":
            result_target = _text(result.get("result_target"))
            if result_target:
                targets.append(result_target)
        if step_kind in {"collect", "set_variable"}:
            result_targets = result.get("result_targets")
            if isinstance(result_targets, list):
                targets.extend(_text(target) for target in result_targets if _text(target))

        state_patch: dict[str, Any] = {}
        for target in dict.fromkeys(targets):
            selector = target.removeprefix(STATE_PREFIX)
            target_value, found = resolve_state_path_with_workflow_data(dict(final_state), selector)
            if not found:
                continue
            set_state_path(
                state_patch,
                dict(final_state),
                target,
                copy.deepcopy(target_value),
            )
        result["_state_patch"] = state_patch
        for key, value in state_patch.items():
            result.setdefault(key, copy.deepcopy(value))
        return result

    if step_kind not in {"transform", "tabular_feed"}:
        return result
    runtime_result = _as_dict(result.get(step_kind))
    result_target = _text(runtime_result.get("result_target"))
    state_patch: dict[str, Any] = {}
    hydrated_runtime_result = copy.deepcopy(runtime_result)
    found = False
    target_value: Any = None
    if result_target:
        selector = result_target.removeprefix(STATE_PREFIX)
        target_value, found = resolve_state_path_with_workflow_data(dict(final_state), selector)
    if found:
        set_state_path(
            state_patch,
            dict(final_state),
            result_target,
            copy.deepcopy(target_value),
        )
        hydrated_runtime_result["output" if step_kind == "transform" else "result"] = copy.deepcopy(
            target_value
        )
    elif step_kind == "tabular_feed":
        hydrated_runtime_result["result"] = {}
    hydrated_runtime_result["state_patch"] = copy.deepcopy(state_patch)
    result[step_kind] = hydrated_runtime_result
    result["_state_patch"] = state_patch
    return result
