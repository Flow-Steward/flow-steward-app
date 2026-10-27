"""Narrow typed collaborator ports for extension query projections."""

from __future__ import annotations

from typing import Protocol

from core.application.management.query_models import ExtensionSummaryView

from .extension_runtime import (
    ExtensionArtifactSnapshotRecord,
    ExtensionEntitlementRecord,
    ExtensionRegistryProjectionRecord,
    ExtensionReleaseRecord,
)
from .install import ExtensionInstallRecord


class ExtensionInstallStorePort(Protocol):
    """Read operations required from an extension install store."""

    def get(self, *, extension_id: str, account_id: str) -> ExtensionInstallRecord | None: ...

    def get_by_install_id(self, install_id: str) -> ExtensionInstallRecord | None: ...

    def list_for_account(
        self,
        account_id: str,
        *,
        include_global: bool = True,
        include_inactive_owned: bool = False,
    ) -> list[ExtensionInstallRecord]: ...


class ExtensionRuntimeStorePort(Protocol):
    """Read operations required from an extension runtime store."""

    def get_release(self, release_id: str) -> ExtensionReleaseRecord | None: ...

    def list_releases(
        self,
        *,
        extension_id: str = "",
        account_id: str = "",
        release_ids: list[str] | None = None,
    ) -> list[ExtensionReleaseRecord]: ...

    def list_entitlements(
        self,
        *,
        account_id: str,
        install_ids: list[str] | None = None,
    ) -> list[ExtensionEntitlementRecord]: ...

    def list_artifact_snapshots(self, release_id: str) -> list[ExtensionArtifactSnapshotRecord]: ...

    def list_artifact_snapshots_for_releases(
        self, release_ids: list[str]
    ) -> list[ExtensionArtifactSnapshotRecord]: ...

    def list_projections(
        self,
        *,
        account_id: str,
        artifact_kind: str = "",
        only_active: bool = True,
    ) -> list[ExtensionRegistryProjectionRecord]: ...


class ExtensionStoresPort(Protocol):
    """Read-only store access shared by focused query components."""

    def logger(self) -> object | None: ...

    def install_store(self) -> ExtensionInstallStorePort: ...

    def runtime_store(self) -> ExtensionRuntimeStorePort: ...


class ExtensionMarketplaceListingPort(ExtensionStoresPort, Protocol):
    """Batch capabilities required by the marketplace data source."""

    def batch_entitlements(
        self,
        runtime_store: ExtensionRuntimeStorePort,
        *,
        account_id: str,
        install_ids: list[str],
    ) -> dict[str, ExtensionEntitlementRecord]: ...

    def batch_artifacts_by_release(
        self,
        runtime_store: ExtensionRuntimeStorePort,
        *,
        release_ids: list[str],
    ) -> dict[str, list[ExtensionArtifactSnapshotRecord]]: ...


class ExtensionDetailListingPort(ExtensionStoresPort, Protocol):
    """Projection capabilities required by extension detail."""

    def summary_from_install_record(
        self,
        *,
        record: ExtensionInstallRecord,
        viewer_account_id: str,
        release: ExtensionReleaseRecord | None,
        entitlement: ExtensionEntitlementRecord | None,
        release_artifacts: list[ExtensionArtifactSnapshotRecord],
        connected_count: int,
    ) -> ExtensionSummaryView: ...


__all__ = [
    "ExtensionDetailListingPort",
    "ExtensionInstallStorePort",
    "ExtensionMarketplaceListingPort",
    "ExtensionRuntimeStorePort",
    "ExtensionStoresPort",
]
