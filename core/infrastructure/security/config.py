"""SEC-002/018: Security scanning feature flags and configuration.

Environment variables
---------------------
SECURITY_SCAN_MODE
    ``monitor``  (default) — scan and log, never block/redact.
    ``enforce_standard`` — apply standard policy actions.
    ``enforce_strict``   — apply strict policy actions.

SECURITY_PII_SALT
    Salt used for deterministic PII pseudonymization (HMAC key derivation).

SECURITY_FAIL_CLOSED_ON_POLICY_ERROR
    ``0`` (default) — in monitor mode, policy resolution errors pass through.
    ``1`` — policy resolution errors block payloads even in monitor mode.
"""

from __future__ import annotations

import logging
import secrets

from core.infrastructure.config.runtime_env import runtime_env as _runtime_env

_config_logger = logging.getLogger(__name__)


def _deployment_environment() -> str:
    markers = (
        _runtime_env.get("FS_STACK_ENVIRONMENT"),
        _runtime_env.get("ENVIRONMENT"),
        _runtime_env.get("APP_ENV"),
    )
    normalized_values = [
        str(value or "").strip().lower().replace("-", "_")
        for value in markers
        if str(value or "").strip()
    ]
    if any(value in {"production", "prod"} for value in normalized_values):
        return "production"
    if normalized_values:
        return normalized_values[0]
    # AC17-R129: nobody said, and for a shipped image the safe reading of silence is
    # production. Every Compose file declares one of these markers, so absence means a
    # deployment started without Compose at all -- a platform, not a laptop. Reading it
    # as local_development turned scanning down to monitor and, in the auth runtime,
    # permitted the switch that disables authentication entirely.
    return "production"


def _production_environment() -> bool:
    return _deployment_environment() in {"production", "prod"}


# ---------------------------------------------------------------------------
# Scan mode
# ---------------------------------------------------------------------------
_VALID_MODES = {"monitor", "enforce_standard", "enforce_strict", "disabled"}
_DEFAULT_SCAN_MODE = "enforce_standard" if _production_environment() else "monitor"

SECURITY_SCAN_MODE: str = _runtime_env.get("SECURITY_SCAN_MODE", _DEFAULT_SCAN_MODE).lower().strip()
if SECURITY_SCAN_MODE not in _VALID_MODES:
    SECURITY_SCAN_MODE = _DEFAULT_SCAN_MODE

if SECURITY_SCAN_MODE == "monitor":
    _config_logger.warning(
        "SECURITY_SCAN_MODE=monitor — scanning is active but blocking is disabled. "
        "Redaction is still applied to persisted data. "
        "Set SECURITY_SCAN_MODE=enforce_standard in production to enable full enforcement."
    )
elif SECURITY_SCAN_MODE == "disabled":
    _config_logger.warning(
        "SECURITY_SCAN_MODE=disabled — ALL security scanning is OFF. "
        "No secrets, injection, or PII detection will occur."
    )

_PII_SALT_PLACEHOLDERS = {"", "change-me", "default-pii-salt-change-me"}


def _resolve_pii_salt() -> str:
    configured = str(_runtime_env.get("SECURITY_PII_SALT", "") or "").strip()
    if configured.lower() not in _PII_SALT_PLACEHOLDERS:
        return configured
    if _production_environment():
        raise RuntimeError("SECURITY_PII_SALT must be set to a unique secret value in production")
    generated = secrets.token_hex(32)
    _config_logger.warning(
        "SECURITY_PII_SALT is not configured; using a process-local development salt. "
        "Set SECURITY_PII_SALT to keep PII pseudonyms stable across restarts."
    )
    return generated


SECURITY_PII_SALT: str = _resolve_pii_salt()
SECURITY_FAIL_CLOSED_ON_POLICY_ERROR: bool = _runtime_env.get(
    "SECURITY_FAIL_CLOSED_ON_POLICY_ERROR",
    "0",
).strip().lower() in {"1", "true", "yes", "on"}

if SECURITY_FAIL_CLOSED_ON_POLICY_ERROR:
    _config_logger.warning(
        "SECURITY_FAIL_CLOSED_ON_POLICY_ERROR=1 — policy parse/resolution "
        "errors will block payloads in monitor mode."
    )


def security_enabled() -> bool:
    """Return True when scanning is active (any mode except disabled)."""
    return SECURITY_SCAN_MODE != "disabled"


def enforcement_active() -> bool:
    """Return True when the system should actually block/redact (not just monitor)."""
    return SECURITY_SCAN_MODE in ("enforce_standard", "enforce_strict")


def current_mode() -> str:
    return SECURITY_SCAN_MODE


def policy_error_fail_closed() -> bool:
    """Return True when policy-resolution errors should block in monitor mode."""
    return SECURITY_FAIL_CLOSED_ON_POLICY_ERROR
