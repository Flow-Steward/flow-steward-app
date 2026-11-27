from __future__ import annotations

from typing import Any

from core.application.auth.installation_admin import (
    INSTALLATION_ACCOUNT_ID,
    is_installation_account,
)
from core.application.auth.principal import principal_type_has_minimum_role
from core.application.management.errors import ServiceNotFoundError, ServiceUnavailableError
from core.application.management.query_models import ExtensionDetailView
from core.application.management.scope import ScopeContext

from .account_enablement import ENABLED, account_enablement_state
from .install import ExtensionInstallRecord
from .query_detail_projection import ExtensionDetailProjector
from .query_ports import ExtensionDetailListingPort
from .service_support import _normalize_text, _service_scope

_MESSAGE_EXTENSION_INSTALL_STORE_IS_UNAVAILABLE = "Extension install store is unavailable"


class ExtensionQueryDetail:
    """Extension detail projections."""

    def __init__(
        self, *, listings: ExtensionDetailListingPort, projector: ExtensionDetailProjector
    ) -> None:
        self._listings = listings
        self._projector = projector

    def get_extension(
        self,
        extension_id: str,
        *,
        scope: ScopeContext | None = None,
    ) -> ExtensionDetailView:
        scope = _service_scope(scope)
        record = self._visible_install(extension_id, scope=scope)
        view = self._projector.project(record=record, account_id=scope.account_id or "acc_default")
        enablement = account_enablement_state(
            record,
            _normalize_text(scope.account_id),
            getattr(self._listings.install_store(), "account_enablements", None),
        )
        if not enablement:
            return view
        # Another account sees its own enablement of the installation's install (C6).
        return view.model_copy(
            update={
                "account_enablement": enablement,
                "state": "active" if enablement == ENABLED else "disabled",
            }
        )

    def get_extension_identity(
        self,
        extension_id: str,
        *,
        scope: ScopeContext | None = None,
    ) -> str:
        """Resolve visibility without loading release, projection or connection payloads."""
        return self._visible_install(extension_id, scope=_service_scope(scope)).extension_id

    def _visible_install(
        self,
        extension_id: str,
        *,
        scope: ScopeContext,
    ) -> ExtensionInstallRecord:
        account_id = scope.account_id or "acc_default"
        try:
            store = self._listings.install_store()
            record = store.get(extension_id=extension_id, account_id=account_id)
        except ServiceUnavailableError:
            raise
        except Exception as exc:
            raise ServiceUnavailableError(_MESSAGE_EXTENSION_INSTALL_STORE_IS_UNAVAILABLE) from exc
        if not record:
            try:
                visible_records = store.list_for_account(
                    account_id, include_global=True, include_inactive_owned=True
                )
            except Exception as exc:
                raise ServiceUnavailableError(
                    _MESSAGE_EXTENSION_INSTALL_STORE_IS_UNAVAILABLE
                ) from exc
            record = next(
                (row for row in visible_records if row.extension_id == extension_id), None
            )
        if not record:
            record = self._installation_install_to_enable(store, extension_id, scope)
        if not record:
            raise ServiceNotFoundError(f"Extension '{extension_id}' not found")
        return record

    @staticmethod
    def _installation_install_to_enable(
        store: Any, extension_id: str, scope: ScopeContext
    ) -> ExtensionInstallRecord | None:
        """An active install of the installation that another account's administrator
        may enable for that account (ADR 0020, C6)."""
        account_id = _normalize_text(scope.account_id)
        if (
            not account_id
            or is_installation_account(account_id)
            or not principal_type_has_minimum_role(_normalize_text(scope.principal_type), "admin")
        ):
            return None
        enablements = getattr(store, "account_enablements", None)
        try:
            install = store.get(extension_id=extension_id, account_id=INSTALLATION_ACCOUNT_ID)
        except Exception:
            return None
        state = getattr(install, "state", "")
        state = _normalize_text(getattr(state, "value", state)).lower()
        if state != "active" or not account_enablement_state(install, account_id, enablements):
            return None
        return install

    def get_install_detail(
        self,
        install_id: str,
        *,
        scope: ScopeContext | None = None,
    ) -> ExtensionDetailView:
        scope = _service_scope(scope)
        account_id = scope.account_id or "acc_default"
        try:
            store = self._listings.install_store()
            record = store.get_by_install_id(install_id)
        except ServiceUnavailableError:
            raise
        except Exception as exc:
            raise ServiceUnavailableError(_MESSAGE_EXTENSION_INSTALL_STORE_IS_UNAVAILABLE) from exc
        if not record or _normalize_text(record.account_id) != _normalize_text(account_id):
            raise ServiceNotFoundError(f"Install '{install_id}' not found")
        return self._projector.project(record=record, account_id=account_id)


__all__ = [name for name in globals() if not (name.startswith("__") and name.endswith("__"))]
