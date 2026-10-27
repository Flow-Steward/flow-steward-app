# ruff: noqa: I001, RUF100
from __future__ import annotations

from .query_services_common import *  # noqa: F403,F401  # NOSONAR
from .query_services_job_core_mixin import JobQueryServiceCoreMixin
from .query_services_job_journal_mixin import JobQueryServiceJournalMixin
from .query_services_job_details_mixin import JobQueryServiceDetailsMixin
from .query_services_project_core_mixin import ProjectQueryServiceCoreMixin
from .query_services_project_history_mixin import ProjectQueryServiceHistoryMixin
from .query_services_project_ops_mixin import ProjectQueryServiceOpsMixin
from .query_services_approval import ApprovalQueryService
from .query_services_event import EventQueryService
from core.application.extensions.query_service import ExtensionQueryService
from .query_services_tool import ToolQueryService


class JobQueryService(
    JobQueryServiceCoreMixin,
    JobQueryServiceJournalMixin,
    JobQueryServiceDetailsMixin,
):
    pass


class ProjectQueryService(
    ProjectQueryServiceCoreMixin,
    ProjectQueryServiceHistoryMixin,
    ProjectQueryServiceOpsMixin,
):
    pass


__all__ = [
    *globals().get("__all__", []),
    "JobQueryService",
    "ProjectQueryService",
    "ApprovalQueryService",
    "EventQueryService",
    "ExtensionQueryService",
    "ToolQueryService",
]
