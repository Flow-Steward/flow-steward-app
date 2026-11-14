"""Bundle rules of connection slots on extension workflows (ADR 0068).

A bundled workflow that a person runs directly declares the connections its
connector steps read in ``settings.connection_slots``. The release is refused
when the declaration is malformed, names an extension the bundle may not call,
sits on an action implementation (which keeps its own slots, ADR 0065), or when
a step reads a slot the workflow does not declare or names a fixed connection
next to a slot.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from core.application.extensions.composite_actions import required_dependency_ids
from core.application.extensions.extension_contract_policies import (
    WORKFLOW_CONNECTION_SLOTS_PLATFORM_MIN,
)
from core.application.extensions.semver import compare_semver
from core.application.workflows.workflow_action_contract import (
    ACTION_CONFIG_KEY,
    raw_action_implementation,
    slot_of_binding,
)
from core.application.workflows.workflow_connection_slots import (
    CONNECTION_SLOTS_SETTING,
    ConnectionSlotContractError,
    WorkflowConnectionSlot,
    connector_step_slot,
    parse_connection_slots,
    raw_connection_slots,
)


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _rows(value: Any) -> list[Mapping[str, Any]]:
    return [row for row in value if isinstance(row, Mapping)] if isinstance(value, list) else []


def _published_workflows(descriptor: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    workflows: dict[str, Mapping[str, Any]] = {}
    for entry in _rows(_mapping(descriptor.get("artifacts")).get("workflows")):
        workflow_id = _text(entry.get("workflow_id") or entry.get("id"))
        snapshot = entry.get("snapshot") or entry
        if workflow_id and isinstance(snapshot, Mapping) and entry.get("publish", True):
            workflows[workflow_id] = snapshot
    return workflows


def _fixed_connection(step: Mapping[str, Any]) -> bool:
    config = _mapping(_mapping(step.get("override")).get("connector"))
    for owner in (step, config):
        for key in ("connection_binding_jsonb", "connection_binding"):
            binding = _mapping(owner.get(key))
            if any(binding.get(name) for name in ("connection_id", "project_connection_id")):
                return True
    return False


def _step_issues(
    step: Mapping[str, Any], *, path: str, slots: Mapping[str, WorkflowConnectionSlot]
) -> list[dict[str, str]]:
    kind = _text(step.get("step_kind") or step.get("kind")).lower()
    issues: list[dict[str, str]] = []
    if kind == "invoke_workflow":
        config = _mapping(_mapping(step.get("override")).get("invoke_workflow"))
        if ACTION_CONFIG_KEY not in config:
            return issues
        for name, binding in _mapping(config.get("connections")).items():
            parent_slot = slot_of_binding(_mapping(binding))
            if parent_slot and parent_slot not in slots:
                issues.append(
                    {
                        "code": "workflow.connection_slots.undeclared",
                        "message": (
                            f"Slot '{name}' of the called action passes slot '{parent_slot}', "
                            f"which this workflow does not declare in "
                            f"settings.{CONNECTION_SLOTS_SETTING}"
                        ),
                        "path": f"{path}.override.invoke_workflow.connections.{name}",
                    }
                )
        return issues
    if kind not in {"", "connector"}:
        return issues
    slot = connector_step_slot(step)
    if not slot:
        return issues
    if slot not in slots:
        issues.append(
            {
                "code": "workflow.connection_slots.undeclared",
                "message": (
                    f"The step reads connection slot '{slot}', which the workflow does not "
                    f"declare in settings.{CONNECTION_SLOTS_SETTING}"
                ),
                "path": f"{path}.connection_binding.slot",
            }
        )
    elif _fixed_connection(step):
        issues.append(
            {
                "code": "workflow.connection_slots.fixed_connection",
                "message": (
                    "A bundled workflow reads its connection from the slot only; it never "
                    "carries a connection id"
                ),
                "path": f"{path}.connection_binding",
            }
        )
    return issues


def workflow_connection_slot_issues(
    workflow: Mapping[str, Any], *, extension_id: str, required_dependencies: set[str]
) -> list[dict[str, str]]:
    """Rules of one bundled workflow's slots (bundle-local ids)."""
    raw = raw_connection_slots(workflow)
    declared: dict[str, WorkflowConnectionSlot] = {}
    issues: list[dict[str, str]] = []
    if raw is not None:
        if raw_action_implementation(workflow) is not None:
            return [
                {
                    "code": "workflow.connection_slots.implementation_forbidden",
                    "message": (
                        "A workflow that implements an action declares its connections in "
                        "settings.action_implementation.connections, not in "
                        f"settings.{CONNECTION_SLOTS_SETTING}"
                    ),
                    "path": f"settings.{CONNECTION_SLOTS_SETTING}",
                }
            ]
        try:
            parsed = parse_connection_slots(raw)
        except ConnectionSlotContractError as exc:
            return [exc.issue()]
        allowed = {extension_id, *required_dependencies}
        for slot in parsed:
            declared[slot.slot] = slot
            if slot.extension_id not in allowed:
                issues.append(
                    {
                        "code": "workflow.connection_slots.extension_not_declared",
                        "message": (
                            f"Slot '{slot.slot}' names '{slot.extension_id}', which is not this "
                            "extension or a required dependency"
                        ),
                        "path": f"settings.{CONNECTION_SLOTS_SETTING}.{slot.slot}",
                    }
                )
    elif raw_action_implementation(workflow) is not None:
        # An implementation's steps are checked against its own slots (ADR 0065).
        return []
    for index, step in enumerate(_rows(workflow.get("steps"))):
        step_id = _text(step.get("step_id"))
        issues.extend(
            _step_issues(
                step,
                path=f"steps.{step_id}" if step_id else f"steps[{index}]",
                slots=declared,
            )
        )
    return issues


def connection_slot_descriptor_issues(descriptor: Mapping[str, Any]) -> list[dict[str, str]]:
    """Check the slots of every published workflow of a release descriptor.

    *descriptor* is the release descriptor with the bundle runtime merged in.
    Issues use the descriptor error shape ``{path, code, message}``.
    """
    source = _mapping(descriptor)
    extension_id = _text(source.get("extension_id"))
    dependencies = required_dependency_ids(source)
    issues: list[dict[str, str]] = []
    declares_slots = False
    for local_id, workflow in _published_workflows(source).items():
        prefix = f"artifacts.workflows.{local_id}"
        declares_slots = declares_slots or raw_connection_slots(workflow) is not None
        for row in workflow_connection_slot_issues(
            workflow, extension_id=extension_id, required_dependencies=dependencies
        ):
            issues.append({**row, "path": f"{prefix}.{row['path']}"})
    if declares_slots and not _declares_host_minimum(source):
        # An older host ignores the declaration and its slot steps fail at run time;
        # the minimum makes it refuse the bundle instead.
        issues.append(
            {
                "code": "workflow.connection_slots.host_minimum",
                "message": (
                    "Workflows that declare connection slots require host contract "
                    f"{WORKFLOW_CONNECTION_SLOTS_PLATFORM_MIN} or later"
                ),
                "path": "runtime.compatibility.platform_min",
            }
        )
    return issues


def _declares_host_minimum(descriptor: Mapping[str, Any]) -> bool:
    runtime = _mapping(descriptor.get("runtime"))
    minimum = _text(_mapping(runtime.get("compatibility")).get("platform_min"))
    try:
        return (
            compare_semver(
                minimum, WORKFLOW_CONNECTION_SLOTS_PLATFORM_MIN, field_name="platform_min"
            )
            >= 0
        )
    except ValueError:
        return False


__all__ = ["connection_slot_descriptor_issues", "workflow_connection_slot_issues"]
