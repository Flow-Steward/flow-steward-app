"""Test-only aggregate for storage contract and PostgreSQL integration fixtures.

Production code must use the responsibility-owned stores in
``core.infrastructure.db.platform_stores``.  Storage contract tests sometimes
need several independent persistence surfaces on one fake connection, so this
aggregate deliberately lives under ``tests``.
"""

from __future__ import annotations

from typing import ClassVar

from core.application.notifications.repository import NotificationRepositoryMixin
from core.application.projects.connection_repository import ProjectConnectionRepositoryMixin
from core.infrastructure.db.logger_dashboard_rollups_mixin import LoggerDashboardRollupsMixin
from core.infrastructure.db.logger_events_dispatch_mixin import PlatformLoggerEventsDispatchMixin
from core.infrastructure.db.logger_extensions_accounts_mixin import (
    PlatformLoggerExtensionsAccountsMixin,
)
from core.infrastructure.db.logger_lifecycle_projects_mixin import (
    PlatformLoggerLifecycleProjectsMixin,
)
from core.infrastructure.db.logger_policy_projects_mixin import PlatformLoggerPolicyProjectsMixin
from core.infrastructure.db.logger_scheduled_providers_mixin import (
    PlatformLoggerScheduledProvidersMixin,
)
from core.infrastructure.db.logger_trace_mixin import PlatformLoggerTraceMixin
from core.infrastructure.db.platform_logger_activity_mixin import PlatformLoggerActivityMixin
from core.infrastructure.db.platform_logger_admin_queries_mixin import (
    PlatformLoggerAdminQueriesMixin,
)
from core.infrastructure.db.platform_logger_core_mixin import PlatformLoggerCoreMixin
from core.infrastructure.db.platform_logger_decisions_mixin import PlatformLoggerDecisionsMixin
from core.infrastructure.db.platform_logger_jobs_mixin import PlatformLoggerJobsMixin
from core.infrastructure.db.platform_logger_scheduled_locks_mixin import (
    PlatformLoggerScheduledLocksMixin,
)


class PlatformTestStore(
    PlatformLoggerCoreMixin,
    PlatformLoggerJobsMixin,
    PlatformLoggerScheduledLocksMixin,
    PlatformLoggerAdminQueriesMixin,
    PlatformLoggerDecisionsMixin,
    PlatformLoggerActivityMixin,
    LoggerDashboardRollupsMixin,
    PlatformLoggerTraceMixin,
    PlatformLoggerScheduledProvidersMixin,
    ProjectConnectionRepositoryMixin,
    NotificationRepositoryMixin,
    PlatformLoggerEventsDispatchMixin,
    PlatformLoggerExtensionsAccountsMixin,
    PlatformLoggerPolicyProjectsMixin,
    PlatformLoggerLifecycleProjectsMixin,
):
    """All persistence contracts on one connection for test fixtures only."""

    _shared_instances: ClassVar[dict[str, PlatformTestStore]] = {}
