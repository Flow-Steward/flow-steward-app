"""Canonical ownership policy for environment values carried by a Clone."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Protocol


class CloneSecretSpec(Protocol):
    @property
    def name(self) -> str: ...

    @property
    def secret(self) -> bool: ...

    @property
    def clone_source_owned(self) -> bool: ...

    @property
    def environments(self) -> tuple[str, ...]: ...

    @property
    def groups(self) -> tuple[str, ...]: ...


def select_clone_source_configuration(
    values: Mapping[str, str],
    *,
    specs: Iterable[CloneSecretSpec],
    environment: str,
) -> dict[str, str]:
    """Select source-owned values from the canonical environment catalog."""
    active_environment = "local_development" if environment == "development" else environment
    selected: dict[str, str] = {}
    for spec in specs:
        if (
            active_environment in spec.environments
            and spec.name in values
            and spec.clone_source_owned
        ):
            selected[spec.name] = str(values[spec.name])
    return selected


def select_clone_source_secrets(
    values: Mapping[str, str],
    *,
    specs: Iterable[CloneSecretSpec],
    environment: str,
) -> dict[str, str]:
    """Select only source-owned secret values for legacy secret-only callers."""
    selected = select_clone_source_configuration(
        values,
        specs=specs,
        environment=environment,
    )
    secret_names = {spec.name for spec in specs if spec.secret}
    return {name: value for name, value in selected.items() if name in secret_names}


__all__ = [
    "CloneSecretSpec",
    "select_clone_source_configuration",
    "select_clone_source_secrets",
]
