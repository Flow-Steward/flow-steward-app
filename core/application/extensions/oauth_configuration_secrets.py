"""Account-scoped secret references for extension-owned OAuth configurations."""

from __future__ import annotations

from typing import Any

from core.application.auth.secrets import encrypt_secret
from core.infrastructure.db import model_provider_store


def configuration_secret_ref_key(configuration_id: str, field_id: str) -> str:
    normalized_configuration_id = str(configuration_id or "").strip()
    normalized_field_id = str(field_id or "").strip()
    if not normalized_configuration_id:
        raise ValueError("configuration_id is required")
    if not normalized_field_id:
        raise ValueError("field_id is required")
    return f"extension_oauth_configuration:{normalized_configuration_id}:{normalized_field_id}"


class DatabaseOAuthConfigurationSecretStore:
    """Persist encrypted values without ever returning their contents."""

    def __init__(self, conn: Any) -> None:
        self._conn = conn

    def present(self, *, account_id: str, ref_key: str) -> bool:
        return (
            model_provider_store.get_secret_ref_ciphertext(
                self._conn,
                account_id=str(account_id or "").strip(),
                ref_key=str(ref_key or "").strip(),
            )
            is not None
        )

    def write(self, *, account_id: str, ref_key: str, value: str) -> None:
        model_provider_store.upsert_secret_ref(
            self._conn,
            account_id=str(account_id or "").strip(),
            ref_key=str(ref_key or "").strip(),
            encrypted_value=encrypt_secret(value),
        )

    def delete(self, *, account_id: str, ref_key: str) -> None:
        model_provider_store.delete_secret_ref(
            self._conn,
            account_id=str(account_id or "").strip(),
            ref_key=str(ref_key or "").strip(),
        )


__all__ = ["DatabaseOAuthConfigurationSecretStore", "configuration_secret_ref_key"]
