"""Application-owned persistence ports used by transport composition roots."""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class PlatformStorePort(Protocol):
    """Common connection surface exposed by responsibility-owned stores."""

    dsn: str
    conn: Any


class JobStorePort(PlatformStorePort, Protocol):
    """Job persistence needed by CLI lifecycle composition."""

    def get_active_jobs(self) -> list[dict[str, Any]]: ...


class ProjectStorePort(PlatformStorePort, Protocol):
    """Project persistence needed by transport composition."""

    def get_project(self, project_id: Any) -> dict[str, Any] | None: ...

    def get_project_run_config(self, project_id: Any) -> dict[str, Any] | None: ...

    def get_projects(self, *, account_id: str) -> list[dict[str, Any]]: ...


class WorkflowKnowledgeStorePort(Protocol):
    """Runtime contract needed by workflow knowledge orchestration."""

    def enforce_runtime(self, *, project_id: str | None = None) -> Any: ...


__all__ = [
    "JobStorePort",
    "PlatformStorePort",
    "ProjectStorePort",
    "WorkflowKnowledgeStorePort",
]
