"""Composed application service shared by extension API and CLI transports."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from core.application.management.command_models import (
    ExtensionActionResultView,
    ExtensionArchiveInstallResultView,
    ExtensionArchiveUninstallResultView,
    ExtensionEnableResultView,
    ExtensionQueryResultView,
    ExtensionScanResultView,
)
from core.application.management.query_models import ExtensionArchiveLifecycleAvailabilityView
from core.application.management.scope import ScopeContext
from core.infrastructure.extension_runtime.browser_permissions import (
    refresh_browser_permissions_for_lifecycle,
)

from .archive_extraction_service import (
    ExtensionArchiveExtractionService,
    safe_extension_archive_file_mode,
)
from .archive_install_service import ExtensionArchiveInstallService
from .archive_lifecycle_capability import ExtensionArchiveLifecycleCapability
from .archive_new_install_service import ExtensionArchiveNewInstallService
from .archive_update_service import ExtensionArchiveUpdateService
from .archive_validation_service import ExtensionArchiveValidationService
from .binding_cleanup_service import ExtensionCleanupService
from .binding_context import (
    ExtensionInstallStoreProvider,
    ExtensionLifecycleContext,
    ExtensionQueryServiceFactory,
    ExtensionRuntimeStoreProvider,
)
from .binding_projection_service import ExtensionBindingProjectionService
from .direct_effect_recovery import DirectEffectRecoveryService
from .direct_effect_recovery_models import (
    DirectEffectDecisionRequest,
    DirectEffectDecisionView,
    DirectEffectListView,
)
from .extension_state_service import ExtensionStateService
from .managed_release_service import ManagedReleaseService
from .operation_service import ExtensionOperationHandlerMode, ExtensionOperationService
from .operation_support import (
    ExtensionCommandDispatchServiceFactory,
    ExtensionEventCommandServiceFactory,
)
from .uninstall_inventory_repository import ExtensionUninstallInventoryRepository
from .uninstall_persistence_service import ExtensionUninstallPersistenceService
from .uninstall_plan_service import ExtensionUninstallPlanService
from .uninstall_service import ExtensionUninstallService


class ExtensionBindingService:
    """Stable extension command facade backed by focused application services."""

    def __init__(
        self,
        *,
        get_logger: Callable[[], Any],
        get_operation_store: Callable[[], Any],
        resolve_extension_runtime_record: Callable[[str], tuple[dict[str, Any] | None, Any]],
        extension_identifier: Callable[[dict[str, Any] | None], str],
        extension_operation_handler_mode: ExtensionOperationHandlerMode,
        extension_operation_failure_status: Callable[[dict[str, Any] | None], int],
        get_project_by_id: Callable[[str], Any] | None = None,
        event_command_service_factory: ExtensionEventCommandServiceFactory | None = None,
        command_dispatch_service_factory: ExtensionCommandDispatchServiceFactory | None = None,
        get_extension_install_store: ExtensionInstallStoreProvider | None = None,
        get_extension_runtime_store: ExtensionRuntimeStoreProvider | None = None,
        extension_query_service_factory: ExtensionQueryServiceFactory | None = None,
        artifact_plane: Any | None = None,
        get_artifact_plane: Callable[[], Any] | None = None,
        archive_lifecycle_capability: ExtensionArchiveLifecycleCapability | None = None,
    ) -> None:
        self._get_logger = get_logger
        self._archive_lifecycle_capability = (
            archive_lifecycle_capability or ExtensionArchiveLifecycleCapability()
        )
        self._context = ExtensionLifecycleContext(
            get_extension_install_store=get_extension_install_store,
            get_extension_runtime_store=get_extension_runtime_store,
            extension_query_service_factory=extension_query_service_factory,
        )
        self._managed_release_service = ManagedReleaseService(context=self._context)
        self._cleanup_service = ExtensionCleanupService(
            context=self._context,
            managed_release=self._managed_release_service,
        )
        self._projection_service = ExtensionBindingProjectionService(
            context=self._context,
            managed_release=self._managed_release_service,
        )
        self._state_service = ExtensionStateService(
            context=self._context,
            projection=self._projection_service,
            cleanup=self._cleanup_service,
            managed_release=self._managed_release_service,
            get_logger=get_logger,
        )
        self._archive_new_install_service = ExtensionArchiveNewInstallService(
            context=self._context,
            projection=self._projection_service,
            managed_release=self._managed_release_service,
            cleanup=self._cleanup_service,
            state=self._state_service,
            get_logger=get_logger,
        )
        self._archive_update_service = ExtensionArchiveUpdateService(
            context=self._context,
            projection=self._projection_service,
            managed_release=self._managed_release_service,
            cleanup=self._cleanup_service,
            state=self._state_service,
        )
        self._archive_validation_service = ExtensionArchiveValidationService(
            context=self._context,
            projection=self._projection_service,
        )
        self._archive_extraction_service = ExtensionArchiveExtractionService(
            file_mode=safe_extension_archive_file_mode,
        )
        self._archive_install_service = ExtensionArchiveInstallService(
            context=self._context,
            projection=self._projection_service,
            managed_release=self._managed_release_service,
            state=self._state_service,
            new_install=self._archive_new_install_service,
            update=self._archive_update_service,
            get_logger=get_logger,
            validation=self._archive_validation_service,
            extraction=self._archive_extraction_service,
        )
        self._uninstall_inventory_repository = ExtensionUninstallInventoryRepository()
        self._uninstall_plan_service = ExtensionUninstallPlanService(
            context=self._context,
            state=self._state_service,
            inventory=self._uninstall_inventory_repository,
            get_logger=get_logger,
        )
        self._uninstall_persistence_service = ExtensionUninstallPersistenceService(
            context=self._context,
            cleanup=self._cleanup_service,
            managed_release=self._managed_release_service,
            plan=self._uninstall_plan_service,
        )
        self._uninstall_service = ExtensionUninstallService(
            context=self._context,
            cleanup=self._cleanup_service,
            projection=self._projection_service,
            managed_release=self._managed_release_service,
            plan=self._uninstall_plan_service,
            persistence=self._uninstall_persistence_service,
            state=self._state_service,
            get_logger=get_logger,
            require_archive_mutation_available=lambda: self.require_archive_lifecycle_available(
                operation="uninstall"
            ),
        )
        self._operation_service = ExtensionOperationService(
            get_logger=get_logger,
            get_operation_store=get_operation_store,
            get_project_by_id=get_project_by_id,
            resolve_extension_runtime_record=resolve_extension_runtime_record,
            extension_operation_handler_mode=extension_operation_handler_mode,
            extension_operation_failure_status=extension_operation_failure_status,
            event_command_service_factory=event_command_service_factory,
            command_dispatch_service_factory=command_dispatch_service_factory,
            artifact_plane=artifact_plane,
            get_artifact_plane=get_artifact_plane,
        )
        self._direct_effect_recovery = DirectEffectRecoveryService(
            get_operation_store=get_operation_store,
            get_install_store=get_logger,
            get_project_by_id=get_project_by_id,
        )
        self._extension_identifier = extension_identifier
        self._event_command_service_factory = event_command_service_factory

    def scan_extensions(
        self,
        *,
        root: str = "",
        scope: ScopeContext | None = None,
        _bl: Any | None = None,
    ) -> ExtensionScanResultView:
        result = self._state_service.scan_extensions(root=root, scope=scope, _bl=_bl)
        refresh_browser_permissions_for_lifecycle(
            bl=_bl if _bl is not None else self._get_logger(),
            lifecycle_context=self._context,
        )
        return result

    def sync_extension_scan(
        self,
        scanned: list[dict[str, Any]],
        *,
        _bl: Any,
    ) -> Any:
        """Apply already-scanned rows while an outer lifecycle lock is held."""
        from .extension_scan_lifecycle import sync_extension_scan_with_llm_lifecycle

        result = sync_extension_scan_with_llm_lifecycle(_bl, scanned)
        refresh_browser_permissions_for_lifecycle(bl=_bl, lifecycle_context=self._context)
        return result

    def install_extension_archive_path(
        self,
        *,
        archive_path: Path,
        scope: ScopeContext | None = None,
        approved_browser_authority_impact_hash: str = "",
    ) -> ExtensionArchiveInstallResultView:
        self.require_archive_lifecycle_available(operation="install_update")
        try:
            result = self._archive_install_service.install_extension_archive_path(
                archive_path=archive_path,
                scope=scope,
                approved_browser_authority_impact_hash=approved_browser_authority_impact_hash,
            )
        except OSError as exc:
            self._archive_lifecycle_capability.translate_managed_root_error(
                exc, operation="install_update"
            )
        refresh_browser_permissions_for_lifecycle(
            bl=self._get_logger(), lifecycle_context=self._context
        )
        return result

    def install_extension_archive(
        self,
        *,
        filename: str,
        content: bytes,
        scope: ScopeContext | None = None,
        approved_browser_authority_impact_hash: str = "",
    ) -> ExtensionArchiveInstallResultView:
        self.require_archive_lifecycle_available(operation="install_update")
        try:
            result = self._archive_install_service.install_extension_archive(
                filename=filename,
                content=content,
                scope=scope,
                approved_browser_authority_impact_hash=approved_browser_authority_impact_hash,
            )
        except OSError as exc:
            self._archive_lifecycle_capability.translate_managed_root_error(
                exc, operation="install_update"
            )
        refresh_browser_permissions_for_lifecycle(
            bl=self._get_logger(), lifecycle_context=self._context
        )
        return result

    def preview_extension_uninstall(
        self,
        extension_id: str,
        *,
        scope: ScopeContext | None = None,
    ) -> dict[str, Any]:
        return self._uninstall_plan_service.preview_extension_uninstall(
            extension_id,
            scope=scope,
        )

    def archive_lifecycle_availability(self) -> ExtensionArchiveLifecycleAvailabilityView:
        return self._archive_lifecycle_capability.availability()

    def require_archive_lifecycle_available(self, *, operation: str) -> None:
        self._archive_lifecycle_capability.require_available(operation=operation)

    def uninstall_extension(
        self,
        extension_id: str,
        *,
        scope: ScopeContext | None = None,
        plan_revision: str = "",
    ) -> ExtensionArchiveUninstallResultView:
        try:
            result = self._uninstall_service.uninstall_extension(
                extension_id,
                scope=scope,
                plan_revision=plan_revision,
            )
        except OSError as exc:
            self._archive_lifecycle_capability.translate_managed_root_error(
                exc, operation="uninstall"
            )
        refresh_browser_permissions_for_lifecycle(
            bl=self._get_logger(), lifecycle_context=self._context
        )
        return result

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
        result = self._state_service.set_enabled_state(
            extension_id,
            version=version,
            enabled=enabled,
            config=config,
            scope=scope,
            approved_browser_authority_impact_hash=(approved_browser_authority_impact_hash),
            _skip_rescan=_skip_rescan,
        )
        refresh_browser_permissions_for_lifecycle(
            bl=self._get_logger(), lifecycle_context=self._context
        )
        return result

    def list_external_effects(
        self, install_id: str, *, project_id: str, scope: ScopeContext | None = None
    ) -> DirectEffectListView:
        return self._direct_effect_recovery.list_external_effects(
            install_id, project_id=project_id, scope=scope
        )

    def resolve_external_effect(
        self,
        install_id: str,
        effect_id: str,
        body: DirectEffectDecisionRequest,
        *,
        project_id: str,
        scope: ScopeContext | None = None,
    ) -> DirectEffectDecisionView:
        return self._direct_effect_recovery.resolve_external_effect(
            install_id, effect_id, body, project_id=project_id, scope=scope
        )

    def execute_action(
        self,
        extension_id: str,
        action_id: str,
        body: Any,
        *,
        scope: ScopeContext | None = None,
        idempotency_key: str = "",
    ) -> ExtensionActionResultView:
        return self._operation_service.execute_action(
            extension_id,
            action_id,
            body,
            scope=scope,
            idempotency_key=idempotency_key,
        )

    def execute_query(
        self,
        extension_id: str,
        query_id: str,
        body: Any,
        *,
        scope: ScopeContext | None = None,
    ) -> ExtensionQueryResultView:
        return self._operation_service.execute_query(
            extension_id,
            query_id,
            body,
            scope=scope,
        )


__all__ = ["ExtensionBindingService"]
