from __future__ import annotations

import contextlib
from collections.abc import Callable, Mapping
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from core.application.auth.config import DEFAULT_ACCOUNT_ID
from core.application.management.command_models import ExtensionDescriptorImportFileInput
from core.application.management.command_models_registry_tools import (
    ExtensionEnableResultView,
    ExtensionScanResultView,
)
from core.application.management.errors import (
    ServiceConflictError,
    ServiceError,
    ServiceNotFoundError,
    ServicePreconditionError,
    ServiceUnavailableError,
    ServiceValidationError,
)
from core.application.management.scope import ScopeContext

from .binding_cleanup_service import ExtensionCleanupService
from .binding_constants import (
    MAX_EXTENSION_ARCHIVE_BYTES,
    MAX_EXTENSION_ARCHIVE_UNCOMPRESSED_BYTES,
    archive_too_large_message,
)
from .binding_context import ExtensionLifecycleContext
from .binding_projection_service import ExtensionBindingProjectionService
from .lifecycle_ports import ERR_DATABASE_NOT_CONFIGURED, service_scope
from .managed_release_service import ManagedReleaseService

_MAX_EXTENSION_ARCHIVE_BYTES = MAX_EXTENSION_ARCHIVE_BYTES
_MAX_EXTENSION_ARCHIVE_UNCOMPRESSED_BYTES = MAX_EXTENSION_ARCHIVE_UNCOMPRESSED_BYTES
_archive_too_large_message = archive_too_large_message


class ExtensionStateService:
    def __init__(
        self,
        *,
        context: ExtensionLifecycleContext,
        projection: ExtensionBindingProjectionService,
        cleanup: ExtensionCleanupService,
        managed_release: ManagedReleaseService,
        get_logger: Callable[[], Any],
    ) -> None:
        self._context = context
        self._projection = projection
        self._cleanup = cleanup
        self._managed_release = managed_release
        self._get_logger = get_logger

    def extension_install_record(
        self,
        bl: Any,
        *,
        extension_id: str,
        scope: ScopeContext,
        install_store: Any | None = None,
    ) -> Any | None:
        return self._extension_install_record(
            bl,
            extension_id=extension_id,
            scope=scope,
            install_store=install_store,
        )

    def current_extension_install_record(
        self,
        bl: Any,
        *,
        extension_id: str,
        scope: ScopeContext,
        extensions_root: Path | None = None,
        install_store: Any | None = None,
    ) -> Any | None:
        return self._current_extension_install_record(
            bl,
            extension_id=extension_id,
            scope=scope,
            extensions_root=extensions_root,
            install_store=install_store,
        )

    def recover_managed_release_publications(self, *, bl: Any) -> None:
        self._recover_managed_release_publications(bl=bl)

    def sync_staged_extension_release(
        self,
        *,
        bl: Any,
        release_root: Path,
        extension_id: str,
    ) -> dict[str, Any]:
        return self._sync_staged_extension_release(
            bl=bl,
            release_root=release_root,
            extension_id=extension_id,
        )

    def set_enabled_state_locked(
        self,
        extension_id: str,
        *,
        bl: Any,
        version: str | None,
        enabled: bool,
        config: dict[str, Any],
        scope: ScopeContext | None = None,
        approved_browser_authority_impact_hash: str = "",
        _skip_rescan: bool = False,
        _skip_browser_authority_gate: bool = False,
        _install_store: Any | None = None,
        _runtime_store: Any | None = None,
    ) -> ExtensionEnableResultView:
        return self._set_enabled_state_locked(
            extension_id,
            bl=bl,
            version=version,
            enabled=enabled,
            config=config,
            scope=scope,
            approved_browser_authority_impact_hash=(approved_browser_authority_impact_hash),
            _skip_rescan=_skip_rescan,
            _skip_browser_authority_gate=_skip_browser_authority_gate,
            _install_store=_install_store,
            _runtime_store=_runtime_store,
        )

    def scan_extensions(
        self,
        *,
        root: str = "",
        scope: ScopeContext | None = None,
        approved_browser_authority_impact_hash: str = "",
        _bl: Any | None = None,
    ) -> ExtensionScanResultView:
        normalized_scope = service_scope(scope)
        bl = _bl if _bl is not None else self._get_logger()
        if not bl:
            raise ServiceUnavailableError(ERR_DATABASE_NOT_CONFIGURED)

        from core.infrastructure.extension_runtime.extension_runtime import (
            default_extensions_root,
            scan_extensions,
        )

        default_root_text = str(default_extensions_root())
        default_root = Path(default_root_text).resolve()
        requested_root_text = str(root or "").strip() or default_root_text
        requested_root = Path(requested_root_text).resolve()
        if requested_root != default_root:
            raise ServiceValidationError("Extension scan root must be the managed extensions root")
        from core.infrastructure.extension_runtime.lifecycle_lock import (
            extension_lifecycle_lock,
        )

        with extension_lifecycle_lock(connection=getattr(bl, "conn", None)) as acquired:
            if not acquired:
                raise ServiceUnavailableError(
                    "Extension lifecycle reconciliation is already in progress"
                )
            self._managed_release.reconcile_managed_extension_staging(
                extensions_root=requested_root
            )
            self._context.recover_managed_release_publications(bl=bl)
            scanned = scan_extensions(str(requested_root))
            if not hasattr(bl, "sync_extension_scan_records"):
                raise ServiceUnavailableError("Extension scan synchronization is not available")
            from .extension_scan_lifecycle import sync_extension_scan_with_llm_lifecycle

            sync_extension_scan_with_llm_lifecycle(bl, scanned)
            cleanup = self._cleanup.reconcile_extension_cleanup(
                bl=bl,
                discard_installed_intents=True,
                discovered_extension_ids={
                    str(item.get("extension_key") or "").strip()
                    for item in scanned
                    if isinstance(item, dict) and str(item.get("extension_key") or "").strip()
                },
            )
            if cleanup.pending:
                raise ServiceUnavailableError(
                    "Extension cleanup reconciliation has pending intents"
                )
            extensions = (
                bl.list_extensions(enabled_only=False)
                if hasattr(bl, "list_extensions")
                else scanned
            )
        self._enrich_scanned_descriptor_bundles(
            bl=bl,
            scanned=scanned,
            scope=normalized_scope,
            approved_browser_authority_impact_hash=(approved_browser_authority_impact_hash),
        )
        return ExtensionScanResultView(
            root=requested_root_text,
            extensions=list(extensions or []),
            scanned_count=len(scanned),
        )

    def _enrich_scanned_descriptor_bundles(
        self,
        *,
        bl: Any,
        scanned: list[dict[str, Any]],
        scope: ScopeContext,
        approved_browser_authority_impact_hash: str = "",
    ) -> None:
        candidates: list[tuple[str, Path]] = []
        for item in scanned:
            if not isinstance(item, dict) or str(item.get("status") or "") != "valid":
                continue
            extension_id = str(item.get("extension_key") or "").strip()
            bundle_root = str(item.get("bundle_root") or "").strip()
            if not extension_id or not bundle_root:
                continue
            descriptor_path = Path(bundle_root) / "extension-descriptor.yaml"
            if descriptor_path.is_file():
                candidates.append((extension_id, descriptor_path))
        if not candidates:
            return

        command_service = self._context.command_service(bl)
        for extension_id, descriptor_path in candidates:
            command_service.import_descriptor_file(
                ExtensionDescriptorImportFileInput(
                    descriptor_path=str(descriptor_path),
                    source_ref=extension_id,
                    install_after_import=True,
                    approved_browser_authority_impact_hash=(approved_browser_authority_impact_hash),
                ),
                scope=scope,
                source_kind_override="extension_scan",
                allow_same_version_enrichment=True,
            )

    def _extension_install_record(
        self,
        bl: Any,
        *,
        extension_id: str,
        scope: ScopeContext,
        install_store: Any | None = None,
    ) -> Any | None:
        store = install_store if install_store is not None else self._context.install_store(bl)
        if not hasattr(store, "get"):
            return None
        account_id = scope.account_id or DEFAULT_ACCOUNT_ID
        return store.get(extension_id=extension_id, account_id=account_id)

    def _current_extension_install_record(
        self,
        bl: Any,
        *,
        extension_id: str,
        scope: ScopeContext,
        extensions_root: Path | None = None,
        install_store: Any | None = None,
    ) -> Any | None:
        current_install = self._extension_install_record(
            bl,
            extension_id=extension_id,
            scope=scope,
            install_store=install_store,
        )
        if current_install is not None:
            return current_install
        extension_row = bl.get_extension(extension_id) if hasattr(bl, "get_extension") else None
        if not isinstance(extension_row, Mapping):
            extension_row = None
        if extension_row is None and extensions_root is not None:
            with contextlib.suppress(Exception):
                bundle_root = self._managed_release.managed_simple_extension_root(extension_id)
                if bundle_root.exists():
                    self.scan_extensions(root=str(extensions_root), scope=scope, _bl=bl)
            extension_row = bl.get_extension(extension_id) if hasattr(bl, "get_extension") else None
            if not isinstance(extension_row, Mapping):
                extension_row = None
        if not extension_row:
            return None
        normalized_version = str(
            extension_row.get("version")
            or extension_row.get("latest_version")
            or self._projection.extension_version_from_payload(extension_row)
            or ""
        ).strip()
        return SimpleNamespace(
            extension_id=extension_id,
            install_id=str(extension_row.get("install_id") or "").strip(),
            release_id=str(extension_row.get("version_id") or "").strip(),
            version=normalized_version,
            state="active" if bool(extension_row.get("enabled")) else "disabled",
            config=dict(extension_row.get("config") or {}),
            descriptor_source="extension_scan",
        )

    def _recover_managed_release_publications(self, *, bl: Any) -> None:
        self._context.recover_managed_release_publications(bl=bl)

    def _sync_staged_extension_release(
        self,
        *,
        bl: Any,
        release_root: Path,
        extension_id: str,
    ) -> dict[str, Any]:
        """Persist a validated staged release without exposing it to runtime readers."""
        from core.application.extensions.identity import extension_folder_name
        from core.infrastructure.extension_runtime.extension_runtime import (
            compile_extension_bundle,
            extension_host_metadata,
        )

        compiled = compile_extension_bundle(
            release_root,
            logical_bundle_dir_name=extension_folder_name(extension_id),
        )
        compiled.update(extension_host_metadata(compiled))
        if str(compiled.get("extension_key") or "").strip() != extension_id:
            raise ServiceValidationError(
                "Staged extension identity does not match the install request"
            )
        if str(compiled.get("status") or "").strip().lower() != "valid":
            raise ServiceValidationError(
                str(compiled.get("scan_error") or "").strip()
                or "Staged extension failed validation"
            )
        if not hasattr(bl, "sync_extension_scan_records"):
            raise ServiceUnavailableError("Extension scan synchronization is not available")
        from .extension_scan_lifecycle import sync_extension_scan_with_llm_lifecycle

        sync_extension_scan_with_llm_lifecycle(bl, [compiled])
        try:
            extensions = (
                bl.list_extensions(enabled_only=False) if hasattr(bl, "list_extensions") else []
            )
        except Exception:
            extensions = []
        for extension in extensions or []:
            payload = dict(extension or {})
            if str(payload.get("extension_key") or "").strip() == extension_id:
                return payload
        return compiled

    def set_enabled_state(
        self,
        extension_id: str,
        *,
        version: str | None,
        enabled: bool,
        config: dict[str, Any],
        scope: ScopeContext | None = None,
        approved_browser_authority_impact_hash: str = "",
        _skip_rescan: bool = False,
    ) -> ExtensionEnableResultView:
        bl = self._get_logger()
        if not bl:
            raise ServiceUnavailableError(ERR_DATABASE_NOT_CONFIGURED)
        from core.infrastructure.extension_runtime.lifecycle_lock import (
            extension_lifecycle_lock,
        )

        with extension_lifecycle_lock(connection=getattr(bl, "conn", None)) as acquired:
            if not acquired:
                raise ServiceConflictError(
                    "Another extension lifecycle operation is already in progress"
                )
            return self._set_enabled_state_locked(
                extension_id,
                bl=bl,
                version=version,
                enabled=enabled,
                config=config,
                scope=scope,
                approved_browser_authority_impact_hash=(approved_browser_authority_impact_hash),
                _skip_rescan=_skip_rescan,
            )

    def _set_enabled_state_locked(
        self,
        extension_id: str,
        *,
        bl: Any,
        version: str | None,
        enabled: bool,
        config: dict[str, Any],
        scope: ScopeContext | None = None,
        approved_browser_authority_impact_hash: str = "",
        _skip_rescan: bool = False,
        _skip_browser_authority_gate: bool = False,
        _install_store: Any | None = None,
        _runtime_store: Any | None = None,
    ) -> ExtensionEnableResultView:
        _ = service_scope(scope)
        resolved_install_store = (
            _install_store if _install_store is not None else self._context.install_store(bl)
        )
        resolved_runtime_store = (
            _runtime_store if _runtime_store is not None else self._context.runtime_store(bl)
        )
        extension_row = bl.get_extension(extension_id) if hasattr(bl, "get_extension") else None
        if enabled:
            from core.infrastructure.extension_runtime.extension_runtime import (
                default_extensions_root,
                extension_broken_reason,
            )

            if not _skip_rescan:
                try:
                    self.scan_extensions(
                        root=str(default_extensions_root()),
                        scope=scope,
                        approved_browser_authority_impact_hash=(
                            approved_browser_authority_impact_hash
                        ),
                        _bl=bl,
                    )
                    extension_row = (
                        bl.get_extension(extension_id)
                        if hasattr(bl, "get_extension")
                        else extension_row
                    )
                except ServicePreconditionError:
                    raise
                except Exception:
                    pass
            if isinstance(extension_row, Mapping):
                broken_reason = extension_broken_reason(dict(extension_row))
                if broken_reason:
                    raise ServiceValidationError(broken_reason)
                descriptor = dict(
                    extension_row.get("compiled_manifest") or extension_row.get("manifest") or {}
                )
                if not _skip_rescan:
                    active_root = self._managed_release.managed_simple_extension_root(extension_id)
                    from .command_lifecycle_activation import (
                        validate_managed_release_artifact,
                    )

                    validate_managed_release_artifact(
                        extension_id,
                        descriptor,
                        active_root=active_root,
                    )
                if (
                    str(descriptor.get("release_stage") or "active").strip().lower()
                    == "placeholder"
                ):
                    raise ServiceValidationError(
                        "Placeholder extensions cannot be enabled until their runtime is available."
                    )
                account_id = service_scope(scope).account_id or DEFAULT_ACCOUNT_ID
                extension_command = self._context.command_service(
                    bl,
                    install_store=resolved_install_store,
                    runtime_store=resolved_runtime_store,
                )
                extension_command.validate_release_dependencies_for_install(
                    release=SimpleNamespace(
                        extension_id=extension_id,
                        descriptor=descriptor,
                    ),
                    account_id=account_id,
                    runtime_store=resolved_runtime_store,
                    install_store=resolved_install_store,
                )
                from .health_gate import require_extension_health_ready

                require_extension_health_ready(descriptor)
                if not _skip_browser_authority_gate:
                    extension_command.authorize_enable_extension(
                        extension_id,
                        scope=scope,
                        approved_browser_authority_impact_hash=(
                            approved_browser_authority_impact_hash
                        ),
                    )
        if hasattr(bl, "get_extension") and not extension_row:
            runtime_extension, extension_detail = self._projection.runtime_extension_detail(
                bl=bl,
                extension_id=extension_id,
                scope=scope,
                install_store=resolved_install_store,
                runtime_store=resolved_runtime_store,
            )
            if runtime_extension is not None and extension_detail is not None:
                extension_command = self._context.command_service(
                    bl,
                    install_store=resolved_install_store,
                    runtime_store=resolved_runtime_store,
                )
                if enabled:
                    extension_command.enable_extension(
                        extension_id,
                        scope=scope,
                        approved_browser_authority_impact_hash=(
                            approved_browser_authority_impact_hash
                        ),
                    )
                else:
                    extension_command.disable_extension(extension_id, scope=scope)
                refreshed = self._context.query_service(
                    bl,
                    install_store=resolved_install_store,
                    runtime_store=resolved_runtime_store,
                ).get_extension(extension_id, scope=scope)
                return ExtensionEnableResultView(
                    extension=self._projection.runtime_extension_payload(refreshed)
                )
        try:
            if not hasattr(bl, "sync_enabled_extension_record"):
                raise ServiceError("Extension enablement is not available", status_code=500)
            from .extension_scan_lifecycle import sync_enabled_extension_with_llm_lifecycle

            extension = sync_enabled_extension_with_llm_lifecycle(
                bl,
                extension_id,
                version=version,
                enabled=enabled,
                config=config,
            )
        except ValueError as exc:
            message = str(exc)
            if "compile/validation errors" in message.lower():
                raise ServiceValidationError(message) from exc
            raise ServiceNotFoundError(message) from exc
        return ExtensionEnableResultView(extension=extension or {})


__all__ = ["ExtensionStateService"]
