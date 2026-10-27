"""Read Compact observations without refreshing their provenance."""

from core.infrastructure.runtime.compact.readiness import ReadinessRegistry
from core.infrastructure.runtime.monitoring.observations import SourceObservation


def read_compact_observations(registry: ReadinessRegistry) -> tuple[SourceObservation, ...]:
    return registry.observations()
