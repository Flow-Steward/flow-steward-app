from __future__ import annotations

from typing import Any

from core.application.management.query_models import (
    ExtensionMarketplaceFilterInput,
    ExtensionMarketplaceItemView,
    ExtensionMarketplaceListView,
)
from core.application.management.scope import ScopeContext

from .query_marketplace_data import ExtensionMarketplaceDataSource, MarketplaceQueryData
from .query_marketplace_filters import ExtensionMarketplaceFilter
from .query_marketplace_projection import ExtensionMarketplaceProjector
from .service_support import _normalize_text, _service_scope


class ExtensionQueryMarketplace:
    """Coordinate explicit marketplace query components."""

    def __init__(
        self,
        *,
        data_source: ExtensionMarketplaceDataSource,
        projector: ExtensionMarketplaceProjector,
        filters: ExtensionMarketplaceFilter,
    ) -> None:
        self._data_source = data_source
        self._projector = projector
        self._filters = filters

    def list_marketplace(
        self,
        *,
        filters: ExtensionMarketplaceFilterInput | dict[str, Any] | None = None,
        scope: ScopeContext | None = None,
    ) -> ExtensionMarketplaceListView:
        query = ExtensionMarketplaceFilterInput.model_validate(
            filters or ExtensionMarketplaceFilterInput()
        )
        account_id = _service_scope(scope).account_id or "acc_default"
        data = self._data_source.load(account_id=account_id)
        items = self._project_items(data=data)
        filtered = [item for item in items if self._filters.matches(item, query)]
        self._filters.sort(filtered, query.sort)
        facets = self._filters.facets(filtered) if query.facets else None
        per_page = max(1, min(int(query.per_page or 20), 200))
        total = len(filtered)
        total_pages = max(1, -(-total // per_page))
        page = max(1, min(int(query.page or 1), total_pages))
        offset = (page - 1) * per_page
        return ExtensionMarketplaceListView(
            extensions=filtered[offset : offset + per_page],
            total=total,
            page=page,
            per_page=per_page,
            total_pages=total_pages,
            facets=facets,
        )

    def _project_items(self, *, data: MarketplaceQueryData) -> list[ExtensionMarketplaceItemView]:
        return [
            self._projector.project(
                release=release,
                release_artifacts=data.artifacts_by_release.get(
                    _normalize_text(getattr(release, "release_id", "")), []
                ),
                install_record=data.installs_by_extension.get(
                    _normalize_text(getattr(release, "extension_id", ""))
                ),
                entitlements_by_install=data.entitlements_by_install,
                connection_counts=data.connection_counts,
            )
            for release in data.releases
        ]


__all__ = ["ExtensionQueryMarketplace"]
