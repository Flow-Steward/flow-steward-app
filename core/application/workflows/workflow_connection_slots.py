"""Connection slots of an extension workflow (ADR 0068, amends ADR 0065).

An extension workflow a person runs directly (from the UI, a webhook, a
schedule or an Invoke) can read a project connection the same way an action
implementation does: its connector steps bind ``{"slot": <name>}`` and the
workflow declares the slot in ``settings.connection_slots``. The project picks
one connection per slot; the host pins that choice into the job when it is
submitted, under the same ``connections`` map an action job carries.

Pure rules only: nothing here reads a store or runs a step.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass
from typing import Any

from core.application.extensions.identity import is_canonical_extension_id
from core.application.workflows.workflow_action_contract import (
    SLOT_PATTERN,
    ActionContractError,
    action_implementation_of,
    composite_action_context,
)

CONNECTION_SLOTS_SETTING = "connection_slots"
#: ``jobs.meta`` key of the connections the host pinned for a slot workflow's job.
#: Host-written at submission; any value a caller sends is dropped first.
CONNECTION_SLOTS_META_KEY = "connection_slots"

_SLOT_FIELDS = frozenset({"slot", "extension_id", "connection_type_ids", "label"})
_LABEL_MAX = 120


class ConnectionSlotContractError(ValueError):
    def __init__(self, code: str, message: str, path: str) -> None:
        super().__init__(message)
        self.code = code
        self.path = path

    def issue(self) -> dict[str, str]:
        return {"code": self.code, "message": str(self), "path": self.path}


@dataclass(frozen=True)
class WorkflowConnectionSlot:
    """One connection a workflow needs: whose extension it is, its types and its label."""

    slot: str
    extension_id: str
    connection_type_ids: tuple[str, ...] = ()
    label: str = ""

    @property
    def display_label(self) -> str:
        return self.label or self.slot

    def accepts_type(self, connection_type: str) -> bool:
        return not self.connection_type_ids or connection_type in self.connection_type_ids

    def contract_key(self) -> tuple[str, tuple[str, ...]]:
        """What a stored choice depends on; a label change keeps the choice valid."""
        return self.extension_id, tuple(sorted(self.connection_type_ids))

    def dump(self) -> dict[str, Any]:
        return {
            "slot": self.slot,
            "extension_id": self.extension_id,
            "connection_type_ids": list(self.connection_type_ids),
            "label": self.display_label,
        }


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _settings(workflow: Mapping[str, Any] | None) -> Mapping[str, Any]:
    if not isinstance(workflow, Mapping):
        return {}
    settings = workflow.get("settings")
    if isinstance(settings, str):
        try:
            settings = json.loads(settings or "{}")
        except ValueError:
            return {}
    return settings if isinstance(settings, Mapping) else {}


def _parse_slot(raw: Any, path: str) -> WorkflowConnectionSlot:
    if not isinstance(raw, Mapping):
        raise ConnectionSlotContractError(
            "workflow.connection_slots.slot_invalid", "A connection slot is an object", path
        )
    unknown = sorted(set(raw) - _SLOT_FIELDS)
    if unknown:
        raise ConnectionSlotContractError(
            "workflow.connection_slots.field_unknown",
            f"A connection slot does not have the field '{unknown[0]}'",
            f"{path}.{unknown[0]}",
        )
    name = _text(raw.get("slot"))
    if not SLOT_PATTERN.fullmatch(name):
        raise ConnectionSlotContractError(
            "workflow.connection_slots.name_invalid",
            "A slot name starts with a lowercase letter and holds lowercase letters, "
            "digits and underscores",
            f"{path}.slot",
        )
    extension_id = _text(raw.get("extension_id"))
    if not is_canonical_extension_id(extension_id):
        raise ConnectionSlotContractError(
            "workflow.connection_slots.extension_invalid",
            f"Slot '{name}' names the extension its connection belongs to by package id",
            f"{path}.extension_id",
        )
    types = raw.get("connection_type_ids", [])
    if not isinstance(types, list) or any(not _text(item) for item in types):
        raise ConnectionSlotContractError(
            "workflow.connection_slots.types_invalid",
            f"Slot '{name}' lists its connection types as non-empty strings",
            f"{path}.connection_type_ids",
        )
    label = raw.get("label", "")
    if not isinstance(label, str) or len(label.strip()) > _LABEL_MAX:
        raise ConnectionSlotContractError(
            "workflow.connection_slots.label_invalid",
            f"The label of slot '{name}' is text of at most {_LABEL_MAX} characters",
            f"{path}.label",
        )
    return WorkflowConnectionSlot(
        name,
        extension_id,
        tuple(dict.fromkeys(_text(item) for item in types)),
        label.strip(),
    )


def parse_connection_slots(raw: Any) -> tuple[WorkflowConnectionSlot, ...]:
    """The declared slots, or :class:`ConnectionSlotContractError` naming the bad field."""
    base = f"settings.{CONNECTION_SLOTS_SETTING}"
    if not isinstance(raw, list):
        raise ConnectionSlotContractError(
            "workflow.connection_slots.invalid", "The connection slots are a list", base
        )
    slots: list[WorkflowConnectionSlot] = []
    for index, row in enumerate(raw):
        slot = _parse_slot(row, f"{base}[{index}]")
        if any(existing.slot == slot.slot for existing in slots):
            raise ConnectionSlotContractError(
                "workflow.connection_slots.duplicate",
                f"Slot '{slot.slot}' is declared twice",
                f"{base}[{index}].slot",
            )
        slots.append(slot)
    return tuple(slots)


def raw_connection_slots(workflow: Mapping[str, Any] | None) -> Any:
    """The raw declaration of *workflow*, or ``None`` when it declares none."""
    return _settings(workflow).get(CONNECTION_SLOTS_SETTING)


def connection_slots_of(workflow: Mapping[str, Any] | None) -> tuple[WorkflowConnectionSlot, ...]:
    """The parsed slots of *workflow*; empty without a declaration. Raises when malformed."""
    raw = raw_connection_slots(workflow)
    return () if raw is None else parse_connection_slots(raw)


def is_installed_extension_workflow(workflow: Mapping[str, Any] | None) -> bool:
    """A workflow an extension install projected: the only kind whose slots the host fills."""
    return isinstance(workflow, Mapping) and bool(_text(workflow.get("source_install_id")))


def installed_connection_slots(
    workflow: Mapping[str, Any] | None,
) -> tuple[WorkflowConnectionSlot, ...]:
    """Slots the project fills for *workflow*; empty for any other workflow.

    An account's own workflow, including an author's workflow in a Development
    environment, is never filled: there a step names the slot it will read and
    a connection to test with (ADR 0065). A malformed declaration reads as no
    slots here; the release that carries it was refused when it was stored.
    """
    if not is_installed_extension_workflow(workflow):
        return ()
    try:
        return connection_slots_of(workflow)
    except ConnectionSlotContractError:
        return ()


def declared_slot_names(workflow: Mapping[str, Any] | None) -> frozenset[str]:
    """Every slot a connector step of *workflow* may read: its own and its action's."""
    names: set[str] = set()
    with suppress(ConnectionSlotContractError):
        names.update(slot.slot for slot in connection_slots_of(workflow))
    try:
        implementation = action_implementation_of(workflow)
    except ActionContractError:
        implementation = None
    if implementation is not None:
        names.update(slot.slot for slot in implementation.connections)
    return frozenset(names)


def _step_connector_config(step: Mapping[str, Any]) -> Mapping[str, Any]:
    override = step.get("override")
    config = override.get("connector") if isinstance(override, Mapping) else None
    return config if isinstance(config, Mapping) else {}


def connector_step_slot(step: Mapping[str, Any]) -> str:
    """The slot a connector step reads its connection from, or ``""``."""
    config = _step_connector_config(step)
    for owner, key in (
        (step, "connection_binding_jsonb"),
        (step, "connection_binding"),
        (config, "connection_binding_jsonb"),
        (config, "connection_binding"),
    ):
        binding = owner.get(key)
        if isinstance(binding, Mapping) and binding:
            return _text(binding.get("slot"))
    return ""


def undeclared_slot_issues(
    workflow: Mapping[str, Any], *, step: Mapping[str, Any], step_path: str
) -> list[dict[str, str]]:
    """A connector step that reads a slot its workflow does not declare cannot run.

    A step that also names a connection (the author's own workflow, testing
    before packaging; ADR 0065) runs with that connection and is left alone.
    """
    slot = connector_step_slot(step)
    if not slot or slot in declared_slot_names(workflow):
        return []
    config = _step_connector_config(step)
    for owner in (step, config):
        for key in ("connection_binding_jsonb", "connection_binding"):
            binding = owner.get(key)
            if isinstance(binding, Mapping) and _text(binding.get("connection_id")):
                return []
    return [
        {
            "code": "workflow.connection_slots.undeclared",
            "message": (
                f"This step reads connection slot '{slot}', which the workflow does not "
                f"declare in settings.{CONNECTION_SLOTS_SETTING}."
            ),
            "path": f"{step_path}.connection_binding_jsonb.slot",
            "severity": "error",
        }
    ]


def workflow_slot_owner(workflow: Mapping[str, Any]) -> tuple[str, str]:
    """``(source install id, local workflow id)`` that keys a project's choices.

    The release id never appears: a choice survives every update of the install.
    A projected row names its workflow ``extension.<install>.<local id>``.
    """
    install_id = _text(workflow.get("source_install_id"))
    local_id = _text(workflow.get("local_workflow_id"))
    if not local_id:
        workflow_id = _text(workflow.get("workflow_id"))
        prefix = f"extension.{install_id}."
        local_id = workflow_id.removeprefix(prefix) if install_id else workflow_id
    return install_id, local_id


def slot_connection_context(meta: Any) -> dict[str, Any]:
    """The host-pinned slot connections of a job's ``meta``; empty for any other job."""
    if isinstance(meta, str):
        try:
            meta = json.loads(meta or "{}")
        except ValueError:
            return {}
    context = meta.get(CONNECTION_SLOTS_META_KEY) if isinstance(meta, Mapping) else None
    return dict(context) if isinstance(context, Mapping) else {}


def job_connection_context(meta: Any) -> dict[str, Any]:
    """Where a job's connector steps read their slots and installs from.

    The action context of an action child (ADR 0065) or, for a slot workflow
    started any other way, the connections pinned at submission (ADR 0068).
    Both carry the same ``connections`` and ``installs`` maps.
    """
    return composite_action_context(meta) or slot_connection_context(meta)


__all__ = [
    "CONNECTION_SLOTS_META_KEY",
    "CONNECTION_SLOTS_SETTING",
    "ConnectionSlotContractError",
    "WorkflowConnectionSlot",
    "connection_slots_of",
    "connector_step_slot",
    "declared_slot_names",
    "installed_connection_slots",
    "is_installed_extension_workflow",
    "job_connection_context",
    "parse_connection_slots",
    "raw_connection_slots",
    "slot_connection_context",
    "undeclared_slot_issues",
    "workflow_slot_owner",
]
