"""Canonical v1.1 response envelope for extension subprocess responses."""

from __future__ import annotations

import logging
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Command catalog — allowed command_type values
# ---------------------------------------------------------------------------

KNOWN_COMMAND_CATALOG: list[str] = [
    "start_workflow",
    "pause_job",
    "resume_job",
    "cancel_job",
    "retry_step",
    "request_approval",
    "resolve_approval",
    "enqueue_followup_job",
    "invoke_extension_action",
    "update_external_status",
]
EXTENSION_OK_FALSE_MESSAGE = "Extension returned ok=false"

# ---------------------------------------------------------------------------
# Canonical response envelope
# ---------------------------------------------------------------------------


class ExtensionResponseEnvelope(BaseModel):
    """Canonical v1.1 response shape from extension subprocess."""

    model_config = ConfigDict(extra="allow")

    ok: bool = False
    result: dict[str, Any] = Field(default_factory=dict)
    commands: list[dict[str, Any]] = Field(default_factory=list)
    events: list[dict[str, Any]] = Field(default_factory=list)
    reload: list[str] = Field(default_factory=list)
    errors: list[dict[str, Any]] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------


def normalize_extension_response(raw: dict | None) -> ExtensionResponseEnvelope:
    """Normalize a raw extension response into canonical v1.1 envelope.

    Canonical contract:
    - ``ok`` is required and must be boolean.
    - ``result`` must be an object.
    - ``commands``, ``events``, ``reload``, ``errors`` must be arrays.
    """
    if raw is None or not isinstance(raw, dict):
        return ExtensionResponseEnvelope(
            ok=False,
            errors=[
                {
                    "code": "empty_response",
                    "message": "Extension returned empty or non-dict response",
                }
            ],
        )

    data: dict[str, Any] = {}
    normalized_errors: list[dict[str, Any]] = []

    if "ok" in raw:
        data["ok"] = bool(raw.get("ok"))
    else:
        data["ok"] = False
        normalized_errors.append(
            {
                "code": "missing_ok",
                "message": "Extension response is missing required boolean field 'ok'",
            }
        )

    if isinstance(raw.get("result"), dict):
        data["result"] = dict(raw.get("result") or {})
    else:
        data["result"] = {}

    if isinstance(raw.get("commands"), list):
        data["commands"] = [item for item in raw.get("commands") or [] if isinstance(item, dict)]
    else:
        data["commands"] = []

    if isinstance(raw.get("events"), list):
        data["events"] = [item for item in raw.get("events") or [] if isinstance(item, dict)]
    else:
        data["events"] = []

    if isinstance(raw.get("reload"), list):
        data["reload"] = [
            str(item).strip()
            for item in raw.get("reload") or []
            if item is not None and str(item).strip()
        ]
    else:
        data["reload"] = []

    if isinstance(raw.get("errors"), list):
        normalized_errors.extend(
            [item for item in raw.get("errors") or [] if isinstance(item, dict)]
        )
    if not data["ok"] and not normalized_errors:
        fallback_error = raw.get("error")
        fallback_message = str(fallback_error or "").strip() or EXTENSION_OK_FALSE_MESSAGE
        normalized_errors.append(
            {
                "code": "extension_error",
                "message": fallback_message,
            }
        )
    data["errors"] = normalized_errors

    known_keys = {"ok", "result", "commands", "events", "reload", "errors"}
    for key, val in raw.items():
        if key not in known_keys and key not in data:
            data[key] = val

    return ExtensionResponseEnvelope(**data)


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------


def validate_response_commands(
    commands: list[dict[str, Any]],
    allowed_types: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Validate commands against the known command catalog.

    Returns a list of validation error dicts for commands with unknown
    ``command_type``. Does not modify the input list.
    """
    catalog = allowed_types if allowed_types is not None else KNOWN_COMMAND_CATALOG
    validation_errors: list[dict[str, Any]] = []
    for idx, cmd in enumerate(commands):
        if not isinstance(cmd, dict):
            validation_errors.append(
                {
                    "code": "invalid_command",
                    "message": f"Command at index {idx} is not a dict",
                    "index": idx,
                }
            )
            continue
        cmd_type = cmd.get("command_type", "")
        if not cmd_type:
            validation_errors.append(
                {
                    "code": "missing_command_type",
                    "message": f"Command at index {idx} has no command_type",
                    "index": idx,
                }
            )
        elif cmd_type not in catalog:
            validation_errors.append(
                {
                    "code": "unknown_command_type",
                    "message": f"Command type '{cmd_type}' is not in the allowed catalog",
                    "index": idx,
                    "command_type": cmd_type,
                }
            )
    return validation_errors


def validate_response_events(
    events: list[dict[str, Any]],
    declared_event_types: list[str],
) -> list[dict[str, Any]]:
    """Validate events against the extension's declared supported_events.

    Returns a list of validation error dicts for events whose ``event_type``
    is not declared by the extension.
    """
    validation_errors: list[dict[str, Any]] = []
    for idx, event in enumerate(events):
        if not isinstance(event, dict):
            validation_errors.append(
                {
                    "code": "invalid_event",
                    "message": f"Event at index {idx} is not a dict",
                    "index": idx,
                }
            )
            continue
        event_type = event.get("event_type", "")
        if not event_type:
            validation_errors.append(
                {
                    "code": "missing_event_type",
                    "message": f"Event at index {idx} has no event_type",
                    "index": idx,
                }
            )
        elif event_type not in declared_event_types:
            validation_errors.append(
                {
                    "code": "undeclared_event_type",
                    "message": f"Event type '{event_type}' is not in the extension's supported_events",
                    "index": idx,
                    "event_type": event_type,
                }
            )
    return validation_errors


def filter_valid_commands(
    commands: list[dict[str, Any]],
    allowed_types: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Return only commands with valid command_type, logging warnings for rejected ones."""
    catalog = allowed_types if allowed_types is not None else KNOWN_COMMAND_CATALOG
    valid: list[dict[str, Any]] = []
    for cmd in commands:
        if not isinstance(cmd, dict):
            logger.warning("extension_response: dropping non-dict command: %s", cmd)
            continue
        cmd_type = cmd.get("command_type", "")
        if cmd_type in catalog:
            valid.append(cmd)
        else:
            logger.warning("extension_response: dropping command with unknown type '%s'", cmd_type)
    return valid


def filter_valid_events(
    events: list[dict[str, Any]],
    declared_event_types: list[str],
) -> list[dict[str, Any]]:
    """Return only events with declared event_type, logging warnings for rejected ones."""
    valid: list[dict[str, Any]] = []
    for event in events:
        if not isinstance(event, dict):
            logger.warning("extension_response: dropping non-dict event: %s", event)
            continue
        event_type = event.get("event_type", "")
        if event_type in declared_event_types:
            valid.append(event)
        else:
            logger.warning(
                "extension_response: dropping event with undeclared type '%s'", event_type
            )
    return valid
