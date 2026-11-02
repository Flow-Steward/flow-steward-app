"""PostgreSQL adapter for atomic workflow input secret-default mutations."""

from __future__ import annotations

from contextlib import AbstractContextManager
from typing import Any

from core.infrastructure.security.secret_codec import encrypt_secret


class PostgresWorkflowSecretDefaultRepository:
    def __init__(self, registry_store: Any) -> None:
        self.registry_store = registry_store

    def transaction(self) -> AbstractContextManager[Any]:
        transaction = getattr(self.registry_store, "_transaction", None)
        if not callable(transaction):
            raise RuntimeError("workflow secret transaction is unavailable")
        return transaction()

    def get_workflow(self, workflow_id: str) -> dict[str, Any] | None:
        return self.registry_store.get_workflow(workflow_id)

    def save_workflow(
        self,
        data: dict[str, Any],
        *,
        author: str,
        expected_revision: int | None,
    ) -> int:
        return int(
            self.registry_store.save_workflow(
                data,
                author=author,
                expected_revision=expected_revision,
            )
        )

    @staticmethod
    def encrypt(plaintext: str) -> str:
        return encrypt_secret(plaintext)

    @staticmethod
    def write_ciphertext(
        transaction: Any,
        *,
        account_id: str,
        ref_key: str,
        ciphertext: str,
    ) -> None:
        transaction.execute(
            """INSERT INTO secret_refs (account_id, ref_key, encrypted_value)
               VALUES (%s, %s, %s)
               ON CONFLICT (account_id, ref_key)
               DO UPDATE SET encrypted_value = EXCLUDED.encrypted_value""",
            (account_id, ref_key, ciphertext),
        )

    @staticmethod
    def delete_owner_ciphertexts(
        transaction: Any,
        *,
        account_id: str,
        owner_prefix: str,
        keep_ref_key: str = "",
    ) -> None:
        transaction.execute(
            """DELETE FROM secret_refs
                WHERE account_id = %s
                  AND left(ref_key, length(%s)) = %s
                  AND (%s = '' OR ref_key <> %s)""",
            (account_id, owner_prefix, owner_prefix, keep_ref_key, keep_ref_key),
        )

    @staticmethod
    def delete_catalog_binding(
        transaction: Any,
        *,
        account_id: str,
        workflow_id: str,
        field_name: str,
    ) -> None:
        """Remove the catalog binding of an input that left the definition (ADR 0055)."""
        transaction.execute(
            """DELETE FROM catalog_secret_bindings
                WHERE consumer_kind = 'workflow_input'
                  AND account_id = %s AND workflow_id = %s AND field_name = %s""",
            (account_id, workflow_id, field_name),
        )

    @staticmethod
    def purge_workflow_secrets(
        transaction: Any,
        *,
        account_id: str,
        owner_prefix: str,
    ) -> None:
        transaction.execute(
            """DELETE FROM secret_refs
                WHERE account_id = %s
                  AND left(ref_key, length(%s)) = %s""",
            (account_id, owner_prefix, owner_prefix),
        )


__all__ = ["PostgresWorkflowSecretDefaultRepository"]
