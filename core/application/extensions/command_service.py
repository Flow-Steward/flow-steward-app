"""Concrete production command service for extension lifecycle operations."""

from __future__ import annotations

import contextlib
from collections.abc import Callable, Iterator
from typing import Any

from core.application.management.command_models_extensions import (
    ExtensionDescriptorImportFileInput,
    ExtensionDescriptorImportUrlInput,
    ExtensionDescriptorValidateInput,
    ExtensionDescriptorValidateResultView,
    ExtensionGeneratedJobCreateInput,
    ExtensionGeneratedJobResultView,
    ExtensionInstallInput,
    ExtensionInstallResultView,
    ExtensionReleaseImportResultView,
    ExtensionReleaseInstallInput,
    ExtensionRuntimeHealthResultView,
    ExtensionRuntimeTokenInput,
    ExtensionRuntimeTokenResultView,
    ExtensionToggleResultView,
    ExtensionUninstallResultView,
)
from core.application.management.errors import ServiceConflictError
from core.application.management.scope import ScopeContext

from .browser_authority_approval import PreparedBrowserAuthorityApproval
from .command_context import ExtensionCommandContext
from .command_core import ExtensionCommandCore
from .command_install import ExtensionCommandInstall
from .command_lifecycle import ExtensionCommandLifecycle


class ExtensionCommandService:
    """Command-side extension lifecycle service used by production wiring."""

    def __init__(
        self,
        *,
        get_logger: Callable[[], Any],
        get_install_store: Callable[[], Any] | None = None,
        get_runtime_store: Callable[[], Any] | None = None,
    ) -> None:
        self._context = ExtensionCommandContext(
            get_logger=get_logger,
            get_install_store=get_install_store,
            get_runtime_store=get_runtime_store,
        )
        self._core = ExtensionCommandCore(
            context=self._context,
            install_release=self._delegate_install_release,
            cleanup_release_artifacts=self._delegate_cleanup_release_artifacts,
        )
        self._install = ExtensionCommandInstall(
            context=self._context,
            core=self._core,
            lifecycle=lambda: self._lifecycle,
        )
        self._lifecycle = ExtensionCommandLifecycle(
            context=self._context,
            core=self._core,
            install=self._install,
        )

    def _delegate_install_release(
        self,
        inp: ExtensionReleaseInstallInput,
        *,
        scope: ScopeContext | None = None,
        allow_same_version_enrichment: bool = False,
        _browser_authority_handoff: PreparedBrowserAuthorityApproval | None = None,
    ) -> ExtensionInstallResultView:
        return self._install.install_release(
            inp,
            scope=scope,
            allow_same_version_enrichment=allow_same_version_enrichment,
            _browser_authority_handoff=_browser_authority_handoff,
        )

    def _delegate_cleanup_release_artifacts(self, release_id: str) -> None:
        self._lifecycle.cleanup_release_artifacts(release_id)

    def _logger(self) -> Any:
        return self._context.logger()

    def _install_store(self) -> Any:
        return self._context.install_store()

    def _runtime_store(self) -> Any:
        return self._context.runtime_store()

    def _extension_persistence_connection(self) -> Any:
        return self._context.persistence_connection()

    @staticmethod
    def _store_connection(store: Any) -> Any:
        return ExtensionCommandContext.store_connection(store)

    def _validated_lifecycle_connection(self, bl: Any, stores: tuple[Any, Any]) -> Any:
        return self._context.validated_connection(bl, stores)

    @contextlib.contextmanager
    def _extension_lifecycle_gate(self) -> Iterator[None]:
        from core.infrastructure.extension_runtime.lifecycle_lock import (
            extension_lifecycle_lock,
        )

        bl = self._context.logger()
        if bl is None:
            bl = self._context.logger()
        with self._context.pin_logger(bl):
            stores = (self._context.install_store(), self._context.runtime_store())
        connection = self._validated_lifecycle_connection(bl, stores)
        with (
            self._context.pin(logger=bl, stores=stores, connection=connection),
            extension_lifecycle_lock(connection=connection) as acquired,
        ):
            if not acquired:
                raise ServiceConflictError(
                    "Another extension lifecycle operation is already in progress"
                )
            yield

    def import_descriptor_file(
        self,
        inp: ExtensionDescriptorImportFileInput,
        *,
        scope: ScopeContext | None = None,
        source_kind_override: str = "",
        allow_same_version_enrichment: bool = False,
        _browser_authority_handoff: PreparedBrowserAuthorityApproval | None = None,
    ) -> ExtensionReleaseImportResultView:
        with self._extension_lifecycle_gate():
            return self._core.import_descriptor_file(
                inp,
                scope=scope,
                source_kind_override=source_kind_override,
                allow_same_version_enrichment=allow_same_version_enrichment,
                _browser_authority_handoff=_browser_authority_handoff,
            )

    def import_descriptor_url(
        self,
        inp: ExtensionDescriptorImportUrlInput,
        *,
        scope: ScopeContext | None = None,
    ) -> ExtensionReleaseImportResultView:
        with self._extension_lifecycle_gate():
            return self._core.import_descriptor_url(inp, scope=scope)

    def install_release(
        self,
        inp: ExtensionReleaseInstallInput,
        *,
        scope: ScopeContext | None = None,
        allow_same_version_enrichment: bool = False,
        _browser_authority_handoff: PreparedBrowserAuthorityApproval | None = None,
    ) -> ExtensionInstallResultView:
        with self._extension_lifecycle_gate():
            return self._install.install_release(
                inp,
                scope=scope,
                allow_same_version_enrichment=allow_same_version_enrichment,
                _browser_authority_handoff=_browser_authority_handoff,
            )

    def run_install_handshake(
        self,
        *,
        install_id: str,
        scope: ScopeContext | None = None,
    ) -> ExtensionRuntimeHealthResultView:
        with self._extension_lifecycle_gate():
            return self._install.run_install_handshake(install_id=install_id, scope=scope)

    def uninstall_extension(
        self,
        extension_id: str,
        *,
        scope: ScopeContext | None = None,
    ) -> ExtensionUninstallResultView:
        with self._extension_lifecycle_gate():
            return self._lifecycle.uninstall_extension(extension_id, scope=scope)

    def enable_extension(
        self,
        extension_id: str,
        *,
        scope: ScopeContext | None = None,
        approved_browser_authority_impact_hash: str = "",
    ) -> ExtensionToggleResultView:
        with self._extension_lifecycle_gate(), self._extension_lifecycle_transaction():
            return self._lifecycle.enable_extension(
                extension_id,
                scope=scope,
                approved_browser_authority_impact_hash=(approved_browser_authority_impact_hash),
            )

    def authorize_enable_extension(
        self,
        extension_id: str,
        *,
        scope: ScopeContext | None = None,
        approved_browser_authority_impact_hash: str = "",
    ) -> None:
        with self._extension_lifecycle_gate(), self._extension_lifecycle_transaction():
            self._lifecycle.authorize_enable_extension(
                extension_id,
                scope=scope,
                approved_browser_authority_impact_hash=(approved_browser_authority_impact_hash),
            )

    def disable_extension(
        self,
        extension_id: str,
        *,
        scope: ScopeContext | None = None,
    ) -> ExtensionToggleResultView:
        with self._extension_lifecycle_gate(), self._extension_lifecycle_transaction():
            return self._lifecycle.disable_extension(extension_id, scope=scope)

    @contextlib.contextmanager
    def _extension_lifecycle_transaction(self) -> Iterator[None]:
        connection = self._context.persistence_connection()
        if connection is None:
            yield
            return
        from core.infrastructure.db.db_pool import (
            active_transaction_connection,
            managed_transaction_connection,
            transaction_connection,
        )

        if (
            active_transaction_connection(connection) is not None
            and managed_transaction_connection(connection) is None
        ):
            yield
            return
        with transaction_connection(connection):
            yield

    def validate_descriptor(
        self,
        inp: ExtensionDescriptorValidateInput,
        *,
        scope: ScopeContext | None = None,
    ) -> ExtensionDescriptorValidateResultView:
        return self._core.validate_descriptor(inp, scope=scope)

    def descriptor_validation_or_error(
        self, result: ExtensionDescriptorValidateResultView
    ) -> dict[str, Any]:
        return self._core.descriptor_validation_or_error(result)

    def install_extension(
        self,
        inp: ExtensionInstallInput,
        *,
        scope: ScopeContext | None = None,
    ) -> ExtensionInstallResultView:
        return self._install.install_extension(inp, scope=scope)

    def validate_release_dependencies_for_install(
        self,
        *,
        release: Any,
        account_id: str,
        runtime_store: Any,
        install_store: Any,
    ) -> list[dict[str, Any]]:
        return self._install.validate_release_dependencies_for_install(
            release=release,
            account_id=account_id,
            runtime_store=runtime_store,
            install_store=install_store,
        )

    def issue_runtime_token(
        self,
        inp: ExtensionRuntimeTokenInput,
        *,
        scope: ScopeContext | None = None,
    ) -> ExtensionRuntimeTokenResultView:
        return self._install.issue_runtime_token(inp, scope=scope)

    def create_generated_extension_job(
        self,
        inp: ExtensionGeneratedJobCreateInput,
        *,
        scope: ScopeContext | None = None,
    ) -> ExtensionGeneratedJobResultView:
        return self._lifecycle.create_generated_extension_job(inp, scope=scope)

    def install_record_payload(self, existing: Any | None) -> dict[str, Any] | None:
        return self._lifecycle.install_record_payload(existing)

    def rollback_install_release_failure(
        self,
        *,
        install_store: Any,
        runtime_store: Any,
        extension_id: str,
        account_id: str,
        failed_install_id: str,
        previous: dict[str, Any] | None,
        previous_release: Any | None = None,
        previous_entitlement: Any | None = None,
        previous_projections: list[Any] | None = None,
    ) -> None:
        self._lifecycle.rollback_install_release_failure(
            install_store=install_store,
            runtime_store=runtime_store,
            extension_id=extension_id,
            account_id=account_id,
            failed_install_id=failed_install_id,
            previous=previous,
            previous_release=previous_release,
            previous_entitlement=previous_entitlement,
            previous_projections=previous_projections,
        )

    def cleanup_release_if_unreferenced(
        self,
        *,
        install_store: Any,
        release_id: str,
        extension_id: str,
    ) -> None:
        self._lifecycle.cleanup_release_if_unreferenced(
            install_store=install_store,
            release_id=release_id,
            extension_id=extension_id,
        )

    def cleanup_release_artifacts(self, release_id: str) -> None:
        self._lifecycle.cleanup_release_artifacts(release_id)


__all__ = ["ExtensionCommandService"]
