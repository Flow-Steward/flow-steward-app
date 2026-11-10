"""A release never breaks the steps that call its composite actions (ADR 0065).

An add-on update changes what the next entry of an action step runs, and the
person saves nothing. So the update itself is judged against every step that
calls an action of the install: the action must still exist, the step's
mappings must still fit the workflow that implements it, and every connection
slot and dependency the new release needs must already be chosen on the step.
A breaking release is refused with the exact caller, as a save of an ordinary
child workflow is (``workflow_consumer_guard``); a disabled caller only gets a
diagnostic.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from core.application.extensions.projected_dataset_bridge import projection_bridge_id
from core.application.workflows.workflow_action_contract import (
    ACTION_CONFIG_KEY,
    ActionContractError,
    action_connections,
    action_implementation_of,
    action_installs,
    parse_action_ref,
)
from core.application.workflows.workflow_where_used import WorkflowConsumer, workflow_consumers

RELEASE_ACTION_CODE = "workflow.action.release_incompatible"


class ActionReleaseBlockedError(ValueError):
    """The release would break enabled steps that call its composite actions."""

    def __init__(self, issues: list[dict[str, Any]]) -> None:
        first = str(issues[0].get("message") or "") if issues else ""
        super().__init__(
            f"The release would break {len(issues)} workflow step(s) that call its actions: {first}"
        )
        self.issues = issues


def _text(value: Any) -> str:
    return str(value or "").strip()


def _caller_action_step(db: Any, consumer: WorkflowConsumer) -> dict[str, Any] | None:
    """The Invoke config of the caller's step when it calls an action, else ``None``."""
    caller = db.get_workflow(consumer.caller_workflow_id)
    if not isinstance(caller, Mapping):
        return None
    for step in caller.get("steps") or []:
        if not isinstance(step, Mapping) or step.get("step_id") != consumer.step_id:
            continue
        override = step.get("override")
        config = override.get("invoke_workflow") if isinstance(override, Mapping) else None
        if isinstance(config, Mapping) and ACTION_CONFIG_KEY in config:
            return {"config": dict(config), "lane_id": step.get("lane_id")}
        return None
    return None


def _step_reasons(
    step: Mapping[str, Any],
    *,
    install_id: str,
    extension_id: str,
    local_id: str,
    workflows: Mapping[str, Mapping[str, Any]],
    action_handlers: Mapping[str, str],
    required_dependencies: frozenset[str],
) -> list[str]:
    from core.application.workflows.workflow_definition_validation import (
        definition_authored_waits,
    )
    from core.application.workflows.workflow_hook_binding_service import mapping_compatibility

    config = step["config"]
    try:
        ref = parse_action_ref(config.get(ACTION_CONFIG_KEY))
    except ActionContractError:
        return []
    if ref.install_id != install_id:
        return []
    workflow = workflows.get(local_id)
    try:
        implementation = action_implementation_of(workflow)
    except ActionContractError:
        implementation = None
    if (
        action_handlers.get(ref.action_id) != local_id
        or implementation is None
        or implementation.action_id != ref.action_id
    ):
        return [f"the release no longer offers action '{ref.action_id}'"]
    assert workflow is not None
    reasons = mapping_compatibility(
        dict(config.get("input_mapping") or {}),
        dict(config.get("output_mapping") or {}),
        workflow.get("settings") or {},
    )
    bound = action_connections(config)
    reasons.extend(
        f"the release adds connection '{slot.slot}', which the step has not chosen"
        for slot in implementation.connections
        if slot.slot not in bound
    )
    chosen = action_installs(config)
    needed = (
        set(required_dependencies) | {slot.extension_id for slot in implementation.connections}
    ) - {extension_id}
    reasons.extend(
        f"the release needs extension '{dependency}', for which the step has no install"
        for dependency in sorted(needed - set(chosen))
    )
    waits = definition_authored_waits(workflow)
    if waits and step.get("lane_id"):
        reasons.append(
            f"the caller runs it in a parallel lane, where its "
            f"{waits[0]['step_kind']} step '{waits[0]['step_id']}' may not wait"
        )
    return reasons


def release_action_issues(
    db: Any,
    cur: Any,
    *,
    account_id: Any,
    install_id: str,
    extension_id: str,
    workflows: Mapping[str, Mapping[str, Any]],
    action_handlers: Mapping[str, str],
    required_dependencies: frozenset[str] = frozenset(),
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """``(blockers, diagnostics)`` of activating a release for one install.

    *workflows* are the published workflows of the new release by local id and
    *action_handlers* its ``action_id → local workflow id`` pairs.
    """
    account = _text(account_id)
    install = _text(install_id)
    if not account or not install:
        return [], []
    prefix = projection_bridge_id(install, "x")[:-1]
    like = prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
    cur.execute(
        """SELECT DISTINCT dependency_ref
             FROM workflow_runtime_dependencies
            WHERE dependency_kind = 'child_workflow'
              AND account_id::text = %s
              AND dependency_ref LIKE %s
            ORDER BY dependency_ref""",
        (account, like),
    )
    targets = [
        _text(row["dependency_ref"] if isinstance(row, Mapping) else row[0])
        for row in cur.fetchall() or []
    ]
    local_ids = {projection_bridge_id(install, local_id): local_id for local_id in workflows}
    blockers: list[dict[str, Any]] = []
    diagnostics: list[dict[str, Any]] = []
    for target in targets:
        local_id = local_ids.get(target, target[len(prefix) :])
        for consumer in workflow_consumers(cur, account_id=account, workflow_id=target):
            if consumer.kind != "invoke":
                continue
            step = _caller_action_step(db, consumer)
            if step is None:
                continue
            for reason in _step_reasons(
                step,
                install_id=install,
                extension_id=_text(extension_id),
                local_id=local_id,
                workflows=workflows,
                action_handlers=action_handlers,
                required_dependencies=required_dependencies,
            ):
                issue = {
                    "code": RELEASE_ACTION_CODE,
                    "message": (
                        f"step '{consumer.step_id}' of '{consumer.caller_workflow_id}': {reason}"
                    ),
                    "consumer": consumer.dump(),
                }
                (blockers if consumer.caller_enabled else diagnostics).append(issue)
    return blockers, diagnostics


def check_release_actions(db: Any, cur: Any, **kwargs: Any) -> list[dict[str, Any]]:
    """Refuse a release that breaks an enabled action step; return diagnostics otherwise."""
    blockers, diagnostics = release_action_issues(db, cur, **kwargs)
    if blockers:
        raise ActionReleaseBlockedError(blockers)
    return diagnostics


__all__ = [
    "RELEASE_ACTION_CODE",
    "ActionReleaseBlockedError",
    "check_release_actions",
    "release_action_issues",
]
