# ruff: noqa: F401
# mypy: ignore-errors
"""OrchestratorSupervisor — validates and hydrates the workflow a run is bound to.

Routing resolves an explicitly selected workflow binding (classification,
re-entry lineage, project visibility). It plans nothing: the run executes the
pinned definition through the step hub.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from core.application.orchestration.classifier import TaskClassification
    from core.application.orchestration.state import OrchestratorState
    from core.application.registry.registry_manager import RegistryManager

logger = logging.getLogger(__name__)


@dataclass
class RoutingResolution:
    classification: TaskClassification
    workflow: object
    workflow_id: str
    routing_reason: str
    routing_signals: dict
    reentry_context: dict


class OrchestratorSupervisorCommonMixin:
    """Holds the registry the routing mixin reads."""

    def __init__(self, registry: RegistryManager):
        self.registry = registry


__all__ = [
    "Any",
    "OrchestratorSupervisorCommonMixin",
    "RoutingResolution",
    "logger",
]
