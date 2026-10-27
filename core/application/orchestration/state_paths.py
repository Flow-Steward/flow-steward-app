"""One state-path language, shared by every step kind that reads workflow state.

Before this module each family of steps carried its own resolver. The transform
step understood ``rows[0].sku``; connector mappings, ``set_variable`` assignments
and control flow walked dictionaries only, so the same path returned ``null``
there — silently, which let a job finish green having written nothing. Authors
had no way to tell which spelling a given step would honour.

The rules here are deliberately small:

* ``a.b.c`` walks mapping keys.
* ``a[0].b`` indexes a list, and ``a.0.b`` means the same thing when ``a`` is a
  list — a plain segment that is all digits is read as an index rather than a key
  when, and only when, the value in hand is a list.
* A path that runs off the end of the data is *missing*, which is different from
  a path that resolves to ``None``. Callers decide what to do about missing;
  they can no longer confuse the two.
"""

from __future__ import annotations

from typing import Any

from core.application.orchestration.transform_selectors import (
    SelectorToken,
    parse_transform_selector,
)

__all__ = [
    "STATE_PREFIX",
    "StatePathError",
    "parse_state_path",
    "path_segments",
    "resolve_state_path",
    "resolve_state_path_with_workflow_data",
    "strip_state_prefix",
]

STATE_PREFIX = "state."


class StatePathError(ValueError):
    """A state path could not be parsed."""


def strip_state_prefix(path: str) -> str:
    """Return ``path`` without a leading ``state.``, which is optional everywhere."""
    text = str(path or "").strip()
    if text == "state":
        return ""
    if text.startswith(STATE_PREFIX):
        return text[len(STATE_PREFIX) :]
    return text


def path_segments(path: str) -> list[str]:
    """Return the plain dotted segments of ``path``, ignoring index syntax.

    Kept for the few call sites that still need the old shape to reach a
    secondary lookup.
    """
    return [segment for segment in strip_state_prefix(path).split(".") if segment]


def parse_state_path(path: str) -> list[SelectorToken]:
    """Parse a state path with the one grammar used by every runtime family."""
    selector = strip_state_prefix(path)
    if not selector:
        return []
    try:
        return parse_transform_selector(selector, allow_wildcard=False)
    except ValueError as exc:
        raise StatePathError(f"invalid state path '{path}': {exc}") from exc


def resolve_state_path(root: Any, path: str) -> tuple[Any, bool]:
    """Resolve ``path`` against ``root``.

    Returns ``(value, found)``. ``found`` is ``False`` when the path runs off the
    end of the data, which a caller must not mistake for a resolved ``None``.

    Raises:
        StatePathError: the path is not valid syntax.
    """
    tokens = parse_state_path(path)
    if not tokens:
        return root, root is not None

    cursor: Any = root
    for token in tokens:
        if token.kind == "index":
            index = int(token.value or 0)
            if not isinstance(cursor, list) or index >= len(cursor):
                return None, False
            cursor = cursor[index]
            continue
        key = str(token.value)
        if isinstance(cursor, dict):
            if key not in cursor:
                return None, False
            cursor = cursor[key]
            continue
        # `rows.0.sku` is how an author naturally writes what the transform step
        # spells `rows[0].sku`. Honour it, but only against a list, so a mapping
        # whose keys happen to be digits keeps its own meaning.
        if isinstance(cursor, list) and key.isdigit():
            index = int(key)
            if index >= len(cursor):
                return None, False
            cursor = cursor[index]
            continue
        return None, False
    return cursor, True


def resolve_state_path_with_workflow_data(state: dict[str, Any], path: str) -> tuple[Any, bool]:
    """Resolve primary state first, falling back to its secondary mirror only if missing."""
    resolved, found = resolve_state_path(state, path)
    selector = strip_state_prefix(path)
    root_segment = selector.split(".", 1)[0].split("[", 1)[0] if selector else ""
    if found or not selector or root_segment == "workflow_data":
        return resolved, found

    workflow_data = state.get("workflow_data")
    if not isinstance(workflow_data, dict):
        return None, False
    return resolve_state_path(workflow_data, path)
