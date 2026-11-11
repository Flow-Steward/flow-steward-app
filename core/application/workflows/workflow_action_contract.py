"""A composite action: an extension action implemented by a workflow (ADR 0065).

Two shapes live here, both pure rules shared by validators, projection and the
Invoke runtime; nothing in this module runs a child or reads a store.

* The implementation marker ``settings.action_implementation`` on the workflow
  that implements an action. It names the action and the connection slots the
  workflow's connector steps read. An installed (projected) implementation is
  never a workflow a person browses or launches: it runs only as the child of
  an action Invoke.
* The action origin of an Invoke: ``override.invoke_workflow.action`` next to
  the direct ``workflow_id``, with the installs and slot connections the caller
  chose. The origin is what entitles the Invoke to call a hidden
  implementation; the host verifies that the action really resolves to that
  workflow.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from core.application.extensions.identity import is_canonical_extension_id

ACTION_IMPLEMENTATION_SETTING = "action_implementation"
ACTION_CONFIG_KEY = "action"
ACTION_INSTALLS_KEY = "installs"
ACTION_CONNECTIONS_KEY = "connections"
SLOT_BINDING_KEY = "slot"
#: ``jobs.meta`` key of the action context a child job runs with, written by the
#: host when the Invoke creates the child; never read from author input.
COMPOSITE_ACTION_META_KEY = "composite_action"

SLOT_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,62}$")

_MARKER_FIELDS = frozenset({"action_id", "connections"})
_SLOT_FIELDS = frozenset({"slot", "extension_id", "connection_type_ids"})
_ACTION_REF_FIELDS = frozenset({"extension_id", "install_id", "action_id"})


class ActionContractError(ValueError):
    def __init__(self, code: str, message: str, path: str) -> None:
        super().__init__(message)
        self.code = code
        self.path = path

    def issue(self) -> dict[str, str]:
        return {"code": self.code, "message": str(self), "path": self.path}


@dataclass(frozen=True)
class ConnectionSlot:
    """One connection the caller supplies: which extension it belongs to and its types."""

    slot: str
    extension_id: str
    connection_type_ids: tuple[str, ...] = ()

    def dump(self) -> dict[str, Any]:
        return {
            "slot": self.slot,
            "extension_id": self.extension_id,
            "connection_type_ids": list(self.connection_type_ids),
        }


@dataclass(frozen=True)
class ActionImplementation:
    action_id: str
    connections: tuple[ConnectionSlot, ...] = ()

    def slot(self, name: str) -> ConnectionSlot | None:
        return next((row for row in self.connections if row.slot == name), None)

    def dump(self) -> dict[str, Any]:
        return {
            "action_id": self.action_id,
            "connections": [row.dump() for row in self.connections],
        }


@dataclass(frozen=True)
class ActionRef:
    """The action an Invoke stands for: one action of one installed extension."""

    extension_id: str
    install_id: str
    action_id: str

    def dump(self) -> dict[str, str]:
        return {
            "extension_id": self.extension_id,
            "install_id": self.install_id,
            "action_id": self.action_id,
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


def _parse_slot(raw: Any, path: str) -> ConnectionSlot:
    if not isinstance(raw, Mapping):
        raise ActionContractError(
            "workflow.action.slot_invalid", "A connection slot is an object", path
        )
    unknown = sorted(set(raw) - _SLOT_FIELDS)
    if unknown:
        raise ActionContractError(
            "workflow.action.slot_field_unknown",
            f"A connection slot does not have the field '{unknown[0]}'",
            f"{path}.{unknown[0]}",
        )
    name = _text(raw.get("slot"))
    if not SLOT_PATTERN.fullmatch(name):
        raise ActionContractError(
            "workflow.action.slot_name_invalid",
            "A slot name starts with a lowercase letter and holds lowercase letters, "
            "digits and underscores",
            f"{path}.slot",
        )
    extension_id = _text(raw.get("extension_id"))
    if not is_canonical_extension_id(extension_id):
        raise ActionContractError(
            "workflow.action.slot_extension_invalid",
            f"Slot '{name}' names the extension its connection belongs to by package id",
            f"{path}.extension_id",
        )
    types = raw.get("connection_type_ids", [])
    if not isinstance(types, list) or any(not _text(item) for item in types):
        raise ActionContractError(
            "workflow.action.slot_types_invalid",
            f"Slot '{name}' lists its connection types as non-empty strings",
            f"{path}.connection_type_ids",
        )
    return ConnectionSlot(name, extension_id, tuple(dict.fromkeys(_text(item) for item in types)))


def parse_action_implementation(raw: Any) -> ActionImplementation:
    """The canonical marker, or :class:`ActionContractError` naming the bad field."""
    base = f"settings.{ACTION_IMPLEMENTATION_SETTING}"
    if not isinstance(raw, Mapping):
        raise ActionContractError(
            "workflow.action.marker_invalid", "The action implementation marker is an object", base
        )
    unknown = sorted(set(raw) - _MARKER_FIELDS)
    if unknown:
        raise ActionContractError(
            "workflow.action.marker_field_unknown",
            f"The action implementation marker does not have the field '{unknown[0]}'",
            f"{base}.{unknown[0]}",
        )
    action_id = _text(raw.get("action_id"))
    if not action_id:
        raise ActionContractError(
            "workflow.action.marker_action_missing",
            "The action implementation marker names the action it implements",
            f"{base}.action_id",
        )
    rows = raw.get("connections", [])
    if not isinstance(rows, list):
        raise ActionContractError(
            "workflow.action.slot_invalid",
            "The connection slots are a list",
            f"{base}.connections",
        )
    slots: list[ConnectionSlot] = []
    for index, row in enumerate(rows):
        slot = _parse_slot(row, f"{base}.connections[{index}]")
        if any(existing.slot == slot.slot for existing in slots):
            raise ActionContractError(
                "workflow.action.slot_duplicate",
                f"Slot '{slot.slot}' is declared twice",
                f"{base}.connections[{index}].slot",
            )
        slots.append(slot)
    return ActionImplementation(action_id, tuple(slots))


def raw_action_implementation(workflow: Mapping[str, Any] | None) -> Any:
    """The raw marker of *workflow*, or ``None`` when it carries none."""
    return _settings(workflow).get(ACTION_IMPLEMENTATION_SETTING)


def is_action_implementation(workflow: Mapping[str, Any] | None) -> bool:
    """Whether *workflow* implements an action: it carries the marker, valid or not.

    A row that already says ``action_implementation`` (a list projection) counts
    as well. A malformed marker still counts: a workflow that claims to be an
    implementation is treated as one everywhere it must be hidden or refused.
    """
    if isinstance(workflow, Mapping) and workflow.get(ACTION_IMPLEMENTATION_SETTING) is True:
        return True
    return raw_action_implementation(workflow) is not None


def is_installed_action_implementation(workflow: Mapping[str, Any] | None) -> bool:
    """An implementation an extension install projected: hidden and never launched directly.

    The author's own marked workflow in a Development environment has no source
    install; it stays an editable workflow until it ships in a bundle.
    """
    return (
        isinstance(workflow, Mapping)
        and bool(_text(workflow.get("source_install_id")))
        and is_action_implementation(workflow)
    )


def action_implementation_of(workflow: Mapping[str, Any] | None) -> ActionImplementation | None:
    """The parsed marker of *workflow*; ``None`` without one. Raises on a malformed marker."""
    raw = raw_action_implementation(workflow)
    return None if raw is None else parse_action_implementation(raw)


def parse_action_ref(raw: Any) -> ActionRef:
    base = f"override.invoke_workflow.{ACTION_CONFIG_KEY}"
    if not isinstance(raw, Mapping):
        raise ActionContractError(
            "workflow.action.ref_invalid", "The action of an Invoke is an object", base
        )
    unknown = sorted(set(raw) - _ACTION_REF_FIELDS)
    if unknown:
        raise ActionContractError(
            "workflow.action.ref_field_unknown",
            f"The action of an Invoke does not have the field '{unknown[0]}'",
            f"{base}.{unknown[0]}",
        )
    extension_id = _text(raw.get("extension_id"))
    if not is_canonical_extension_id(extension_id):
        raise ActionContractError(
            "workflow.action.ref_extension_invalid",
            "The action of an Invoke names its extension by package id",
            f"{base}.extension_id",
        )
    action_id = _text(raw.get("action_id"))
    if not action_id:
        raise ActionContractError(
            "workflow.action.ref_action_missing",
            "The action of an Invoke names the action it calls",
            f"{base}.action_id",
        )
    # The install is empty only inside a bundled implementation, where the
    # caller's pinned installs (or the bundle's own install) supply it.
    return ActionRef(extension_id, _text(raw.get("install_id")), action_id)


def action_ref_of(config: Mapping[str, Any] | None) -> ActionRef | None:
    """The parsed action origin of an Invoke config; ``None`` for a plain Invoke."""
    source = dict(config or {})
    if ACTION_CONFIG_KEY not in source:
        return None
    return parse_action_ref(source[ACTION_CONFIG_KEY])


def action_installs(config: Mapping[str, Any] | None) -> dict[str, str]:
    """``extension_id → install_id`` the Invoke chose for the action's dependencies."""
    raw = dict(config or {}).get(ACTION_INSTALLS_KEY)
    if not isinstance(raw, Mapping):
        return {}
    return {_text(key): _text(value) for key, value in raw.items() if _text(key) and _text(value)}


def action_connections(config: Mapping[str, Any] | None) -> dict[str, dict[str, str]]:
    """``slot → binding`` of an action Invoke.

    A binding is ``{"connection_id": ...}`` chosen by a person, or
    ``{"slot": ...}`` inside an implementation, which passes its own caller's
    slot down to a nested action.
    """
    raw = dict(config or {}).get(ACTION_CONNECTIONS_KEY)
    if not isinstance(raw, Mapping):
        return {}
    bindings: dict[str, dict[str, str]] = {}
    for key, value in raw.items():
        name = _text(key)
        if not name or not isinstance(value, Mapping):
            continue
        connection_id = _text(value.get("connection_id"))
        parent_slot = _text(value.get(SLOT_BINDING_KEY))
        if connection_id:
            bindings[name] = {"connection_id": connection_id}
        elif parent_slot:
            bindings[name] = {SLOT_BINDING_KEY: parent_slot}
    return bindings


def action_config_issues(config: Mapping[str, Any] | None) -> list[dict[str, str]]:
    """Shape rules of the action keys of one Invoke config.

    ``installs`` and ``connections`` belong to an action Invoke only, and an
    action annotates a direct target: it never stands next to a hook.
    """
    source = dict(config or {})
    base = "override.invoke_workflow"
    has_action = ACTION_CONFIG_KEY in source
    for key in (ACTION_INSTALLS_KEY, ACTION_CONNECTIONS_KEY):
        if key in source and not has_action:
            return [
                {
                    "code": "workflow.action.origin_missing",
                    "message": f"'{key}' belongs to an Invoke that calls an extension action",
                    "path": f"{base}.{key}",
                }
            ]
    if not has_action:
        return []
    if "hook" in source:
        return [
            {
                "code": "workflow.action.hook_conflict",
                "message": "An Invoke calls an extension action or declares a hook, not both",
                "path": f"{base}.{ACTION_CONFIG_KEY}",
            }
        ]
    try:
        parse_action_ref(source[ACTION_CONFIG_KEY])
    except ActionContractError as exc:
        return [exc.issue()]
    for key in (ACTION_INSTALLS_KEY, ACTION_CONNECTIONS_KEY):
        if key in source and not isinstance(source[key], Mapping):
            return [
                {
                    "code": "workflow.action.mapping_invalid",
                    "message": f"'{key}' of an action Invoke is an object",
                    "path": f"{base}.{key}",
                }
            ]
    for name, binding in (source.get(ACTION_CONNECTIONS_KEY) or {}).items():
        valid = isinstance(binding, Mapping) and (
            _text(binding.get("connection_id")) or _text(binding.get(SLOT_BINDING_KEY))
        )
        if not SLOT_PATTERN.fullmatch(_text(name)) or not valid:
            return [
                {
                    "code": "workflow.action.connection_invalid",
                    "message": (
                        f"Slot '{name}' of an action Invoke names a connection_id, or the "
                        "caller's slot inside an implementation"
                    ),
                    "path": f"{base}.{ACTION_CONNECTIONS_KEY}.{name}",
                }
            ]
    return []


def action_invoke_dependencies(config: Any) -> list[tuple[str, str, str]]:
    """``(dependency kind, ref, install id)`` rows of one action Invoke config.

    The same kinds a connector step records, so where-used and the uninstall
    inventory see an action step: its installs, its action and the connections
    its slots hold. A malformed or plain Invoke has none.
    """
    if not isinstance(config, Mapping) or ACTION_CONFIG_KEY not in config:
        return []
    try:
        ref = parse_action_ref(config[ACTION_CONFIG_KEY])
    except ActionContractError:
        return []
    rows: list[tuple[str, str, str]] = []
    if ref.install_id:
        rows.append(("extension_install", ref.install_id, ref.install_id))
        rows.append(("extension_action", ref.action_id, ref.install_id))
    for install_id in sorted(set(action_installs(config).values()) - {ref.install_id}):
        rows.append(("extension_install", install_id, install_id))
    for binding in action_connections(config).values():
        connection_id = binding.get("connection_id", "")
        if connection_id:
            rows.append(("project_connection", connection_id, ""))
    return rows


def slot_of_binding(binding: Mapping[str, Any] | None) -> str:
    """The slot a connector step's ``connection_binding`` reads, or ``""``."""
    return _text(dict(binding or {}).get(SLOT_BINDING_KEY))


def composite_action_context(meta: Any) -> dict[str, Any]:
    """The host-written action context of a job's ``meta``; empty for any other job."""
    if isinstance(meta, str):
        try:
            meta = json.loads(meta or "{}")
        except ValueError:
            return {}
    context = meta.get(COMPOSITE_ACTION_META_KEY) if isinstance(meta, Mapping) else None
    return dict(context) if isinstance(context, Mapping) else {}


def action_run_of(job: Mapping[str, Any] | None) -> dict[str, str]:
    """The extension action a job runs, or ``{}`` for any other job.

    *job* is a job row: its ``meta``, or the ``composite_action`` column a list
    query selects out of it.
    """
    row = job if isinstance(job, Mapping) else {}
    context = composite_action_context(
        {COMPOSITE_ACTION_META_KEY: row.get(COMPOSITE_ACTION_META_KEY)}
    ) or composite_action_context(row.get("meta"))
    extension_id = _text(context.get("extension_id"))
    action_id = _text(context.get("action_id"))
    if not extension_id or not action_id:
        return {}
    return {
        "extension_id": extension_id,
        "action_id": action_id,
        "extension_name": _text(context.get("extension_name")) or extension_id,
    }


def action_run_label(job: Mapping[str, Any] | None) -> str | None:
    """How the run of an action reads in Jobs, or ``None`` for an ordinary run.

    The implementation is never a workflow a person knows: its run reads as the
    action (the implementation carries the action's name) followed by the
    extension that offers it, e.g. ``Create order with shipment · Amazon Pro``.
    """
    run = action_run_of(job)
    if not run:
        return None
    assert job is not None
    name = _text(job.get("selected_workflow_display_name")) or run["action_id"]
    return f"{name} · {run['extension_name']}"


__all__ = [
    "ACTION_CONFIG_KEY",
    "ACTION_CONNECTIONS_KEY",
    "ACTION_IMPLEMENTATION_SETTING",
    "ACTION_INSTALLS_KEY",
    "COMPOSITE_ACTION_META_KEY",
    "SLOT_BINDING_KEY",
    "ActionContractError",
    "ActionImplementation",
    "ActionRef",
    "ConnectionSlot",
    "action_config_issues",
    "action_connections",
    "action_implementation_of",
    "action_installs",
    "action_invoke_dependencies",
    "action_ref_of",
    "action_run_label",
    "action_run_of",
    "composite_action_context",
    "is_action_implementation",
    "is_installed_action_implementation",
    "parse_action_implementation",
    "parse_action_ref",
    "raw_action_implementation",
    "slot_of_binding",
]
