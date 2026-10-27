"""Responsibility-owned PostgreSQL stores for Flow Steward platform data.

Each store exposes one cohesive persistence surface.  Callers should depend on the
narrowest store that owns the operations they use instead of the legacy
``PlatformLogger`` aggregate.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import ClassVar

from core.application.notifications.repository import NotificationRepositoryMixin
from core.application.projects.connection_repository import ProjectConnectionRepositoryMixin
from core.infrastructure.db.logger_dashboard_rollups_mixin import LoggerDashboardRollupsMixin
from core.infrastructure.db.logger_events_dispatch_domain_events_mixin import (
    PlatformLoggerEventsDomainEventsMixin,
)
from core.infrastructure.db.logger_events_dispatch_mixin import PlatformLoggerEventsDispatchMixin
from core.infrastructure.db.logger_events_dispatch_outbox_mixin import (
    PlatformLoggerEventsOutboxMixin,
)
from core.infrastructure.db.logger_extensions_accounts_mixin import (
    PlatformLoggerExtensionsAccountsMixin,
)
from core.infrastructure.db.logger_lifecycle_projects_mixin import (
    PlatformLoggerLifecycleProjectsMixin,
)
from core.infrastructure.db.logger_policy_projects_mixin import PlatformLoggerPolicyProjectsMixin
from core.infrastructure.db.logger_provider_integrations_mixin import (
    PlatformLoggerProviderIntegrationsMixin,
)
from core.infrastructure.db.logger_scheduled_providers_mixin import (
    PlatformLoggerScheduledProvidersMixin,
)
from core.infrastructure.db.logger_trace_mixin import PlatformLoggerTraceMixin
from core.infrastructure.db.platform_logger_action_audit_mixin import (
    PlatformLoggerActionAuditMixin,
)
from core.infrastructure.db.platform_logger_activity_mixin import PlatformLoggerActivityMixin
from core.infrastructure.db.platform_logger_admin_queries_mixin import (
    PlatformLoggerAdminQueriesMixin,
    PlatformLoggerCommandAuditMixin,
)
from core.infrastructure.db.platform_logger_common import (
    resolve_observability_dsn as resolve_observability_dsn,
)
from core.infrastructure.db.platform_logger_core_mixin import PlatformLoggerCoreMixin
from core.infrastructure.db.platform_logger_decisions_mixin import PlatformLoggerDecisionsMixin
from core.infrastructure.db.platform_logger_jobs_mixin import PlatformLoggerJobsMixin
from core.infrastructure.db.platform_logger_scheduled_locks_mixin import (
    PlatformLoggerScheduledLocksMixin,
)
from core.infrastructure.db.platform_project_settings_history_mixin import (
    PlatformProjectSettingsHistoryMixin,
)


class PlatformConnectionStore(PlatformLoggerCoreMixin):
    """Connection, schema, retention, and project-registration primitives."""

    _shared_instances: ClassVar[dict[str, PlatformConnectionStore]] = {}


class PlatformJobStore(
    PlatformLoggerCoreMixin,
    PlatformLoggerJobsMixin,
    PlatformLoggerEventsOutboxMixin,
    PlatformLoggerDecisionsMixin,
    PlatformLoggerActivityMixin,
    PlatformLoggerAdminQueriesMixin,
    PlatformLoggerLifecycleProjectsMixin,
):
    """Job state, execution activity, decisions, and operator job queries."""

    _shared_instances: ClassVar[dict[str, PlatformJobStore]] = {}


class PlatformScheduleStore(
    PlatformLoggerCoreMixin,
    PlatformLoggerScheduledLocksMixin,
    PlatformLoggerScheduledProvidersMixin,
):
    """Scheduled jobs, execution locks, and provider configuration."""

    _shared_instances: ClassVar[dict[str, PlatformScheduleStore]] = {}


class PlatformWorkerStore(
    PlatformLoggerCoreMixin,
    PlatformLoggerJobsMixin,
    PlatformLoggerEventsOutboxMixin,
    PlatformLoggerEventsDomainEventsMixin,
    PlatformLoggerDecisionsMixin,
    PlatformLoggerActivityMixin,
    PlatformLoggerAdminQueriesMixin,
    PlatformLoggerScheduledProvidersMixin,
    ProjectConnectionRepositoryMixin,
    NotificationRepositoryMixin,
    PlatformLoggerLifecycleProjectsMixin,
):
    """Worker queue, lease, project, provider, and inbound-connection persistence."""

    _shared_instances: ClassVar[dict[str, PlatformWorkerStore]] = {}


class PlatformControlScheduleStore(
    PlatformLoggerCoreMixin,
    PlatformLoggerJobsMixin,
    PlatformLoggerEventsOutboxMixin,
    PlatformLoggerActivityMixin,
    PlatformLoggerScheduledLocksMixin,
    PlatformLoggerScheduledProvidersMixin,
    ProjectConnectionRepositoryMixin,
    NotificationRepositoryMixin,
    PlatformLoggerPolicyProjectsMixin,
    PlatformLoggerLifecycleProjectsMixin,
):
    """Scheduler-control persistence plus the project run configuration it consumes."""

    _shared_instances: ClassVar[dict[str, PlatformControlScheduleStore]] = {}


class PlatformProjectStore(
    PlatformLoggerCoreMixin,
    PlatformLoggerActionAuditMixin,
    PlatformLoggerJobsMixin,
    PlatformLoggerEventsOutboxMixin,
    PlatformLoggerActivityMixin,
    PlatformLoggerScheduledLocksMixin,
    PlatformLoggerScheduledProvidersMixin,
    ProjectConnectionRepositoryMixin,
    NotificationRepositoryMixin,
    PlatformLoggerPolicyProjectsMixin,
    PlatformLoggerLifecycleProjectsMixin,
    PlatformProjectSettingsHistoryMixin,
):
    """Project configuration, schedules, connections, policy, and lifecycle history."""

    _shared_instances: ClassVar[dict[str, PlatformProjectStore]] = {}


class PlatformEventStore(
    PlatformLoggerCoreMixin,
    PlatformLoggerEventsDispatchMixin,
):
    """Domain events, dispatch outbox, delivery attempts, and job reactions."""

    _shared_instances: ClassVar[dict[str, PlatformEventStore]] = {}


class PlatformReactionStore(
    PlatformLoggerCoreMixin,
    PlatformLoggerEventsDispatchMixin,
    ProjectConnectionRepositoryMixin,
    NotificationRepositoryMixin,
    PlatformLoggerLifecycleProjectsMixin,
):
    """Workflow rules, job reaction events, and their project authorization context."""

    _shared_instances: ClassVar[dict[str, PlatformReactionStore]] = {}


class PlatformIdentityStore(
    PlatformLoggerCoreMixin,
    PlatformLoggerActionAuditMixin,
    PlatformLoggerCommandAuditMixin,
    PlatformLoggerExtensionsAccountsMixin,
    PlatformLoggerProviderIntegrationsMixin,
):
    """Accounts, credentials, extension installs, and extension runtime records.

    The extension lifecycle runs on this store, and uninstalling an extension
    that declares an OAuth provider writes a durable command audit row in the
    same transaction, so the command audit writer belongs here too.
    """

    _shared_instances: ClassVar[dict[str, PlatformIdentityStore]] = {}


class PlatformNotificationStore(
    PlatformLoggerCoreMixin,
    ProjectConnectionRepositoryMixin,
    NotificationRepositoryMixin,
):
    """Connection definitions and notification persistence."""

    _shared_instances: ClassVar[dict[str, PlatformNotificationStore]] = {}


class PlatformIntegrationStore(
    PlatformLoggerCoreMixin,
    PlatformLoggerActionAuditMixin,
    PlatformLoggerScheduledProvidersMixin,
    ProjectConnectionRepositoryMixin,
    NotificationRepositoryMixin,
    PlatformLoggerExtensionsAccountsMixin,
    PlatformLoggerLifecycleProjectsMixin,
):
    """Provider, connector, extension, and owning-project persistence."""

    _shared_instances: ClassVar[dict[str, PlatformIntegrationStore]] = {}


class PlatformInboundPollingStore(
    PlatformLoggerCoreMixin,
    PlatformLoggerJobsMixin,
    PlatformLoggerActivityMixin,
    PlatformLoggerScheduledProvidersMixin,
    ProjectConnectionRepositoryMixin,
    NotificationRepositoryMixin,
    PlatformLoggerExtensionsAccountsMixin,
    PlatformLoggerEventsDispatchMixin,
    PlatformLoggerLifecycleProjectsMixin,
):
    """Inbound provider catalog, connections, jobs, and event persistence."""

    _shared_instances: ClassVar[dict[str, PlatformInboundPollingStore]] = {}

    @classmethod
    @contextmanager
    def dedicated(cls, dsn: object) -> Iterator[PlatformInboundPollingStore]:
        """Open one non-pooled connection owned only by an inbound poll cycle."""
        from core.infrastructure.db import psycopg_runtime

        resolved = resolve_observability_dsn(dsn)
        connection = psycopg_runtime.connect(resolved, autocommit=True)
        instance = cls.__new__(cls)
        instance.dsn = resolved
        instance._direct_conn = connection
        instance._pool = None
        instance.conn = connection
        instance._column_exists_cache = {}
        instance._lifecycle_starts = {}
        try:
            yield instance
        finally:
            connection.close()


class PlatformProviderWebhookStore(
    PlatformLoggerCoreMixin,
    PlatformLoggerScheduledProvidersMixin,
    ProjectConnectionRepositoryMixin,
    NotificationRepositoryMixin,
    PlatformLoggerExtensionsAccountsMixin,
    PlatformLoggerEventsDispatchMixin,
    PlatformLoggerLifecycleProjectsMixin,
):
    """Provider ingress resolution, extension invocation, and event persistence."""

    _shared_instances: ClassVar[dict[str, PlatformProviderWebhookStore]] = {}


class PlatformReportingStore(
    PlatformLoggerCoreMixin,
    PlatformLoggerJobsMixin,
    LoggerDashboardRollupsMixin,
    PlatformLoggerTraceMixin,
    PlatformLoggerLifecycleProjectsMixin,
):
    """Project-aware reporting, analytics, trace, and dashboard persistence."""

    _shared_instances: ClassVar[dict[str, PlatformReportingStore]] = {}


class PlatformLifecycleStore(
    PlatformLoggerCoreMixin,
    PlatformLoggerJobsMixin,
    PlatformLoggerScheduledLocksMixin,
    PlatformLoggerScheduledProvidersMixin,
    PlatformLoggerExtensionsAccountsMixin,
    PlatformLoggerPolicyProjectsMixin,
    PlatformLoggerLifecycleProjectsMixin,
    LoggerDashboardRollupsMixin,
):
    """HTTP process startup, recovery, scheduled scans, and seed persistence."""

    _shared_instances: ClassVar[dict[str, PlatformLifecycleStore]] = {}


class PlatformUiStore(
    PlatformLoggerCoreMixin,
    PlatformLoggerJobsMixin,
    PlatformLoggerDecisionsMixin,
    PlatformLoggerActivityMixin,
    PlatformLoggerScheduledLocksMixin,
    PlatformLoggerScheduledProvidersMixin,
    PlatformLoggerPolicyProjectsMixin,
    PlatformLoggerLifecycleProjectsMixin,
):
    """Authenticated project, schedule, and live job UI persistence."""

    _shared_instances: ClassVar[dict[str, PlatformUiStore]] = {}


class PlatformObservabilityStore(
    PlatformLoggerCoreMixin,
    PlatformLoggerActivityMixin,
    PlatformLoggerDecisionsMixin,
    PlatformLoggerExtensionsAccountsMixin,
    PlatformLoggerEventsDispatchMixin,
    LoggerDashboardRollupsMixin,
    PlatformLoggerTraceMixin,
):
    """Trace assembly, dashboard facts, rollups, and SLO queries."""

    _shared_instances: ClassVar[dict[str, PlatformObservabilityStore]] = {}
