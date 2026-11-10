"""New public hook declarations require Development; existing hooks remain editable.

This policy reads raw carrier rows, including incomplete declarations and duplicate
rows. Contract validation, protected-core authority and placement remain separate.
Trusted extension projection and hook bindings do not enter this public write gate.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterator, Mapping
from typing import Any

from core.application.workflows.workflow_action_contract import (
    ACTION_IMPLEMENTATION_SETTING,
    raw_action_implementation,
)
from core.infrastructure.config.installation_mode import installation_mode

HOOK_DECLARATION_MODE_CODE = "workflow.hook.declaration_requires_development"
HOOK_DECLARATION_MODE_MESSAGE = "Creating a hook zone requires a Development environment."
ACTION_MARKER_MODE_CODE = "workflow.action.marker_requires_development"
ACTION_MARKER_MODE_MESSAGE = (
    "Marking a workflow as the implementation of an extension action requires a "
    "Development environment."
)
_Carrier = tuple[str, str, str, str]


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _hook_carriers(workflow: Mapping[str, Any]) -> Iterator[tuple[_Carrier, str]]:
    owner = _text(workflow.get("workflow_id"))
    steps = workflow.get("steps")
    for index, step in enumerate(steps if isinstance(steps, list) else []):
        if not isinstance(step, Mapping):
            continue
        override = step.get("override")
        config = override.get("invoke_workflow") if isinstance(override, Mapping) else None
        if not isinstance(config, Mapping) or "hook" not in config:
            continue
        hook = config["hook"]
        hook_id = _text(hook.get("hook_id")) if isinstance(hook, Mapping) else ""
        step_id = _text(step.get("step_id"))
        # Kind is part of the carrier: dormant malformed config on another kind
        # must not authorize turning it into a live Invoke declaration.
        kind = _text(step.get("step_kind")).lower()
        path = f"steps.{step_id}" if step_id else f"steps[{index}]"
        yield (owner, step_id, kind, hook_id), f"{path}.override.invoke_workflow.hook"


def action_marker_authoring_issues(
    candidate: Mapping[str, Any], *, existing: Mapping[str, Any] | None = None
) -> list[dict[str, str]]:
    """Refuse a new or changed action implementation marker outside Development.

    Marking a workflow as the implementation of an extension action is how an
    extension author builds a composite action (ADR 0065). A marker the stored
    workflow already carries, unchanged, is not a new declaration.
    """
    if installation_mode() == "development":
        return []
    marker = raw_action_implementation(candidate)
    if marker is None or marker == raw_action_implementation(existing):
        return []
    return [
        {
            "code": ACTION_MARKER_MODE_CODE,
            "message": ACTION_MARKER_MODE_MESSAGE,
            "path": f"settings.{ACTION_IMPLEMENTATION_SETTING}",
            "severity": "error",
        }
    ]


def hook_declaration_authoring_issues(
    candidate: Mapping[str, Any], *, existing: Mapping[str, Any] | None = None
) -> list[dict[str, str]]:
    """Refuse new owner/carrier/identity occurrences outside Development.

    Compare multisets, not hook-id sets: copying a declaration to a new step,
    duplicating the same carrier, or introducing even an incomplete hook is a new
    declaration. Editing a retained contract, moving it or deleting it is not.
    The caller supplies the authoritative existing owner, never a client hint.
    """
    if installation_mode() == "development":
        return []
    retained = Counter(identity for identity, _ in _hook_carriers(existing or {}))
    issues = action_marker_authoring_issues(candidate, existing=existing)
    for identity, path in _hook_carriers(candidate):
        if retained[identity]:
            retained[identity] -= 1
            continue
        issues.append(
            {
                "code": HOOK_DECLARATION_MODE_CODE,
                "message": HOOK_DECLARATION_MODE_MESSAGE,
                "path": path,
                "severity": "error",
            }
        )
    return issues
