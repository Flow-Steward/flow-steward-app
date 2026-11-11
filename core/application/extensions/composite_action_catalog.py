"""What the workflow builder needs to offer a composite action as a step (ADR 0065).

For one install: the workflow each composite action stands for, the installs of
the extensions it depends on and, per connection slot, the project connections
a person may choose. Everything resolves through ``composite_action_resolution``,
the same reading save-time validation and the Invoke runtime use.
"""

from __future__ import annotations

from typing import Any

from core.application.auth.config import DEFAULT_ACCOUNT_ID
from core.application.extensions.composite_action_resolution import installed_composites


def _text(value: Any) -> str:
    return str(value or "").strip()


def _active_installs(
    connection: Any, *, account_id: str
) -> tuple[dict[str, list[str]], dict[str, str]]:
    """Active installs the account may use: ids by extension, and each install's release."""
    from core.application.extensions.install import DBExtensionInstallStore, InstallState

    records = DBExtensionInstallStore(connection).list_for_account(
        account_id or DEFAULT_ACCOUNT_ID, include_global=True, include_inactive_owned=True
    )
    installs: dict[str, list[str]] = {}
    releases: dict[str, str] = {}
    for record in records:
        state = getattr(record, "state", "")
        if _text(getattr(state, "value", state)) != str(InstallState.active):
            continue
        extension_id = _text(getattr(record, "extension_id", ""))
        install_id = _text(getattr(record, "install_id", ""))
        if extension_id and install_id:
            installs.setdefault(extension_id, []).append(install_id)
            releases[install_id] = _text(getattr(record, "release_id", ""))
    return {key: sorted(set(value)) for key, value in installs.items()}, releases


def _extension_name(connection: Any, release_id: str, *, locale: str) -> str:
    """The name a person knows the extension by, from the release an install pins."""
    from core.application.extensions.display_labels import localized_extension_display_name
    from core.application.extensions.runtime_store_db import DBExtensionRuntimeStore

    release = DBExtensionRuntimeStore(connection).get_release(release_id) if release_id else None
    descriptor = getattr(release, "descriptor", None)
    return localized_extension_display_name(descriptor, locale=locale) if descriptor else ""


def composite_operation_payloads(
    connection: Any,
    *,
    install_id: str,
    account_id: str = "",
    project_id: str = "",
    connection_store: Any = None,
    locale: str = "",
) -> dict[str, dict[str, Any]]:
    """``action_id → implementation`` for every composite action of *install_id*.

    ``installs`` holds the dependency installs that are unambiguous (exactly one
    active install of that extension); ``install_choices`` lists all of them so
    the builder can ask when there are several or say what is missing.
    """
    from core.application.extensions.workflow_install_contract import workflow_install_connections

    installed = installed_composites(connection, install_id=install_id, account_id=account_id)
    if installed is None or not installed.actions:
        return {}
    active, releases = _active_installs(connection, account_id=_text(account_id))
    names: dict[str, str] = {}
    payloads: dict[str, dict[str, Any]] = {}
    for action_id, action in installed.actions.items():
        needed = (
            set(installed.required_dependencies)
            | {slot.extension_id for slot in action.implementation.connections}
        ) - {installed.extension_id}
        choices = {extension_id: active.get(extension_id, []) for extension_id in sorted(needed)}
        installs = {key: value[0] for key, value in choices.items() if len(value) == 1}
        slots: list[dict[str, Any]] = []
        for slot in action.implementation.connections:
            slot_install = (
                installed.install_id
                if slot.extension_id == installed.extension_id
                else installs.get(slot.extension_id, "")
            )
            connections = (
                workflow_install_connections(
                    connection_store, project_id=project_id, install_id=slot_install
                )
                if connection_store is not None and project_id and slot_install
                else []
            )
            if slot.connection_type_ids:
                connections = [
                    row
                    for row in connections
                    if _text(row.get("connection_type")) in slot.connection_type_ids
                ]
            if slot_install and slot_install not in names:
                names[slot_install] = _extension_name(
                    connection, releases.get(slot_install, ""), locale=locale
                )
            slots.append(
                {
                    **slot.dump(),
                    "install_id": slot_install,
                    # What the builder calls the connection: the extension it belongs to.
                    "extension_name": names.get(slot_install, ""),
                    "connections": connections,
                }
            )
        payloads[action_id] = {
            "kind": "workflow",
            "workflow_id": action.workflow_id(installed.install_id),
            "installs": installs,
            "install_choices": choices,
            "slots": slots,
        }
    return payloads


__all__ = ["composite_operation_payloads"]
