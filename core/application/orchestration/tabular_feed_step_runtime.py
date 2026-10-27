"""Thin workflow adapter for ``step_kind=tabular_feed``."""

from __future__ import annotations

import copy
from typing import Any

import psycopg

from core.application.orchestration.step_templating import (
    _render_value,
    _resolve_path,
    set_state_path,
)
from core.application.tabular_feed.context import context_from_state_and_step
from core.application.tabular_feed.contracts import (
    PAYLOAD_COPY_FIELDS,
    STEP_KIND_TABULAR_FEED,
    operation_action,
    snapshot_mode,
)
from core.application.tabular_feed.service import TabularFeedService
from core.application.tabular_feed.validation import validate_tabular_feed_config
from core.infrastructure.tabular_feed.storage import (
    plane_from_state_or_env,
    snapshot_repository_from_state_or_env,
)


def _as_dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _text(value: Any) -> str:
    return str(value or "").strip()


_ROWS_REQUIRED_ACTIONS = frozenset({"create_dataset", "merge_rows"})


class TabularStateReferenceError(ValueError):
    def __init__(self, selector: str, path: str) -> None:
        self.selector = str(selector or "").strip()
        self.path = path
        super().__init__(f"Tabular state reference is missing: {self.selector}")


class TabularRowsNotArrayError(ValueError):
    def __init__(self, selector: str) -> None:
        self.selector = str(selector or "").strip()
        super().__init__(f"Tabular rows selector did not resolve to an array: {self.selector}")


def _render_tabular_value(value: Any, state: dict[str, Any], *, path: str) -> Any:
    rendered = _render_value(value, state)
    if isinstance(rendered, str) and rendered.startswith("state."):
        resolved = _resolve_path(state, rendered)
        if resolved is None:
            raise TabularStateReferenceError(rendered, path)
        return resolved
    return rendered


def _collect_result_value(result: dict[str, Any], output_name: str) -> tuple[bool, Any]:
    key = _text(output_name)
    if not key:
        return False, None
    if key == "result":
        return True, result
    if key in result:
        return True, result.get(key)
    return False, None


def _apply_output_mapping(
    *,
    state_patch: dict[str, Any],
    step: dict[str, Any],
    state: dict[str, Any],
    result: dict[str, Any],
) -> None:
    output_mapping = _as_dict(step.get("output_mapping_jsonb") or step.get("output_mapping"))
    if not output_mapping:
        return
    for output_name, target in output_mapping.items():
        found, value = _collect_result_value(result, str(output_name))
        if found and _text(target).startswith("state."):
            set_state_path(state_patch, dict(state or {}), _text(target), value)


def _action_payload(config: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
    action_id = operation_action(config.get("operation"))
    input_cfg = _as_dict(config.get("input"))
    rendered_input = {
        key: [
            _render_tabular_value(item, state, path=f"input.dataset_handles[{index}]")
            for index, item in enumerate(value)
        ]
        if key == "dataset_handles" and isinstance(value, list)
        else _render_tabular_value(value, state, path=f"input.{key}")
        for key, value in input_cfg.items()
    }
    payload: dict[str, Any] = {
        "action_id": action_id,
        "input": dict(rendered_input),
        **rendered_input,
    }
    for key in PAYLOAD_COPY_FIELDS:
        if key in config and key not in payload:
            rendered = _render_tabular_value(config.get(key), state, path=key)
            payload[key] = rendered
            payload["input"].setdefault(key, rendered)
    if action_id in _ROWS_REQUIRED_ACTIONS and not isinstance(payload.get("rows"), list):
        # ``rows`` may be authored as a state selector; once resolved it must be an
        # array, otherwise the engine would silently build an empty dataset.
        raise TabularRowsNotArrayError(_text(config.get("rows")))
    return payload


def execute_tabular_feed_step(*, step: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
    from core.application.orchestration.workflow_state_selector_contract import (
        RUNTIME_ERROR_CODE,
        validate_step_state_selectors,
    )

    override = _as_dict(step.get("override"))
    config = _as_dict(override.get(STEP_KIND_TABULAR_FEED) or step.get(STEP_KIND_TABULAR_FEED))
    selector_issues = validate_step_state_selectors(
        {"step_kind": STEP_KIND_TABULAR_FEED, "override": {STEP_KIND_TABULAR_FEED: config}}
    )
    if selector_issues:
        issue = selector_issues[0]
        return {
            "ok": False,
            "runtime": STEP_KIND_TABULAR_FEED,
            "detail": RUNTIME_ERROR_CODE,
            "errors": [
                {
                    "reason": RUNTIME_ERROR_CODE,
                    "selector": issue["selector"],
                    "path": issue["path"].removeprefix("step."),
                }
            ],
            "result": {},
            "state_patch": {},
        }
    errors = validate_tabular_feed_config(config)
    if errors:
        return {
            "ok": False,
            "runtime": STEP_KIND_TABULAR_FEED,
            "detail": "tabular_feed validation failed",
            "errors": errors,
            "result": {},
            "state_patch": {},
        }
    try:
        action_payload = _action_payload(config, state)
    except TabularStateReferenceError as exc:
        return {
            "ok": False,
            "runtime": STEP_KIND_TABULAR_FEED,
            "detail": "tabular_feed state reference is missing",
            "errors": [
                {
                    "reason": "state_reference_missing",
                    "selector": exc.selector,
                    "path": exc.path,
                }
            ],
            "result": {},
            "state_patch": {},
        }
    except TabularRowsNotArrayError as exc:
        return {
            "ok": False,
            "runtime": STEP_KIND_TABULAR_FEED,
            "detail": "tabular_feed rows did not resolve to an array",
            "errors": [
                {
                    "reason": "rows_not_array",
                    "selector": exc.selector,
                }
            ],
            "result": {},
            "state_patch": {},
        }
    action_id = _text(action_payload.get("action_id"))
    try:
        purpose = (
            snapshot_mode(
                action_payload.get("snapshot_mode"),
                has_reference=_as_dict(action_payload.get("input")).get("expected_snapshot_ref")
                is not None,
            )
            if action_id == "diff"
            else "compare"
        )
        with snapshot_repository_from_state_or_env(
            state, operation=action_id, snapshot_mode=purpose
        ) as snapshots:
            service = TabularFeedService(
                plane=plane_from_state_or_env(state), snapshot_repository=snapshots
            )
            dispatch = service.execute(
                operation=action_id,
                payload=action_payload,
                context=context_from_state_and_step(state=state, step=step),
            )
    except psycopg.Error:
        dispatch = {
            "ok": False,
            "result": {},
            "errors": [
                {
                    "code": "snapshot_storage_unavailable",
                    "message": "Snapshot publication outcome could not be confirmed; retry the same candidate",
                }
            ],
        }
    ok = bool(dispatch.get("ok"))
    invocation = {
        "status": "succeeded" if ok else "failed",
        "runtime": STEP_KIND_TABULAR_FEED,
        "response": copy.deepcopy(dispatch),
    }
    response = _as_dict(invocation.get("response"))
    raw_result = {}
    if isinstance(response.get("result"), dict):
        raw_result = response["result"]
    elif isinstance(dispatch.get("result"), dict):
        raw_result = dispatch["result"]
    result = copy.deepcopy(raw_result)
    if not result:
        result = {
            key: dispatch.get(key)
            for key in ("artifact_handle", "dataset_handle", "grant_outputs")
            if dispatch.get(key)
        }
    state_patch: dict[str, Any] = {}
    if ok:
        set_state_path(state_patch, dict(state or {}), _text(config.get("result_target")), result)
        _apply_output_mapping(state_patch=state_patch, step=step, state=state, result=result)
    return {
        "ok": ok,
        "runtime": STEP_KIND_TABULAR_FEED,
        "detail": "tabular_feed step executed"
        if ok
        else _text(dispatch.get("detail")) or "tabular_feed step failed",
        "operation": _text(config.get("operation")).lower(),
        "action_id": action_id,
        "result": result,
        "errors": dispatch.get("errors") or response.get("errors") or [],
        "invocation": invocation,
        "state_patch": state_patch,
    }


__all__ = ["execute_tabular_feed_step", "validate_tabular_feed_config"]
