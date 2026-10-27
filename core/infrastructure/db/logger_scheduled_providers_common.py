from __future__ import annotations

import json
from typing import Any

from core.infrastructure.db.platform_logger_common import (
    _UNSET,
    DEFAULT_PROVIDER_TYPES,
    ERR_PROVIDER_KEY_REQUIRED,
    ERR_WORKFLOW_ID_REQUIRED,
    SQL_AND,
    SQL_LAST_ERROR_EQ,
    SQL_STATUS_EQ,
    SQL_WORKFLOW_ID_EQ,
)


def _json_dumps(value: Any) -> str:
    return json.dumps(value, default=str)


def _helpers():
    from core.infrastructure.db import platform_logger as _platform_logger

    return _platform_logger


def _as_dict(value):
    return _helpers()._as_dict(value)


def _coerce_bool(value, default: bool = False) -> bool:
    return _helpers()._coerce_bool(value, default=default)


def _coerce_int(value, default: int) -> int:
    return _helpers()._coerce_int(value, default)


def _decrypt_secret_value(ciphertext: str, nonce_text: str | None, key_version: str | None) -> str:
    return _helpers()._decrypt_secret_value(ciphertext, nonce_text, key_version)


def _encrypt_secret_value(plain_text: str) -> tuple[str, str | None, str]:
    return _helpers()._encrypt_secret_value(plain_text)


def _normalize_string_list(value) -> list[str]:
    return _helpers()._normalize_string_list(value)


def _truncate_text(value, max_len: int = 4000) -> str | None:
    return _helpers()._truncate_text(value, max_len=max_len)


def _json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            return {}
        try:
            parsed = json.loads(raw)
        except Exception:
            return {}
        return dict(parsed) if isinstance(parsed, dict) else {}
    return {}


__all__ = [
    "DEFAULT_PROVIDER_TYPES",
    "ERR_PROVIDER_KEY_REQUIRED",
    "ERR_WORKFLOW_ID_REQUIRED",
    "SQL_AND",
    "SQL_LAST_ERROR_EQ",
    "SQL_STATUS_EQ",
    "SQL_WORKFLOW_ID_EQ",
    "_UNSET",
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
