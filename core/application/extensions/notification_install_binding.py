"""Select the unique visible installed release for connectionless delivery."""

from __future__ import annotations

from typing import Any

from .install import ExtensionInstallRecord


def select_notification_install(
    connection: Any, *, extension_id: str, account_id: str
) -> ExtensionInstallRecord:
    """Ambiguity requires a connection; catalog identity is never executable."""
    from .install import DBExtensionInstallStore

    if connection is None:
        raise RuntimeError("Notification install store is not available")
    records = DBExtensionInstallStore(connection).list_for_account(
        account_id, include_global=True, include_inactive_owned=False
    )
    candidates = [
        record
        for record in records
        if record.extension_id == extension_id
        and record.state == "active"
        and (record.account_id == account_id or record.visibility == "global")
    ]
    if len(candidates) != 1:
        raise ValueError("Notification provider requires one active visible install")
    selected = candidates[0]
    if not str(selected.install_id or "").strip() or not str(selected.release_id or "").strip():
        raise ValueError("Notification provider install release identity is missing")
    return selected
