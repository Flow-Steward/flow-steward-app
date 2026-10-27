from __future__ import annotations

import inspect


class _Repository:
    def __init__(self) -> None:
        self.logger = None

    def load(self, logger):
        self.logger = logger
        return {"resume_pending_jobs": 3}


def test_health_counts_query_service_delegates_to_repository() -> None:
    from core.application.monitoring.health_query import HealthCountsQueryService

    repository = _Repository()
    service = HealthCountsQueryService(repository=repository)
    logger = object()

    assert service.get_counts(logger) == {"resume_pending_jobs": 3}
    assert repository.logger is logger


def test_deep_health_entrypoint_contains_no_persistence_query() -> None:
    from core.entrypoints.api import health

    source = inspect.getsource(health.health_deep)

    assert "SELECT " not in source
    assert "health_counts_query.get_counts" in source
