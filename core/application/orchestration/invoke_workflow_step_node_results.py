"""Child output, artifact, and state-patch helpers for invoke_workflow_step_node."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from core.application.orchestration.invoke_workflow_step_node_common import (
    STATE_TARGET_PREFIX,
    _extract_envelope,
    _json_object,
)
from core.application.orchestration.step_templating import set_state_path

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from core.application.orchestration.state import OrchestratorState


def _collect_child_outputs(
    bl, child_job_id: str, declared_output_names: list[str]
) -> dict[str, Any]:
    def _collect_from_output_container(container: Any, output_name: str) -> tuple[bool, Any]:
        payload = _json_object(container)
        if not payload:
            return False, None
        if output_name in payload:
            return True, payload.get(output_name)

        for outer_key in (
            "output",
            "outputs",
            "output_state",
            "state_output",
            "workflow_output",
            "workflow_outputs",
            "result",
            "results",
            "state",
        ):
            outer = _json_object(payload.get(outer_key))
            if not outer:
                continue
            if output_name in outer:
                return True, outer.get(output_name)
            for inner_key in (
                "output",
                "outputs",
                "output_state",
                "state_output",
                "workflow_output",
                "workflow_outputs",
                "result",
                "results",
            ):
                inner = _json_object(outer.get(inner_key))
                if output_name in inner:
                    return True, inner.get(output_name)
        return False, None

    remaining = {str(name).strip() for name in declared_output_names if str(name).strip()}
    if not remaining:
        return {}

    row_obj: dict[str, Any] = {}
    if callable(getattr(bl, "get_job", None)):
        try:
            child_row = bl.get_job(child_job_id)
        except Exception:
            child_row = None
        row_obj = child_row if isinstance(child_row, dict) else {}
    final_contract = _json_object(_json_object(row_obj.get("meta")).get("workflow_outputs"))
    if final_contract.get("ok") is True and isinstance(final_contract.get("outputs"), dict):
        # Successful boundary validation is the final result, including absent
        # optional fields. Do not revive an older diagnostic message with the
        # same name (or overwrite false/null/empty values with it).
        return {key: value for key, value in final_contract["outputs"].items() if key in remaining}

    resolved: dict[str, Any] = {}
    rows = bl.get_agent_messages(child_job_id, since_id=0, limit=5000, newest=True) or []
    # PlatformLogger.get_agent_messages(..., newest=True) still returns rows in
    # runtime order (oldest -> newest) after applying the query limit. Child
    # output promotion must prefer the newest artifact for each declared key,
    # otherwise an early review fail can overwrite a later review pass after rework.
    for row in reversed(rows):
        if not isinstance(row, dict):
            continue
        envelope = _extract_envelope(row)
        artifacts = envelope.get("artifacts")
        if not isinstance(artifacts, dict):
            continue
        state_patch = _json_object(artifacts.get("_state_patch"))
        for key in tuple(remaining):
            if key in artifacts:
                resolved[key] = artifacts.get(key)
                remaining.remove(key)
                continue
            found, value = _collect_from_output_container(state_patch, key)
            if found:
                resolved[key] = value
                remaining.remove(key)
        if not remaining:
            break

    if remaining and row_obj:
        row_meta = _json_object(row_obj.get("meta"))
        containers = [
            row_obj,
            row_meta,
            _json_object(row_obj.get("result")),
            _json_object(row_obj.get("state")),
            _json_object(row_obj.get("output")),
            _json_object(row_obj.get("outputs")),
            _json_object(row_obj.get("output_state")),
            _json_object(row_obj.get("state_output")),
            _json_object(row_obj.get("workflow_output")),
            _json_object(row_obj.get("workflow_outputs")),
            _json_object(row_obj.get("workflow_data")),
            _json_object(row_meta.get("result")),
            _json_object(row_meta.get("state")),
            _json_object(row_meta.get("output")),
            _json_object(row_meta.get("outputs")),
            _json_object(row_meta.get("output_state")),
            _json_object(row_meta.get("state_output")),
            _json_object(row_meta.get("workflow_output")),
            _json_object(row_meta.get("workflow_outputs")),
            _json_object(row_meta.get("workflow_data")),
        ]
        for key in tuple(remaining):
            for container in containers:
                found, value = _collect_from_output_container(container, key)
                if not found:
                    continue
                resolved[key] = value
                remaining.remove(key)
                break

    if remaining:
        thread_id = str(row_obj.get("thread_id") or "").strip()
        if thread_id:
            try:
                from core.infrastructure.runtime.control import state_reader

                runtime_state = state_reader.get_thread_state_values(
                    thread_id,
                    force_refresh=True,
                )
            except Exception as exc:
                logger.debug(
                    "invoke_workflow: could not read child runtime state child_job_id=%s "
                    "thread_id=%s: %s",
                    child_job_id,
                    thread_id,
                    exc,
                )
                runtime_state = {}
            runtime_state = runtime_state if isinstance(runtime_state, dict) else {}
            workflow_outputs = _json_object(runtime_state.get("workflow_outputs"))
            runtime_containers = [
                runtime_state,
                _json_object(runtime_state.get("outputs")),
                _json_object(runtime_state.get("workflow_data")),
                workflow_outputs,
                _json_object(workflow_outputs.get("outputs")),
            ]
            for key in tuple(remaining):
                for container in runtime_containers:
                    found, value = _collect_from_output_container(container, key)
                    if not found:
                        continue
                    resolved[key] = value
                    remaining.remove(key)
                    break
    return resolved


def _collect_child_artifacts(
    bl, child_job_id: str, declared_artifact_names: list[str]
) -> dict[str, Any]:
    remaining = {str(name).strip() for name in declared_artifact_names if str(name).strip()}
    if not remaining:
        return {}

    row_obj: dict[str, Any] = {}
    if callable(getattr(bl, "get_job", None)):
        try:
            child_row = bl.get_job(child_job_id)
        except Exception:
            child_row = None
        row_obj = child_row if isinstance(child_row, dict) else {}
    final_contract = _json_object(_json_object(row_obj.get("meta")).get("workflow_outputs"))
    if final_contract.get("ok") is True and isinstance(final_contract.get("artifacts"), dict):
        # Successful boundary validation is the final result, including absent
        # optional fields. Do not revive an older diagnostic message with the
        # same name (or overwrite false/null/empty values with it).
        return {
            key: value for key, value in final_contract["artifacts"].items() if key in remaining
        }

    resolved: dict[str, Any] = {}
    rows = bl.get_agent_messages(child_job_id, since_id=0, limit=5000, newest=True) or []
    for row in reversed(rows):
        if not isinstance(row, dict):
            continue
        envelope = _extract_envelope(row)
        artifacts = envelope.get("artifacts")
        if not isinstance(artifacts, dict):
            continue
        state_patch = _json_object(artifacts.get("_state_patch"))
        patch_artifacts = _json_object(state_patch.get("artifacts"))
        workflow_outputs = _json_object(artifacts.get("workflow_outputs"))
        workflow_output_artifacts = _json_object(workflow_outputs.get("artifacts"))
        terminal = _json_object(artifacts.get("workflow_terminal"))
        terminal_artifacts = _json_object(terminal.get("artifacts"))
        containers = (artifacts, patch_artifacts, workflow_output_artifacts, terminal_artifacts)
        for key in tuple(remaining):
            for container in containers:
                if key not in container:
                    continue
                resolved[key] = container.get(key)
                remaining.remove(key)
                break
        if not remaining:
            break

    if remaining and row_obj:
        row_meta = _json_object(row_obj.get("meta"))
        containers = [
            _json_object(row_obj.get("artifacts")),
            _json_object(row_meta.get("artifacts")),
            _json_object(row_obj.get("workflow_outputs")).get("artifacts"),
            _json_object(row_meta.get("workflow_outputs")).get("artifacts"),
        ]
        for key in tuple(remaining):
            for raw_container in containers:
                container = _json_object(raw_container)
                if key not in container:
                    continue
                resolved[key] = container.get(key)
                remaining.remove(key)
                break
    return resolved


def _set_nested_state_update(
    *,
    state: OrchestratorState,
    updates: dict,
    state_target: str,
    value: Any,
) -> None:
    if str(state_target or "").strip().startswith(STATE_TARGET_PREFIX):
        set_state_path(updates, dict(state or {}), state_target, value)


def _artifact_key_for_state_target(state_target: str) -> str:
    target = str(state_target or "").strip()
    if target.startswith(STATE_TARGET_PREFIX):
        target = target[len(STATE_TARGET_PREFIX) :]
    parts = [part for part in target.split(".") if part]
    return parts[-1] if parts else ""
