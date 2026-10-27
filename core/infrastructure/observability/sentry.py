from __future__ import annotations

import logging
import threading
from typing import Any

from core.infrastructure.config.runtime_env import runtime_env as _runtime_env

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_initialized = False
_missing_config_logged = False


def _parse_float(value: Any, default: float = 0.0) -> float:
    try:
        parsed = float(value)
    except Exception:
        return default
    if parsed < 0:
        return default
    return parsed


def _parse_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    return default


def _resolve_dsn(config: dict[str, Any] | None = None) -> str:
    cfg = config or {}
    return str(cfg.get("sentry_dsn") or _runtime_env.get("SENTRY_DSN") or "").strip()


def init_sentry(
    *,
    config: dict[str, Any] | None = None,
    component: str = "app",
    framework: str = "generic",
) -> bool:
    """Initialize Sentry safely (never raises).

    Returns True when Sentry is enabled in this process, else False.
    """
    global _initialized, _missing_config_logged

    with _lock:
        if _initialized:
            return True

        dsn = _resolve_dsn(config)
        if not dsn:
            if not _missing_config_logged:
                logger.info("Sentry disabled: no SENTRY_DSN configured")
                _missing_config_logged = True
            return False

        try:
            import sentry_sdk
            from sentry_sdk.integrations.logging import LoggingIntegration
        except Exception as exc:
            logger.warning("Sentry disabled: sentry_sdk import failed (non-fatal): %s", exc)
            return False

        cfg = config or {}
        traces_sample_rate = _parse_float(
            cfg.get("sentry_traces_sample_rate")
            or _runtime_env.get("SENTRY_TRACES_SAMPLE_RATE", "0.0"),
            default=0.0,
        )
        profiles_sample_rate = _parse_float(
            cfg.get("sentry_profiles_sample_rate")
            or _runtime_env.get("SENTRY_PROFILES_SAMPLE_RATE", "0.0"),
            default=0.0,
        )
        environment = (
            str(
                cfg.get("sentry_environment")
                or _runtime_env.get("SENTRY_ENVIRONMENT")
                or _runtime_env.get("ENVIRONMENT")
                or ""
            ).strip()
            or None
        )
        release = str(_runtime_env.get("SENTRY_RELEASE", "")).strip() or None
        send_default_pii = _parse_bool(_runtime_env.get("SENTRY_SEND_DEFAULT_PII"), default=False)
        auto_enabling_integrations = _parse_bool(
            cfg.get("sentry_auto_enabling_integrations")
            or _runtime_env.get("SENTRY_AUTO_ENABLING_INTEGRATIONS", "0"),
            default=False,
        )

        integrations: list[Any] = [
            LoggingIntegration(level=logging.INFO, event_level=logging.ERROR),
        ]

        if framework == "fastapi":
            try:
                from sentry_sdk.integrations.fastapi import FastApiIntegration
                from sentry_sdk.integrations.starlette import StarletteIntegration

                integrations.append(FastApiIntegration())
                integrations.append(StarletteIntegration())
            except Exception as exc:
                logger.debug("Sentry FastAPI/Starlette integrations unavailable: %s", exc)

        init_kwargs: dict[str, Any] = {
            "dsn": dsn,
            "environment": environment,
            "release": release,
            "integrations": integrations,
            "traces_sample_rate": traces_sample_rate,
            "profiles_sample_rate": profiles_sample_rate,
            "send_default_pii": send_default_pii,
            "auto_enabling_integrations": auto_enabling_integrations,
        }
        try:
            try:
                sentry_sdk.init(**init_kwargs)
            except TypeError as exc:
                # Retry without auto_enabling_integrations when the installed
                # sentry-sdk does not support that kwarg.
                if "auto_enabling_integrations" not in str(exc):
                    raise
                init_kwargs.pop("auto_enabling_integrations", None)
                sentry_sdk.init(**init_kwargs)
            sentry_sdk.set_tag("component", component)
            _initialized = True
            logger.info("Sentry initialized (component=%s)", component)
            return True
        except Exception as exc:
            logger.warning("Sentry initialization failed (non-fatal): %s", exc)
            return False
