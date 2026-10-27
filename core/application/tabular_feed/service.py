"""Use-case service for first-party tabular feed operations."""

from __future__ import annotations

from typing import Any

from core.application.extensions.artifact_dataset_plane_v2 import ArtifactDatasetPlane
from core.application.tabular_feed.context import TabularFeedExecutionContext
from core.application.tabular_feed.results import TabularFeedExecutionResult
from core.application.tabular_feed.snapshot_repository import SnapshotRepository
from core.infrastructure.tabular_feed import engine


class TabularFeedService:
    """Execute tabular feed operations against the injected Artifact/Dataset Plane."""

    def __init__(
        self, *, plane: ArtifactDatasetPlane, snapshot_repository: SnapshotRepository | None = None
    ) -> None:
        self._plane = plane
        self._snapshot_repository = snapshot_repository

    def execute(
        self,
        *,
        operation: str,
        payload: dict[str, Any],
        context: TabularFeedExecutionContext,
    ) -> dict[str, Any]:
        action = dict(payload or {})
        runtime_context = context.as_runtime_context()
        action.pop("runtime_context", None)
        action.pop("context", None)
        action.pop("scope", None)
        action["runtime_context"] = runtime_context
        action["_trusted_core_context"] = True
        envelope = engine.execute_operation(
            operation, action, plane=self._plane, snapshot_repository=self._snapshot_repository
        )
        return TabularFeedExecutionResult.from_envelope(envelope).as_dict()
