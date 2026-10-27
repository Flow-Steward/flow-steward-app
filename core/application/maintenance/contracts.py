"""Closed, secret-free maintenance mode contracts."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

MaintenancePhase = Literal["draining", "enabled"]
MaintenanceOwnerKind = Literal["manual", "release"]
MaintenanceStatus = Literal["disabled", "draining", "enabled"]


class MaintenanceState(BaseModel):
    """Durable owner record for one maintenance interval."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    phase: MaintenancePhase
    owner_kind: MaintenanceOwnerKind
    owner_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
    activated_at: datetime


class MaintenanceStatusResult(BaseModel):
    """Read-only public maintenance status."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    status: MaintenanceStatus
    activated_at: datetime | None = None
    owner_kind: MaintenanceOwnerKind | None = None


class MaintenanceChangeResult(BaseModel):
    """Idempotent maintenance mutation result."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Literal["enabled", "already_enabled", "disabled", "already_disabled"]
    changed: bool
    activated_at: datetime | None = None
    owner_kind: MaintenanceOwnerKind | None = None
    next_steps: tuple[str, ...] = ()
