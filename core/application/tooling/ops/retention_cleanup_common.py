from __future__ import annotations

from contextlib import suppress
from typing import Any

from core.application.auth.config import DEFAULT_ACCOUNT_ID

try:
    from core.infrastructure.db import psycopg_runtime as psycopg_runtime
except ModuleNotFoundError:  # pragma: no cover - exercised in lightweight unit envs
    psycopg_runtime = None

try:
    from core.infrastructure.db import system_settings_store
except ModuleNotFoundError:  # pragma: no cover - lightweight unit env without psycopg_runtime

    class _SystemSettingsStoreFallback:
        @staticmethod
        def normalize_account_id(account_id: Any) -> str:
            normalized = str(account_id or DEFAULT_ACCOUNT_ID).strip()
            return normalized or DEFAULT_ACCOUNT_ID

        @staticmethod
        def normalize_retention_settings(
            settings: dict[str, Any] | None,
            *,
            defaults: dict[str, int] | None = None,
        ) -> dict[str, int]:
            base = {
                "action_logs_days": 30,
                "job_logs_days": 30,
                "trash_days": 30,
                "terminal_job_checkpoints_days": 30,
                "revisions_keep": 5,
            }

            def _bounded(key: str, value: Any) -> int:
                upper = 100 if key == "revisions_keep" else 3650
                return max(1, min(upper, int(value)))

            for key, value in (defaults or {}).items():
                with suppress(TypeError, ValueError):
                    base[key] = _bounded(key, value)
            payload = settings if isinstance(settings, dict) else {}
            normalized = {}
            for key, default in base.items():
                try:
                    normalized[key] = _bounded(key, payload.get(key, default))
                except (TypeError, ValueError):
                    normalized[key] = default
            return normalized

        @staticmethod
        def get_retention_settings(
            *,
            conn,
            table_exists,
            defaults: dict[str, int] | None = None,
            account_id: Any = None,
        ) -> dict[str, Any]:
            del conn, table_exists
            payload = _SystemSettingsStoreFallback.normalize_retention_settings(
                None,
                defaults=defaults,
            )
            payload["account_id"] = _SystemSettingsStoreFallback.normalize_account_id(account_id)
            return payload

    system_settings_store = _SystemSettingsStoreFallback()

try:
    from core.infrastructure.db.platform_logger import resolve_observability_dsn
except ModuleNotFoundError:  # pragma: no cover - lightweight unit env without psycopg_runtime

    def resolve_observability_dsn(explicit_dsn: str = "") -> str:
        return str(explicit_dsn or "").strip()
