"""A tiny vendored distribution that proves a platform module loads its own dependencies."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

__version__ = "1.0.0"


def normalize_feature_names(values: Iterable[Any]) -> frozenset[str]:
    """Trimmed, lowercase, non-empty feature names; anything that is not text is refused."""
    if isinstance(values, (str, bytes)) or not isinstance(values, Iterable):
        raise ValueError("features must be a list of names")
    names: set[str] = set()
    for value in values:
        if not isinstance(value, str):
            raise ValueError("a feature name must be text")
        name = value.strip().lower()
        if name:
            names.add(name)
    return frozenset(names)


def normalize_limits(values: Mapping[str, Any]) -> dict[str, int]:
    """Trimmed, lowercase limit names mapped to non-negative integers."""
    if not isinstance(values, Mapping):
        raise ValueError("limits must be an object")
    limits: dict[str, int] = {}
    for key, value in values.items():
        name = key.strip().lower() if isinstance(key, str) else ""
        if not name or isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError("limits must map names to non-negative integers")
        limits[name] = value
    return limits
