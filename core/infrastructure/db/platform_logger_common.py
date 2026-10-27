from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import uuid

from core.infrastructure.config.platform_identity import DEFAULT_ACCOUNT_ID, DEFAULT_TENANT_ID
from core.infrastructure.config.runtime_env import runtime_env as _runtime_env

logger = logging.getLogger(__name__)


class _RuntimeConfigProxy:
    @property
    def config(self):
        from core.infrastructure.config import runtime_config

        return runtime_config.CONFIG


_core_config = _RuntimeConfigProxy()

try:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
except Exception:  # pragma: no cover - optional dependency in some test environments
    AESGCM = None

DEFAULT_PROJECT_POLICY: dict = {
    "autonomy_mode": "supervised",
    "autonomy_confidence_threshold": 0.80,
    "retry_strategy": "bounded",
    "retry_max_attempts": 2,
    "retry_backoff_initial_seconds": 2,
    "retry_backoff_max_seconds": 120,
    "concurrency_cap": 1,
    "tool_policy_mode": "registry_only",
}

PROJECT_POLICY_AUTONOMY_MODES = {"manual", "supervised", "auto"}
PROJECT_POLICY_RETRY_STRATEGIES = {"bounded", "flat", "failure_class"}
PROJECT_POLICY_TOOL_MODES = {"registry_only", "restricted", "permissive"}

DEFAULT_PROVIDER_TYPES: list[dict] = [
    {
        "key": "task_provider",
        "label": "Task Provider",
        "description": "Normalizes external task systems into Flow Steward intake envelopes.",
        "enabled": True,
    },
    {
        "key": "notification_provider",
        "label": "Notification Provider",
        "description": "Delivers Flow Steward domain events to external notification channels.",
        "enabled": True,
    },
    {
        "key": "tool_provider",
        "label": "Tool Provider",
        "description": "Exposes out-of-process tools and custom task helpers.",
        "enabled": True,
    },
]

_UNSET = object()

EVENT_DISPATCH_ALLOWED_STATUSES = {
    "matched",
    "skipped",
    "queued",
    "dispatching",
    "succeeded",
    "failed",
    "dead_lettered",
}

# Dispatch state-machine is intentionally strict:
# - only "matched" can fan out to queued/skipped/failed
# - terminal statuses stay terminal unless explicitly redriven to queued
EVENT_DISPATCH_ALLOWED_TRANSITIONS: dict[str, set[str]] = {
    "matched": {"matched", "queued", "skipped", "failed"},
    "queued": {"queued", "dispatching", "succeeded", "failed", "dead_lettered"},
    "dispatching": {"dispatching", "succeeded", "failed", "dead_lettered"},
    "failed": {"failed", "queued", "dead_lettered"},
    "dead_lettered": {"dead_lettered", "queued"},
    "succeeded": {"succeeded"},
    "skipped": {"skipped"},
}

SQL_STATUS_EQ = "status = %s"
SQL_LAST_ERROR_EQ = "last_error = %s"
SQL_DELETED_AT_IS_NULL = "deleted_at IS NULL"
SQL_ACCOUNT_ID_EQ = "account_id = %s"
SQL_FINISHED_AT_GTE = "j.finished_at >= %s"
SQL_PROJECT_DELETED_AT_IS_NULL = "p.deleted_at IS NULL"
SQL_JOB_DELETED_AT_IS_NULL = "j.deleted_at IS NULL"
SQL_PROJECT_ACCOUNT_ID_EQ = "p.account_id = %s"
SQL_AND = " AND "
SQL_ID_EQ = "id = %s"
SQL_PROJECT_ID_EQ = "project_id = %s"
SQL_WORKFLOW_ID_EQ = "workflow_id = %s"
SQL_ENABLED_TRUE = "enabled = true"
SQL_JOB_ID_EQ = "job_id = %s"
SQL_EVENT_ID_EQ = "event_id = %s"
SQL_JOB_PROJECT_ID_EQ = "j.project_id = %s"

ERR_WORKFLOW_ID_REQUIRED = "workflow_id is required"
ERR_PROVIDER_KEY_REQUIRED = "provider_key is required"
ERR_EVENT_TYPE_REQUIRED = "event_type is required"
ERR_EVENT_ID_REQUIRED = "event_id is required"


def resolve_observability_dsn(explicit_dsn: object | None = None) -> str:
    """Resolve DSN for Flow Steward observability storage.

    Resolution order (first non-empty wins):
    1. explicit_dsn argument
    2. FS_OBSERVABILITY_DSN
    3. FS_DB_DSN
    4. DATABASE_URL
    5. POSTGRES_DSN
    """
    candidates = [
        str(explicit_dsn or ""),
        _runtime_env.get("FS_OBSERVABILITY_DSN", ""),
        _runtime_env.get("FS_DB_DSN", ""),
        _runtime_env.get("DATABASE_URL", ""),
        _runtime_env.get("POSTGRES_DSN", ""),
    ]
    for value in candidates:
        if value and value.strip():
            return value.strip()
    return ""


def _as_dict(value) -> dict:
    return value if isinstance(value, dict) else {}


def _configured_string_set(key: str, default: tuple[str, ...] = ()) -> frozenset[str]:
    configured_raw = _core_config.config.get(key)
    if isinstance(configured_raw, (list, tuple, set, frozenset)):
        configured = configured_raw
    else:
        configured = default
    return frozenset(
        str(item or "").strip().lower() for item in configured if str(item or "").strip()
    )


def _configured_string_tuple(key: str, default: tuple[str, ...] = ()) -> tuple[str, ...]:
    configured_raw = _core_config.config.get(key)
    if isinstance(configured_raw, (list, tuple, set, frozenset)):
        configured = configured_raw
    else:
        configured = default
    normalized: list[str] = []
    seen: set[str] = set()
    for item in configured:
        text = str(item or "").strip().lower()
        if not text or text in seen:
            continue
        seen.add(text)
        normalized.append(text)
    return tuple(normalized)


def _extension_debug_payloads_enabled() -> bool:
    return bool(_core_config.config.get("extension_debug_payloads", False))


def _extension_payload_redact_max_depth() -> int:
    return max(1, int(_core_config.config.get("extension_payload_redact_max_depth") or 8))


def _extension_payload_string_truncate_length() -> int:
    return max(
        32,
        int(_core_config.config.get("extension_payload_string_truncate_length") or 500),
    )


def _trace_redact_max_length() -> int:
    return max(32, int(_core_config.config.get("trace_redact_max_length") or 1000))


_BUILTIN_REDACT_EXACT_KEYS = frozenset({"callback_url"})


def _redact_payload(payload: dict, *, max_depth: int | None = None) -> dict:
    """Return a redacted copy of a payload dict, replacing sensitive values with '[REDACTED]'.

    Nested dicts are traversed. Large string values are truncated unless
    payload debugging is explicitly enabled in the global config.
    """
    if _extension_debug_payloads_enabled():
        return payload
    return _redact_dict(
        payload,
        depth=0,
        max_depth=_extension_payload_redact_max_depth() if max_depth is None else max(1, max_depth),
        redact_keys=_configured_string_set("extension_payload_redact_keys"),
        redact_exact_keys=_configured_string_set("extension_payload_redact_exact_keys")
        | _BUILTIN_REDACT_EXACT_KEYS,
        redact_key_fragments=_configured_string_tuple("extension_payload_redact_key_fragments"),
        truncate_length=_extension_payload_string_truncate_length(),
    )


def _redact_dict(
    d: dict,
    *,
    depth: int,
    max_depth: int,
    redact_keys: frozenset[str],
    redact_exact_keys: frozenset[str],
    redact_key_fragments: tuple[str, ...],
    truncate_length: int,
) -> dict:
    if depth >= max_depth:
        return {"__truncated__": True}
    out: dict = {}
    for k, v in d.items():
        key_lower = str(k).lower()
        if (
            key_lower in redact_keys
            or key_lower in redact_exact_keys
            or any(fragment in key_lower for fragment in redact_key_fragments)
        ):
            out[k] = "[REDACTED]"
        elif isinstance(v, dict):
            out[k] = _redact_dict(
                v,
                depth=depth + 1,
                max_depth=max_depth,
                redact_keys=redact_keys,
                redact_exact_keys=redact_exact_keys,
                redact_key_fragments=redact_key_fragments,
                truncate_length=truncate_length,
            )
        elif isinstance(v, list):
            out[k] = _redact_list(
                v,
                depth=depth + 1,
                max_depth=max_depth,
                redact_keys=redact_keys,
                redact_exact_keys=redact_exact_keys,
                redact_key_fragments=redact_key_fragments,
                truncate_length=truncate_length,
            )
        elif isinstance(v, str) and len(v) > truncate_length:
            out[k] = v[:truncate_length] + f"... [{len(v)} chars total]"
        else:
            out[k] = v
    return out


def _redact_list(
    lst: list,
    *,
    depth: int,
    max_depth: int,
    redact_keys: frozenset[str],
    redact_exact_keys: frozenset[str],
    redact_key_fragments: tuple[str, ...],
    truncate_length: int,
) -> list:
    if depth >= max_depth:
        return [{"__truncated__": True}]
    out: list = []
    for item in lst:
        if isinstance(item, dict):
            out.append(
                _redact_dict(
                    item,
                    depth=depth + 1,
                    max_depth=max_depth,
                    redact_keys=redact_keys,
                    redact_exact_keys=redact_exact_keys,
                    redact_key_fragments=redact_key_fragments,
                    truncate_length=truncate_length,
                )
            )
        elif isinstance(item, str) and len(item) > truncate_length:
            out.append(item[:truncate_length] + f"... [{len(item)} chars total]")
        else:
            out.append(item)
    return out


def _as_list(value) -> list:
    return value if isinstance(value, list) else []


def _json_clone(value):
    try:
        return json.loads(json.dumps(value, default=str))
    except Exception:
        return value


def _merge_string_fields(*sources: dict) -> dict:
    """Merge dict sources keeping non-empty string values (last source wins)."""
    merged: dict = {}
    for source in sources:
        if not isinstance(source, dict):
            continue
        for key, value in source.items():
            if isinstance(value, str):
                if value.strip():
                    merged[key] = value.strip()
            elif value not in (None, "", [], {}):
                merged[key] = value
    return merged


def _coerce_bool(value, default: bool = False) -> bool:
    if value is None:
        return bool(default)
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
    return bool(default)


def _coerce_int(value, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return int(default)


def _coerce_float(value, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def _normalize_string_list(value) -> list[str]:
    normalized: list[str] = []
    for item in value or []:
        text = str(item or "").strip()
        if text:
            normalized.append(text)
    return list(dict.fromkeys(normalized))


def _normalize_int_list(value) -> list[str]:
    normalized: list[str] = []
    seen: set[str] = set()
    for item in value or []:
        parsed = str(item or "").strip()
        if not parsed or parsed in seen:
            continue
        seen.add(parsed)
        normalized.append(parsed)
    return normalized


def _scope_granted(scope_set: set[str], required_scope: str) -> bool:
    required = str(required_scope or "").strip().lower()
    if not required:
        return True
    if required in scope_set or "admin:all" in scope_set:
        return True
    separator = ":" if ":" in required else ""
    if not separator and "." in required:
        separator = "."
    if not separator:
        return required in scope_set
    domain, _, action = required.partition(separator)
    return f"{domain}:*" in scope_set or f"*:{action}" in scope_set or "*:*" in scope_set


def _scopes_within(parent_scopes: list[str], child_scopes: list[str]) -> bool:
    parent = {
        str(item or "").strip().lower() for item in (parent_scopes or []) if str(item or "").strip()
    }
    child = [
        str(item or "").strip().lower() for item in (child_scopes or []) if str(item or "").strip()
    ]
    if not child:
        return True
    if not parent:
        # Empty parent means unbounded at service-account level.
        return True
    return all(_scope_granted(parent, scope) for scope in child)


def _normalize_policy_enum(
    value,
    *,
    default: str,
    allowed: set[str],
    strict: bool = False,
    aliases: dict[str, str] | None = None,
    field_name: str,
) -> str:
    normalized = str(value or "").strip().lower()
    if not normalized:
        return default
    if aliases:
        normalized = aliases.get(normalized, normalized)
    if normalized in allowed:
        return normalized
    if strict:
        raise ValueError(
            f"Invalid project policy {field_name}='{value}'. "
            f"Allowed values: {', '.join(sorted(allowed))}"
        )
    return default


def _truncate_text(value, max_len: int = 4000) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if max_len <= 0:
        return text
    return text[:max_len]


def _step_execution_id(value) -> str | None:
    """A step execution id as stored (uuid column), or None when absent or malformed."""
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return str(uuid.UUID(text))
    except ValueError:
        return None


def _default_account_id() -> str:
    return DEFAULT_ACCOUNT_ID


def _default_tenant_id() -> str:
    return DEFAULT_TENANT_ID


def _env_flag(name: str, default: bool = False) -> bool:
    raw = _runtime_env.get(name)
    if raw is None:
        return bool(default)
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def _trace_id_for_job(job_id) -> str | None:
    normalized = str(job_id or "").strip()
    return f"job:{normalized}" if normalized else None


def _trace_json_value(value, *, default):
    if isinstance(value, (dict, list)):
        return _json_clone(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return _json_clone(default)
        try:
            return json.loads(text)
        except Exception:
            return _json_clone(default)
    return _json_clone(default)


def _trace_iso(value) -> str | None:
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        try:
            return value.isoformat()
        except Exception:
            return _truncate_text(value, 255)
    return _truncate_text(value, 255)


def _redact_trace_value(value, *, max_len: int | None = None):
    if isinstance(value, dict):
        return _redact_payload(_as_dict(value))
    if isinstance(value, list):
        return _redact_list(
            list(value),
            depth=0,
            max_depth=min(_extension_payload_redact_max_depth(), 4),
            redact_keys=_configured_string_set("extension_payload_redact_keys"),
            redact_exact_keys=_configured_string_set("extension_payload_redact_exact_keys")
            | _BUILTIN_REDACT_EXACT_KEYS,
            redact_key_fragments=_configured_string_tuple("extension_payload_redact_key_fragments"),
            truncate_length=_extension_payload_string_truncate_length(),
        )
    if isinstance(value, str):
        return _truncate_text(
            value,
            _trace_redact_max_length() if max_len is None else max(32, int(max_len)),
        )
    return value


def _secret_master_key_bytes() -> bytes | None:
    raw = str(_runtime_env.get("FS_SECRET_KEY", "") or "").strip()
    if not raw and _runtime_env.get("PYTEST_CURRENT_TEST"):
        raw = "flow-steward-pytest-secret-key"
    if not raw:
        return None
    return hashlib.sha256(raw.encode("utf-8")).digest()


def _attachment_master_key_bytes() -> bytes | None:
    raw = str(_runtime_env.get("FS_SECRET_KEY", "") or "").strip()
    if not raw and _runtime_env.get("PYTEST_CURRENT_TEST"):
        raw = "flow-steward-pytest-secret-key"
    if not raw:
        return None
    return hashlib.sha256(f"attachment-blobs:{raw}".encode()).digest()


def _coerce_bytes(value) -> bytes:
    if value is None:
        return b""
    if isinstance(value, memoryview):
        return value.tobytes()
    if isinstance(value, bytearray):
        return bytes(value)
    if isinstance(value, bytes):
        return value
    return bytes(value)


def _encrypt_secret_value(plain_text: str) -> tuple[str, str | None, str]:
    text = str(plain_text or "")
    key = _secret_master_key_bytes()
    if key and AESGCM is not None:
        nonce = os.urandom(12)
        cipher = AESGCM(key).encrypt(nonce, text.encode("utf-8"), None)
        return (
            base64.urlsafe_b64encode(cipher).decode("ascii"),
            base64.urlsafe_b64encode(nonce).decode("ascii"),
            "aesgcm-v1",
        )
    if not key:
        raise RuntimeError("FS_SECRET_KEY is required for encrypted secret storage")
    raise RuntimeError("cryptography/AESGCM is required for encrypted secret storage")


def _decrypt_secret_value(ciphertext: str, nonce_text: str | None, key_version: str | None) -> str:
    ctext = str(ciphertext or "")
    version = str(key_version or "").strip().lower()
    if not ctext:
        return ""
    if version.startswith("plain") or not nonce_text:
        return ctext
    key = _secret_master_key_bytes()
    if not key or AESGCM is None:
        return ""
    try:
        nonce = base64.urlsafe_b64decode(str(nonce_text).encode("ascii"))
        cipher_bytes = base64.urlsafe_b64decode(ctext.encode("ascii"))
        plain = AESGCM(key).decrypt(nonce, cipher_bytes, None)
        return plain.decode("utf-8")
    except Exception:
        return ""


def _encrypt_attachment_value(content: bytes) -> tuple[bytes, bytes, str]:
    payload = _coerce_bytes(content)
    key = _attachment_master_key_bytes()
    if key and AESGCM is not None:
        nonce = os.urandom(12)
        cipher = AESGCM(key).encrypt(nonce, payload, None)
        return cipher, nonce, "aesgcm-v1"
    if not key:
        raise RuntimeError("FS_SECRET_KEY is required for encrypted attachment storage")
    raise RuntimeError("cryptography/AESGCM is required for encrypted attachment storage")


def _decrypt_attachment_value(
    ciphertext: bytes, nonce_value: bytes | None, key_version: str | None
) -> bytes:
    cipher_bytes = _coerce_bytes(ciphertext)
    nonce_bytes = _coerce_bytes(nonce_value)
    version = str(key_version or "").strip().lower()
    if not cipher_bytes:
        return b""
    if version.startswith("plain") or not nonce_bytes:
        return cipher_bytes
    key = _attachment_master_key_bytes()
    if not key or AESGCM is None:
        return b""
    try:
        return AESGCM(key).decrypt(nonce_bytes, cipher_bytes, None)
    except Exception:
        return b""


def _normalize_error_source(value: str | None) -> str:
    source = str(value or "").strip().lower()
    if source in {"provider", "tool", "system", "http", "unknown"}:
        return source
    return "unknown"


def _normalize_error_category(value: str | None) -> str:
    category = str(value or "").strip().lower()
    if category in {
        "transient",
        "contract",
        "tool",
        "validation",
        "provider",
        "human_blocked",
        "unknown",
    }:
        return category
    return "unknown"
