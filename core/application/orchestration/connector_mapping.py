"""Connector input selectors, output mapping and suppressed-output provenance."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from core.application.orchestration.state_path_guards import is_reserved_state_target
from core.application.orchestration.state_paths import (
    StatePathError,
    parse_state_path,
    resolve_state_path_with_workflow_data,
)
from core.application.orchestration.step_templating import set_state_path

_TOKEN_EXTENSION = "extension."

CONNECTOR_PREFIX = "connector."

STATE_PREFIX = "state."

_HOST_TEST_MODE_SUPPRESSION_FLAG = "_host_test_mode_suppression"

_TEST_MODE_SUPPRESSED_OUTPUTS_KEY = "_test_mode_suppressed_outputs"


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


def _project_extension_output_aliases(
    result: dict[str, Any],
    *,
    visible_grant_outputs: dict[str, Any],
    visible_produced_artifacts: list[dict[str, Any]],
    visible_produced_datasets: list[dict[str, Any]],
) -> dict[str, Any]:
    if visible_grant_outputs:
        result.update(visible_grant_outputs)
    if len(visible_produced_artifacts) == 1:
        artifact_id = str(visible_produced_artifacts[0].get("artifact_id") or "").strip()
        result["artifact_handle"] = f"artifact:{artifact_id}" if artifact_id else ""
    if len(visible_produced_datasets) == 1:
        dataset_id = str(visible_produced_datasets[0].get("dataset_id") or "").strip()
        result["dataset_handle"] = f"dataset:{dataset_id}" if dataset_id else ""
    return result


def _project_retry_signal(result: dict[str, Any]) -> dict[str, Any]:
    """Expose one top-level closed retry contract for every connector backend."""
    from core.application.orchestration.retry_policy import (
        normalize_retry_signal,
        request_semantics_for_operation_kind,
    )

    projected = normalize_retry_signal(result)
    trusted_operation_kind = str(projected.get("operation_kind") or "")
    trusted_request_semantics = (
        request_semantics_for_operation_kind(trusted_operation_kind)
        if trusted_operation_kind
        else str(projected.get("request_semantics") or "")
    )
    invocation = _to_object(result.get("invocation"))
    projected.update(
        normalize_retry_signal(
            invocation,
            operation_kind=trusted_operation_kind,
            request_semantics=trusted_request_semantics,
        )
    )
    projected.update(
        normalize_retry_signal(
            _to_object(invocation.get("response")),
            operation_kind=trusted_operation_kind,
            request_semantics=trusted_request_semantics,
        )
    )
    result.update(projected)
    return result


def _resolve_state_path(state: dict[str, Any], path: str) -> Any:
    resolved, found = resolve_state_path_with_workflow_data(state, path)
    return resolved if found else None


def _parse_literal_value(raw: str) -> Any:
    text = str(raw or "").strip()
    if not text:
        return ""
    try:
        return json.loads(text)
    except Exception:
        return text


def _split_default_filter(token: str) -> tuple[str, Any, bool]:
    text = str(token or "").strip()
    if "|" not in text:
        return text, None, False
    selector, _, filter_text = text.partition("|")
    filter_text = filter_text.strip()
    if not filter_text.startswith("default"):
        return text, None, False
    _, sep, raw_default = filter_text.partition(":")
    if not sep:
        start = filter_text.find("(")
        end = filter_text.rfind(")")
        raw_default = filter_text[start + 1 : end] if start >= 0 and end > start else ""
    return selector.strip(), _parse_literal_value(raw_default), True


def _resolve_mapping_value(value: Any, state: dict[str, Any]) -> Any:
    if not isinstance(value, str):
        return value
    token, default_value, has_default = _split_default_filter(value.strip())
    if not token:
        return ""
    if token.startswith(STATE_PREFIX):
        resolved = _resolve_state_path(state, token[len(STATE_PREFIX) :])
        return default_value if has_default and resolved in (None, "") else resolved
    if token == "state":
        return state
    if token.startswith("literal:"):
        return _parse_literal_value(token[len("literal:") :])
    return value


def _render_input_payload(
    raw_mapping: dict[str, Any], state: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, str]]:
    """Render a step's input mapping, and report what would not resolve.

    A `state.` path that leads nowhere used to be sent as null, which the
    receiving contract reads as a supplied empty value: an update carrying a
    field the workflow never had. Leave it out instead — absent is what the
    workflow means — and hand the caller the paths that were dropped so the
    failure can name them rather than the operator guessing.
    """
    payload: dict[str, Any] = {}
    unresolved: dict[str, str] = {}
    for raw_key, raw_value in (raw_mapping or {}).items():
        key = str(raw_key or "").strip()
        if not key:
            continue
        if isinstance(raw_value, str):
            token, _default, has_default = _split_default_filter(raw_value.strip())
            if token.startswith(STATE_PREFIX) and not has_default:
                resolved, found = resolve_state_path_with_workflow_data(
                    state, token[len(STATE_PREFIX) :]
                )
                if not found:
                    unresolved[key] = token
                    continue
                payload[key] = resolved
                continue
        payload[key] = _resolve_mapping_value(raw_value, state)
    return payload, unresolved


def _get_path(payload: dict[str, Any], path: str) -> Any:
    parts = [part for part in str(path or "").split(".") if part]
    cursor: Any = payload
    for part in parts:
        if not isinstance(cursor, dict):
            return None
        cursor = cursor.get(part)
    return cursor


def _set_path(update: dict[str, Any], state: dict[str, Any], target: str, value: Any) -> None:
    set_state_path(update, state, target, value)


def _object_from_result(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except Exception:
            return {}
        return dict(parsed) if isinstance(parsed, dict) else {}
    return {}


def _resolve_output_path(value: Any, path: str, *, depth: int = 0) -> tuple[bool, Any]:
    if depth >= 64:
        return False, None
    parts = [part for part in str(path or "").split(".") if part]
    if not parts:
        return False, None
    return _resolve_output_path_parts(value, parts, depth=depth)


def _resolve_output_path_parts(
    value: Any,
    parts: list[str],
    *,
    depth: int,
) -> tuple[bool, Any]:
    if not parts:
        return True, value
    if depth >= 64:
        return False, None
    if isinstance(value, list):
        resolved_items: list[Any] = []
        for item in value:
            found, resolved = _resolve_output_path_parts(item, parts, depth=depth + 1)
            if not found:
                return False, None
            resolved_items.append(resolved)
        return True, resolved_items
    if not isinstance(value, dict):
        return False, None

    raw_part = parts[0]
    is_array = raw_part.endswith("[]")
    key = raw_part[:-2] if is_array else raw_part
    if not key or key not in value:
        return False, None
    resolved = value[key]
    if is_array and not isinstance(resolved, list):
        return False, None
    return _resolve_output_path_parts(resolved, parts[1:], depth=depth + 1)


def _collect_output_value(result: dict[str, Any], output_name: str) -> tuple[bool, Any]:
    key = str(output_name or "").strip()
    if not key:
        return False, None
    grant_outputs = _object_from_result(result.get("grant_outputs"))
    if key in grant_outputs:
        return True, grant_outputs.get(key)
    if key in {"artifact_handle", "dataset_handle"} and key in result:
        return True, result.get(key)
    for obj in _iter_output_objects(result):
        if key in obj:
            return True, obj.get(key)
        if "." in key or "[]" in key:
            found, value = _resolve_output_path(obj, key)
            if found:
                return True, value
    return False, None


def _iter_output_objects(result: dict[str, Any]):
    """Visit every result envelope the output resolver treats as addressable."""
    containers: list[Any] = [
        result.get("result"),
        result.get("invocation"),
        result.get("response"),
        result,
    ]
    visited: set[int] = set()
    visited_count = 0
    while containers and visited_count < 64:
        container = containers.pop(0)
        if isinstance(container, dict):
            if id(container) in visited:
                continue
            visited.add(id(container))
            obj = container
        else:
            obj = _object_from_result(container)
        if not obj:
            continue
        visited_count += 1
        yield obj
        containers.extend((obj.get("result"), obj.get("response"), obj.get("invocation")))


def _apply_output_mapping(
    *,
    step_payload: dict[str, Any],
    dispatch_result: dict[str, Any],
    state: dict[str, Any],
) -> None:
    effect = dispatch_result.get("external_effect")
    if isinstance(effect, Mapping) and effect.get("status") == "timeout_unknown":
        # Retain diagnostics, but a partial failed response is not a verified
        # producer output. Independent pre-existing state/lookup values survive.
        return
    override = _to_object(step_payload.get("override"))
    connector_override = _to_object(override.get("connector"))
    output_mapping = (
        _to_object(step_payload.get("output_mapping_jsonb"))
        or _to_object(connector_override.get("output_mapping_jsonb"))
        or _to_object(connector_override.get("output_mapping"))
    )
    if not output_mapping:
        return

    blocked_targets = sorted(
        {
            str(target).strip()
            for target in output_mapping.values()
            if is_reserved_state_target(target)
        }
    )
    if blocked_targets:
        dispatch_result["ok"] = False
        dispatch_result["detail"] = "output mapping target is reserved runtime state"
        errors = dispatch_result.setdefault("errors", [])
        if isinstance(errors, list):
            errors.append(
                {
                    "code": "output_mapping_reserved_target",
                    "message": "Output mapping cannot write security-sensitive runtime state",
                    "path": "output_mapping_jsonb",
                    "blocked_targets": blocked_targets,
                }
            )
        return

    state_patch: dict[str, Any] = {}
    mapped_outputs: dict[str, Any] = {}
    unmapped_outputs: list[str] = []
    mapped_state_targets: list[str] = []
    for output_name, target in output_mapping.items():
        found, value = _collect_output_value(dispatch_result, str(output_name))
        if not found:
            unmapped_outputs.append(str(output_name))
            continue
        mapped_outputs[str(output_name)] = value
        target_text = str(target or "").strip()
        if target_text.startswith(STATE_PREFIX):
            _set_path(state_patch, state, target_text, value)
            mapped_state_targets.append(target_text)
        elif target_text:
            mapped_outputs[target_text] = value

    provenance = _to_object(state.get(_TEST_MODE_SUPPRESSED_OUTPUTS_KEY))
    next_provenance = dict(provenance)
    for target in mapped_state_targets:
        _clear_suppressed_output_provenance(next_provenance, target)
    safe_suppression = bool(unmapped_outputs) and _is_safe_test_mode_suppression(dispatch_result)
    if safe_suppression:
        producer_step_id = str(step_payload.get("step_id") or "").strip()
        for output_name in unmapped_outputs:
            target = str(output_mapping.get(output_name) or "").strip()
            if not target.startswith(STATE_PREFIX) or is_reserved_state_target(target):
                continue
            next_provenance[target] = {
                "producer_step_id": producer_step_id,
                "producer_output": output_name,
            }
    if next_provenance != provenance:
        state_patch[_TEST_MODE_SUPPRESSED_OUTPUTS_KEY] = next_provenance

    if state_patch:
        existing_patch = dispatch_result.get("state_patch")
        if isinstance(existing_patch, dict):
            state_patch = {**existing_patch, **state_patch}
        dispatch_result["state_patch"] = state_patch
    if mapped_outputs:
        dispatch_result["mapped_outputs"] = mapped_outputs
    if unmapped_outputs:
        if safe_suppression:
            dispatch_result["suppressed_output_mappings"] = list(unmapped_outputs)
        else:
            _report_unresolved_output_mapping(dispatch_result, unmapped_outputs)


def _state_path_tokens(path: str) -> tuple[tuple[str, str], ...]:
    try:
        return tuple((token.kind, str(token.value)) for token in parse_state_path(path))
    except StatePathError:
        return ()


def _clear_suppressed_output_provenance(provenance: dict[str, Any], produced_target: str) -> None:
    produced_tokens = _state_path_tokens(produced_target)
    if not produced_tokens:
        return
    for candidate in provenance:
        candidate_tokens = _state_path_tokens(candidate)
        if candidate_tokens[: len(produced_tokens)] == produced_tokens:
            provenance.pop(candidate, None)


def _suppressed_input_dependencies(
    unresolved_inputs: dict[str, str], state: dict[str, Any]
) -> dict[str, dict[str, str]]:
    """Return provenance only when every unresolved selector has a suppressed producer."""
    if not unresolved_inputs:
        return {}
    provenance = _to_object(state.get(_TEST_MODE_SUPPRESSED_OUTPUTS_KEY))
    if not provenance:
        return {}
    dependencies: dict[str, dict[str, str]] = {}
    for input_name, selector in unresolved_inputs.items():
        selector_tokens = _state_path_tokens(selector)
        if not selector_tokens:
            return {}
        matched: dict[str, str] | None = None
        for target, raw_producer in provenance.items():
            target_tokens = _state_path_tokens(target)
            producer = _to_object(raw_producer)
            if (
                target_tokens
                and selector_tokens[: len(target_tokens)] == target_tokens
                and str(producer.get("producer_step_id") or "").strip()
                and str(producer.get("producer_output") or "").strip()
            ):
                matched = {
                    "selector": selector,
                    "producer_step_id": str(producer["producer_step_id"]),
                    "producer_output": str(producer["producer_output"]),
                }
                break
        if matched is None:
            return {}
        dependencies[input_name] = matched
    return dependencies


def _dependency_suppressed_result(
    *,
    connector_id: str,
    input_payload: dict[str, Any],
    unresolved_inputs: dict[str, str],
    dependencies: dict[str, dict[str, str]],
    connection_binding: dict[str, Any],
) -> dict[str, Any]:
    suppressed = {
        "test_mode_status": "suppressed",
        "external_effect_status": "suppressed",
        "definitely_no_external_effect": True,
    }
    return {
        "handled": True,
        "ok": True,
        "runtime": "dependency_suppression",
        "connector_id": connector_id,
        "result": dict(suppressed),
        "invocation": {
            "status": "succeeded",
            "response": {
                "ok": True,
                "result": dict(suppressed),
                "external_effect_status": "suppressed",
                "definitely_no_external_effect": True,
            },
        },
        "input_payload": input_payload,
        "input_mapping_unresolved": dict(unresolved_inputs),
        "suppressed_dependencies": dict(dependencies),
        "connection_binding": connection_binding,
        "detail": "test_mode_dependency_suppressed",
    }


def _is_safe_test_mode_suppression(dispatch_result: dict[str, Any]) -> bool:
    invocation = _object_from_result(dispatch_result.get("invocation"))
    response = _object_from_result(invocation.get("response"))
    result = _object_from_result(response.get("result"))
    return bool(
        invocation.get("status") == "succeeded"
        and response.get("ok") is True
        and response.get("external_effect_status") == "suppressed"
        and response.get("definitely_no_external_effect") is True
        and result.get("test_mode_status") == "suppressed"
        and result.get("external_effect_status") == "suppressed"
        and result.get("definitely_no_external_effect") is True
    )


def _result_key_names(dispatch_result: dict[str, Any]) -> list[str]:
    """Name what the result actually offered, so the author can see what to map."""
    keys = set(_object_from_result(dispatch_result.get("grant_outputs")))
    keys.update(
        str(key)
        for obj in _iter_output_objects(dispatch_result)
        for key in obj
        if key not in {"result", "response", "invocation", "grant_outputs"}
    )
    return sorted(keys)


def _report_unresolved_output_mapping(
    dispatch_result: dict[str, Any],
    unmapped_outputs: list[str],
) -> None:
    """Fail the step that owns the mapping instead of leaving the output missing.

    A mapping that resolves to nothing used to be skipped, so the run continued and
    broke one step later on a value nobody had written — naming neither the mapping
    nor the step. Nested fields are still read the supported way: map the parent
    object, then read `state.<target>.<field>`.
    """
    dispatch_result["unmapped_outputs"] = list(unmapped_outputs)
    if dispatch_result.get("ok") is False:
        # The connector already failed for its own reason; do not relabel it.
        return
    available = _result_key_names(dispatch_result)
    names = ", ".join(unmapped_outputs)
    detail = (
        f"Connector output mapping found no value for: {names}. "
        f"The result provides: {', '.join(available) or '(nothing)'}. "
        "Map one of those and read a nested field from it as "
        "state.<target>.<field>."
    )
    dispatch_result["ok"] = False
    dispatch_result["detail"] = detail
    existing_errors = dispatch_result.get("errors")
    errors = list(existing_errors) if isinstance(existing_errors, list) else []
    errors.append(
        {
            "code": "output_mapping_unresolved",
            "message": detail,
            "path": "output_mapping_jsonb",
        }
    )
    dispatch_result["errors"] = errors
