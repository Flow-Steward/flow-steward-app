"""Pure list normalization and operations for standard action adapters."""

from __future__ import annotations

import copy
from typing import Any


def coerce(value: Any) -> list[Any]:
    if not isinstance(value, list):
        raise ValueError(f"{type(value).__name__} is not an array")
    return copy.deepcopy(value)


def length(items: list[Any]) -> int:
    return len(items)


def first(items: list[Any], default: Any = None) -> Any:
    return items[0] if items else default


def at(items: list[Any], index: int, default: Any = None) -> Any:
    if index < 0:
        raise ValueError("index must be non-negative")
    return items[index] if index < len(items) else default


def compact(items: list[Any]) -> list[Any]:
    return [item for item in items if item is not None]


def join_text(items: list[Any], separator: str) -> str:
    return separator.join("" if item is None else str(item) for item in items)
