from __future__ import annotations

from dataclasses import dataclass

from core.application.management.errors import ServiceUnavailableError

from .extension_runtime import (
    ExtensionArtifactSnapshotRecord,
    ExtensionEntitlementRecord,
    ExtensionReleaseRecord,
)
from .install import ExtensionInstallRecord
from .query_ports import ExtensionMarketplaceListingPort, ExtensionRuntimeStorePort
from .service_support import (
    _list_extension_runtime_connection_counts,
    _normalize_text,
    _release_visible_to_account,
)


@dataclass(frozen=True)
class MarketplaceQueryData:
    releases: list[ExtensionReleaseRecord]
    artifacts_by_release: dict[str, list[ExtensionArtifactSnapshotRecord]]
    installs_by_extension: dict[str, ExtensionInstallRecord]
    entitlements_by_install: dict[str, ExtensionEntitlementRecord]
    connection_counts: dict[str, int]


class ExtensionMarketplaceDataSource:
    """Load the marketplace records required by projection."""

    def __init__(self, *, listings: ExtensionMarketplaceListingPort) -> None:
        self._listings = listings

    def load(self, *, account_id: str) -> MarketplaceQueryData:
        runtime_store = self._listings.runtime_store()
        releases = self._latest_visible_releases(runtime_store=runtime_store, account_id=account_id)
        release_ids = [_normalize_text(getattr(row, "release_id", "")) for row in releases]
        artifacts = self._listings.batch_artifacts_by_release(
            runtime_store, release_ids=release_ids
        )
        owned_installs = self._owned_installs(account_id=account_id)
        install_ids = [
            _normalize_text(getattr(row, "install_id", ""))
            for row in owned_installs
            if _normalize_text(getattr(row, "install_id", ""))
        ]
        return MarketplaceQueryData(
            releases=releases,
            artifacts_by_release=artifacts,
            installs_by_extension={
                _normalize_text(getattr(row, "extension_id", "")): row for row in owned_installs
            },
            entitlements_by_install=self._listings.batch_entitlements(
                runtime_store, account_id=account_id, install_ids=install_ids
            ),
            connection_counts=_list_extension_runtime_connection_counts(
                self._listings.logger(),
                account_id=account_id,
                install_ids=install_ids,
                enabled_only=True,
            ),
        )

    @staticmethod
    def _visible_releases(
        *, runtime_store: ExtensionRuntimeStorePort, account_id: str
    ) -> list[ExtensionReleaseRecord]:
        try:
            releases = runtime_store.list_releases() or []
        except Exception as exc:
            raise ServiceUnavailableError("Extension release store is unavailable") from exc
        return [
            row
            for row in releases
            if _normalize_text(getattr(row, "extension_id", ""))
            and _release_visible_to_account(
                row, account_id=account_id, allow_marketplace_public=True
            )
        ]

    def _latest_visible_releases(
        self, *, runtime_store: ExtensionRuntimeStorePort, account_id: str
    ) -> list[ExtensionReleaseRecord]:
        releases = self._visible_releases(runtime_store=runtime_store, account_id=account_id)
        releases.sort(key=lambda row: float(getattr(row, "created_at", 0.0) or 0.0), reverse=True)
        latest: dict[str, ExtensionReleaseRecord] = {}
        for row in releases:
            extension_id = _normalize_text(getattr(row, "extension_id", ""))
            if extension_id and extension_id not in latest:
                latest[extension_id] = row
        return list(latest.values())

    def _owned_installs(self, *, account_id: str) -> list[ExtensionInstallRecord]:
        store = self._listings.install_store()
        try:
            return store.list_for_account(
                account_id,
                include_global=False,
                include_inactive_owned=True,
            )
        except Exception as exc:
            raise ServiceUnavailableError("Extension install store is unavailable") from exc


__all__ = ["ExtensionMarketplaceDataSource", "MarketplaceQueryData"]
