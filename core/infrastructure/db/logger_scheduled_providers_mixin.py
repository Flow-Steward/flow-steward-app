from __future__ import annotations

from core.infrastructure.db.logger_provider_integrations_mixin import (
    PlatformLoggerProviderIntegrationsMixin,
)
from core.infrastructure.db.logger_scheduled_jobs_mixin import PlatformLoggerScheduledJobsMixin
from core.infrastructure.db.logger_scheduled_providers_common import (
    _UNSET,
    DEFAULT_PROVIDER_TYPES,
    ERR_PROVIDER_KEY_REQUIRED,
    ERR_WORKFLOW_ID_REQUIRED,
    SQL_AND,
    SQL_LAST_ERROR_EQ,
    SQL_STATUS_EQ,
    SQL_WORKFLOW_ID_EQ,
    _as_dict,
    _coerce_bool,
    _coerce_int,
    _decrypt_secret_value,
    _encrypt_secret_value,
    _helpers,
    _json_dumps,
    _json_object,
    _normalize_string_list,
    _truncate_text,
)


class PlatformLoggerScheduledProvidersMixin(
    PlatformLoggerScheduledJobsMixin,
    PlatformLoggerProviderIntegrationsMixin,
):
    conn: object
    _table_exists: object


__all__ = [
    "DEFAULT_PROVIDER_TYPES",
    "ERR_PROVIDER_KEY_REQUIRED",
    "ERR_WORKFLOW_ID_REQUIRED",
    "SQL_AND",
    "SQL_LAST_ERROR_EQ",
    "SQL_STATUS_EQ",
    "SQL_WORKFLOW_ID_EQ",
    "_UNSET",
    "PlatformLoggerScheduledProvidersMixin",
    "_as_dict",
    "_coerce_bool",
    "_coerce_int",
    "_decrypt_secret_value",
    "_encrypt_secret_value",
    "_helpers",
    "_json_dumps",
    "_json_object",
    "_normalize_string_list",
    "_truncate_text",
]
