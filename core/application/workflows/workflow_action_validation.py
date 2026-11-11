"""Save-time rules of an Invoke that calls an extension action (ADR 0065).

The runtime resolves the same things again when the step runs
(``orchestration.composite_action_runtime``); this is the earlier answer a
person gets in the builder. Both resolve the action through
``composite_action_resolution``, so they agree on which workflow it stands for.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from core.application.extensions.composite_action_resolution import (
    InstalledComposites,
    installed_composites_for_store,
)
from core.application.workflows.workflow_action_contract import (
    ACTION_CONFIG_KEY,
    ACTION_CONNECTIONS_KEY,
    ACTION_INSTALLS_KEY,
    ActionContractError,
    action_config_issues,
    action_connections,
    action_installs,
    is_action_implementation,
    is_installed_action_implementation,
    parse_action_ref,
)

_BASE = "override.invoke_workflow"


def _issue(code: str, message: str, path: str) -> dict[str, str]:
    return {"code": code, "message": message, "path": path}


def _usable(
    db: Any, *, install_id: str, extension_id: str, account_id: str
) -> InstalledComposites | None:
    installed = installed_composites_for_store(db, install_id=install_id, account_id=account_id)
    if installed is None or installed.extension_id != extension_id or not installed.active:
        return None
    return installed


def action_invoke_issues(
    db: Any,
    *,
    spec: Mapping[str, Any],
    child_workflow: Mapping[str, Any] | None,
    parent_workflow: Mapping[str, Any] | None = None,
    account_id: str = "",
) -> list[dict[str, str]]:
    """Issues of one Invoke config against the action it names; paths are step-relative."""
    from core.application.workflows.extension_connection_validation import (
        _connection_matches,
        _get_connection_metadata,
    )

    source = dict(spec or {})
    if ACTION_CONFIG_KEY not in source:
        if is_installed_action_implementation(child_workflow):
            return [
                _issue(
                    "workflow.action.origin_required",
                    "This workflow implements an extension action and runs only as that "
                    "action. Add the action of its extension instead.",
                    f"{_BASE}.workflow_id",
                )
            ]
        return []
    shape = action_config_issues(source)
    if shape:
        return shape
    if is_action_implementation(parent_workflow) and not is_installed_action_implementation(
        parent_workflow
    ):
        # The author's own implementation: bundle rules check it when it is packaged.
        return []
    try:
        ref = parse_action_ref(source[ACTION_CONFIG_KEY])
    except ActionContractError as exc:  # pragma: no cover - action_config_issues checked it
        return [exc.issue()]
    if not ref.install_id:
        return [
            _issue(
                "workflow.extension_install_missing",
                "Choose the installed extension action again before saving this step.",
                f"{_BASE}.{ACTION_CONFIG_KEY}.install_id",
            )
        ]
    installed = _usable(
        db, install_id=ref.install_id, extension_id=ref.extension_id, account_id=account_id
    )
    if installed is None:
        return [
            _issue(
                "workflow.extension_unavailable",
                f"Extension '{ref.extension_id}' is not installed and active for this account.",
                f"{_BASE}.{ACTION_CONFIG_KEY}.install_id",
            )
        ]
    action = installed.action(ref.action_id)
    if action is None:
        return [
            _issue(
                "workflow.extension_operation_unavailable",
                f"Extension '{ref.extension_id}' no longer offers action '{ref.action_id}'.",
                f"{_BASE}.{ACTION_CONFIG_KEY}.action_id",
            )
        ]
    if str(source.get("workflow_id") or "").strip() != action.workflow_id(ref.install_id):
        return [
            _issue(
                "workflow.action.target_mismatch",
                f"Action '{ref.action_id}' is implemented by another workflow than this "
                "step names. Choose the action again.",
                f"{_BASE}.workflow_id",
            )
        ]
    issues: list[dict[str, str]] = []
    chosen = action_installs(source)
    installs = {ref.extension_id: ref.install_id}
    needed = set(installed.required_dependencies) | {
        slot.extension_id for slot in action.implementation.connections
    }
    for extension_id in sorted(needed - {ref.extension_id}):
        install_id = chosen.get(extension_id, "")
        path = f"{_BASE}.{ACTION_INSTALLS_KEY}.{extension_id}"
        if not install_id:
            issues.append(
                _issue(
                    "workflow.extension_install_missing",
                    f"Choose which install of extension '{extension_id}' this action uses.",
                    path,
                )
            )
        elif (
            _usable(db, install_id=install_id, extension_id=extension_id, account_id=account_id)
            is None
        ):
            issues.append(
                _issue(
                    "workflow.extension_unavailable",
                    f"Extension '{extension_id}' is not installed and active for this account.",
                    path,
                )
            )
        else:
            installs[extension_id] = install_id
    bindings = action_connections(source)
    declared = {slot.slot for slot in action.implementation.connections}
    for name in sorted(set(bindings) - declared):
        issues.append(
            _issue(
                "workflow.action.slot_unknown",
                f"Action '{ref.action_id}' has no connection slot '{name}'.",
                f"{_BASE}.{ACTION_CONNECTIONS_KEY}.{name}",
            )
        )
    for slot in action.implementation.connections:
        path = f"{_BASE}.{ACTION_CONNECTIONS_KEY}.{slot.slot}.connection_id"
        connection_id = (bindings.get(slot.slot) or {}).get("connection_id", "")
        if not connection_id:
            issues.append(
                _issue(
                    "workflow.connection_required",
                    "Choose a connection for this extension action before running this workflow.",
                    path,
                )
            )
            continue
        install_id = installs.get(slot.extension_id, "")
        if not install_id:
            continue  # the missing install is already reported
        if not _connection_matches(
            _get_connection_metadata(db, connection_id),
            extension_id=slot.extension_id,
            install_id=install_id,
            connection_type_ids=frozenset(slot.connection_type_ids),
        ):
            issues.append(
                _issue(
                    "workflow.connection_unavailable",
                    "The selected extension connection is no longer available.",
                    path,
                )
            )
    return issues


__all__ = ["action_invoke_issues"]
