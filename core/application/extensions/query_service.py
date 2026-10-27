"""Concrete production query service for extension projections."""

from __future__ import annotations

import hmac
import re
from collections.abc import Callable
from typing import Any, TypeVar

from core.application.management.errors import ServiceNotFoundError, ServicePreconditionError
from core.application.management.query_models import (
    ExtensionDetailView,
    ExtensionHealthView,
    ExtensionListFilterInput,
    ExtensionListView,
    ExtensionMarketplaceFilterInput,
    ExtensionMarketplaceListView,
    ExtensionPageContextView,
    ExtensionSafeDetailView,
    ExtensionSummaryView,
)
from core.application.management.scope import ScopeContext, normalize_scope

from .installed_icon import installed_extension_icon, installed_extension_icon_url
from .query_detail import ExtensionQueryDetail
from .query_detail_projection import ExtensionDetailProjector
from .query_list import ExtensionQueryList
from .query_marketplace import ExtensionQueryMarketplace
from .query_marketplace_data import ExtensionMarketplaceDataSource
from .query_marketplace_filters import ExtensionMarketplaceFilter
from .query_marketplace_projection import ExtensionMarketplaceProjector
from .query_ports import ExtensionInstallStorePort, ExtensionRuntimeStorePort
from .query_presentation import (
    ExtensionPresentationQuery,
    GovernanceReader,
    ManifestBuilder,
    ProjectRecordLister,
    RuntimeOperation,
    RuntimeRecordLister,
    RuntimeRecordResolver,
    ValidationSummarizer,
)


class ExtensionQueryService:
    """Query-side extension projection service used by production wiring."""

    def __init__(
        self,
        *,
        get_logger: Callable[[], object | None],
        get_install_store: Callable[[], ExtensionInstallStorePort] | None = None,
        get_runtime_store: Callable[[], ExtensionRuntimeStorePort] | None = None,
        list_extension_projects: ProjectRecordLister | None = None,
        resolve_extension_runtime_record: RuntimeRecordResolver | None = None,
        list_extension_runtime_records: RuntimeRecordLister | None = None,
        build_extension_compiled_manifest: ManifestBuilder | None = None,
        extension_validation_summary: ValidationSummarizer | None = None,
        compile_extension_preview: RuntimeOperation | None = None,
        run_extension_health: RuntimeOperation | None = None,
        extension_governance_status: GovernanceReader | None = None,
    ) -> None:
        self._listings = ExtensionQueryList(
            get_logger=get_logger,
            get_install_store=get_install_store,
            get_runtime_store=get_runtime_store,
        )
        self._marketplace = ExtensionQueryMarketplace(
            data_source=ExtensionMarketplaceDataSource(listings=self._listings),
            projector=ExtensionMarketplaceProjector(),
            filters=ExtensionMarketplaceFilter(),
        )
        self._details = ExtensionQueryDetail(
            listings=self._listings,
            projector=ExtensionDetailProjector(listings=self._listings),
        )
        self._presentation = ExtensionPresentationQuery(
            list_projects=list_extension_projects,
            resolve_runtime_record=resolve_extension_runtime_record,
            list_runtime_records=list_extension_runtime_records,
            build_runtime_manifest=build_extension_compiled_manifest,
            summarize_validation=extension_validation_summary,
            compile_runtime_preview=compile_extension_preview,
            run_runtime_health=run_extension_health,
            governance_reader=extension_governance_status,
        )

    def list_extensions(
        self,
        *,
        filters: ExtensionListFilterInput | dict[str, Any] | None = None,
        scope: ScopeContext | None = None,
        locale: str = "",
    ) -> ExtensionListView:
        scope = normalize_scope(scope)
        installed = self._listings.list_extensions(filters=None, scope=scope, locale=locale)
        installed_ids = {row.extension_id for row in installed.extensions}
        runtime_only = [
            row
            for row in self._presentation.runtime_summaries(scope=scope)
            if row.extension_id not in installed_ids
        ]
        listed = self._listings.filter_summaries(
            [*installed.extensions, *runtime_only],
            filters=filters,
        )
        return listed.model_copy(
            update={"extensions": [_with_icon(row) for row in listed.extensions]}
        )

    def list_marketplace(
        self,
        *,
        filters: ExtensionMarketplaceFilterInput | dict[str, Any] | None = None,
        scope: ScopeContext | None = None,
    ) -> ExtensionMarketplaceListView:
        scope = normalize_scope(scope)
        return self._marketplace.list_marketplace(filters=filters, scope=scope)

    def get_extension(
        self,
        extension_id: str,
        *,
        scope: ScopeContext | None = None,
    ) -> ExtensionDetailView:
        scope = normalize_scope(scope)
        try:
            detail = self._details.get_extension(extension_id, scope=scope)
        except ServiceNotFoundError:
            detail = self._presentation.runtime_detail(
                extension_id,
                selected_project_id=None,
                scope=scope,
            )
        else:
            detail = self._presentation.enrich(detail, scope=scope)
        return _with_icon(detail)

    def get_extension_icon(
        self,
        extension_id: str,
        *,
        scope: ScopeContext | None = None,
    ) -> bytes:
        """The installed extension's logo, for a caller who may see the extension.

        Visibility is decided exactly as ``get_extension`` decides it — the
        account's install record, else the runtime record — without building
        the rest of the detail page, because a list asks for every row's logo.
        """
        scope = normalize_scope(scope)
        try:
            detail = self._details.get_extension(extension_id, scope=scope)
        except ServiceNotFoundError:
            detail = self._presentation.runtime_detail(
                extension_id,
                selected_project_id=None,
                scope=scope,
            )
        data = installed_extension_icon(detail.extension_id or extension_id)
        if data is None:
            raise ServiceNotFoundError("Extension icon not found")
        return data

    def get_extension_safe(
        self,
        extension_id: str,
        *,
        scope: ScopeContext | None = None,
    ) -> ExtensionSafeDetailView:
        """Return the closed non-admin projection for project extension pages."""
        return self._presentation.safe_detail(self.get_extension(extension_id, scope=scope))

    def get_install_detail(
        self,
        install_id: str,
        *,
        scope: ScopeContext | None = None,
    ) -> ExtensionDetailView:
        scope = normalize_scope(scope)
        detail = self._details.get_install_detail(install_id, scope=scope)
        return _with_icon(self._presentation.enrich(detail, scope=scope))

    def get_compiled_manifest(
        self,
        extension_id: str,
        *,
        install_id: str | None = None,
        scope: ScopeContext | None = None,
    ) -> dict[str, Any]:
        scope = normalize_scope(scope)
        normalized_install_id = str(install_id or "").strip()
        if normalized_install_id:
            detail = self._details.get_install_detail(normalized_install_id, scope=scope)
            if str(detail.extension_id or "").strip() != str(extension_id or "").strip():
                raise ServiceNotFoundError("Extension install does not match requested extension")
        else:
            detail = self._detail_or_none(extension_id, scope=scope)
        if detail is not None:
            current_manifest = self._require_executable_browser_authority(detail)
            detail = detail.model_copy(update={"compiled_manifest": current_manifest})
        return self._presentation.compiled_manifest(
            extension_id,
            detail=detail,
            scope=scope,
        )

    def get_compiled_manifest_safe(
        self,
        extension_id: str,
        *,
        install_id: str | None = None,
        scope: ScopeContext | None = None,
    ) -> dict[str, Any]:
        """Return the closed browser manifest after normal authority checks."""
        return self._presentation.browser_manifest(
            self.get_compiled_manifest(
                extension_id,
                install_id=install_id,
                scope=scope,
            )
        )

    def get_browser_asset_manifest(
        self,
        extension_id: str,
        *,
        install_id: str,
        release_id: str,
        descriptor_sha256: str,
        project_id: str,
        scope: ScopeContext | None = None,
    ) -> dict[str, Any]:
        """Return a manifest bound to one active install, release, and project.

        Browser pages retain these immutable values in their asset URLs.  Re-resolve
        all of them for every request so an install update cannot serve new assets to
        a page rendered for the previous release.
        """
        scope = normalize_scope(scope)
        expected = {
            "extension_id": str(extension_id or "").strip(),
            "install_id": str(install_id or "").strip(),
            "release_id": str(release_id or "").strip(),
            "descriptor_sha256": str(descriptor_sha256 or "").strip().lower(),
            "project_id": str(project_id or "").strip(),
        }
        if (
            not all(expected.values())
            or re.fullmatch(r"[0-9a-f]{64}", expected["descriptor_sha256"]) is None
            or str(scope.project_id or "").strip() != expected["project_id"]
            or (
                scope.allowed_project_ids
                and expected["project_id"] not in scope.allowed_project_ids
            )
        ):
            raise ServiceNotFoundError("Extension browser asset not found")

        detail = self.get_install_detail(expected["install_id"], scope=scope)
        release = detail.metadata.get("release")
        release = release if isinstance(release, dict) else {}
        available_project_ids = {str(project.id or "").strip() for project in detail.projects}
        if (
            str(detail.extension_id or "").strip() != expected["extension_id"]
            or str(detail.state or "").strip().lower() != "active"
            or not hmac.compare_digest(str(detail.release_id or "").strip(), expected["release_id"])
            or not hmac.compare_digest(
                str(release.get("descriptor_sha256") or "").strip().lower(),
                expected["descriptor_sha256"],
            )
            or expected["project_id"] not in available_project_ids
        ):
            raise ServiceNotFoundError("Extension browser asset not found")

        manifest = self._require_executable_browser_authority(detail)
        manifest["browser_asset_authority"] = {
            **expected,
            "account_id": scope.account_id,
            "install_state": "active",
        }
        return manifest

    def get_extension_page_context(
        self,
        *,
        extension_id: str,
        preview_mode: bool = False,
        selected_project_id: str | None = None,
        locale: str = "",
        scope: ScopeContext | None = None,
    ) -> ExtensionPageContextView:
        scope = normalize_scope(scope)
        try:
            detail = self._presentation.enrich(
                self._details.get_extension(extension_id, scope=scope),
                selected_project_id=selected_project_id,
                scope=scope,
            )
        except ServiceNotFoundError:
            detail = self._presentation.runtime_detail(
                extension_id,
                selected_project_id=selected_project_id,
                scope=scope,
            )
        current_manifest = self._require_executable_browser_authority(detail)
        detail = detail.model_copy(update={"compiled_manifest": current_manifest})
        return self._presentation.page_context(
            detail,
            preview_mode=preview_mode,
            locale=locale,
        )

    def _require_executable_browser_authority(self, detail: ExtensionDetailView) -> dict[str, Any]:
        from .browser_authority_approval import (
            BROWSER_AUTHORITY_PROVENANCE_KEY,
            browser_authority_required,
            require_current_browser_authority,
            resolve_browser_authority_manifest,
        )

        install_id = str(detail.install_id or "").strip()
        if not install_id:
            if browser_authority_required(detail.compiled_manifest):
                raise ServicePreconditionError("External browser code approval required")
            return dict(detail.compiled_manifest)
        record = self._listings.install_store().get_by_install_id(install_id)
        if record is None:
            raise ServiceNotFoundError("Extension install not found")
        release = self._listings.runtime_store().get_release(
            str(getattr(record, "release_id", "") or "").strip()
        )
        if release is None:
            if browser_authority_required(detail.compiled_manifest):
                raise ServicePreconditionError("External browser code approval required")
            return dict(detail.compiled_manifest)
        require_current_browser_authority(record=record, release=release)
        stored_authority_required = browser_authority_required(detail.compiled_manifest)
        approval_provenance = getattr(record, "provenance", {})
        previously_approved_authority = (
            isinstance(approval_provenance, dict)
            and BROWSER_AUTHORITY_PROVENANCE_KEY in approval_provenance
        )
        current_manifest = resolve_browser_authority_manifest(
            release,
            require_bundle_manifest=(stored_authority_required or previously_approved_authority),
        )
        if stored_authority_required or browser_authority_required(current_manifest):
            return current_manifest
        return dict(detail.compiled_manifest)

    def get_health(
        self,
        extension_id: str,
        *,
        scope: ScopeContext | None = None,
    ) -> ExtensionHealthView:
        scope = normalize_scope(scope)
        try:
            detail = self._details.get_extension(extension_id, scope=scope)
        except ServiceNotFoundError:
            try:
                detail = self._presentation.runtime_detail(
                    extension_id,
                    selected_project_id=None,
                    scope=scope,
                )
            except ServiceNotFoundError:
                detail = None
        return self._presentation.health(extension_id, detail=detail, scope=scope)

    def get_preview(
        self,
        extension_id: str,
        *,
        scope: ScopeContext | None = None,
    ) -> dict[str, Any]:
        scope = normalize_scope(scope)
        detail = self._detail_or_none(extension_id, scope=scope)
        return self._presentation.preview(extension_id, detail=detail, scope=scope)

    def _detail_or_none(
        self,
        extension_id: str,
        *,
        scope: ScopeContext | None,
    ) -> ExtensionDetailView | None:
        try:
            detail = self.get_extension(extension_id, scope=scope)
        except ServiceNotFoundError:
            return None
        if str(detail.extension_id or "").strip() != str(extension_id or "").strip():
            return None
        return detail


_View = TypeVar("_View", bound=ExtensionSummaryView)


def _with_icon(row: _View) -> _View:
    icon_url = installed_extension_icon_url(row.extension_id, version=row.version)
    return row.model_copy(update={"has_icon": bool(icon_url), "icon_url": icon_url})


def create_extension_query_service(
    *,
    get_logger: Callable[[], object | None],
    get_install_store: Callable[[], ExtensionInstallStorePort] | None = None,
    get_runtime_store: Callable[[], ExtensionRuntimeStorePort] | None = None,
    list_extension_projects: ProjectRecordLister | None = None,
    resolve_extension_runtime_record: RuntimeRecordResolver | None = None,
    list_extension_runtime_records: RuntimeRecordLister | None = None,
    build_extension_compiled_manifest: ManifestBuilder | None = None,
    extension_validation_summary: ValidationSummarizer | None = None,
    compile_extension_preview: RuntimeOperation | None = None,
    run_extension_health: RuntimeOperation | None = None,
    extension_governance_status: GovernanceReader | None = None,
) -> ExtensionQueryService:
    """Canonical construction seam for all extension query consumers."""
    return ExtensionQueryService(
        get_logger=get_logger,
        get_install_store=get_install_store,
        get_runtime_store=get_runtime_store,
        list_extension_projects=list_extension_projects,
        resolve_extension_runtime_record=resolve_extension_runtime_record,
        list_extension_runtime_records=list_extension_runtime_records,
        build_extension_compiled_manifest=build_extension_compiled_manifest,
        extension_validation_summary=extension_validation_summary,
        compile_extension_preview=compile_extension_preview,
        run_extension_health=run_extension_health,
        extension_governance_status=extension_governance_status,
    )


__all__ = ["ExtensionQueryService", "create_extension_query_service"]
