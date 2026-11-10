"""Composite actions of an extension release: an action implemented by a bundled workflow.

The action definition says ``handler: {mode: workflow, workflow_id: <local id>}``
and the bundle ships that workflow with ``settings.action_implementation`` naming
the action back (ADR 0065). This module pairs the two and holds the bundle-level
rules of an implementation; the shapes themselves are in
``core.application.workflows.workflow_action_contract``.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from core.application.extensions.projected_dataset_bridge import projection_bridge_id
from core.application.workflows.workflow_action_contract import (
    ACTION_CONFIG_KEY,
    ActionContractError,
    ActionImplementation,
    action_implementation_of,
    parse_action_ref,
    raw_action_implementation,
    slot_of_binding,
)

HANDLER_MODE_WORKFLOW = "workflow"
PARENT_WORKFLOW_CHANNEL = "parent_workflow"


@dataclass(frozen=True)
class CompositeAction:
    """One composite action of a release: the action and its bundled implementation."""

    extension_id: str
    action_id: str
    local_workflow_id: str
    implementation: ActionImplementation

    def workflow_id(self, install_id: str) -> str:
        """The projected id of the implementation in one install."""
        return projection_bridge_id(install_id, self.local_workflow_id)


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _rows(value: Any) -> list[Mapping[str, Any]]:
    return [row for row in value if isinstance(row, Mapping)] if isinstance(value, list) else []


def action_handler_mode(action: Mapping[str, Any] | None) -> str:
    handler = _mapping(_mapping(action).get("handler"))
    mode = _text(handler.get("mode")).lower() or "extension"
    return "platform" if mode == "core" else mode


def composite_action_handlers(action_definitions: Any) -> dict[str, str]:
    """``action_id → local workflow id`` of every ``handler.mode: workflow`` action."""
    handlers: dict[str, str] = {}
    for action in _rows(action_definitions):
        action_id = _text(action.get("action_id") or action.get("id"))
        if action_id and action_handler_mode(action) == HANDLER_MODE_WORKFLOW:
            handlers[action_id] = _text(_mapping(action.get("handler")).get("workflow_id"))
    return handlers


def is_composite_action(extension: Mapping[str, Any] | None, action_id: str) -> bool:
    """Whether *action_id* of an extension record or install contract is composite."""
    record = _mapping(extension)
    wanted = _text(action_id)
    for source in (record, _mapping(record.get("compiled_manifest"))):
        if wanted and wanted in composite_action_handlers(source.get("action_definitions")):
            return True
    return False


def workflow_handler_issues(
    action: Mapping[str, Any], *, manifest_path: str, index: int
) -> list[dict[str, str]]:
    """Manifest-level rules of one ``handler.mode: workflow`` action."""
    base = f"action_definitions[{index}]"
    issues: list[dict[str, str]] = []

    def issue(code: str, message: str, field: str, hint: str = "") -> None:
        issues.append(
            {
                "severity": "error",
                "code": code,
                "message": message,
                "file_path": manifest_path,
                "field_path": f"{base}.{field}",
                "hint": hint,
            }
        )

    handler = _mapping(action.get("handler"))
    action_id = _text(action.get("action_id") or action.get("id"))
    if not _text(handler.get("workflow_id")):
        issue(
            "composite_action_workflow_missing",
            f"Action '{action_id}' has handler.mode workflow and must name handler.workflow_id",
            "handler.workflow_id",
            "Set handler.workflow_id to the local id of the workflow that implements the action.",
        )
    if action.get("workflow_visible") is not True:
        issue(
            "composite_action_not_workflow_visible",
            f"Action '{action_id}' runs only as a workflow step; set workflow_visible: true",
            "workflow_visible",
        )
    if _mapping(handler.get("config")):
        issue(
            "composite_action_handler_config",
            f"Action '{action_id}' is implemented by a workflow and takes no handler.config",
            "handler.config",
        )
    return issues


def _published_workflows(descriptor: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    workflows: dict[str, Mapping[str, Any]] = {}
    for entry in _rows(_mapping(descriptor.get("artifacts")).get("workflows")):
        workflow_id = _text(entry.get("workflow_id") or entry.get("id"))
        # The same reading as the stored artifact snapshot: an inline entry is its own body.
        snapshot = entry.get("snapshot") or entry
        if workflow_id and isinstance(snapshot, Mapping) and entry.get("publish", True):
            workflows[workflow_id] = snapshot
    return workflows


def required_dependency_ids(descriptor: Mapping[str, Any]) -> set[str]:
    return {
        _text(row.get("extension_id"))
        for row in _rows(_mapping(descriptor.get("dependencies")).get("extensions"))
        if _text(row.get("extension_id")) and row.get("required", True)
    }


def release_composite_actions(descriptor: Mapping[str, Any] | None) -> dict[str, CompositeAction]:
    """The composite actions of a stored release descriptor, by action id.

    Only complete pairs are returned: the release was validated when it was
    stored, and a half-declared action is never offered or run.
    """
    source = _mapping(descriptor)
    extension_id = _text(source.get("extension_id"))
    workflows = _published_workflows(source)
    actions: dict[str, CompositeAction] = {}
    for action_id, local_id in composite_action_handlers(source.get("action_definitions")).items():
        try:
            implementation = action_implementation_of(workflows.get(local_id))
        except ActionContractError:
            continue
        if implementation is None or implementation.action_id != action_id:
            continue
        actions[action_id] = CompositeAction(extension_id, action_id, local_id, implementation)
    return actions


def _step_config(step: Mapping[str, Any], kind: str) -> Mapping[str, Any]:
    return _mapping(_mapping(step.get("override")).get(kind))


def _connector_binding(step: Mapping[str, Any]) -> Any:
    config = _step_config(step, "connector")
    for owner, key in (
        (step, "connection_binding_jsonb"),
        (step, "connection_binding"),
        (config, "connection_binding_jsonb"),
        (config, "connection_binding"),
    ):
        if owner.get(key):
            return owner.get(key)
    return None


def _implementation_step_issues(
    step: Mapping[str, Any],
    *,
    path: str,
    extension_id: str,
    allowed_extensions: set[str],
    handlers: Mapping[str, str],
    implementation: ActionImplementation,
) -> list[dict[str, str]]:
    from core.application.workflows.extension_connection_validation import (
        named_extension_without_install,
        step_connector_install_id,
    )

    kind = _text(step.get("step_kind") or step.get("kind")).lower()
    issues: list[dict[str, str]] = []

    def issue(code: str, message: str, field: str = "") -> None:
        issues.append(
            {"code": code, "message": message, "path": f"{path}.{field}" if field else path}
        )

    if kind == "invoke_workflow":
        config = _step_config(step, "invoke_workflow")
        if "hook" in config:
            issue(
                "workflow.action.hook_forbidden",
                "A workflow that implements an action cannot declare a hook",
                "override.invoke_workflow.hook",
            )
            return issues
        if ACTION_CONFIG_KEY not in config:
            issue(
                "workflow.action.plain_invoke_forbidden",
                "A workflow that implements an action calls other workflows only as "
                "extension actions",
                "override.invoke_workflow",
            )
            return issues
        try:
            ref = parse_action_ref(config[ACTION_CONFIG_KEY])
        except ActionContractError as exc:
            issues.append({**exc.issue(), "path": f"{path}.{exc.path}"})
            return issues
        if ref.install_id:
            issue(
                "workflow.action.bundle_install_forbidden",
                "A bundled workflow names an action by extension, never by an install id",
                "override.invoke_workflow.action.install_id",
            )
        if ref.extension_id not in allowed_extensions:
            issue(
                "workflow.action.extension_not_declared",
                f"Extension '{ref.extension_id}' is not this extension or a required dependency",
                "override.invoke_workflow.action.extension_id",
            )
        target = _text(config.get("workflow_id"))
        if ref.extension_id == extension_id:
            expected = handlers.get(ref.action_id)
            if expected is None:
                issue(
                    "workflow.action.unknown",
                    f"'{ref.action_id}' is not an action this extension implements by a workflow",
                    "override.invoke_workflow.action.action_id",
                )
            elif target != expected:
                issue(
                    "workflow.action.target_mismatch",
                    f"Action '{ref.action_id}' is implemented by workflow '{expected}'",
                    "override.invoke_workflow.workflow_id",
                )
        elif target:
            issue(
                "workflow.action.dependency_target_forbidden",
                "An action of another extension is named by the action only; its "
                "workflow is resolved in the caller's install",
                "override.invoke_workflow.workflow_id",
            )
        for name, binding in _mapping(config.get("connections")).items():
            parent_slot = slot_of_binding(_mapping(binding))
            if not parent_slot or implementation.slot(parent_slot) is None:
                issue(
                    "workflow.action.slot_unknown",
                    f"Slot '{name}' of the called action must pass one of this workflow's "
                    "declared slots",
                    f"override.invoke_workflow.connections.{name}",
                )
        if _mapping(config.get("installs")):
            issue(
                "workflow.action.bundle_install_forbidden",
                "A bundled workflow does not choose installs; the caller's choice is passed on",
                "override.invoke_workflow.installs",
            )
        return issues
    if kind not in {"", "connector"}:
        return issues
    if step_connector_install_id(step):
        issue(
            "workflow.action.bundle_install_forbidden",
            "A bundled workflow names an extension by package id, never by an install id",
            "connector_install_id",
        )
    named = named_extension_without_install({**step, "connector_install_id": ""})
    step_extension = named.extension_id if named is not None else ""
    if step_extension and step_extension not in allowed_extensions:
        issue(
            "workflow.action.extension_not_declared",
            f"Extension '{step_extension}' is not this extension or a required dependency",
            "connector_id",
        )
    binding = _connector_binding(step)
    if binding is None:
        return issues
    slot_name = slot_of_binding(_mapping(binding))
    slot = implementation.slot(slot_name) if slot_name else None
    fixed = any(
        _mapping(binding).get(key)
        for key in ("connection_id", "project_connection_id", "connection_ref")
    )
    if slot is None or fixed:
        issue(
            "workflow.action.connection_must_be_slot",
            "A workflow that implements an action reads a connection from a declared "
            "slot, never from a fixed connection id",
            "connection_binding",
        )
    elif step_extension and slot.extension_id != step_extension:
        issue(
            "workflow.action.slot_extension_mismatch",
            f"Slot '{slot_name}' belongs to '{slot.extension_id}', the step calls "
            f"'{step_extension}'",
            "connection_binding.slot",
        )
    return issues


def implementation_content_issues(
    workflow: Mapping[str, Any],
    *,
    extension_id: str,
    required_dependencies: set[str],
    handlers: Mapping[str, str],
) -> list[dict[str, str]]:
    """Rules of the body of one implementation workflow (bundle-local ids)."""
    try:
        implementation = action_implementation_of(workflow)
    except ActionContractError as exc:
        return [exc.issue()]
    if implementation is None:
        return []
    issues: list[dict[str, str]] = []
    allowed = {extension_id, *required_dependencies}
    callable_settings = _mapping(_mapping(workflow.get("settings")).get("callable"))
    channels = callable_settings.get("trigger_channels")
    if callable_settings.get("enabled") is not True or channels != [PARENT_WORKFLOW_CHANNEL]:
        issues.append(
            {
                "code": "workflow.action.channel_invalid",
                "message": (
                    "A workflow that implements an action is callable and allows only the "
                    "parent_workflow trigger channel"
                ),
                "path": "settings.callable.trigger_channels",
            }
        )
    for slot in implementation.connections:
        if slot.extension_id not in allowed:
            issues.append(
                {
                    "code": "workflow.action.extension_not_declared",
                    "message": (
                        f"Slot '{slot.slot}' names '{slot.extension_id}', which is not this "
                        "extension or a required dependency"
                    ),
                    "path": f"settings.action_implementation.connections.{slot.slot}",
                }
            )
    for index, step in enumerate(_rows(workflow.get("steps"))):
        step_id = _text(step.get("step_id"))
        issues.extend(
            _implementation_step_issues(
                step,
                path=f"steps.{step_id}" if step_id else f"steps[{index}]",
                extension_id=extension_id,
                allowed_extensions=allowed,
                handlers=handlers,
                implementation=implementation,
            )
        )
    return issues


def _names(rows: Any) -> set[str]:
    return {_text(row.get("name")) for row in _rows(rows) if _text(row.get("name"))}


def _operation_interface_issues(
    operation: Mapping[str, Any] | None, workflow: Mapping[str, Any], *, action_id: str
) -> list[dict[str, str]]:
    """The action's Contract V2 operation must describe the workflow's own interface.

    The builder shows the operation's inputs and outputs; the run passes them to
    the workflow. A name on one side only is a step that saves and cannot run.
    """
    if operation is None:
        return []
    issues: list[dict[str, str]] = []
    path = f"operation_manifest.{action_id}"
    if operation.get("connection_type_ids") or operation.get("connection_type"):
        issues.append(
            {
                "path": f"{path}.connection_type_ids",
                "code": "composite_action_operation_connection",
                "message": (
                    f"Action '{action_id}' is implemented by a workflow: its connections are "
                    "the slots of that workflow, not connection_type_ids of the operation"
                ),
            }
        )
    callable_settings = _mapping(_mapping(workflow.get("settings")).get("callable"))
    declared_inputs = {
        _text(row.get("name"))
        for row in _rows(operation.get("inputs"))
        if _text(row.get("name"))
        and _text(row.get("value_type") or row.get("type")) != "connection_ref"
    }
    for side, declared, expected in (
        ("inputs", declared_inputs, _names(callable_settings.get("expected_inputs"))),
        (
            "outputs",
            _names(operation.get("outputs")),
            _names(callable_settings.get("expected_outputs")),
        ),
    ):
        if declared != expected:
            difference = ", ".join(sorted(declared ^ expected))
            issues.append(
                {
                    "path": f"{path}.{side}",
                    "code": "composite_action_interface_mismatch",
                    "message": (
                        f"The {side} of action '{action_id}' and of the workflow that "
                        f"implements it differ: {difference}"
                    ),
                }
            )
    return issues


def composite_action_descriptor_issues(descriptor: Mapping[str, Any]) -> list[dict[str, str]]:
    """Pair every composite action with its bundled workflow and check the workflow.

    *descriptor* is the release descriptor with the bundle runtime merged in:
    ``action_definitions``, ``artifacts.workflows`` and ``dependencies``. Issues
    use the descriptor error shape ``{path, code, message}``.
    """
    source = _mapping(descriptor)
    extension_id = _text(source.get("extension_id"))
    handlers = composite_action_handlers(source.get("action_definitions"))
    workflows = _published_workflows(source)
    dependencies = required_dependency_ids(source)
    operations = {
        _text(row.get("operation_id")): row for row in _rows(source.get("operation_manifest"))
    }
    issues: list[dict[str, str]] = []
    implemented: dict[str, str] = {}
    for local_id, workflow in workflows.items():
        if raw_action_implementation(workflow) is None:
            continue
        prefix = f"artifacts.workflows.{local_id}"
        content = implementation_content_issues(
            workflow,
            extension_id=extension_id,
            required_dependencies=dependencies,
            handlers=handlers,
        )
        issues.extend({**row, "path": f"{prefix}.{row['path']}"} for row in content)
        try:
            implementation = action_implementation_of(workflow)
        except ActionContractError:
            continue
        assert implementation is not None
        action_id = implementation.action_id
        if handlers.get(action_id) != local_id:
            issues.append(
                {
                    "path": f"{prefix}.settings.action_implementation.action_id",
                    "code": "composite_action_marker_mismatch",
                    "message": (
                        f"Workflow '{local_id}' says it implements action '{action_id}', but "
                        "that action does not name it in handler.workflow_id"
                    ),
                }
            )
        implemented[action_id] = local_id
        if handlers.get(action_id) == local_id:
            issues.extend(
                _operation_interface_issues(
                    operations.get(action_id), workflow, action_id=action_id
                )
            )
    for action_id, local_id in handlers.items():
        if not local_id or implemented.get(action_id) == local_id:
            continue
        if local_id not in workflows:
            message = f"Action '{action_id}' names workflow '{local_id}', which the bundle lacks"
            code = "composite_action_workflow_unknown"
        else:
            message = (
                f"Workflow '{local_id}' must carry settings.action_implementation naming "
                f"action '{action_id}'"
            )
            code = "composite_action_marker_missing"
        issues.append({"path": "action_definitions", "code": code, "message": message})
    return issues


__all__ = [
    "HANDLER_MODE_WORKFLOW",
    "CompositeAction",
    "action_handler_mode",
    "composite_action_descriptor_issues",
    "composite_action_handlers",
    "implementation_content_issues",
    "is_composite_action",
    "release_composite_actions",
    "required_dependency_ids",
    "workflow_handler_issues",
]
