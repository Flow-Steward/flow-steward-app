"""Resolve explicitly configured account-scoped embedding credentials."""

from __future__ import annotations

from typing import Any

from core.application.settings.embedding_repository import get_embedding_provider
from core.application.settings.model_catalog import is_custom_provider_key


def resolve_embedding_openai_api_key(*, account_id: str, conn: Any) -> str | None:
    """Resolve the selected provider's encrypted key, never an ambient fallback."""
    normalized_account_id = str(account_id or "").strip()
    if not normalized_account_id or conn is None:
        return None
    try:
        provider = get_embedding_provider(account_id=normalized_account_id, conn=conn)
        if provider is None or not is_custom_provider_key(provider):
            return None
        provider_id = provider.removeprefix("custom:").strip()
        if not provider_id:
            return None
        from core.application.orchestration.model_provider import _resolve_named_provider

        named_provider = _resolve_named_provider(provider_id, account_id=normalized_account_id)
        if str(named_provider.get("provider_type") or "").strip().lower() != "openai":
            return None
        return str(named_provider.get("_decrypted_api_key") or "").strip() or None
    except Exception:
        return None
