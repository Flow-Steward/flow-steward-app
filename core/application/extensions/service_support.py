from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin
from urllib.request import Request

from core.application.auth.config import DEFAULT_ACCOUNT_ID
from core.application.extensions.semver import compare_semver, normalize_semver
from core.application.management.command_models import (
    ExtensionDescriptorValidateResultView,
)
from core.application.management.errors import ServiceForbiddenError as ServiceForbiddenError
from core.application.management.errors import ServiceUnavailableError, ServiceValidationError
from core.application.management.scope import ScopeContext, normalize_scope
from core.infrastructure.config.runtime_env import runtime_env as _runtime_env
from core.infrastructure.db import extension_runtime_connection_store

from .install import DBExtensionInstallStore, ExtensionInstallStore
from .runtime_store import DBExtensionRuntimeStore, ExtensionRuntimeStore

logger = logging.getLogger(__name__)


_default_extension_install_store: ExtensionInstallStore | DBExtensionInstallStore | None = None
_default_extension_runtime_store: ExtensionRuntimeStore | DBExtensionRuntimeStore | None = None


def _normalize_text(value: Any) -> str:
    return str(value or "").strip()


def _normalize_lower(value: Any) -> str:
    return _normalize_text(value).lower()


def _csv_values(value: Any) -> list[str]:
    if isinstance(value, (list, tuple, set)):
        raw_items: list[str] = []
        for item in value:
            raw_items.extend(_csv_values(item))
        return raw_items
    normalized = _normalize_text(value)
    if not normalized:
        return []
    return [chunk.strip() for chunk in normalized.split(",") if chunk.strip()]


def _source_label(raw: Any) -> str:
    normalized = _normalize_lower(raw)
    if normalized in _SOURCE_LABEL_VALUES:
        return normalized
    if normalized in {"marketplace_managed", "marketplace_published"}:
        return "marketplace"
    if normalized == "builtin":
        return "builtin"
    if normalized in {"url", "http", "https"}:
        return "url"
    if normalized in {"generated", "ai_generated"}:
        return "generated"
    if normalized in {"extension", "bundle"}:
        return "extension_bundle"
    return "file" if normalized else ""


def _entitlement_status_value(entitlement: Any) -> str:
    status = getattr(entitlement, "status", "")
    if hasattr(status, "value"):
        status = status.value
    normalized = _normalize_lower(status)
    return normalized or "unknown"


def _status_group_for_install(*, record: Any, entitlement_status: str) -> str:
    normalized_entitlement = _normalize_lower(entitlement_status)
    state = _normalize_lower(getattr(record, "state", ""))
    runtime_mode = _normalize_lower(getattr(record, "runtime_mode", ""))
    compatibility = _normalize_lower(getattr(record, "runtime_status", ""))
    health = _normalize_lower(getattr(record, "health_status", ""))
    handshake = _normalize_lower(getattr(record, "handshake_status", ""))

    if normalized_entitlement in _ENTITLEMENT_BILLING_BLOCKED:
        return "billing_blocked"
    if health == "failed" or handshake == "failed":
        return "runtime_issue"
    if compatibility == "incompatible":
        return "incompatible"
    if (
        state == "active"
        and normalized_entitlement in _ENTITLEMENT_USABLE
        and compatibility != "incompatible"
    ):
        if runtime_mode == "remote_https":
            if handshake == "ok" and health == "ok":
                return "usable"
            return "runtime_issue"
        return "usable"
    return "unknown"


def _contains_kind_from_artifact_kind(kind: Any) -> str:
    normalized = _normalize_lower(kind)
    if normalized == "extension":
        return "runtime_module"
    if normalized in _MARKETPLACE_CONTAINS_ALLOWED:
        return normalized
    return ""


def _compatibility_for_descriptor(descriptor: dict[str, Any]) -> str:
    error = _platform_compatibility_error(
        descriptor or {}, platform_version=_runtime_platform_version()
    )
    return "incompatible" if error else "compatible"


def _release_visible_to_account(
    release: Any,
    *,
    account_id: str,
    allow_marketplace_public: bool = True,
) -> bool:
    release_account_id = _normalize_text(getattr(release, "account_id", ""))
    normalized_account_id = _normalize_text(account_id)
    if not release_account_id:
        return True
    if release_account_id == normalized_account_id:
        return True
    return bool(
        allow_marketplace_public
        and _normalize_lower(getattr(release, "channel", "")) == "marketplace_managed"
        and release_account_id == _normalize_text(DEFAULT_ACCOUNT_ID)
    )


def _service_scope(scope: ScopeContext | None) -> ScopeContext:
    return normalize_scope(scope)


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


_ENTITLEMENT_BILLING_BLOCKED = {"trial_expired", "suspended_nonpayment", "disabled"}
_ENTITLEMENT_USABLE = {"trial_active", "active", "grace_period"}
_STATUS_GROUP_ORDER = ("billing_blocked", "runtime_issue", "incompatible", "usable", "unknown")
_SOURCE_LABEL_VALUES = {"marketplace", "url", "file", "builtin", "generated", "extension_bundle"}
_MARKETPLACE_CONTAINS_ALLOWED = {
    "workflow",
    "agent",
    "skill",
    "tool",
    "data_contract",
    "guardrail",
    "runtime_module",
}


def _token_secret() -> str:
    return (
        _normalize_text(_runtime_env.get("FS_EXTENSION_RUNTIME_SIGNING_SECRET"))
        or _normalize_text(_runtime_env.get("FS_SECRET_KEY"))
        or _normalize_text(_runtime_env.get("SECRET_KEY"))
    )


def _runtime_platform_version() -> str:
    """Extension host contract version, NOT the application build version.

    platform_min is declared against DEFAULT_FS_RUNTIME_VERSION, which every
    lifecycle.py validation entry point defaults to. Returning APP_VERSION
    compared different scales (0.1.0 vs 1.0.0), failing every shipped extension.
    """
    from core.application.extensions.extension_descriptor import DEFAULT_FS_RUNTIME_VERSION

    return _normalize_text(_runtime_env.get("FS_VERSION")) or DEFAULT_FS_RUNTIME_VERSION


def _platform_compatibility_error(descriptor: dict[str, Any], *, platform_version: str) -> str:
    runtime_block = descriptor.get("runtime") if isinstance(descriptor, dict) else {}
    compatibility = runtime_block.get("compatibility") if isinstance(runtime_block, dict) else {}
    if not isinstance(compatibility, dict):
        compatibility = {}
    try:
        normalized_platform = normalize_semver(platform_version, "platform_version")
        platform_min = normalize_semver(
            compatibility.get("platform_min") or "1.0.0", "runtime.compatibility.platform_min"
        )
        if (
            compare_semver(
                normalized_platform,
                platform_min,
                field_name="runtime.compatibility.platform_min",
            )
            < 0
        ):
            return (
                f"Platform version '{normalized_platform}' is below required minimum "
                f"'{platform_min}'"
            )
        platform_max = _normalize_text(compatibility.get("platform_max"))
        if platform_max:
            normalized_max = normalize_semver(platform_max, "runtime.compatibility.platform_max")
            if (
                compare_semver(
                    normalized_platform,
                    normalized_max,
                    field_name="runtime.compatibility.platform_max",
                )
                > 0
            ):
                return (
                    f"Platform version '{normalized_platform}' exceeds supported maximum "
                    f"'{normalized_max}'"
                )
    except Exception as exc:
        return f"Invalid runtime compatibility metadata: {exc}"
    return ""


def _validate_install_version_transition(
    *,
    extension_id: str,
    current_version: str,
    candidate_version: str,
) -> tuple[str, str]:
    normalized_current = normalize_semver(current_version, "current_version")
    normalized_candidate = normalize_semver(candidate_version, "candidate_version")
    comparison = compare_semver(
        normalized_candidate,
        normalized_current,
        field_name=f"extension '{extension_id}' version",
    )
    if comparison == 0:
        raise ServiceValidationError(
            f"Extension '{extension_id}' version '{normalized_candidate}' is already installed"
        )
    if comparison < 0:
        raise ServiceValidationError(
            f"Downgrade not supported for extension '{extension_id}': {normalized_current} -> {normalized_candidate}"
        )
    return normalized_current, normalized_candidate


def _safe_remote_http_url(url: str, *, purpose: str) -> str:
    from core.application.extensions.url_safety import assert_safe_remote_http_url

    return assert_safe_remote_http_url(url, purpose=purpose)


def validate_remote_url(url: str, *, purpose: str = "remote_url") -> str:
    """Validate that *url* is safe for outbound HTTP requests.

    Raises ``ValueError`` if the URL targets a private / loopback / SSRF-capable
    address.  Returns the normalised URL string on success.

    This is the public equivalent of the internal ``_safe_remote_http_url``
    helper, exposed so that security tests and external callers can verify the
    SSRF guard without importing private extension internals.
    """
    return _safe_remote_http_url(url, purpose=purpose)


def _sign_runtime_token(payload: dict[str, Any], *, secret: str) -> str:
    header = {"alg": "HS256", "typ": "JWT"}
    header_b64 = _b64url(
        json.dumps(header, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    )
    payload_b64 = _b64url(
        json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    )
    signing_input = f"{header_b64}.{payload_b64}".encode()
    signature = hmac.new(secret.encode("utf-8"), signing_input, hashlib.sha256).digest()
    return f"{header_b64}.{payload_b64}.{_b64url(signature)}"


def _artifact_kind_from_section(section: str) -> str:
    mapping = {
        "workflows": "workflow",
        "agents": "agent",
        "skills": "skill",
        "tools": "tool",
        "datasets": "dataset",
        "saved_queries": "saved_query",
        "data_contracts": "data_contract",
        "guardrails": "guardrail",
        "extensions": "extension",
    }
    return mapping.get(section, section.rstrip("s"))


def _descriptor_validation_status_code(result: ExtensionDescriptorValidateResultView) -> int:
    for error in result.errors or []:
        code = _normalize_text((error or {}).get("code")).lower()
        if code == "descriptor_file_too_large":
            return 413
    return 400


def _artifact_id_from_entry(entry: dict[str, Any]) -> str:
    for field in (
        "workflow_id",
        "agent_id",
        "skill_id",
        "tool_id",
        "contract_id",
        "data_contract_id",
        "dataset_id",
        "query_template_id",
        "saved_query_id",
        "guardrail_id",
        "extension_id",
        "id",
    ):
        candidate = _normalize_text(entry.get(field))
        if candidate:
            return candidate
    return ""


def _descriptor_sha256(descriptor: dict[str, Any]) -> str:
    canonical = json.dumps(descriptor, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _deterministic_release_id(
    *,
    extension_id: str,
    release_version: str,
    source_kind: str,
    source_ref: str,
    descriptor_sha256: str,
) -> str:
    digest = hashlib.sha256(
        (
            f"{_normalize_text(extension_id)}|{_normalize_text(release_version)}|"
            f"{_normalize_text(source_kind)}|{_normalize_text(source_ref)}|"
            f"{_normalize_text(descriptor_sha256).lower()}"
        ).encode()
    ).hexdigest()
    return f"pr_{digest[:24]}"


def _safe_iso_from_timestamp(value: float | int | None) -> str | None:
    if value is None:
        return None
    try:
        return datetime.fromtimestamp(float(value), tz=UTC).isoformat()
    except Exception:
        return None


def _http_probe(url: str, *, timeout_seconds: float = 8.0) -> tuple[bool, str]:
    try:
        safe_url = _safe_remote_http_url(url, purpose="runtime probe URL")
    except Exception as exc:
        return False, str(exc)
    request = Request(safe_url, method="GET", headers={"Accept": "application/json, text/plain"})
    try:
        from core.application.extensions.url_safety import open_pinned_url

        with open_pinned_url(  # nosec B310
            request, timeout_seconds=timeout_seconds, purpose="runtime probe URL"
        ) as response:
            status = int(getattr(response, "status", 200) or 200)
        if 200 <= status < 300:
            return True, ""
        return False, f"HTTP {status}"
    except HTTPError as exc:
        if 300 <= int(exc.code) < 400:
            location = str(exc.headers.get("Location") or "").strip()
            if location:
                try:
                    _safe_remote_http_url(
                        urljoin(safe_url, location),
                        purpose="runtime probe redirect URL",
                    )
                except Exception as redirect_exc:
                    return False, str(redirect_exc)
            return False, "HTTP redirect not allowed"
        return False, f"HTTP {exc.code}"
    except URLError as exc:
        return False, f"Network error: {exc.reason}"
    except Exception as exc:
        return False, f"Request error: {exc}"


def _list_extension_runtime_connections(
    bl: Any,
    *,
    account_id: str,
    install_id: str,
    connection_id: str = "",
    project_id: str | None = None,
    enabled_only: bool = True,
) -> list[dict[str, Any]]:
    if not bl or not hasattr(bl, "cursor"):
        return []
    return extension_runtime_connection_store.list_extension_runtime_connections(
        bl,
        account_id=account_id,
        install_id=install_id,
        connection_id=connection_id,
        project_id=project_id,
        enabled_only=enabled_only,
    )


def _list_extension_runtime_connection_counts(
    bl: Any,
    *,
    account_id: str,
    install_ids: list[str],
    enabled_only: bool = True,
) -> dict[str, int]:
    if not bl or not hasattr(bl, "cursor"):
        return {}
    try:
        return extension_runtime_connection_store.list_extension_runtime_connection_counts(
            bl,
            account_id=account_id,
            install_ids=install_ids,
            enabled_only=enabled_only,
        )
    except Exception as exc:
        logger.warning("extension runtime connection count lookup failed: %s", exc)
        return {}


def _get_default_extension_install_store(
    get_logger: Callable[[], Any] | None = None,
) -> ExtensionInstallStore | DBExtensionInstallStore:
    global _default_extension_install_store

    if get_logger is not None:
        try:
            bl = get_logger()
            if bl is not None:
                if not hasattr(bl, "conn") or bl.conn is None:
                    raise ServiceUnavailableError("Extension install store is unavailable")
                connection = bl.conn
                if (
                    not isinstance(_default_extension_install_store, DBExtensionInstallStore)
                    or _default_extension_install_store._conn is not connection
                ):
                    _default_extension_install_store = DBExtensionInstallStore(connection)
                return _default_extension_install_store
        except Exception as exc:
            raise ServiceUnavailableError("Extension install store is unavailable") from exc
    if isinstance(_default_extension_install_store, DBExtensionInstallStore):
        return _default_extension_install_store
    if _default_extension_install_store is None:
        _default_extension_install_store = ExtensionInstallStore()
    return _default_extension_install_store


def get_default_extension_install_store(
    *, get_logger: Callable[[], Any] | None = None
) -> ExtensionInstallStore | DBExtensionInstallStore:
    return _get_default_extension_install_store(get_logger=get_logger)


def _get_default_extension_runtime_store(
    get_logger: Callable[[], Any] | None = None,
) -> ExtensionRuntimeStore | DBExtensionRuntimeStore:
    global _default_extension_runtime_store

    if get_logger is not None:
        try:
            bl = get_logger()
            if bl is not None:
                if not hasattr(bl, "conn") or bl.conn is None:
                    raise ServiceUnavailableError("Extension runtime store is unavailable")
                connection = bl.conn
                if (
                    not isinstance(_default_extension_runtime_store, DBExtensionRuntimeStore)
                    or _default_extension_runtime_store._conn is not connection
                ):
                    _default_extension_runtime_store = DBExtensionRuntimeStore(connection)
                return _default_extension_runtime_store
        except Exception as exc:
            raise ServiceUnavailableError("Extension runtime store is unavailable") from exc
    if isinstance(_default_extension_runtime_store, DBExtensionRuntimeStore):
        return _default_extension_runtime_store
    if _default_extension_runtime_store is None:
        _default_extension_runtime_store = ExtensionRuntimeStore()
    return _default_extension_runtime_store


def get_default_extension_runtime_store(
    *, get_logger: Callable[[], Any] | None = None
) -> ExtensionRuntimeStore | DBExtensionRuntimeStore:
    return _get_default_extension_runtime_store(get_logger=get_logger)
