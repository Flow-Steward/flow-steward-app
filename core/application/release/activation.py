"""Application contract for resumable installer-owned release activation."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict

from core.application.installer.contracts import InstallConfig
from core.application.release.contracts import ReleaseIdentity
from core.application.release.service import ReleaseResult, ReleaseTransitionBinding


class ReleaseHandoffJournal(BaseModel):
    """Owner-only durable boundary between release policy and the installer."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    target: ReleaseIdentity
    phase: Literal["prepared", "checkout_applied", "installer_completed", "migration_required"]
    install_config: InstallConfig | None = None
    explicit_fields: tuple[str, ...] = ()
    release_repository: str = "Flow-Steward/flow-steward-app"
    release_registries: str = ""


class ReleaseHandoffPort(Protocol):
    def activate(
        self,
        release: ReleaseIdentity,
        *,
        timeout_seconds: int,
        held_locks: Mapping[str, int] | None = None,
        transition_binding: ReleaseTransitionBinding | None = None,
    ) -> ReleaseResult: ...


class ReleaseActivator:
    """Thin application owner; technical self-update mechanics stay in infrastructure."""

    def __init__(self, handoff: ReleaseHandoffPort) -> None:
        self.handoff = handoff

    def activate(
        self,
        release: ReleaseIdentity,
        *,
        timeout_seconds: int,
        **operation_sinks: object,
    ) -> ReleaseResult:
        raw_held_locks = operation_sinks.pop("held_locks", None)
        if raw_held_locks is not None and not isinstance(raw_held_locks, Mapping):
            raise TypeError("Release held locks must be a path-to-descriptor mapping")
        held_locks = (
            None
            if raw_held_locks is None
            else {str(path): int(descriptor) for path, descriptor in raw_held_locks.items()}
        )
        raw_transition_binding = operation_sinks.pop("transition_binding", None)
        if raw_transition_binding is not None and not isinstance(
            raw_transition_binding, ReleaseTransitionBinding
        ):
            raise TypeError("Release transition binding must use the application contract")
        del operation_sinks
        if raw_transition_binding is not None:
            return self.handoff.activate(
                release,
                timeout_seconds=timeout_seconds,
                held_locks=held_locks,
                transition_binding=raw_transition_binding,
            )
        return self.handoff.activate(
            release,
            timeout_seconds=timeout_seconds,
            held_locks=held_locks,
        )


__all__ = ["ReleaseActivator", "ReleaseHandoffJournal", "ReleaseHandoffPort"]
