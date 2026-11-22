"""Commit a ``connection_poll`` result against its connection as it is now.

A background poll starts from the connection's ``state_version`` and
``config_generation``. When its result comes back, one transaction locks the
connection row (``SELECT ... FOR UPDATE``) and judges the result:

- the connection is gone or disabled, its poll mode is off, or its
  ``config_generation`` moved: the result belongs to an old configuration and
  is dropped completely, events and cursor alike;
- same generation, but ``state_version`` moved: another worker saved state in
  the meantime; the events are kept (their ``source_id`` drops repeats) and the
  cursor is dropped;
- otherwise everything commits, and ``state_version`` moves on.

Every configuration change that moves ``config_generation`` is an UPDATE of the
same row, so it waits for, or is seen by, this transaction.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

ACTIVE_POLL_MODES = frozenset({"auto", "polling"})

VERDICT_ACCEPT = "accept"
VERDICT_STALE_STATE = "stale_state"
VERDICT_STALE_CONFIGURATION = "stale_configuration"


def _text(value: Any) -> str:
    return str(value or "").strip()


def _as_dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def poll_mode(ingress_config: Any, *, mode_field: str, mode_default: str = "auto") -> str:
    """The connection's poll mode; a missing value takes the declared default."""
    ingress = _as_dict(ingress_config)
    default = _text(mode_default).lower() or "auto"
    return _text(ingress.get(_text(mode_field))).lower() or default


@dataclass(frozen=True)
class ConnectionPollContext:
    """What a background poll started from, carried to its result."""

    connection_id: str
    state_version: int
    config_generation: int
    mode_field: str = ""
    mode_default: str = "auto"

    @classmethod
    def from_mapping(cls, value: Any) -> ConnectionPollContext | None:
        payload = _as_dict(value)
        connection_id = _text(payload.get("connection_id"))
        state_version = _int(payload.get("state_version"))
        generation = _int(payload.get("config_generation"))
        if not connection_id or state_version is None or generation is None:
            return None
        return cls(
            connection_id=connection_id,
            state_version=state_version,
            config_generation=generation,
            mode_field=_text(payload.get("mode_field")),
            mode_default=_text(payload.get("mode_default")) or "auto",
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "connection_id": self.connection_id,
            "state_version": self.state_version,
            "config_generation": self.config_generation,
            "mode_field": self.mode_field,
            "mode_default": self.mode_default,
        }


@dataclass(frozen=True)
class PollVerdict:
    verdict: str
    reason: str = ""

    @property
    def keeps_events(self) -> bool:
        return self.verdict in {VERDICT_ACCEPT, VERDICT_STALE_STATE}

    @property
    def keeps_state(self) -> bool:
        return self.verdict == VERDICT_ACCEPT


def lock_and_judge(cursor: Any, context: ConnectionPollContext) -> PollVerdict:
    """Lock the connection row and judge the poll result against it."""
    cursor.execute(
        "SELECT enabled, state_version, config_generation, ingress_config "
        "FROM project_connections WHERE connection_id = %s FOR UPDATE",
        (context.connection_id,),
    )
    row = cursor.fetchone()
    if not row:
        return PollVerdict(VERDICT_STALE_CONFIGURATION, "connection_missing")
    if isinstance(row, dict):
        enabled, state_version, generation, ingress = (
            row.get("enabled"),
            row.get("state_version"),
            row.get("config_generation"),
            row.get("ingress_config"),
        )
    else:
        enabled, state_version, generation, ingress = row[0], row[1], row[2], row[3]
    if not enabled:
        return PollVerdict(VERDICT_STALE_CONFIGURATION, "connection_disabled")
    if _int(generation) != context.config_generation:
        return PollVerdict(VERDICT_STALE_CONFIGURATION, "config_generation_changed")
    if context.mode_field and (
        poll_mode(ingress, mode_field=context.mode_field, mode_default=context.mode_default)
        not in ACTIVE_POLL_MODES
    ):
        return PollVerdict(VERDICT_STALE_CONFIGURATION, "trigger_inactive")
    if _int(state_version) != context.state_version:
        return PollVerdict(VERDICT_STALE_STATE, "state_version_changed")
    return PollVerdict(VERDICT_ACCEPT)


def advance_state_version(cursor: Any, connection_id: str) -> None:
    """Record one state save; the caller holds the row lock."""
    cursor.execute(
        "UPDATE project_connections SET state_version = state_version + 1 WHERE connection_id = %s",
        (connection_id,),
    )


def bump_config_generation(cursor: Any, connection_id: str) -> None:
    """Move the generation for a change the row trigger cannot see (poll mode, scope)."""
    cursor.execute(
        "UPDATE project_connections SET config_generation = config_generation + 1 "
        "WHERE connection_id = %s",
        (connection_id,),
    )


def background_poll_mode_fields(extension: dict[str, Any] | None) -> set[str]:
    """Every ``mode_field`` the extension's connection_poll actions declare."""
    payload = _as_dict(extension)
    compiled = _as_dict(payload.get("compiled_manifest"))
    rows = payload.get("action_definitions") or compiled.get("action_definitions") or []
    fields: set[str] = set()
    for row in rows if isinstance(rows, list) else []:
        background = _as_dict(_as_dict(_as_dict(row).get("contract")).get("background"))
        if _text(background.get("kind")) == "connection_poll" and _text(
            background.get("mode_field")
        ):
            fields.add(_text(background.get("mode_field")))
    return fields


def background_poll_interval_fields(extension: dict[str, Any] | None) -> set[str]:
    payload = _as_dict(extension)
    compiled = _as_dict(payload.get("compiled_manifest"))
    rows = payload.get("action_definitions") or compiled.get("action_definitions") or []
    fields: set[str] = set()
    for row in rows if isinstance(rows, list) else []:
        background = _as_dict(_as_dict(_as_dict(row).get("contract")).get("background"))
        if _text(background.get("kind")) == "connection_poll" and _text(
            background.get("interval_field")
        ):
            fields.add(_text(background.get("interval_field")))
    return fields


__all__ = [
    "ACTIVE_POLL_MODES",
    "VERDICT_ACCEPT",
    "VERDICT_STALE_CONFIGURATION",
    "VERDICT_STALE_STATE",
    "ConnectionPollContext",
    "PollVerdict",
    "advance_state_version",
    "background_poll_interval_fields",
    "background_poll_mode_fields",
    "bump_config_generation",
    "lock_and_judge",
    "poll_mode",
]
