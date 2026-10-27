"""Resolve an expected input's source selector against run state.

Message selectors (``message.agent.*``, ``message.step.*``, ``message.group.*``)
are strict and must resolve; ``state.`` paths and bare names read state and the
public runtime containers.
"""

from __future__ import annotations

from core.application.orchestration.state_paths import resolve_state_path_with_workflow_data

_STATE_PREFIX = "state."


def _has_value(value) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, tuple, set, dict)):
        return len(value) > 0
    return True


def resolve_state_path(state: dict, path: str):
    """Resolve a dotted path from state dict (best-effort)."""
    if not path:
        return None
    resolved, found = resolve_state_path_with_workflow_data(state, path)
    return resolved if found else None


def resolve_runtime_public_value(state: dict, name: str):
    """Resolve a public workflow value from canonical runtime containers.

    Source-less expected_inputs are used by authored workflow steps to request
    workflow outputs/artifacts like ``workspace_state`` without repeating the
    selector syntax. Prefer direct state first, then public outputs/artifacts,
    then the latest published agent message payloads.
    """
    field = str(name or "").strip()
    if not field:
        return None

    direct, found = resolve_state_path_with_workflow_data(state, field)
    if found:
        return direct

    for container_name in ("materialized_inputs", "inputs"):
        container = state.get(container_name)
        if isinstance(container, dict) and _has_value(container.get(field)):
            return container.get(field)

    task_envelope = state.get("task_envelope")
    if isinstance(task_envelope, dict):
        envelope_inputs = task_envelope.get("inputs")
        if isinstance(envelope_inputs, dict) and _has_value(envelope_inputs.get(field)):
            return envelope_inputs.get(field)

    for container_name in ("outputs", "artifacts"):
        container = state.get(container_name)
        if isinstance(container, dict) and _has_value(container.get(field)):
            return container.get(field)

    workflow_data = state.get("workflow_data")
    if isinstance(workflow_data, dict):
        for container_name in ("outputs", "artifacts"):
            container = workflow_data.get(container_name)
            if isinstance(container, dict) and _has_value(container.get(field)):
                return container.get(field)

    def _object(value: object) -> dict:
        if isinstance(value, dict):
            return value
        return {}

    try:
        from core.application.orchestration.agent_messages import get_state_messages

        messages = get_state_messages(state)
    except Exception:
        messages = state.get("agent_messages") or []

    for msg in reversed(messages if isinstance(messages, list) else []):
        if not isinstance(msg, dict):
            continue
        artifacts = _object(msg.get("artifacts"))
        if _has_value(artifacts.get(field)):
            return artifacts.get(field)

        state_patch = _object(artifacts.get("_state_patch"))
        for container_name in ("outputs", "artifacts"):
            container = _object(state_patch.get(container_name))
            if _has_value(container.get(field)):
                return container.get(field)

        workflow_outputs = _object(artifacts.get("workflow_outputs"))
        for container_name in ("outputs", "artifacts"):
            container = _object(workflow_outputs.get(container_name))
            if _has_value(container.get(field)):
                return container.get(field)

        workflow_terminal = _object(artifacts.get("workflow_terminal"))
        for container_name in ("outputs", "artifacts"):
            container = _object(workflow_terminal.get(container_name))
            if _has_value(container.get(field)):
                return container.get(field)

    return None


def resolve_expected_input_source(
    state: dict,
    source: str,
    name: str,
) -> tuple[object | None, str]:
    source = str(source or "").strip()
    if not source:
        return None, ""

    # Message selectors are strict and must resolve.
    if source.startswith("message."):
        try:
            from core.application.orchestration.agent_messages import (
                MessageSelectorError,
                resolve_message_selector,
            )

            return resolve_message_selector(source, state), ""
        except MessageSelectorError as selector_err:
            return None, f"{name} ({selector_err})"
        except Exception as exc:
            return None, f"{name} (selector resolution failed: {exc})"

    # Explicit state selectors are still allowed for non-message inputs like task metadata.
    if source == "state":
        return state.get(name), ""
    if source.startswith(_STATE_PREFIX):
        return resolve_state_path(state, source[len(_STATE_PREFIX) :]), ""
    # Bare selectors are treated as direct state lookups.
    # Some registry workflows declare inputs like `current_item` rather than
    # `state.current_item`; keep those contracts working while preserving strict
    # handling for message selectors above. When the top-level state key is
    # absent, fall back to canonical runtime containers so callable child workflows
    # can resolve queued public inputs like `current_item`.
    if source and "." not in source:
        direct_value = state.get(source)
        if _has_value(direct_value):
            return direct_value, ""
        runtime_value = resolve_runtime_public_value(state, source)
        return runtime_value, ""
    return None, f"{name} (invalid source selector '{source}')"


__all__ = [
    "resolve_expected_input_source",
    "resolve_runtime_public_value",
    "resolve_state_path",
]
