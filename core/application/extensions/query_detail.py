from __future__ import annotations

from core.application.management.errors import ServiceNotFoundError, ServiceUnavailableError
from core.application.management.query_models import ExtensionDetailView
from core.application.management.scope import ScopeContext

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
            raise ServiceNotFoundError(f"Extension '{extension_id}' not found")
        return self._projector.project(record=record, account_id=account_id)

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
