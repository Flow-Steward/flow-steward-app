from __future__ import annotations

import sys
from types import ModuleType

from core.infrastructure.db.logger_extensions_accounts_accounts_mixin import (
    PlatformLoggerAccountsMixin,
)
from core.infrastructure.db.logger_extensions_accounts_callbacks_mixin import (
    PlatformLoggerExtensionsAccountsCallbacksMixin,
)
from core.infrastructure.db.logger_extensions_accounts_common import (
    ACCOUNT_ID_EXCLUDED_ACCOUNT_ID_SQL,
    ACCOUNT_ID_IS_REQUIRED_MESSAGE,
    REQUIRED_SCOPES_JSONB_EXCLUDED_REQUIRED_SCOPES_JSONB_SQL,
    SQL_ACCOUNT_ID_EQ,
    SQL_AND,
    SQL_JOB_ID_EQ,
    PlatformLoggerExtensionsAccountsCommonMixin,
    _as_dict,
    _as_list,
    _coerce_bool,
    _coerce_int,
    _default_account_id,
    _normalize_int_list,
    _normalize_string_list,
    _redact_payload,
    _scopes_within,
    _trace_id_for_job,
    _trace_json_value,
)
from core.infrastructure.db.logger_extensions_accounts_pat_mixin import (
    PlatformLoggerPersonalAccessTokensMixin,
)
from core.infrastructure.db.logger_extensions_accounts_query_mixin import (
    PlatformLoggerExtensionsAccountsQueryMixin,
)
from core.infrastructure.db.logger_extensions_accounts_runtime_mixin import (
    PlatformLoggerExtensionsAccountsRuntimeMixin,
)
from core.infrastructure.db.logger_extensions_accounts_service_tokens_mixin import (
    PlatformLoggerServiceTokensMixin,
)
from core.infrastructure.db.logger_extensions_accounts_sync_mixin import (
    PlatformLoggerExtensionsAccountsSyncMixin,
)

_SPLIT_MODULE_NAMES = (
    "core.infrastructure.db.logger_extensions_accounts_common",
    "core.infrastructure.db.logger_extensions_accounts_accounts_mixin",
    "core.infrastructure.db.logger_extensions_accounts_callbacks_mixin",
    "core.infrastructure.db.logger_extensions_accounts_pat_mixin",
    "core.infrastructure.db.logger_extensions_accounts_query_mixin",
    "core.infrastructure.db.logger_extensions_accounts_runtime_mixin",
    "core.infrastructure.db.logger_extensions_accounts_service_tokens_mixin",
    "core.infrastructure.db.logger_extensions_accounts_sync_mixin",
)

_PATCHABLE_HELPER_NAMES = {
    "_as_dict",
    "_as_list",
    "_coerce_bool",
    "_coerce_int",
    "_default_account_id",
    "_normalize_int_list",
    "_normalize_string_list",
    "_redact_payload",
    "_scopes_within",
    "_trace_id_for_job",
    "_trace_json_value",
}


def _propagate_split_global(name: str, value) -> None:
    for module_name in _SPLIT_MODULE_NAMES:
        module = sys.modules.get(module_name)
        if module is not None and hasattr(module, name):
            setattr(module, name, value)


class PlatformLoggerExtensionsAccountsMixin(
    PlatformLoggerExtensionsAccountsCommonMixin,
    PlatformLoggerExtensionsAccountsSyncMixin,
    PlatformLoggerExtensionsAccountsQueryMixin,
    PlatformLoggerExtensionsAccountsCallbacksMixin,
    PlatformLoggerExtensionsAccountsRuntimeMixin,
    PlatformLoggerAccountsMixin,
    PlatformLoggerServiceTokensMixin,
    PlatformLoggerPersonalAccessTokensMixin,
):
    pass


class _LoggerExtensionsAccountsFacadeModule(ModuleType):
    def __setattr__(self, name: str, value) -> None:
        super().__setattr__(name, value)
        if name in _PATCHABLE_HELPER_NAMES:
            _propagate_split_global(name, value)


sys.modules[__name__].__class__ = _LoggerExtensionsAccountsFacadeModule


__all__ = [
    "ACCOUNT_ID_EXCLUDED_ACCOUNT_ID_SQL",
    "ACCOUNT_ID_IS_REQUIRED_MESSAGE",
    "REQUIRED_SCOPES_JSONB_EXCLUDED_REQUIRED_SCOPES_JSONB_SQL",
    "SQL_ACCOUNT_ID_EQ",
    "SQL_AND",
    "SQL_JOB_ID_EQ",
    "PlatformLoggerExtensionsAccountsMixin",
    "_as_dict",
    "_as_list",
    "_coerce_bool",
    "_coerce_int",
    "_default_account_id",
    "_normalize_int_list",
    "_normalize_string_list",
    "_redact_payload",
    "_scopes_within",
    "_trace_id_for_job",
    "_trace_json_value",
]
