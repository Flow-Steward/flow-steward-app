"""An extension update never leaves a linked workflow without its connection (ADR 0068).

Before a release's workflows are projected, every slot the release adds, or
whose extension or connection types it changes, is resolved in every project
that links the workflow, with the one rule of
``workflow_connection_slot_service``. A slot that would stay empty blocks the
activation with the workflow, the project and the reason, as a changed hook
does (ADR 0038). Nothing is chosen or rewritten silently.

When the release activates, the choices of slots it no longer declares are
removed in the same transaction.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from core.application.workflows.workflow_action_contract import is_action_implementation
from core.application.workflows.workflow_connection_slot_repository import (
    SlotOwner,
    WorkflowConnectionSlotRepository,
)
from core.application.workflows.workflow_connection_slot_service import slot_states
from core.application.workflows.workflow_connection_slots import (
    ConnectionSlotContractError,
    WorkflowConnectionSlot,
    connection_slots_of,
)

RELEASE_NEEDS_CHOICE_CODE = "workflow.connection_slots.release_needs_choice"


class ConnectionSlotReleaseBlockedError(ValueError):
    def __init__(self, issues: list[dict[str, Any]]) -> None:
        super().__init__(
            "The release needs a connection the project has not chosen: "
            + "; ".join(issue["message"] for issue in issues[:5])
        )
        self.issues = issues


def _slots(workflow: Mapping[str, Any]) -> dict[str, WorkflowConnectionSlot]:
    if is_action_implementation(workflow):
        return {}
    try:
        return {slot.slot: slot for slot in connection_slots_of(workflow)}
    except ConnectionSlotContractError:
        return {}


def release_slot_declarations(
    workflows: Mapping[str, Mapping[str, Any]],
) -> dict[str, dict[str, WorkflowConnectionSlot]]:
    """``local workflow id → slot → declaration`` of every workflow that declares slots."""
    declared: dict[str, dict[str, WorkflowConnectionSlot]] = {}
    for local_id, workflow in workflows.items():
        slots = _slots(workflow)
        if slots:
            declared[str(local_id)] = slots
    return declared


def changed_slots(
    new: Mapping[str, Mapping[str, WorkflowConnectionSlot]],
    old: Mapping[str, Mapping[str, WorkflowConnectionSlot]],
) -> dict[str, list[WorkflowConnectionSlot]]:
    """Slots a release adds or whose extension or accepted types it changes."""
    changed: dict[str, list[WorkflowConnectionSlot]] = {}
    for local_id, slots in new.items():
        before = old.get(local_id, {})
        rows = [
            slot
            for name, slot in slots.items()
            if name not in before or before[name].contract_key() != slot.contract_key()
        ]
        if rows:
            changed[local_id] = rows
    return changed


def check_and_apply_release_slots(
    cur: Any,
    *,
    account_id: str,
    install_id: str,
    release_id: str,
    workflows: Mapping[str, Mapping[str, Any]],
    previous_workflows: Mapping[str, Mapping[str, Any]],
    display_names: Mapping[str, str] | None = None,
) -> int:
    """Refuse a release that leaves a linked workflow without a connection, else clean up.

    Returns the number of removed choices of slots the release no longer declares.
    """
    repository = WorkflowConnectionSlotRepository(cur)
    new = release_slot_declarations(workflows)
    changed = changed_slots(new, release_slot_declarations(previous_workflows))
    issues: list[dict[str, Any]] = []
    if changed:
        workflow_ids = {f"extension.{install_id}.{local_id}": local_id for local_id in changed}
        names = dict(display_names or {})
        for link in repository.linked_projects(account_id=account_id, workflow_ids=workflow_ids):
            local_id = workflow_ids[link["workflow_id"]]
            owner = SlotOwner(account_id, link["project_id"], install_id, local_id)
            for state in slot_states(repository, changed[local_id], owner=owner):
                if state.ready:
                    continue
                name = names.get(local_id) or local_id
                issues.append(
                    {
                        "code": RELEASE_NEEDS_CHOICE_CODE,
                        "message": (
                            f"workflow '{name}' in project {link['project_id']}: {state.reason}"
                        ),
                        "workflow_id": link["workflow_id"],
                        "project_id": link["project_id"],
                        "slot": state.slot.slot,
                        "release_id": release_id,
                    }
                )
    if issues:
        raise ConnectionSlotReleaseBlockedError(issues)
    keep: Iterable[tuple[str, str]] = [
        (local_id, slot) for local_id, slots in new.items() for slot in slots
    ]
    return repository.delete_slots(account_id=account_id, install_id=install_id, keep=keep)


__all__ = [
    "RELEASE_NEEDS_CHOICE_CODE",
    "ConnectionSlotReleaseBlockedError",
    "changed_slots",
    "check_and_apply_release_slots",
    "release_slot_declarations",
]
