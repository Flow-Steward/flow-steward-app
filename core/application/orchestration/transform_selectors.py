"""Application selector validation with shared pure grammar and traversal."""

from __future__ import annotations

from typing import Any

from core.infrastructure.standard_json import (
    MISSING,
    SelectorToken,
    json_get,
    parse_transform_selector,
    select_value,
    selector_has_wildcard,
)


def transform_state_selector_error(config: dict[str, Any]) -> dict[str, Any] | None:
    from core.application.orchestration.workflow_state_selector_contract import (
        RUNTIME_ERROR_CODE,
        validate_step_state_selectors,
    )

    issues = validate_step_state_selectors(
        {"step_kind": "transform", "override": {"transform": config}}
    )
    if not issues:
        return None
    issue = issues[0]
    return {
        "ok": False,
        "errors": [
            {
                "reason": RUNTIME_ERROR_CODE,
                "selector": issue["selector"],
                "path": issue["path"].removeprefix("step."),
            }
        ],
        "state_patch": {},
        "detail": RUNTIME_ERROR_CODE,
    }


__all__ = [
    "MISSING",
    "SelectorToken",
    "json_get",
    "parse_transform_selector",
    "select_value",
    "selector_has_wildcard",
    "transform_state_selector_error",
]
