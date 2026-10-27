"""Application query boundary for deep-health persistence aggregates."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


class HealthCountsRepository(Protocol):
    """Persistence port for bounded deep-health counts."""

    def load(self, logger: Any) -> dict[str, int]: ...


@dataclass(frozen=True)
class HealthCountsQueryService:
    """Transport-neutral deep-health count query."""

    repository: HealthCountsRepository

    def get_counts(self, logger: Any) -> dict[str, int]:
        return dict(self.repository.load(logger))
