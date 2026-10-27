"""Application port and domain snapshot for lifecycle preservation capture."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True)
class DatabasePreservationSnapshot:
    """Closed database facts returned by a preservation repository adapter."""

    database_reachable: bool
    fields: Mapping[str, Any]


class PreservationRepository(Protocol):
    """Application-facing database preservation capture port."""

    def capture(self) -> DatabasePreservationSnapshot: ...
