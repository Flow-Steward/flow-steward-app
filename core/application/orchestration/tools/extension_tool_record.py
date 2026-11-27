# mypy: ignore-errors
"""Extension record lookup for extension-backed tool manifests.

Resolves the observability DSN a tool call should use and loads the extension
row that backs a tool, within the caller's account scope.
"""

from __future__ import annotations

from typing import Any

from core.infrastructure.config.runtime_env import runtime_env as _runtime_env

_OBSERVABILITY_DSN_ENV_NAMES = (
    "FS_OBSERVABILITY_DSN",
    "FS_DB_DSN",
    "DATABASE_URL",
    "POSTGRES_DSN",
)


def _observability_dsn_env_candidate() -> str:
    for name in _OBSERVABILITY_DSN_ENV_NAMES:
        value = _runtime_env.get(name, "").strip()
        if value:
            return value
    return ""


def _resolve_observability_dsn_with_preimport_env(
    resolve_observability_dsn,
    preimport_dsn: str,
) -> str:
    return (
        resolve_observability_dsn(preimport_dsn) if preimport_dsn else resolve_observability_dsn()
    )


def _resolve_extension_record(
    manifest,
    *,
    runtime_context: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], Any]:
    implementation_payload = manifest.implementation.to_dict()
    extension_key = str(
        implementation_payload.get("extension_key")
        or implementation_payload.get("extension_id")
        or (getattr(manifest, "config", {}) or {}).get("extension_key")
        or ""
    ).strip()
    if not extension_key:
        raise RuntimeError(f"Tool '{manifest.tool_id}' is missing implementation.extension_key")

    preimport_dsn = _observability_dsn_env_candidate()
    from core.infrastructure.db.platform_logger_common import resolve_observability_dsn
    from core.infrastructure.db.platform_stores import PlatformIdentityStore
    from core.infrastructure.extension_runtime.extension_runtime import (
        normalize_extension_row_for_runtime,
    )

    normalized_runtime_context = runtime_context if isinstance(runtime_context, dict) else {}
    candidate_logger = normalized_runtime_context.get("logger")
    bl = candidate_logger if hasattr(candidate_logger, "get_extension") else None
    if bl is None:
        dsn = _resolve_observability_dsn_with_preimport_env(
            resolve_observability_dsn,
            preimport_dsn,
        )
        if not dsn:
            raise RuntimeError(f"Tool '{manifest.tool_id}' cannot resolve observability DSN")
        bl = PlatformIdentityStore.get_shared_instance(dsn)
    # FS-DEEP-CAND-060: resolve the tool-provider extension within the caller's
    # account scope. Without account_id the store returns the first matching
    # extension_key across all accounts, letting a scoped caller execute another
    # account's extension. Pass the caller account so cross-account keys are
    # invisible (get_extension returns None -> "not found").
    # ADR 0020, C6: an account that enabled another account's (the installation's)
    # install uses that install; the account's enablement, not the bundle's owner,
    # decides. An install the account has not enabled stays "not found".
    caller_account_id = str(normalized_runtime_context.get("account_id") or "").strip()
    access: dict[str, Any] | None = None
    if hasattr(bl, "get_extension"):
        if caller_account_id:
            from core.infrastructure.extension_runtime.extension_runtime_helpers import (
                extension_install_account,
            )

            lookup_account_id, access = extension_install_account(
                bl, extension_key, caller_account_id
            )
            extension = (
                bl.get_extension(extension_key, account_id=lookup_account_id)
                if lookup_account_id
                else None
            )
        else:
            extension = bl.get_extension(extension_key)
    else:
        extension = None
    if not extension:
        raise RuntimeError(f"Tool '{manifest.tool_id}' extension '{extension_key}' not found")
    normalized = normalize_extension_row_for_runtime(extension)
    if (
        access is not None
        and access.get("access") in {"enabled", "global"}
        and access.get("install_state") not in {"", "active"}
    ):
        normalized["enabled"] = False
    if not bool(normalized.get("enabled")):
        raise RuntimeError(f"Tool '{manifest.tool_id}' extension '{extension_key}' is disabled")
    return normalized, bl
