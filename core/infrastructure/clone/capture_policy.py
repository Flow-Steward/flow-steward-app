"""Shared filesystem exclusions for every Clone capture transport."""

from __future__ import annotations

from core.application.clone.contracts import CloneDataClass
from core.application.clone.state_policy import CLONE_SOURCE_EXCLUDED_STATE_NAMES

_GENERATED_NAMES = frozenset(
    {"node_modules", ".venv", "venv", "__pycache__", ".pytest_cache", ".mypy_cache"}
)
_EPHEMERAL_STATE_NAMES = CLONE_SOURCE_EXCLUDED_STATE_NAMES | frozenset(
    {"journals", "recovery", "tmp", "cache", "caches"}
)


def clone_ignored_names(data_class: CloneDataClass) -> frozenset[str]:
    """Return the canonical names excluded from one filesystem data class."""
    ignored = set(_GENERATED_NAMES)
    if data_class is CloneDataClass.DURABLE_STATE:
        ignored.update(_EPHEMERAL_STATE_NAMES)
        ignored.add(".git")
    if data_class is CloneDataClass.EXTENSIONS:
        ignored.add(".git")
    return frozenset(ignored)


__all__ = ["clone_ignored_names"]
