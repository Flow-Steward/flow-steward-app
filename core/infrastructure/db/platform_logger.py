# ruff: noqa: E402
from __future__ import annotations

import sys
from types import ModuleType

from core.infrastructure.db import psycopg_runtime as psycopg_runtime
from core.infrastructure.db.db_pool import (
    PooledConnectionProxy,
    get_threaded_pool,
    pool_bounds,
    pooling_enabled,
)
from core.infrastructure.db.platform_logger_common import (
    _UNSET,
    AESGCM,
    DEFAULT_PROJECT_POLICY,
    DEFAULT_PROVIDER_TYPES,
    ERR_EVENT_ID_REQUIRED,
    ERR_EVENT_TYPE_REQUIRED,
    ERR_PROVIDER_KEY_REQUIRED,
    ERR_WORKFLOW_ID_REQUIRED,
    EVENT_DISPATCH_ALLOWED_STATUSES,
    EVENT_DISPATCH_ALLOWED_TRANSITIONS,
    PROJECT_POLICY_AUTONOMY_MODES,
    PROJECT_POLICY_RETRY_STRATEGIES,
    PROJECT_POLICY_TOOL_MODES,
    SQL_ACCOUNT_ID_EQ,
    SQL_AND,
    SQL_DELETED_AT_IS_NULL,
    SQL_ENABLED_TRUE,
    SQL_EVENT_ID_EQ,
    SQL_FINISHED_AT_GTE,
    SQL_ID_EQ,
    SQL_JOB_DELETED_AT_IS_NULL,
    SQL_JOB_ID_EQ,
    SQL_JOB_PROJECT_ID_EQ,
    SQL_LAST_ERROR_EQ,
    SQL_PROJECT_ACCOUNT_ID_EQ,
    SQL_PROJECT_DELETED_AT_IS_NULL,
    SQL_PROJECT_ID_EQ,
    SQL_STATUS_EQ,
    SQL_WORKFLOW_ID_EQ,
    _as_dict,
    _as_list,
    _attachment_master_key_bytes,
    _coerce_bool,
    _coerce_bytes,
    _coerce_float,
    _coerce_int,
    _configured_string_set,
    _configured_string_tuple,
    _core_config,
    _decrypt_attachment_value,
    _decrypt_secret_value,
    _default_account_id,
    _encrypt_attachment_value,
    _encrypt_secret_value,
    _env_flag,
    _extension_debug_payloads_enabled,
    _extension_payload_redact_max_depth,
    _extension_payload_string_truncate_length,
    _json_clone,
    _merge_string_fields,
    _normalize_error_category,
    _normalize_error_source,
    _normalize_int_list,
    _normalize_policy_enum,
    _normalize_string_list,
    _redact_dict,
    _redact_list,
    _redact_payload,
    _redact_trace_value,
    _scope_granted,
    _scopes_within,
    _secret_master_key_bytes,
    _trace_id_for_job,
    _trace_iso,
    _trace_json_value,
    _trace_redact_max_length,
    _truncate_text,
    logger,
    resolve_observability_dsn,
)

try:
    from langchain_core.callbacks import BaseCallbackHandler
except Exception:  # pragma: no cover - optional in focused storage/unit test envs

    class BaseCallbackHandler:
        pass


_SPLIT_MODULE_NAMES = (
    "core.infrastructure.db.platform_logger_common",
    "core.infrastructure.db.platform_logger_activity_mixin",
    "core.infrastructure.db.platform_logger_admin_queries_mixin",
    "core.infrastructure.db.platform_logger_core_mixin",
    "core.infrastructure.db.platform_logger_decisions_mixin",
    "core.infrastructure.db.platform_logger_jobs_mixin",
    "core.infrastructure.db.platform_logger_scheduled_locks_mixin",
    "core.infrastructure.db.logger_provider_integrations_mixin",
    "core.infrastructure.db.logger_scheduled_jobs_mixin",
    "core.infrastructure.db.logger_scheduled_providers_common",
    "core.infrastructure.db.logger_scheduled_providers_mixin",
)

_PATCHABLE_GLOBAL_NAMES = {
    "AESGCM",
    "_as_dict",
    "_as_list",
    "_coerce_bool",
    "_coerce_float",
    "_coerce_int",
    "_configured_string_set",
    "_configured_string_tuple",
    "_default_account_id",
    "_env_flag",
    "_extension_debug_payloads_enabled",
    "_extension_payload_redact_max_depth",
    "_extension_payload_string_truncate_length",
    "_json_clone",
    "_merge_string_fields",
    "_normalize_error_category",
    "_normalize_error_source",
    "_normalize_int_list",
    "_normalize_policy_enum",
    "_normalize_string_list",
    "_redact_payload",
    "_redact_trace_value",
    "_scope_granted",
    "_scopes_within",
    "_trace_id_for_job",
    "_trace_iso",
    "_trace_json_value",
    "_trace_redact_max_length",
    "_truncate_text",
    "PooledConnectionProxy",
    "get_threaded_pool",
    "pool_bounds",
    "pooling_enabled",
    "psycopg_runtime",
}


def _propagate_split_global(name: str, value) -> None:
    for module_name in _SPLIT_MODULE_NAMES:
        module = sys.modules.get(module_name)
        if module is not None and hasattr(module, name):
            setattr(module, name, value)


class _PlatformLoggerFacadeModule(ModuleType):
    def __setattr__(self, name: str, value) -> None:
        super().__setattr__(name, value)
        if name in _PATCHABLE_GLOBAL_NAMES:
            _propagate_split_global(name, value)


sys.modules[__name__].__class__ = _PlatformLoggerFacadeModule


from core.infrastructure.db.platform_logger_callback import (
    PlatformLoggerCallback as PlatformLoggerCallback,
)

__all__ = [
    "AESGCM",
    "DEFAULT_PROJECT_POLICY",
    "DEFAULT_PROVIDER_TYPES",
    "ERR_EVENT_ID_REQUIRED",
    "ERR_EVENT_TYPE_REQUIRED",
    "ERR_PROVIDER_KEY_REQUIRED",
    "ERR_WORKFLOW_ID_REQUIRED",
    "EVENT_DISPATCH_ALLOWED_STATUSES",
    "EVENT_DISPATCH_ALLOWED_TRANSITIONS",
    "PROJECT_POLICY_AUTONOMY_MODES",
    "PROJECT_POLICY_RETRY_STRATEGIES",
    "PROJECT_POLICY_TOOL_MODES",
    "SQL_ACCOUNT_ID_EQ",
    "SQL_AND",
    "SQL_DELETED_AT_IS_NULL",
    "SQL_ENABLED_TRUE",
    "SQL_EVENT_ID_EQ",
    "SQL_FINISHED_AT_GTE",
    "SQL_ID_EQ",
    "SQL_JOB_DELETED_AT_IS_NULL",
    "SQL_JOB_ID_EQ",
    "SQL_JOB_PROJECT_ID_EQ",
    "SQL_LAST_ERROR_EQ",
    "SQL_PROJECT_ACCOUNT_ID_EQ",
    "SQL_PROJECT_DELETED_AT_IS_NULL",
    "SQL_PROJECT_ID_EQ",
    "SQL_STATUS_EQ",
    "SQL_WORKFLOW_ID_EQ",
    "_UNSET",
    "BaseCallbackHandler",
    "PlatformLoggerCallback",
    "PooledConnectionProxy",
    "_as_dict",
    "_as_list",
    "_attachment_master_key_bytes",
    "_coerce_bool",
    "_coerce_bytes",
    "_coerce_float",
    "_coerce_int",
    "_configured_string_set",
    "_configured_string_tuple",
    "_core_config",
    "_decrypt_attachment_value",
    "_decrypt_secret_value",
    "_default_account_id",
    "_encrypt_attachment_value",
    "_encrypt_secret_value",
    "_env_flag",
    "_extension_debug_payloads_enabled",
    "_extension_payload_redact_max_depth",
    "_extension_payload_string_truncate_length",
    "_json_clone",
    "_merge_string_fields",
    "_normalize_error_category",
    "_normalize_error_source",
    "_normalize_int_list",
    "_normalize_policy_enum",
    "_normalize_string_list",
    "_redact_dict",
    "_redact_list",
    "_redact_payload",
    "_redact_trace_value",
    "_scope_granted",
    "_scopes_within",
    "_secret_master_key_bytes",
    "_trace_id_for_job",
    "_trace_iso",
    "_trace_json_value",
    "_trace_redact_max_length",
    "_truncate_text",
    "get_threaded_pool",
    "logger",
    "pool_bounds",
    "pooling_enabled",
    "psycopg_runtime",
    "resolve_observability_dsn",
]
