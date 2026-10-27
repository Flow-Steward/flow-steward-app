from __future__ import annotations

from core.infrastructure.db.logger_events_dispatch_common import (
    _UNSET,
    ERR_EVENT_ID_REQUIRED,
    ERR_EVENT_TYPE_REQUIRED,
    EVENT_DISPATCH_ALLOWED_STATUSES,
    EVENT_DISPATCH_ALLOWED_TRANSITIONS,
    EVENT_TYPE_S_OR_EVENT_TYPE_LIKE_S_SQL,
    EVENT_TYPE_S_SQL,
    OCCURRED_AT_S_VALUE,
    OCCURRED_AT_S_VALUE_2,
    SQL_ACCOUNT_ID_EQ,
    SQL_AND,
    SQL_EVENT_ID_EQ,
    SQL_JOB_ID_EQ,
    SQL_LAST_ERROR_EQ,
    SQL_PROJECT_ID_EQ,
    SQL_STATUS_EQ,
    SQL_WORKFLOW_ID_EQ,
    _as_dict,
    _coerce_bool,
    _coerce_int,
    _default_account_id,
    build_event_envelope_v1,
    psycopg_runtime,
)
from core.infrastructure.db.logger_events_dispatch_delivery_attempts_mixin import (
    PlatformLoggerEventsDeliveryAttemptsMixin,
)
from core.infrastructure.db.logger_events_dispatch_dispatches_mixin import (
    PlatformLoggerEventsDispatchesMixin,
)
from core.infrastructure.db.logger_events_dispatch_domain_events_mixin import (
    PlatformLoggerEventsDomainEventsMixin,
)
from core.infrastructure.db.logger_events_dispatch_job_reactions_mixin import (
    PlatformLoggerEventsJobReactionsMixin,
)
from core.infrastructure.db.logger_events_dispatch_outbox_mixin import (
    PlatformLoggerEventsOutboxMixin,
)


class PlatformLoggerEventsDispatchMixin(
    PlatformLoggerEventsJobReactionsMixin,
    PlatformLoggerEventsOutboxMixin,
    PlatformLoggerEventsDomainEventsMixin,
    PlatformLoggerEventsDispatchesMixin,
    PlatformLoggerEventsDeliveryAttemptsMixin,
):
    pass


__all__ = [
    "ERR_EVENT_ID_REQUIRED",
    "ERR_EVENT_TYPE_REQUIRED",
    "EVENT_DISPATCH_ALLOWED_STATUSES",
    "EVENT_DISPATCH_ALLOWED_TRANSITIONS",
    "EVENT_TYPE_S_OR_EVENT_TYPE_LIKE_S_SQL",
    "EVENT_TYPE_S_SQL",
    "OCCURRED_AT_S_VALUE",
    "OCCURRED_AT_S_VALUE_2",
    "SQL_ACCOUNT_ID_EQ",
    "SQL_AND",
    "SQL_EVENT_ID_EQ",
    "SQL_JOB_ID_EQ",
    "SQL_LAST_ERROR_EQ",
    "SQL_PROJECT_ID_EQ",
    "SQL_STATUS_EQ",
    "SQL_WORKFLOW_ID_EQ",
    "_UNSET",
    "PlatformLoggerEventsDeliveryAttemptsMixin",
    "PlatformLoggerEventsDispatchMixin",
    "PlatformLoggerEventsDispatchesMixin",
    "PlatformLoggerEventsDomainEventsMixin",
    "PlatformLoggerEventsJobReactionsMixin",
    "PlatformLoggerEventsOutboxMixin",
    "_as_dict",
    "_coerce_bool",
    "_coerce_int",
    "_default_account_id",
    "build_event_envelope_v1",
    "psycopg_runtime",
]
