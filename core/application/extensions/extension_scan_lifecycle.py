"""Application-owned coordination for extension scan and LLM route lifecycle."""

from __future__ import annotations

from typing import Any

from core.application.auth.config import DEFAULT_ACCOUNT_ID
from core.application.settings import llm_provider_extension_repository as llm_store

from .install import DBExtensionInstallStore
from .provider_registration import (
    ProviderRegistrationError,
    provider_descriptor_for_extension,
    provider_row_for_extension,
    set_provider_enabled_for_extension,
)
from .runtime_store_db import DBExtensionRuntimeStore


def _apply_llm_lifecycle_changes(transaction_conn: Any, changes: list[dict]) -> None:
    invalid_extension_ids: set[str] = set()
    for change in changes:
        kind = str(change.get("kind") or "").strip()
        if kind == "invalid":
            extension_id = str(change.get("extension_id") or "").strip()
            if extension_id and extension_id not in invalid_extension_ids:
                llm_store.deactivate_invalid_extension_routes(
                    transaction_conn,
                    extension_id=extension_id,
                )
                invalid_extension_ids.add(extension_id)
        elif kind == "release_changed":
            llm_store.invalidate_extension_models_for_release_change(
                transaction_conn,
                account_id=str(change.get("account_id") or ""),
                extension_install_id=str(change.get("extension_install_id") or ""),
                changed_release_id=str(change.get("changed_release_id") or ""),
            )


def sync_extension_scan_with_llm_lifecycle(
    logger: Any,
    scanned_extensions: list[dict] | None = None,
) -> list[dict]:
    """Persist one scan and apply all route transitions in the same transaction."""
    with logger.extension_lifecycle_transaction() as transaction_conn:
        rows, changes = logger.sync_extension_scan_records(
            transaction_conn,
            scanned_extensions,
        )
        _apply_llm_lifecycle_changes(transaction_conn, changes)
    return rows


def sync_enabled_extension_with_llm_lifecycle(
    logger: Any,
    extension_key: str,
    *,
    version: str | None = None,
    enabled: bool = True,
    config: dict | None = None,
) -> dict | None:
    """Persist legacy enablement, provider state and descriptor drift atomically."""
    with logger.extension_lifecycle_transaction() as transaction_conn:
        result = logger.sync_enabled_extension_record(
            transaction_conn,
            extension_key,
            version=version,
            enabled=enabled,
            config=config,
        )
        if result is None:
            return None
        normalized_key, changes = result
        _apply_llm_lifecycle_changes(transaction_conn, changes)
        if not enabled:
            from .oauth_lifecycle import (
                invalidate_install_oauth_on_disable,
                resolve_install_id_for_disable,
            )

            account_id = str((config or {}).get("account_id") or DEFAULT_ACCOUNT_ID)
            install_id = resolve_install_id_for_disable(
                transaction_conn, extension_id=normalized_key, account_id=account_id
            )

            if install_id:
                invalidate_install_oauth_on_disable(
                    transaction_conn,
                    install_id=install_id,
                    extension_id=normalized_key,
                    account_id=account_id,
                )
        descriptor = provider_descriptor_for_extension(logger, extension_id=normalized_key)
        if provider_row_for_extension(descriptor) is not None:
            account_id = str((config or {}).get("account_id") or DEFAULT_ACCOUNT_ID).strip()
            install = DBExtensionInstallStore(transaction_conn).get(
                extension_id=normalized_key, account_id=account_id or DEFAULT_ACCOUNT_ID
            )
            release = (
                DBExtensionRuntimeStore(transaction_conn).get_release(install.release_id)
                if install is not None and install.release_id
                else None
            )
            row = provider_row_for_extension(release.descriptor) if release is not None else None
            if (
                release is None
                or release.extension_id != normalized_key
                or row is None
                or row["provider_key"] != normalized_key
            ):
                raise ProviderRegistrationError(
                    f"cannot update provider '{normalized_key}': selected release is unavailable"
                )
            descriptor = release.descriptor
        set_provider_enabled_for_extension(logger, descriptor, enabled=enabled)
    return logger.get_extension(normalized_key)
