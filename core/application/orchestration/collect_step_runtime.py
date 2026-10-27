"""Collect: add one value to an existing ordinary variable (ADR 0038, spec §6).

``append`` adds one element to an array; ``set_key`` sets one literal key of an
object (a new key is added, an existing key's value is replaced, other keys
stay). The variable is never created, reset, flattened or deduplicated here:
it must exist before the step runs, and its whole new value is written through
the ordinary state assignment. A key such as ``a.b@example.test`` is one
dictionary key, never a nested path.

The update and the step's result message leave the executor together, so the
existing checkpoint boundary commits them at once: a redelivered execution
starts from the state before the update and never adds twice.
"""

from __future__ import annotations

import copy
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from core.application.orchestration.expected_input_sources import resolve_state_path
from core.application.orchestration.state_path_guards import is_reserved_state_target
from core.application.orchestration.state_paths import StatePathError, parse_state_path
from core.application.orchestration.step_templating import set_state_path

MODE_APPEND = "append"
MODE_SET_KEY = "set_key"

TARGET_MISSING = "collect.target_missing"
TARGET_TYPE_INVALID = "collect.target_type_invalid"
TARGET_INVALID = "collect.target_invalid"
KEY_INVALID = "collect.key_invalid"
MODE_INVALID = "collect.mode_invalid"

_MISSING = object()


class CollectError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass
class CollectResult:
    updates: dict[str, Any] = field(default_factory=dict)
    #: Bounded facts for history: target, operation, size after, the key.
    summary: dict[str, Any] = field(default_factory=dict)
    contribution: Any = None


def _present(state: Mapping[str, Any], path: str) -> Any:
    """The value at *path*, or ``_MISSING`` when no key on the way exists.

    A stored null is a value; only an absent key is missing.
    """
    try:
        tokens = parse_state_path(path)
    except StatePathError as exc:
        raise CollectError(TARGET_INVALID, f"'{path}' is not a state path") from exc
    roots: list[Any] = [state]
    workflow_data = state.get("workflow_data")
    if isinstance(workflow_data, Mapping):
        roots.append(workflow_data)
    for root in roots:
        current: Any = root
        found = True
        for token in tokens:
            if (
                token.kind == "key" and isinstance(current, Mapping) and token.value in current
            ) or (
                token.kind == "index"
                and isinstance(current, list)
                and isinstance(token.value, int)
                and -len(current) <= token.value < len(current)
            ):
                current = current[token.value]
            else:
                found = False
                break
        if found:
            return current
    return _MISSING


def _bare(path: Any) -> str:
    return str(path or "").strip().removeprefix("state.")


def _resolve_key(raw: Any, state: Mapping[str, Any]) -> str:
    if isinstance(raw, Mapping) and set(raw) == {"source"}:
        value = resolve_state_path(dict(state), _bare(raw.get("source")))
    else:
        value = raw
    if not isinstance(value, str) or value == "":
        raise CollectError(
            KEY_INVALID, "The set_key key must resolve to a non-empty text; it is used as written"
        )
    return value


def collect_state_update(config: Mapping[str, Any], state: Mapping[str, Any]) -> CollectResult:
    """The ordinary state update one Collect makes, or a precise ``CollectError``."""
    target = _bare(config.get("target"))
    if not target:
        raise CollectError(TARGET_MISSING, "Collect needs the variable it adds to")
    if is_reserved_state_target(target):
        raise CollectError(TARGET_INVALID, f"'{target}' is a reserved runtime state field")
    mode = config.get("mode") or MODE_APPEND
    if mode not in {MODE_APPEND, MODE_SET_KEY}:
        raise CollectError(MODE_INVALID, "Collect mode must be append or set_key")
    current = _present(state, target)
    if current is _MISSING:
        raise CollectError(
            TARGET_MISSING,
            f"'{target}' does not exist yet; create it before the For each "
            "(a Set Variable to [] or {}, or a workflow input)",
        )
    source = _bare(config.get("source"))
    value = resolve_state_path(dict(state), source) if source else None
    value = copy.deepcopy(value)
    if mode == MODE_APPEND:
        if not isinstance(current, list):
            raise CollectError(
                TARGET_TYPE_INVALID,
                f"'{target}' is {type(current).__name__}; append adds to an array",
            )
        new_value: Any = [*copy.deepcopy(current), value]
        summary = {"target": target, "operation": MODE_APPEND, "count": len(new_value)}
    else:
        if not isinstance(current, Mapping):
            raise CollectError(
                TARGET_TYPE_INVALID,
                f"'{target}' is {type(current).__name__}; set_key writes into an object",
            )
        key = _resolve_key(config.get("key"), state)
        new_value = copy.deepcopy(dict(current))
        replaced = key in new_value
        new_value[key] = value
        summary = {
            "target": target,
            "operation": MODE_SET_KEY,
            "key": key,
            "replaced": replaced,
            "count": len(new_value),
        }
    updates: dict[str, Any] = {}
    set_state_path(updates, dict(state), target, new_value)
    return CollectResult(updates=updates, summary=summary, contribution=value)


__all__ = [
    "KEY_INVALID",
    "MODE_APPEND",
    "MODE_SET_KEY",
    "TARGET_MISSING",
    "TARGET_TYPE_INVALID",
    "CollectError",
    "CollectResult",
    "collect_state_update",
]
