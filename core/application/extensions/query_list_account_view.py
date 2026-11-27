"""How the installation's extensions look to another account (ADR 0020, C6).

The installation account owns its installs. An administrator of another account
sees them too and enables, per account, the ones that account uses; the extension
list shows each such install with that account's enablement.
"""

from __future__ import annotations

from typing import Any

from core.application.auth.installation_admin import (
    INSTALLATION_ACCOUNT_ID,
    is_installation_account,
)
from core.application.auth.principal import principal_type_has_minimum_role
from core.application.management.query_models import ExtensionSummaryView
from core.application.management.scope import ScopeContext

from .account_enablement import (
    ENABLED,
    NOT_ENABLED,
    DBExtensionAccountEnablementStore,
    InMemoryExtensionAccountEnablementStore,
)
from .query_ports import ExtensionInstallStorePort
from .service_support import _normalize_text


def with_account_enablement(
    store: ExtensionInstallStorePort,
    records: list[Any],
    scope: ScopeContext,
) -> tuple[list[Any], dict[str, str]]:
    """Add the installation's installs an account administrator may enable (C6).

    Returns the rows and, for every installation-owned row another account sees,
    that account's enablement: ``enabled``, ``disabled`` or ``not_enabled``. The
    installation account owns its installs and gets no entry.
    """
    account_id = _normalize_text(scope.account_id)
    if not account_id or is_installation_account(account_id):
        return records, {}
    enablements = getattr(store, "account_enablements", None)
    if not isinstance(
        enablements,
        (DBExtensionAccountEnablementStore, InMemoryExtensionAccountEnablementStore),
    ):
        return records, {}
    rows = {row.extension_id: row for row in enablements.list_for_account(account_id)}
    visible = list(records)
    known = {_normalize_text(row.extension_id) for row in visible}
    if principal_type_has_minimum_role(_normalize_text(scope.principal_type), "admin"):
        for install in store.list_for_account(
            INSTALLATION_ACCOUNT_ID, include_global=False, include_inactive_owned=False
        ):
            if _normalize_text(install.extension_id) not in known:
                visible.append(install)
                known.add(_normalize_text(install.extension_id))
    result: dict[str, str] = {}
    for install in visible:
        if _normalize_text(getattr(install, "account_id", "")) != INSTALLATION_ACCOUNT_ID:
            continue
        row = rows.get(_normalize_text(install.extension_id))
        if row is None or row.install_id != _normalize_text(install.install_id):
            result[_normalize_text(install.extension_id)] = NOT_ENABLED
        else:
            result[_normalize_text(install.extension_id)] = row.state
    return visible, result


def with_account_view(
    summary: ExtensionSummaryView,
    *,
    record: Any,
    enablement: str,
    connected_count: int,
) -> ExtensionSummaryView:
    """How an installation-owned extension looks to another account (C6)."""
    if not enablement:
        return summary
    enabled = enablement == ENABLED
    return summary.model_copy(
        update={
            "account_enablement": enablement,
            "state": "active" if enabled else "disabled",
            # The account's connections name the install it enabled.
            "install_id": _normalize_text(getattr(record, "install_id", "")) if enabled else "",
            "connected": enabled and int(connected_count or 0) > 0,
        }
    )


__all__ = ["with_account_enablement", "with_account_view"]
