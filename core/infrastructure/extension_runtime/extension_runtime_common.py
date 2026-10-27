# ruff: noqa: RUF022
# mypy: ignore-errors
"""Extension runtime helpers: discovery, compile/validate lifecycle, and subprocess invocation."""

from __future__ import annotations

import fnmatch
import hashlib
import json
import logging
import os
import re
import shlex
import subprocess
import threading
import time
from datetime import UTC
from pathlib import Path
from typing import Any, ClassVar
from uuid import UUID

from core.application.extensions.host_contract import (
    HOST_CONTRACT_VERSION as HOST_CONTRACT_VERSION,
)
from core.application.extensions.identity import extension_folder_name, normalize_extension_id
from core.application.extensions.semver import compare_semver, normalize_semver
from core.application.management.scope import ScopeContext, normalize_scope
from core.application.orchestration.models import build_event_envelope_v1
from core.infrastructure.config.runtime_env import runtime_env as _runtime_env
from core.infrastructure.db.ingress_runtime import (
    TASK_INGRESS_ERROR_CODES,
    TASK_INGRESS_REQUIRED_ACTION_ERROR_CODES,
    TASK_INGRESS_REQUIRED_QUERY_ERROR_CODES,
)

_log = logging.getLogger(__name__)

QUERY_ID_REQUIRED_MESSAGE = "query_id is required"
ACTION_ID_REQUIRED_MESSAGE = "action_id is required"
EXTENSION_INSTALL_METADATA_FILENAME = ".fs-extension-install.yaml"

# ---------------------------------------------------------------------------
# S1 — Subprocess environment allow-list (isolation boundary)
# ---------------------------------------------------------------------------

_ENV_ALLOWLIST: set[str] = {
    "PATH",
    "HOME",
    "USER",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "TMPDIR",
    "TMP",
    "TEMP",
    "PYTHONDONTWRITEBYTECODE",
    "PYTHONUNBUFFERED",
    "NODE_PATH",
    "NODE_ENV",
    "FS_ALLOW_PRIVATE_REMOTE_URLS",
    "FS_EXTENSION_ARTIFACT_MAX_BYTES",
    "FS_EXTENSION_OPERATION_TIMEOUT_SECONDS",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
    # NOTE: platform secrets (database DSNs FS_DB_DSN / DATABASE_URL / POSTGRES_DSN
    # / FS_OBSERVABILITY_DSN and the signing key FS_SECRET_KEY) are intentionally
    # NOT allow-listed. Extension subprocesses are a lower-trust boundary and must
    # not inherit the host's DB credentials or token-signing secret. Extensions
    # receive only explicit per-extension credentials, never platform secrets.
    #
    # FS_ALLOW_PRIVATE_REMOTE_URLS is intentionally allow-listed: it is the
    # storage-network policy bit required for local/internal object storage
    # presigned URLs such as bundled SeaweedFS/MinIO endpoints.
    # SSL_CERT_FILE / SSL_CERT_DIR are the standard process-level trust-store
    # controls. They carry public CA material paths rather than credentials and
    # are forwarded explicitly so a sandboxed extension uses the same
    # operator-approved trust roots as its host process. No other host TLS or
    # proxy environment is inherited.
    #
    # PYTHONPATH is intentionally not inherited. Extension subprocesses receive a
    # platform-constructed PYTHONPATH: extension site dir first, then the public
    # Flow Steward extension SDK export path.
}


def _sandboxed_env(extension: dict[str, Any] | None = None) -> dict[str, str]:
    """Return a filtered copy of os.environ containing only allow-listed keys."""
    env = {k: v for k, v in _runtime_env.items() if k in _ENV_ALLOWLIST}
    from core.infrastructure.extension_runtime.python_dependencies import (
        extension_runtime_pythonpath,
    )

    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONPATH"] = extension_runtime_pythonpath(extension)
    from core.infrastructure.security.artifact_grant_signing import (
        artifact_grant_verification_key,
    )

    verification_key = artifact_grant_verification_key()
    if verification_key:
        env["FS_ARTIFACT_GRANT_VERIFY_KEY"] = verification_key
    return env


_SUBPROCESS_SKIP = object()


def _json_safe_subprocess_value(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, UUID):
        return str(value)
    isoformat = getattr(value, "isoformat", None)
    if callable(isoformat):
        try:
            return isoformat()
        except Exception:
            return _SUBPROCESS_SKIP
    if isinstance(value, dict):
        normalized: dict[str, Any] = {}
        for raw_key, raw_value in value.items():
            safe_value = _json_safe_subprocess_value(raw_value)
            if safe_value is _SUBPROCESS_SKIP:
                continue
            normalized[str(raw_key)] = safe_value
        return normalized
    if isinstance(value, (list, tuple, set)):
        normalized_items: list[Any] = []
        for item in value:
            safe_value = _json_safe_subprocess_value(item)
            if safe_value is _SUBPROCESS_SKIP:
                continue
            normalized_items.append(safe_value)
        return normalized_items
    return str(value)


def _subprocess_runtime_context(runtime_context: dict | None = None) -> dict[str, Any]:
    """The runtime context the extension subprocess reads, with the final scope.

    The four execution-scope keys are always present (a call outside a run has
    them all null); a step context without its owner keys is refused here.
    """
    from core.application.extensions.host_contract import normalize_execution_scope

    context = runtime_context if isinstance(runtime_context, dict) else {}
    serialized: dict[str, Any] = {}
    for key, value in context.items():
        if key in {"logger", "logger_dsn", "observability_dsn"}:
            continue
        safe_value = _json_safe_subprocess_value(value)
        if safe_value is _SUBPROCESS_SKIP:
            continue
        serialized[str(key)] = safe_value
    serialized.update(normalize_execution_scope(context))
    return serialized


# ---------------------------------------------------------------------------
# S2 — Circuit breaker / quarantine state
# ---------------------------------------------------------------------------


class ExtensionCircuitBreaker:
    """Per-extension failure tracker with quarantine support.

    After *failure_threshold* consecutive failures the extension is quarantined
    for *quarantine_seconds*.  While quarantined, invocations are rejected
    immediately without spawning a process.

    Thread-safe — guarded by a lock per breaker instance plus a module lock
    for the registry.
    """

    _registry_lock = threading.Lock()
    _registry: ClassVar[dict[tuple[str, str, str], ExtensionCircuitBreaker]] = {}

    def __init__(
        self,
        extension_key: str,
        *,
        account_id: str = "",
        extension_install_id: str = "",
        failure_threshold: int = 5,
        quarantine_seconds: float = 120.0,
        max_concurrent: int = 8,
    ) -> None:
        self.extension_key = extension_key
        self.account_id = str(account_id or "").strip()
        self.extension_install_id = str(extension_install_id or "").strip()
        self.failure_threshold = max(1, failure_threshold)
        self.quarantine_seconds = max(1.0, quarantine_seconds)
        self.max_concurrent = max(1, max_concurrent)

        self._lock = threading.Lock()
        self._consecutive_failures = 0
        self._quarantined_until: float = 0.0
        self._active_count = 0

    # -- public API --

    @classmethod
    def for_extension(
        cls,
        extension_key: str,
        *,
        account_id: str = "",
        extension_install_id: str = "",
    ) -> ExtensionCircuitBreaker:
        normalized_key = str(extension_key or "").strip()
        normalized_account_id = str(account_id or "").strip()
        normalized_install_id = str(extension_install_id or "").strip()
        registry_key = (normalized_account_id, normalized_install_id, normalized_key)
        with cls._registry_lock:
            if registry_key not in cls._registry:
                cls._registry[registry_key] = cls(
                    normalized_key,
                    account_id=normalized_account_id,
                    extension_install_id=normalized_install_id,
                )
            return cls._registry[registry_key]

    @classmethod
    def reset_all(cls) -> None:
        """Test helper — clear all breaker state."""
        with cls._registry_lock:
            cls._registry.clear()

    @property
    def is_quarantined(self) -> bool:
        with self._lock:
            return time.monotonic() < self._quarantined_until

    @property
    def consecutive_failures(self) -> int:
        with self._lock:
            return self._consecutive_failures

    @property
    def active_count(self) -> int:
        with self._lock:
            return self._active_count

    def pre_invoke(self) -> str | None:
        """Check if invocation is allowed.  Returns None on success, error string on rejection."""
        with self._lock:
            now = time.monotonic()
            if now < self._quarantined_until:
                remaining = self._quarantined_until - now
                return (
                    f"Extension '{self.extension_key}' is quarantined after "
                    f"{self.failure_threshold} consecutive failures "
                    f"({remaining:.0f}s remaining)"
                )
            if self._active_count >= self.max_concurrent:
                return (
                    f"Extension '{self.extension_key}' concurrency limit reached "
                    f"({self.max_concurrent})"
                )
            self._active_count += 1
            return None

    def post_invoke(self, *, succeeded: bool | None) -> None:
        """Record a runtime outcome and potentially quarantine.

        ``None`` represents a well-formed extension-domain error. It releases
        the invocation slot without treating invalid user input as a subprocess
        failure or resetting an existing runtime-failure streak.
        """
        with self._lock:
            self._active_count = max(0, self._active_count - 1)
            if succeeded is None:
                return
            if succeeded:
                self._consecutive_failures = 0
            else:
                self._consecutive_failures += 1
                if self._consecutive_failures >= self.failure_threshold:
                    self._quarantined_until = time.monotonic() + self.quarantine_seconds
                    _log.warning(
                        "Extension '%s' quarantined for %.0fs after %d consecutive failures",
                        self.extension_key,
                        self.quarantine_seconds,
                        self._consecutive_failures,
                    )

    def status(self) -> dict:
        with self._lock:
            now = time.monotonic()
            quarantined = now < self._quarantined_until
            return {
                "extension_key": self.extension_key,
                "account_id": self.account_id,
                "extension_install_id": self.extension_install_id,
                "consecutive_failures": self._consecutive_failures,
                "quarantined": quarantined,
                "quarantine_remaining_seconds": max(0.0, self._quarantined_until - now)
                if quarantined
                else 0.0,
                "active_invocations": self._active_count,
                "failure_threshold": self.failure_threshold,
                "max_concurrent": self.max_concurrent,
            }


EXTENSION_KINDS: tuple[str, ...] = (
    "task_provider",
    "notification_provider",
    "tool_provider",
)

TASK_INGRESS_FEATURE_ID = "task_ingress"
TASK_INGRESS_REQUIRED_ACTION_IDS: tuple[str, ...] = (
    "normalize_task_action",
    "fetch_attachments",
    "sync_completion_status",
)
TASK_INGRESS_REQUIRED_QUERY_IDS: tuple[str, ...] = ("provider_health",)
TASK_INGRESS_REQUIRED_ACTION_RESULT_FIELDS: dict[str, tuple[str, ...]] = {
    "normalize_task_action": ("result.normalized",),
    "fetch_attachments": ("result.attachments",),
    "sync_completion_status": ("result.status",),
}
TASK_INGRESS_REQUIRED_QUERY_RESULT_FIELDS: dict[str, tuple[str, ...]] = {
    "provider_health": ("result.status", "result.configured"),
}

NOTIFICATION_PROVIDER_REQUIRED_FEATURES: tuple[str, ...] = ("notify",)
TOOL_PROVIDER_REQUIRED_FEATURES: tuple[str, ...] = ("tool",)
INGRESS_EVENT_FEATURE_ID = "ingress_event"
INGRESS_EVENT_REQUIRED_ACTION_ID = "ingress_event"
INGRESS_EVENT_REQUIRED_EVENT_TYPES: tuple[str, ...] = ("external.comment.received",)
INGRESS_EVENT_RECOMMENDED_EVENT_TYPES: tuple[str, ...] = ("notification.interaction_received",)
INGRESS_EVENT_REQUIRED_ACTION_RESULT_FIELDS: tuple[str, ...] = ("result.ingress_type",)

MANIFEST_FILENAMES = ("extension.yaml",)
FORBIDDEN_EXTENSION_CONTRACT_JSON_PATTERNS = (
    "extension.json",
    "manifest.json",
    "ui/ui_manifest.json",
    "ui/actions/actions.json",
    "ui/queries/queries.json",
    "ui/pages/*.json",
    "ui/components/*.json",
    "schemas/*.schema.json",
    "locales/*.json",
)

ALLOWED_COMPONENT_TYPES: set[str] = {
    "section",
    "columns",
    "stack",
    "action_bar",
    "form",
    "fieldset",
    "repeater",
    "key_value",
    "json_editor",
    "secret_field",
    "grid",
    "stats_row",
    "key_value_card",
    "markdown",
    "health_panel",
    "log_list",
    "empty_state",
    "alert",
    "confirm_action",
    "connection_form",
    "state_editor",
    "drawer_detail",
    "status_badge",
    "filter_bar",
    "callback_setup",
    "external_resource_picker",
}
ALLOWED_FORM_WIDGET_TYPES: set[str] = {
    "text",
    "textarea",
    "url",
    "email",
    "number",
    "integer",
    "boolean",
    "select",
    "radio_group",
    "checkbox_group",
    "tag_picker",
    "secret",
    "json",
    "key_value",
    "repeater",
    "event_selector",
    "project_selector",
    "workflow_selector",
    "provider_selector",
}
ALLOWED_GRID_COLUMN_TYPES: set[str] = {
    "text",
    "datetime",
    "date",
    "number",
    "badge",
    "boolean",
    "json_summary",
    "link",
    "id",
}
QUERY_REFERENCE_KEYS = {"query_id", "load_query"}
ACTION_REFERENCE_KEYS = {
    "action_id",
    "save_action",
    "validate_action",
    "test_action",
    "link_action",
    "bootstrap_action_id",
}
GRID_ACTION_SCOPES = {"row", "bulk", "page"}
ALLOWED_OPERATION_HANDLER_MODES = {"extension", "platform"}
ALLOWED_SUBSCRIPTION_SEVERITIES = {"any", "debug", "info", "warning", "error", "critical"}
ALLOWED_CALLBACK_METHODS = {"GET", "POST"}
ALLOWED_CALLBACK_AUTH_TYPES = {"none", "shared_secret_header", "hmac_sha256"}
CALLBACK_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
AUTHORING_MANIFEST_ALLOWED_FIELDS: set[str] = {
    "account_scope",
    "action_manifest",
    "artifact_policies",
    "author",
    "billing_mode",
    "callbacks",
    "dependencies",
    "description",
    "display_name",
    "entrypoint",
    "event_subscriptions",
    "extension_id",
    "egress",
    "external_effects",
    "features",
    "health",
    "issues_url",
    "kind",
    "listing",
    "lifecycle",
    "machine_auth",
    "manifest_version",
    "package",
    "project_state",
    "release_stage",
    "python_requirements",
    "query_manifest",
    "required_scopes",
    "runtime",
    "runtime_grants",
    "schemas",
    "screenshots",
    "support_url",
    "supported_url_schemes",
    "ui_manifest",
    "upgrade_notes",
    "vendor",
    "vendor_name",
    "version",
    "website_url",
    "workflow_triggers",
}
COMPILED_MANIFEST_ALLOWED_FIELDS: set[str] = {
    "account_scope",
    "action_definitions",
    "action_manifest_ref",
    "artifact_policies",
    "author",
    "billing_mode",
    "bundle_root",
    "bundle_dir_name",
    "callbacks",
    "compile_errors",
    "compile_status",
    "compile_warnings",
    "connection_types",
    "description",
    "display_name",
    "entrypoint",
    "event_subscriptions",
    "extension_id",
    "external_effects",
    "dependencies",
    "expected_bundle_dir_name",
    "features",
    "healthcheck",
    "kind",
    "issues_url",
    "listing",
    "lifecycle",
    "locale_bundles",
    "locale_codes",
    "machine_auth",
    "manifest_path",
    "manifest_version",
    "oauth_providers",
    "operation_manifest",
    "python_requirements",
    "package",
    "project_state",
    "release_stage",
    "query_definitions",
    "query_manifest_ref",
    "required_scopes",
    "runtime",
    "runtime_grants",
    "schemas",
    "screenshots",
    "step_ui_manifest",
    "support_url",
    "supported_url_schemes",
    "supported_events",
    "ui_manifest",
    "ui_manifest_ref",
    "upgrade_notes",
    "validation_errors",
    "validation_status",
    "validation_warnings",
    "version",
    "vendor",
    "vendor_name",
    "website_url",
    "workflow_triggers",
}
COMPONENT_REF_OBJECT_FIELDS: dict[str, tuple[str, str]] = {
    "schema_ref": ("schema", "invalid_component_schema_ref"),
    "ui_schema_ref": ("ui_schema", "invalid_ui_schema_ref"),
    "filters_schema_ref": ("filters_schema", "invalid_filters_schema_ref"),
}

__all__ = [
    "ACTION_ID_REQUIRED_MESSAGE",
    "ACTION_REFERENCE_KEYS",
    "ALLOWED_CALLBACK_AUTH_TYPES",
    "ALLOWED_CALLBACK_METHODS",
    "ALLOWED_COMPONENT_TYPES",
    "ALLOWED_FORM_WIDGET_TYPES",
    "ALLOWED_GRID_COLUMN_TYPES",
    "ALLOWED_OPERATION_HANDLER_MODES",
    "ALLOWED_SUBSCRIPTION_SEVERITIES",
    "Any",
    "AUTHORING_MANIFEST_ALLOWED_FIELDS",
    "CALLBACK_ID_RE",
    "COMPILED_MANIFEST_ALLOWED_FIELDS",
    "COMPONENT_REF_OBJECT_FIELDS",
    "ClassVar",
    "EXTENSION_INSTALL_METADATA_FILENAME",
    "EXTENSION_KINDS",
    "ExtensionCircuitBreaker",
    "FORBIDDEN_EXTENSION_CONTRACT_JSON_PATTERNS",
    "GRID_ACTION_SCOPES",
    "HOST_CONTRACT_VERSION",
    "INGRESS_EVENT_FEATURE_ID",
    "INGRESS_EVENT_RECOMMENDED_EVENT_TYPES",
    "INGRESS_EVENT_REQUIRED_ACTION_ID",
    "INGRESS_EVENT_REQUIRED_ACTION_RESULT_FIELDS",
    "INGRESS_EVENT_REQUIRED_EVENT_TYPES",
    "MANIFEST_FILENAMES",
    "NOTIFICATION_PROVIDER_REQUIRED_FEATURES",
    "Path",
    "QUERY_ID_REQUIRED_MESSAGE",
    "QUERY_REFERENCE_KEYS",
    "ScopeContext",
    "TASK_INGRESS_ERROR_CODES",
    "TASK_INGRESS_FEATURE_ID",
    "TASK_INGRESS_REQUIRED_ACTION_ERROR_CODES",
    "TASK_INGRESS_REQUIRED_ACTION_IDS",
    "TASK_INGRESS_REQUIRED_ACTION_RESULT_FIELDS",
    "TASK_INGRESS_REQUIRED_QUERY_ERROR_CODES",
    "TASK_INGRESS_REQUIRED_QUERY_IDS",
    "TASK_INGRESS_REQUIRED_QUERY_RESULT_FIELDS",
    "TOOL_PROVIDER_REQUIRED_FEATURES",
    "UTC",
    "UUID",
    "_ENV_ALLOWLIST",
    "_SUBPROCESS_SKIP",
    "_json_safe_subprocess_value",
    "_log",
    "_sandboxed_env",
    "_subprocess_runtime_context",
    "annotations",
    "build_event_envelope_v1",
    "compare_semver",
    "extension_folder_name",
    "fnmatch",
    "hashlib",
    "json",
    "logging",
    "normalize_extension_id",
    "normalize_semver",
    "normalize_scope",
    "os",
    "re",
    "shlex",
    "subprocess",
    "threading",
    "time",
]
