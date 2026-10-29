"""Use-case service for first-party tabular feed operations."""

from __future__ import annotations

from typing import Any

from core.application.extensions.artifact_dataset_plane_v2 import ArtifactDatasetPlane
from core.application.tabular_feed.context import TabularFeedExecutionContext
from core.application.tabular_feed.results import TabularFeedExecutionResult
from core.application.tabular_feed.snapshot_repository import SnapshotRepository
from core.infrastructure.tabular_feed import engine

_DATE_ACTION_IDS = frozenset(
    {
        "date.parse",
        "date.to_utc",
        "date.to_timezone",
        "date.format",
        "date.add",
        "date.part",
        "date.start_of",
        "date.end_of",
    }
)


def _date_locales(payload: dict[str, Any]) -> tuple[str, ...]:
    locales: set[str] = set()
    actions = payload.get("actions")
    if not isinstance(actions, list):
        return ()
    has_date_action = False
    for action in actions:
        if not isinstance(action, dict) or action.get("action") not in _DATE_ACTION_IDS:
            continue
        has_date_action = True
        params = action.get("params")
        if not isinstance(params, dict):
            params = {}
        for key in ("locale", "output_locale"):
            locale = params.get(key)
            if isinstance(locale, str) and locale.strip():
                locales.add(locale.strip())
    if not has_date_action:
        return ()
    locales.add("en")
    return tuple(sorted(locales))


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
        input_payload = action.get("input")
        effective_payload = input_payload if isinstance(input_payload, dict) else {}
        locales = _date_locales(effective_payload)
        date_lexicons = None
        if locales:
            from core.application.orchestration.date_action_support import get_date_lexicons
            from core.infrastructure.date_types import DateValueError

            try:
                date_lexicons = get_date_lexicons(locales)
            except DateValueError as exc:
                envelope = {
                    "ok": False,
                    "result": {},
                    "commands": [],
                    "events": [],
                    "reload": [],
                    "errors": [{"code": exc.code, "message": str(exc), "path": "actions"}],
                }
                return TabularFeedExecutionResult.from_envelope(envelope).as_dict()
        envelope = engine.execute_operation(
            operation,
            action,
            plane=self._plane,
            snapshot_repository=self._snapshot_repository,
            date_lexicons=date_lexicons,
        )
        return TabularFeedExecutionResult.from_envelope(envelope).as_dict()
