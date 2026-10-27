"""Account-scoped storage for the explicitly selected embedding provider."""

import json
from collections.abc import Mapping
from typing import Any

from core.infrastructure.db.system_settings_store import get_system_setting

_PROVIDER_SETTING_KEY = "embedding_settings"


def get_embedding_provider(*, account_id: str, conn: Any) -> str | None:
    """Read only the provider field, failing closed on invalid or unavailable storage."""
    try:
        row = get_system_setting(
            conn=conn, setting_key=_PROVIDER_SETTING_KEY, account_id=account_id
        )
        if not isinstance(row, Mapping) or str(row.get("account_id") or "") != account_id:
            return None
        settings = row.get("setting_value_jsonb")
        if isinstance(settings, str):
            settings = json.loads(settings)
        if not isinstance(settings, Mapping):
            return None
        return str(settings.get("provider") or "").strip() or None
    except Exception:
        return None
