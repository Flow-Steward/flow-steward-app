from __future__ import annotations

from collections.abc import Callable
from typing import Any

from core.application.management.errors import ServiceUnavailableError
from core.application.management.query_models import (
    ExtensionListFilterInput,
    ExtensionListView,
    ExtensionSummaryView,
)
from core.application.management.scope import ScopeContext

from .extension_runtime import (
    ExtensionArtifactSnapshotRecord,
    ExtensionEntitlementRecord,
    ExtensionReleaseRecord,
)
from .install import ExtensionInstallRecord
from .query_catalog import (
    _descriptor_catalog_fields,
    _has_runtime_module,
)
from .query_ports import ExtensionInstallStorePort, ExtensionRuntimeStorePort
from .service_support import (
    _entitlement_status_value,
    _get_default_extension_install_store,
    _get_default_extension_runtime_store,
    _list_extension_runtime_connection_counts,
    _normalize_lower,
    _normalize_text,
    _safe_iso_from_timestamp,
    _service_scope,
    _source_label,
    _status_group_for_install,
)

FILES_MISSING_REASON = "Files missing. Uninstall to clean up."


def extension_files_missing(record: Any) -> bool:
    """A disabled install whose folder under the extensions root has no manifest.

    Only disabled installs are checked: the scan disables an install whose
    bundle disappeared, and an active one is never reported as missing.
    """
    state = getattr(record, "state", "active")
    state = _normalize_lower(getattr(state, "value", state))
    if state == "active":
        return False
    from pathlib import Path

    from core.application.extensions.identity import extension_folder_name
    from core.infrastructure.extension_runtime.extension_runtime import default_extensions_root
    from core.infrastructure.extension_runtime.extension_runtime_common import MANIFEST_FILENAMES

    try:
        folder = Path(default_extensions_root()) / extension_folder_name(
            _normalize_text(getattr(record, "extension_id", ""))
        )
    except (ValueError, OSError):
        return False
    return not any((folder / name).is_file() for name in MANIFEST_FILENAMES)


def _localized_summary(
    row: ExtensionSummaryView,
    release: ExtensionReleaseRecord | None,
    *,
    locale: str,
) -> ExtensionSummaryView:
    """The row's name and description from its own release's dictionaries."""
    from core.application.extensions.extension_locales import resolve_locale_text

    descriptor = getattr(release, "descriptor", None)
    bundles = descriptor.get("locale_bundles") if isinstance(descriptor, dict) else None
    if not isinstance(bundles, dict) or not bundles:
        return row
    return row.model_copy(
        update={
            "display_name": resolve_locale_text(
                bundles, locale, "ext.display_name", row.display_name
            ),
            "description": resolve_locale_text(bundles, locale, "ext.description", row.description),
        }
    )


class ExtensionQueryList:
    """Installed extension listing and filter helpers."""

    def __init__(
        self,
        *,
        get_logger: Callable[[], object | None],
        get_install_store: Callable[[], ExtensionInstallStorePort] | None = None,
        get_runtime_store: Callable[[], ExtensionRuntimeStorePort] | None = None,
    ) -> None:
        self._get_logger = get_logger
        self._get_install_store = get_install_store
        self._get_runtime_store = get_runtime_store

    def logger(self) -> object | None:
        try:
            return self._get_logger()
        except Exception:
            return None

    def install_store(self) -> ExtensionInstallStorePort:
        if self._get_install_store is not None:
            return self._get_install_store()
        return _get_default_extension_install_store(get_logger=self._get_logger)

    def runtime_store(self) -> ExtensionRuntimeStorePort:
        if self._get_runtime_store is not None:
            return self._get_runtime_store()
        return _get_default_extension_runtime_store(get_logger=self._get_logger)

    def list_extensions(
        self,
        *,
        filters: ExtensionListFilterInput | dict[str, Any] | None = None,
        scope: ScopeContext | None = None,
        locale: str = "",
    ) -> ExtensionListView:
        scope = _service_scope(scope)
        query = ExtensionListFilterInput.model_validate(filters or ExtensionListFilterInput())
        account_id = scope.account_id or "acc_default"
        store = self.install_store()
        try:
            records = store.list_for_account(
                account_id,
                include_global=True,
                include_inactive_owned=True,
            )
        except Exception as exc:
            raise ServiceUnavailableError("Extension install store is unavailable") from exc

        runtime_store = self.runtime_store()
        install_ids = [
            _normalize_text(getattr(row, "install_id", ""))
            for row in records
            if _normalize_text(getattr(row, "install_id", ""))
        ]
        release_ids = [
            _normalize_text(getattr(row, "release_id", ""))
            for row in records
            if _normalize_text(getattr(row, "release_id", ""))
        ]
        entitlement_map = self.batch_entitlements(
            runtime_store, account_id=account_id, install_ids=install_ids
        )
        release_map = self._batch_releases(runtime_store, release_ids=release_ids)
        artifacts_by_release = self.batch_artifacts_by_release(
            runtime_store, release_ids=release_ids
        )
        connection_counts = _list_extension_runtime_connection_counts(
            self.logger(),
            account_id=account_id,
            install_ids=install_ids,
            enabled_only=True,
        )

        extensions = [
            self.summary_from_install_record(
                record=row,
                viewer_account_id=account_id,
                release=release_map.get(_normalize_text(getattr(row, "release_id", ""))),
                entitlement=entitlement_map.get(_normalize_text(getattr(row, "install_id", ""))),
                release_artifacts=artifacts_by_release.get(
                    _normalize_text(getattr(row, "release_id", "")), []
                ),
                connected_count=int(
                    connection_counts.get(_normalize_text(getattr(row, "install_id", "")), 0) or 0
                ),
            )
            for row in records
        ]
        if locale:
            extensions = [
                _localized_summary(
                    row,
                    release_map.get(_normalize_text(getattr(record, "release_id", ""))),
                    locale=locale,
                )
                for row, record in zip(extensions, records, strict=True)
            ]
        filtered = [row for row in extensions if self._matches_installed_filters(row, query)]
        filtered.sort(
            key=lambda row: (_normalize_text(row.installed_at), row.extension_id), reverse=True
        )
        return ExtensionListView(extensions=filtered, total=len(filtered))

    def filter_summaries(
        self,
        rows: list[ExtensionSummaryView],
        *,
        filters: ExtensionListFilterInput | dict[str, Any] | None,
    ) -> ExtensionListView:
        query = ExtensionListFilterInput.model_validate(filters or ExtensionListFilterInput())
        filtered = [row for row in rows if self._matches_installed_filters(row, query)]
        filtered.sort(
            key=lambda row: (_normalize_text(row.installed_at), row.extension_id),
            reverse=True,
        )
        return ExtensionListView(extensions=filtered, total=len(filtered))

    def batch_entitlements(
        self,
        runtime_store: ExtensionRuntimeStorePort,
        *,
        account_id: str,
        install_ids: list[str],
    ) -> dict[str, ExtensionEntitlementRecord]:
        normalized_install_ids = [
            _normalize_text(item) for item in install_ids if _normalize_text(item)
        ]
        if not normalized_install_ids:
            return {}
        try:
            rows = runtime_store.list_entitlements(
                account_id=account_id,
                install_ids=normalized_install_ids,
            )
        except Exception as exc:
            raise ServiceUnavailableError("Extension entitlement store is unavailable") from exc
        result: dict[str, ExtensionEntitlementRecord] = {}
        for row in rows:
            if row is None:
                continue
            install_id = _normalize_text(getattr(row, "install_id", ""))
            if install_id:
                result[install_id] = row
        return result

    def _batch_releases(
        self,
        runtime_store: ExtensionRuntimeStorePort,
        *,
        release_ids: list[str],
    ) -> dict[str, ExtensionReleaseRecord]:
        normalized_release_ids = [
            _normalize_text(item) for item in release_ids if _normalize_text(item)
        ]
        if not normalized_release_ids:
            return {}
        try:
            rows = runtime_store.list_releases(release_ids=normalized_release_ids) or []
        except Exception as exc:
            raise ServiceUnavailableError("Extension release store is unavailable") from exc
        result: dict[str, ExtensionReleaseRecord] = {}
        for row in rows:
            if row is None:
                continue
            release_id = _normalize_text(getattr(row, "release_id", ""))
            if release_id:
                result[release_id] = row
        return result

    def batch_artifacts_by_release(
        self,
        runtime_store: ExtensionRuntimeStorePort,
        *,
        release_ids: list[str],
    ) -> dict[str, list[ExtensionArtifactSnapshotRecord]]:
        normalized_release_ids = [
            _normalize_text(item) for item in release_ids if _normalize_text(item)
        ]
        if not normalized_release_ids:
            return {}
        try:
            rows = runtime_store.list_artifact_snapshots_for_releases(normalized_release_ids) or []
        except Exception as exc:
            raise ServiceUnavailableError("Extension artifact store is unavailable") from exc
        grouped: dict[str, list[ExtensionArtifactSnapshotRecord]] = {}
        for row in rows:
            release_id = _normalize_text(getattr(row, "release_id", ""))
            if not release_id:
                continue
            grouped.setdefault(release_id, []).append(row)
        return grouped

    def summary_from_install_record(
        self,
        *,
        record: ExtensionInstallRecord,
        viewer_account_id: str,
        release: ExtensionReleaseRecord | None,
        entitlement: ExtensionEntitlementRecord | None,
        release_artifacts: list[ExtensionArtifactSnapshotRecord],
        connected_count: int,
    ) -> ExtensionSummaryView:
        catalog = _descriptor_catalog_fields(release)
        owned = _normalize_text(record.account_id) == _normalize_text(viewer_account_id)
        entitlement_status = _entitlement_status_value(entitlement) if owned else "unknown"
        status_group = (
            _status_group_for_install(record=record, entitlement_status=entitlement_status)
            if owned
            else "unknown"
        )
        state = getattr(record, "state", "active")
        if hasattr(state, "value"):
            state = state.value
        visibility = getattr(record, "visibility", "account")
        if hasattr(visibility, "value"):
            visibility = visibility.value
        descriptor_source = _normalize_text(
            getattr(record, "descriptor_source", "")
        ) or _normalize_text(getattr(release, "source_kind", ""))
        source_label = _source_label(descriptor_source)
        has_runtime_module = _has_runtime_module(
            release=release, release_artifacts=release_artifacts
        )
        connected = owned and int(connected_count or 0) > 0
        files_missing = owned and extension_files_missing(record)
        return ExtensionSummaryView(
            extension_id=_normalize_text(getattr(record, "extension_id", "")),
            version=_normalize_text(getattr(record, "version", "")),
            display_name=_normalize_text(catalog.get("display_name"))
            or _normalize_text(getattr(record, "extension_id", "")),
            description=_normalize_text(catalog.get("description")),
            vendor_name=_normalize_text(catalog.get("vendor_name")),
            category=_normalize_text(catalog.get("category")),
            tags=list(catalog.get("tags") or []),
            support_url=_normalize_text(catalog.get("support_url")),
            website_url=_normalize_text(catalog.get("website_url")),
            extension_type=_normalize_text(catalog.get("extension_type")),
            install_id=(_normalize_text(getattr(record, "install_id", "")) if owned else ""),
            release_id=getattr(record, "release_id", None),
            descriptor_origin=_normalize_text(getattr(record, "descriptor_origin", "")),
            descriptor_source=descriptor_source,
            runtime_mode=(_normalize_text(getattr(record, "runtime_mode", "")) if owned else ""),
            remote_base_url=(
                _normalize_text(getattr(record, "remote_base_url", "")) if owned else ""
            ),
            handshake_status=(
                _normalize_text(getattr(record, "handshake_status", "")) if owned else ""
            ),
            health_status=(_normalize_text(getattr(record, "health_status", "")) if owned else ""),
            runtime_status=(
                _normalize_text(getattr(record, "runtime_status", "")) if owned else ""
            ),
            state=_normalize_text(state) or "active",
            visibility=_normalize_text(visibility) or "account",
            source_label=source_label,
            entitlement_status=entitlement_status,
            status_group=status_group,
            has_runtime_module=has_runtime_module,
            connected=connected,
            flags=self._summary_flags(
                status_group=status_group,
                has_runtime_module=has_runtime_module,
                connected=connected,
            ),
            installed_at=(
                _safe_iso_from_timestamp(getattr(record, "installed_at", None)) if owned else None
            ),
            files_missing=files_missing,
            broken=files_missing,
            broken_reason=FILES_MISSING_REASON if files_missing else "",
        )

    @staticmethod
    def _summary_flags(
        *, status_group: str, has_runtime_module: bool, connected: bool
    ) -> dict[str, bool]:
        return {
            "billing_blocked": status_group == "billing_blocked",
            "runtime_issue": status_group == "runtime_issue",
            "incompatible": status_group == "incompatible",
            "usable": status_group == "usable",
            "has_runtime_module": has_runtime_module,
            "connected": connected,
        }

    @staticmethod
    def _matches_installed_filters(
        extension: ExtensionSummaryView, query: ExtensionListFilterInput
    ) -> bool:
        q_text = _normalize_lower(query.q)
        if q_text:
            haystack = " ".join(
                [
                    _normalize_text(extension.extension_id),
                    _normalize_text(extension.display_name),
                    _normalize_text(extension.description),
                    _normalize_text(extension.vendor_name),
                    _normalize_text(extension.category),
                    " ".join(extension.tags),
                    _normalize_text(extension.descriptor_origin),
                    _normalize_text(extension.descriptor_source),
                    _normalize_text(extension.source_label),
                ]
            ).lower()
            for token in [item for item in q_text.split() if item]:
                if token not in haystack:
                    return False
        state_filter = {
            normalized
            for item in query.state
            if (normalized := _normalize_lower(item)) and normalized != "all"
        }
        if state_filter and _normalize_lower(extension.state) not in state_filter:
            return False
        status_group_filter = {
            _normalize_lower(item) for item in query.status_group if _normalize_lower(item)
        }
        if (
            status_group_filter
            and _normalize_lower(extension.status_group) not in status_group_filter
        ):
            return False
        runtime_mode_filter = {
            _normalize_lower(item) for item in query.runtime_mode if _normalize_lower(item)
        }
        if (
            runtime_mode_filter
            and _normalize_lower(extension.runtime_mode) not in runtime_mode_filter
        ):
            return False
        entitlement_filter = {
            _normalize_lower(item) for item in query.entitlement_status if _normalize_lower(item)
        }
        if (
            entitlement_filter
            and _normalize_lower(extension.entitlement_status) not in entitlement_filter
        ):
            return False
        origin_filter = {_normalize_lower(item) for item in query.origin if _normalize_lower(item)}
        if origin_filter and _normalize_lower(extension.descriptor_origin) not in origin_filter:
            return False
        source_filter = {_normalize_lower(item) for item in query.source if _normalize_lower(item)}
        if source_filter:
            source_label = _normalize_lower(extension.source_label)
            descriptor_source = _normalize_lower(extension.descriptor_source)
            if source_label not in source_filter and descriptor_source not in source_filter:
                return False
        visibility_filter = {
            _normalize_lower(item) for item in query.visibility if _normalize_lower(item)
        }
        if visibility_filter and _normalize_lower(extension.visibility) not in visibility_filter:
            return False
        if query.has_runtime_module is not None and bool(extension.has_runtime_module) != bool(
            query.has_runtime_module
        ):
            return False
        return not (
            query.connected is not None and bool(extension.connected) != bool(query.connected)
        )


__all__ = [name for name in globals() if not (name.startswith("__") and name.endswith("__"))]
