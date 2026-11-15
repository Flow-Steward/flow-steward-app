"""Which connection fills each slot of an extension workflow in a project (ADR 0068).

One rule decides the connection of a slot, and every reader uses it: the
Overview panel, the pin at job submission and the release check.

* The project's stored choice, while that connection is still enabled, healthy
  and of a type the slot accepts.
* Otherwise, when the project has exactly one usable connection for the slot,
  that one ("chosen automatically").
* Otherwise nothing: the workflow is not ready in that project and says why.

A stored choice that stopped fitting (disabled, failed, another type after an
update) is never replaced silently; the person chooses again.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from core.application.extensions.connection_viability import project_connection_is_viable
from core.application.management.errors import ServiceConflictError, ServiceValidationError
from core.application.workflows.workflow_connection_slot_repository import (
    SlotOwner,
    WorkflowConnectionSlotRepository,
)
from core.application.workflows.workflow_connection_slots import (
    WorkflowConnectionSlot,
    installed_connection_slots,
    workflow_slot_owner,
)

CHOICE_CHOSEN = "chosen"
CHOICE_AUTOMATIC = "automatic"
CHOICE_NONE = "none"

NOT_READY_CODE = "workflow.connection_slots.not_ready"


def _text(value: Any) -> str:
    return str(value or "").strip()


@dataclass(frozen=True)
class SlotState:
    """One slot of one workflow in one project: what fills it, or why nothing does."""

    slot: WorkflowConnectionSlot
    connection_id: str = ""
    choice: str = CHOICE_NONE
    reason: str = ""
    options: tuple[dict[str, Any], ...] = field(default_factory=tuple)
    install_id: str = ""

    @property
    def ready(self) -> bool:
        return bool(self.connection_id)

    def dump(self) -> dict[str, Any]:
        return {
            **self.slot.dump(),
            "connection_id": self.connection_id or None,
            "choice": self.choice,
            "ready": self.ready,
            "reason": self.reason,
            "options": [dict(option) for option in self.options],
        }


def _option(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "connection_id": _text(row.get("connection_id")),
        "display_name": _text(row.get("display_name")) or _text(row.get("connection_id")),
        "connection_type": _text(row.get("connection_type")),
        "extension_install_id": _text(row.get("extension_install_id")),
    }


def usable_connections(
    slot: WorkflowConnectionSlot, candidates: Iterable[Mapping[str, Any]]
) -> list[dict[str, Any]]:
    """The project's connections that may fill *slot*: its extension, type and health."""
    return [
        _option(row)
        for row in candidates
        if _text(row.get("extension_id")) == slot.extension_id
        and slot.accepts_type(_text(row.get("connection_type")))
        and project_connection_is_viable(row)
    ]


def _types_text(slot: WorkflowConnectionSlot) -> str:
    return ", ".join(slot.connection_type_ids) or "any"


def slot_state(
    slot: WorkflowConnectionSlot,
    *,
    stored: str,
    candidates: Iterable[Mapping[str, Any]],
    preferred: str = "",
) -> SlotState:
    """Apply the one rule to one slot. *preferred* is a parent run's connection (inherit)."""
    rows = list(candidates)
    options = usable_connections(slot, rows)
    by_id = {option["connection_id"]: option for option in options}
    label = slot.display_label
    if preferred and preferred in by_id:
        option = by_id[preferred]
        return SlotState(
            slot, preferred, CHOICE_CHOSEN, "", tuple(options), option["extension_install_id"]
        )
    if stored:
        if stored in by_id:
            option = by_id[stored]
            return SlotState(
                slot, stored, CHOICE_CHOSEN, "", tuple(options), option["extension_install_id"]
            )
        known = next((row for row in rows if _text(row.get("connection_id")) == stored), None)
        name = _text((known or {}).get("display_name")) or stored
        return SlotState(
            slot,
            reason=(
                f"The connection chosen for '{label}' ({name}) is disabled, failing or no "
                "longer of a type this workflow accepts. Choose another connection."
            ),
            options=tuple(options),
        )
    if len(options) == 1:
        option = options[0]
        return SlotState(
            slot,
            option["connection_id"],
            CHOICE_AUTOMATIC,
            "",
            tuple(options),
            option["extension_install_id"],
        )
    if not options:
        reason = (
            f"This workflow needs a '{label}' connection. Create one of extension "
            f"'{slot.extension_id}' (type: {_types_text(slot)}) in this project."
        )
    else:
        reason = (
            f"This workflow needs a '{label}' connection and the project has "
            f"{len(options)}. Choose the one it uses."
        )
    return SlotState(slot, reason=reason, options=tuple(options))


class WorkflowConnectionSlotsNotReadyError(ServiceConflictError):
    """A run or trigger of a slot workflow was refused before it started."""

    code = NOT_READY_CODE

    def __init__(self, workflow_id: str, project_id: str, states: list[SlotState]) -> None:
        missing = [state for state in states if not state.ready]
        message = " ".join(state.reason for state in missing) or (
            "This workflow is missing a connection."
        )
        super().__init__(
            message,
            payload={
                "error": message,
                "code": NOT_READY_CODE,
                "workflow_id": workflow_id,
                "project_id": project_id,
                "slots": [
                    {
                        "slot": state.slot.slot,
                        "label": state.slot.display_label,
                        "reason": state.reason,
                    }
                    for state in missing
                ],
            },
        )
        self.workflow_id = workflow_id
        self.project_id = project_id
        self.states = missing


def slot_owner(workflow: Mapping[str, Any], *, account_id: str, project_id: str) -> SlotOwner:
    install_id, local_id = workflow_slot_owner(workflow)
    return SlotOwner(_text(account_id), _text(project_id), install_id, local_id)


def slot_states(
    repository: WorkflowConnectionSlotRepository,
    slots: Iterable[WorkflowConnectionSlot],
    *,
    owner: SlotOwner,
    preferred: Mapping[str, str] | None = None,
) -> list[SlotState]:
    wanted = list(slots)
    if not wanted:
        return []
    stored = repository.choices(owner)
    candidates = repository.candidates(
        account_id=owner.account_id,
        project_id=owner.project_id,
        extension_ids={slot.extension_id for slot in wanted},
    )
    inherited = dict(preferred or {})
    return [
        slot_state(
            slot,
            stored=stored.get(slot.slot, ""),
            candidates=candidates,
            preferred=_text(inherited.get(slot.slot)),
        )
        for slot in wanted
    ]


def pinned_context(states: Iterable[SlotState]) -> dict[str, Any]:
    """The ``connections``/``installs`` maps a job runs with (the action-context shape)."""
    connections: dict[str, str] = {}
    installs: dict[str, str] = {}
    for state in states:
        connections[state.slot.slot] = state.connection_id
        if state.install_id:
            installs.setdefault(state.slot.extension_id, state.install_id)
    return {"connections": connections, "installs": installs}


class WorkflowConnectionSlotService:
    """Read and change a project's slot choices through the registry store."""

    def __init__(self, db: Any, *, account_id: str, actor: str = "") -> None:
        self.db = db
        self.account_id = _text(account_id)
        self.actor = _text(actor)

    def describe(
        self,
        workflow: Mapping[str, Any],
        project_id: str,
        *,
        slots: Iterable[WorkflowConnectionSlot] | None = None,
    ) -> dict[str, Any]:
        declared = list(installed_connection_slots(workflow) if slots is None else slots)
        owner = slot_owner(workflow, account_id=self.account_id, project_id=project_id)
        with self.db._cursor() as cur:
            states = slot_states(WorkflowConnectionSlotRepository(cur), declared, owner=owner)
        return {
            "workflow_id": _text(workflow.get("workflow_id")),
            "project_id": _text(project_id),
            "ready": all(state.ready for state in states),
            "slots": [state.dump() for state in states],
        }

    def choose(
        self,
        workflow: Mapping[str, Any],
        project_id: str,
        *,
        slot: str,
        connection_id: str,
        slots: Iterable[WorkflowConnectionSlot] | None = None,
    ) -> dict[str, Any]:
        """Store *connection_id* for *slot*, or clear the choice when it is empty."""
        declared = list(installed_connection_slots(workflow) if slots is None else slots)
        target = next((row for row in declared if row.slot == slot), None)
        if target is None:
            raise ServiceValidationError(
                f"This workflow declares no connection slot '{slot}'",
                payload={"error": "unknown slot", "code": "workflow.connection_slots.unknown"},
            )
        owner = slot_owner(workflow, account_id=self.account_id, project_id=project_id)
        chosen = _text(connection_id)
        with self.db._transaction() as cur:
            repository = WorkflowConnectionSlotRepository(cur)
            if not chosen:
                repository.clear(owner, slot)
            else:
                candidates = repository.candidates(
                    account_id=owner.account_id,
                    project_id=owner.project_id,
                    extension_ids={target.extension_id},
                )
                if chosen not in {
                    row["connection_id"] for row in usable_connections(target, candidates)
                }:
                    raise ServiceValidationError(
                        f"Connection '{chosen}' cannot fill '{target.display_label}': it must be "
                        f"an enabled connection of extension '{target.extension_id}' "
                        f"(type: {_types_text(target)}) in this project.",
                        payload={
                            "error": "connection not usable for this slot",
                            "code": "workflow.connection_slots.connection_unusable",
                        },
                    )
                repository.save(owner, slot, chosen, actor=self.actor)
        return self.describe(workflow, project_id, slots=declared)

    def pin(
        self,
        workflow: Mapping[str, Any],
        project_id: str,
        *,
        preferred: Mapping[str, str] | None = None,
    ) -> dict[str, Any] | None:
        """The context a new job of *workflow* runs with; raises when a slot is unfilled."""
        declared = installed_connection_slots(workflow)
        if not declared:
            return None
        owner = slot_owner(workflow, account_id=self.account_id, project_id=project_id)
        with self.db._cursor() as cur:
            states = slot_states(
                WorkflowConnectionSlotRepository(cur), declared, owner=owner, preferred=preferred
            )
        if not all(state.ready for state in states):
            raise WorkflowConnectionSlotsNotReadyError(
                _text(workflow.get("workflow_id")), _text(project_id), states
            )
        return pinned_context(states)


def delete_install_slot_choices(db: Any, *, account_id: str, install_id: str) -> int:
    """Remove every project's slot choices of an install (uninstall)."""
    transaction = getattr(db, "_transaction", None)
    if not callable(transaction) or not _text(install_id):
        return 0
    with transaction() as cur:
        return WorkflowConnectionSlotRepository(cur).delete_install(
            account_id=_text(account_id), install_id=_text(install_id)
        )


__all__ = [
    "CHOICE_AUTOMATIC",
    "CHOICE_CHOSEN",
    "CHOICE_NONE",
    "NOT_READY_CODE",
    "SlotState",
    "WorkflowConnectionSlotService",
    "WorkflowConnectionSlotsNotReadyError",
    "delete_install_slot_choices",
    "pinned_context",
    "slot_owner",
    "slot_state",
    "slot_states",
    "usable_connections",
]
