"""Definition rules of a Wait step that waits for an event (ADR 0071).

``override.wait.event`` makes a Wait end on an event an extension delivers
instead of a duration: ``source`` (the extension id), ``event_types`` and the
``correlation_key`` it is addressed by, with a required ``timeout_ms`` and an
optional ``output_mapping`` of the event onto state.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from core.application.jobs.workflow_wait_contract import (
    WAIT_EVENT_TYPES_MAX,
    wait_event_correlation_key,
    wait_event_source_is_valid,
    wait_event_type_is_valid,
)

#: The fields of an event wait's result; ``payload.<field>`` reads inside the payload.
WAIT_EVENT_OUTPUTS = frozenset(
    {"payload", "event_type", "correlation_key", "received_at", "source"}
)
WAIT_EVENT_KEYS = frozenset({"source", "event_types", "correlation_key"})


def _issue(code: str, message: str, path: str) -> dict[str, str]:
    return {"code": code, "message": message, "path": path, "severity": "error"}


def wait_waits_for_event(config: Mapping[str, Any]) -> bool:
    """Whether a Wait config is an event wait rather than a duration."""
    return "event" in config


def _event_types_issues(value: Any, path: str) -> list[dict[str, str]]:
    if (
        not isinstance(value, list)
        or not 0 < len(value) <= WAIT_EVENT_TYPES_MAX
        or not all(wait_event_type_is_valid(item) for item in value)
        or len(set(value)) != len(value)
    ):
        return [
            _issue(
                "workflow.wait.event_types_invalid",
                f"event_types is a list of 1 to {WAIT_EVENT_TYPES_MAX} distinct event types "
                "(lowercase letters, digits, '_', '.', ':' and '-')",
                path,
            )
        ]
    return []


def _correlation_key_issues(value: Any, path: str) -> list[dict[str, str]]:
    if isinstance(value, str) and value.strip():
        return []
    if wait_event_correlation_key(value) is not None:
        return []
    return [
        _issue(
            "workflow.wait.correlation_key_missing",
            "correlation_key is required: a state path, a {{template}} or a literal text",
            path,
        )
    ]


def _timeout_issues(value: Any, path: str, maximum: int) -> list[dict[str, str]]:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 < value <= maximum:
        return [
            _issue(
                "workflow.wait.timeout_invalid",
                f"An event Wait needs timeout_ms, an integer from 1 to {maximum}",
                path,
            )
        ]
    return []


def _output_mapping_issues(value: Any, path: str) -> list[dict[str, str]]:
    if value is None:
        return []
    if not isinstance(value, Mapping):
        return [_issue("workflow.wait.mapping_invalid", "output_mapping must be an object", path)]
    issues: list[dict[str, str]] = []
    for key, target in value.items():
        name = str(key)
        if name not in WAIT_EVENT_OUTPUTS and not name.startswith("payload."):
            issues.append(
                _issue(
                    "workflow.wait.output_unknown",
                    f"An event Wait outputs {', '.join(sorted(WAIT_EVENT_OUTPUTS))} "
                    f"or payload.<field>, not '{name}'",
                    f"{path}.{name}",
                )
            )
        elif not isinstance(target, str) or not target.strip():
            issues.append(
                _issue(
                    "workflow.wait.mapping_invalid",
                    f"output_mapping.{name} must name a target such as state.result",
                    f"{path}.{name}",
                )
            )
    return issues


def wait_event_config_issues(
    config: Mapping[str, Any], path: str, *, timeout_max_ms: int
) -> list[dict[str, str]]:
    """Issues of a Wait config that has ``event``."""
    issues: list[dict[str, str]] = [
        _issue(
            "workflow.wait.event_duration_conflict",
            f"An event Wait has a timeout_ms, not '{key}'",
            f"{path}.{key}",
        )
        for key in ("duration_ms", "input_mapping")
        if key in config
    ]
    event = config.get("event")
    event_path = f"{path}.event"
    if not isinstance(event, Mapping):
        issues.append(
            _issue(
                "workflow.wait.event_invalid",
                "event is an object with source, event_types and correlation_key",
                event_path,
            )
        )
    else:
        issues.extend(
            _issue(
                "workflow.step_config_key_unknown",
                f"Unknown wait event key '{key}'",
                f"{event_path}.{key}",
            )
            for key in sorted(set(event) - WAIT_EVENT_KEYS)
        )
        if not wait_event_source_is_valid(event.get("source")):
            issues.append(
                _issue(
                    "workflow.wait.event_source_invalid",
                    "event.source is the id of the extension that delivers the event",
                    f"{event_path}.source",
                )
            )
        issues.extend(_event_types_issues(event.get("event_types"), f"{event_path}.event_types"))
        issues.extend(
            _correlation_key_issues(event.get("correlation_key"), f"{event_path}.correlation_key")
        )
    issues.extend(_timeout_issues(config.get("timeout_ms"), f"{path}.timeout_ms", timeout_max_ms))
    issues.extend(_output_mapping_issues(config.get("output_mapping"), f"{path}.output_mapping"))
    return issues


def wait_duration_only_key_issues(config: Mapping[str, Any], path: str) -> list[dict[str, str]]:
    """A duration Wait refuses the keys that belong to an event Wait."""
    return [
        _issue(
            "workflow.wait.event_key_without_event",
            f"'{key}' belongs to an event Wait; add event or remove it",
            f"{path}.{key}",
        )
        for key in ("timeout_ms", "output_mapping")
        if key in config
    ]


__all__ = [
    "WAIT_EVENT_OUTPUTS",
    "wait_duration_only_key_issues",
    "wait_event_config_issues",
    "wait_waits_for_event",
]
