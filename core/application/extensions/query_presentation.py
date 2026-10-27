"""Full extension detail, manifest, preview, and health projections."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, Protocol

from core.application.management.errors import ServiceNotFoundError
from core.application.management.query_models import (
    ExtensionDetailView,
    ExtensionHealthView,
    ExtensionPageContextView,
    ExtensionPageProjectView,
    ExtensionSafeDetailView,
    ExtensionSummaryView,
)
from core.application.management.scope import ScopeContext

from .browser_manifest_projection import project_browser_manifest
from .extension_runtime import runtime_has_no_manifest

_MESSAGE_EXTENSION_NOT_FOUND = "Extension not found"


class RuntimeRecordResolver(Protocol):
    def __call__(
        self,
        extension_id: str,
        *,
        account_id: str | None = None,
    ) -> tuple[dict[str, Any] | None, object]: ...


class RuntimeRecordLister(Protocol):
    def __call__(self, *, account_id: str) -> list[dict[str, Any]]: ...


class ProjectRecordLister(Protocol):
    def __call__(self, *, account_id: str) -> list[dict[str, Any]]: ...


ManifestBuilder = Callable[[dict[str, Any] | None], dict[str, Any]]
ValidationSummarizer = Callable[[dict[str, Any] | None], dict[str, Any]]
RuntimeOperation = Callable[[dict[str, Any]], dict[str, Any]]


class GovernanceReader(Protocol):
    def __call__(
        self,
        extension_id: str,
        *,
        account_id: str,
        extension_install_id: str,
    ) -> dict[str, Any]: ...


class ExtensionPresentationQuery:
    """Build presentation-neutral full detail from canonical install state."""

    def __init__(
        self,
        *,
        list_projects: ProjectRecordLister | None = None,
        resolve_runtime_record: RuntimeRecordResolver | None = None,
        list_runtime_records: RuntimeRecordLister | None = None,
        build_runtime_manifest: ManifestBuilder | None = None,
        summarize_validation: ValidationSummarizer | None = None,
        compile_runtime_preview: RuntimeOperation | None = None,
        run_runtime_health: RuntimeOperation | None = None,
        governance_reader: GovernanceReader | None = None,
    ) -> None:
        self._list_projects = list_projects
        self._resolve_runtime_record = resolve_runtime_record
        self._list_runtime_records = list_runtime_records
        self._build_runtime_manifest = build_runtime_manifest
        self._summarize_validation = summarize_validation
        self._compile_runtime_preview = compile_runtime_preview
        self._run_runtime_health = run_runtime_health
        self._governance_reader = governance_reader

    def enrich(
        self,
        detail: ExtensionDetailView,
        *,
        selected_project_id: str | None = None,
        scope: ScopeContext | None = None,
    ) -> ExtensionDetailView:
        runtime = (
            self._runtime_record(detail.extension_id, scope=scope)
            if detail.has_runtime_module and not self._is_foreign_global(detail)
            else None
        )
        manifest = (
            self._runtime_manifest(runtime, extension_id=detail.extension_id)
            if runtime is not None
            else self._manifest_from_detail(detail)
        )
        validation = self._validation_summary(manifest)
        return detail.model_copy(
            update={
                "compiled_manifest": manifest,
                "validation_summary": validation,
                "projects": self._projects(scope=scope),
                "selected_project_id": selected_project_id,
            }
        )

    @staticmethod
    def safe_detail(detail: ExtensionDetailView) -> ExtensionSafeDetailView:
        """Project the closed non-admin detail surface used by project extension pages."""
        return ExtensionSafeDetailView(
            extension_id=detail.extension_id,
            install_id=detail.install_id,
            display_name=detail.display_name,
            description=detail.description,
            version=detail.version,
            state=detail.state,
            active=str(detail.state or "").strip().lower() == "active",
            broken=bool(getattr(detail, "broken", False)),
            category=detail.category,
            tags=list(detail.tags),
            vendor_name=detail.vendor_name,
            support_url=detail.support_url,
            website_url=detail.website_url,
            has_icon=detail.has_icon,
            icon_url=detail.icon_url,
        )

    @staticmethod
    def browser_manifest(manifest: dict[str, Any]) -> dict[str, Any]:
        return project_browser_manifest(manifest)

    def compiled_manifest(
        self,
        extension_id: str,
        *,
        detail: ExtensionDetailView | None,
        scope: ScopeContext | None,
    ) -> dict[str, Any]:
        if detail is not None:
            return dict(detail.compiled_manifest or self._manifest_from_detail(detail))
        runtime = self._runtime_record(extension_id, scope=scope)
        if runtime is None:
            raise ServiceNotFoundError(_MESSAGE_EXTENSION_NOT_FOUND)
        return self._runtime_manifest(runtime, extension_id=extension_id)

    def runtime_detail(
        self,
        extension_id: str,
        *,
        selected_project_id: str | None,
        scope: ScopeContext | None,
    ) -> ExtensionDetailView:
        runtime = self._runtime_record(extension_id, scope=scope)
        if runtime is None:
            raise ServiceNotFoundError(_MESSAGE_EXTENSION_NOT_FOUND)
        return self._runtime_detail_from_record(
            extension_id,
            runtime=runtime,
            selected_project_id=selected_project_id,
            scope=scope,
        )

    def runtime_summaries(self, *, scope: ScopeContext | None) -> list[ExtensionSummaryView]:
        if self._list_runtime_records is None:
            return []
        account_id = scope.account_id if scope is not None else ""
        summaries: list[ExtensionSummaryView] = []
        for runtime in self._list_runtime_records(account_id=account_id):
            extension_id = str(
                runtime.get("extension_id")
                or runtime.get("extension_key")
                or runtime.get("id")
                or ""
            ).strip()
            if not extension_id:
                continue
            if runtime_has_no_manifest(runtime):
                # A folder without a manifest is named after the folder; no
                # install has that id. Its install record stands for it.
                continue
            detail = self._runtime_detail_from_record(
                extension_id,
                runtime=runtime,
                selected_project_id=None,
                scope=None,
            )
            summaries.append(ExtensionSummaryView.model_validate(detail.model_dump()))
        return summaries

    def _runtime_detail_from_record(
        self,
        extension_id: str,
        *,
        runtime: dict[str, Any],
        selected_project_id: str | None,
        scope: ScopeContext | None,
    ) -> ExtensionDetailView:
        manifest = self._runtime_manifest(runtime, extension_id=extension_id)
        return ExtensionDetailView(
            extension_id=extension_id,
            version=str(
                runtime.get("version")
                or runtime.get("latest_version")
                or manifest.get("version")
                or ""
            ),
            display_name=str(
                runtime.get("display_name") or manifest.get("display_name") or extension_id
            ),
            description=str(runtime.get("description") or manifest.get("description") or ""),
            vendor_name=str(runtime.get("vendor_name") or manifest.get("vendor_name") or ""),
            support_url=str(runtime.get("support_url") or manifest.get("support_url") or ""),
            website_url=str(runtime.get("website_url") or manifest.get("website_url") or ""),
            extension_type=str(runtime.get("kind") or manifest.get("kind") or ""),
            install_id=str(runtime.get("extension_install_id") or runtime.get("install_id") or ""),
            state=str(
                runtime.get("state")
                or ("enabled" if runtime.get("enabled") is True else "disabled")
            ),
            descriptor_origin=str(runtime.get("descriptor_origin") or ""),
            has_runtime_module=True,
            metadata={"runtime": dict(runtime)},
            compiled_manifest=manifest,
            validation_summary=self._validation_summary(manifest),
            projects=self._projects(scope=scope),
            selected_project_id=selected_project_id,
        )

    def page_context(
        self,
        detail: ExtensionDetailView,
        *,
        preview_mode: bool,
        locale: str = "",
    ) -> ExtensionPageContextView:
        extension_name, extension_description = self._localized_identity(detail, locale=locale)
        return ExtensionPageContextView(
            compiled_manifest=detail.compiled_manifest,
            preview_mode=preview_mode,
            extension_id=detail.extension_id,
            extension_name=extension_name,
            extension_kind=detail.extension_type,
            extension_version=detail.version,
            extension_description=extension_description,
            compile_status=str(detail.compiled_manifest.get("compile_status") or "ok"),
            current_state=detail.state,
            vendor_name=detail.vendor_name,
            support_url=detail.support_url,
            website_url=detail.website_url,
            validation_summary=detail.validation_summary,
            health_status=detail.health_status,
            projects=detail.projects,
            selected_project_id=detail.selected_project_id,
        )

    def health(
        self,
        extension_id: str,
        *,
        detail: ExtensionDetailView | None,
        scope: ScopeContext | None,
    ) -> ExtensionHealthView:
        if detail is None:
            raise ServiceNotFoundError(_MESSAGE_EXTENSION_NOT_FOUND)
        if not detail.has_runtime_module:
            health = {
                "status": "not_applicable",
                "detail": "Extension has no runtime component.",
            }
        else:
            health: dict[str, Any] | None = None
            state = str(detail.state or "").lower()
            persisted = str(detail.health_status or "").strip().lower()
            runtime_status = str(detail.runtime_status or "").strip().lower()
            if state == "disabled":
                health = {
                    "status": "disabled",
                    "detail": "Extension is disabled; no active probe was run.",
                }
            elif self._run_runtime_health is not None and not self._is_foreign_global(detail):
                runtime = self._runtime_record(extension_id, scope=scope)
                if runtime is not None:
                    try:
                        probe = dict(self._run_runtime_health(runtime) or {})
                    except Exception as exc:
                        probe = {"status": "failed", "detail": str(exc)}
                    raw_status = str(probe.get("status") or "unknown").strip().lower()
                    if raw_status in {"healthy", "ok", "succeeded", "success"}:
                        status = "healthy"
                    elif raw_status in {"failed", "unhealthy", "error"}:
                        status = "failed"
                    elif raw_status == "degraded":
                        status = "degraded"
                    else:
                        status = "unknown"
                    reason = str(probe.get("detail") or "No probe detail returned.").strip()
                    health = {
                        **probe,
                        "status": status,
                        "detail": reason,
                        "reason": reason,
                        "checked_at": datetime.now(UTC).isoformat(),
                    }
                else:
                    health = {
                        "status": "unknown",
                        "detail": "Extension runtime record is unavailable; no active probe was run.",
                    }
            elif runtime_status in {"failed", "incompatible", "unavailable"} or persisted in {
                "failed",
                "unhealthy",
            }:
                status = "failed"
            elif persisted == "degraded":
                status = "degraded"
            elif persisted in {"healthy", "ok"}:
                status = "healthy"
            else:
                status = "unknown"
            if health is None:
                health = {
                    "status": status,
                    "detail": "Persisted extension health state; no active probe was run.",
                }
        health = dict(health or {})
        if self._governance_reader is not None and not self._is_foreign_global(detail):
            health["governance"] = dict(
                self._governance_reader(
                    extension_id,
                    account_id=str(scope.account_id if scope is not None else ""),
                    extension_install_id=str(detail.install_id or ""),
                )
                or {}
            )
        return ExtensionHealthView(extension_id=extension_id, health=health)

    def preview(
        self,
        extension_id: str,
        *,
        detail: ExtensionDetailView | None,
        scope: ScopeContext | None,
    ) -> dict[str, Any]:
        runtime = (
            self._runtime_record(extension_id, scope=scope)
            if detail is None or (detail.has_runtime_module and not self._is_foreign_global(detail))
            else None
        )
        if runtime is not None:
            payload = dict(runtime)
            payload["compiled_manifest"] = self._runtime_manifest(
                runtime, extension_id=extension_id
            )
            if self._compile_runtime_preview is not None:
                return dict(self._compile_runtime_preview(payload) or {})
            return {"extension": payload, "compiled_manifest": payload["compiled_manifest"]}
        if detail is not None:
            return {
                "extension": detail.model_dump(mode="json"),
                "compiled_manifest": dict(detail.compiled_manifest),
            }
        raise ServiceNotFoundError(_MESSAGE_EXTENSION_NOT_FOUND)

    @staticmethod
    def _is_foreign_global(detail: ExtensionDetailView) -> bool:
        """Identify a tenant-safe global projection with owner state redacted."""
        return detail.metadata.get("projection_scope") == "public_global"

    def _runtime_record(
        self,
        extension_id: str,
        *,
        scope: ScopeContext | None,
    ) -> dict[str, Any] | None:
        if self._resolve_runtime_record is None:
            return None
        record, _logger = self._resolve_runtime_record(
            extension_id,
            account_id=scope.account_id if scope is not None else None,
        )
        return dict(record) if isinstance(record, dict) else None

    def _runtime_manifest(
        self,
        runtime: dict[str, Any],
        *,
        extension_id: str,
    ) -> dict[str, Any]:
        if self._build_runtime_manifest is not None:
            manifest = self._build_runtime_manifest(runtime)
            if isinstance(manifest, dict) and manifest:
                return dict(manifest)
        manifest = runtime.get("compiled_manifest")
        return (
            dict(manifest)
            if isinstance(manifest, dict) and manifest
            else self._empty_manifest(extension_id)
        )

    @staticmethod
    def _empty_manifest(extension_id: str) -> dict[str, Any]:
        return {
            "extension_id": extension_id,
            "ui_manifest": {"version": "1", "pages": []},
            "compile_status": "ok",
            "compile_errors": [],
            "compile_warnings": [],
            "validation_errors": [],
            "validation_warnings": [],
        }

    def _manifest_from_detail(self, detail: ExtensionDetailView) -> dict[str, Any]:
        release = detail.metadata.get("release")
        release = release if isinstance(release, dict) else {}
        descriptor = release.get("descriptor")
        descriptor = descriptor if isinstance(descriptor, dict) else {}
        identity = descriptor.get("identity")
        identity = identity if isinstance(identity, dict) else {}
        vendor = descriptor.get("vendor")
        vendor = vendor if isinstance(vendor, dict) else {}
        manifest = self._empty_manifest(detail.extension_id)
        manifest.update(
            {
                "display_name": str(identity.get("display_name") or detail.display_name),
                "description": str(identity.get("description") or detail.description),
                "version": detail.version,
                "vendor_name": str(vendor.get("vendor_name") or vendor.get("name") or ""),
                "support_url": str(vendor.get("support_url") or ""),
                "website_url": str(vendor.get("website_url") or vendor.get("url") or ""),
                "vendor": dict(vendor),
                "descriptor": dict(descriptor),
                "artifacts": list(detail.artifacts),
            }
        )
        return manifest

    def _validation_summary(self, manifest: dict[str, Any]) -> dict[str, Any]:
        if self._summarize_validation is not None:
            return dict(self._summarize_validation(manifest) or {})
        errors = list(manifest.get("compile_errors") or []) + list(
            manifest.get("validation_errors") or []
        )
        warnings = list(manifest.get("compile_warnings") or []) + list(
            manifest.get("validation_warnings") or []
        )
        status = "ok"
        if errors:
            status = "error"
        elif warnings:
            status = "warning"
        return {
            "status": status,
            "error_count": len(errors),
            "warning_count": len(warnings),
            "compile_errors": list(manifest.get("compile_errors") or []),
            "compile_warnings": list(manifest.get("compile_warnings") or []),
            "validation_errors": list(manifest.get("validation_errors") or []),
            "validation_warnings": list(manifest.get("validation_warnings") or []),
        }

    @staticmethod
    def _localized_identity(detail: ExtensionDetailView, *, locale: str) -> tuple[str, str]:
        name = detail.display_name or detail.extension_id
        description = detail.description
        bundles = detail.compiled_manifest.get("locale_bundles")
        if not locale or not isinstance(bundles, dict):
            return name, description
        from .extension_projection_v2 import resolve_locale_string

        return (
            resolve_locale_string(bundles, locale, "ext.display_name", name),
            resolve_locale_string(bundles, locale, "ext.description", description),
        )

    def _projects(self, *, scope: ScopeContext | None) -> list[ExtensionPageProjectView]:
        if scope is None or self._list_projects is None:
            return []
        rows = self._list_projects(account_id=scope.account_id)
        projects: list[ExtensionPageProjectView] = []
        for row in rows:
            payload = row if isinstance(row, dict) else {}
            project_id = str(payload.get("id") or "").strip()
            account_id = str(payload.get("account_id") or "").strip()
            if (
                project_id
                and account_id == scope.account_id
                and (not scope.allowed_project_ids or project_id in scope.allowed_project_ids)
            ):
                projects.append(
                    ExtensionPageProjectView(
                        id=project_id,
                        name=str(payload.get("name") or f"Project {project_id}"),
                    )
                )
        return projects
