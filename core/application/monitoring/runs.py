from __future__ import annotations

from collections.abc import Callable
from typing import Any

from core.infrastructure.monitoring.runs_storage import list_recent_runs_rows


class MonitoringRunsService:
    def __init__(
        self,
        *,
        list_rows: Callable[..., list[dict[str, Any]]] = list_recent_runs_rows,
    ) -> None:
        self._list_rows = list_rows

    def list_runs(
        self,
        *,
        project_name: str | None = None,
        status: str | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        return self._list_rows(project_name=project_name, status=status, limit=limit)
