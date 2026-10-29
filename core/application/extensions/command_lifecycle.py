from __future__ import annotations

import logging
from typing import Any

from core.application.management.command_models import (
    ExtensionGeneratedJobCreateInput,
    ExtensionGeneratedJobResultView,
    ExtensionToggleResultView,
    ExtensionUninstallResultView,
)
from core.application.management.errors import ServiceNotFoundError, ServiceValidationError
from core.application.management.scope import ScopeContext

from .command_context import ExtensionCommandContext
from .command_core import ExtensionCommandCore
from .command_install import ExtensionCommandInstall
from .command_lifecycle_activation import ExtensionLifecycleActivationOperations
from .command_lifecycle_dependencies import ExtensionLifecycleDependencyOperations
from .command_lifecycle_recovery import ExtensionLifecycleRecoveryOperations
from .extension_runtime import GeneratedExtensionJobRecord
from .lifecycle_ports import require_role as _require_role
from .service_support import _normalize_text, _service_scope

_logger = logging.getLogger(__name__)


class ExtensionCommandLifecycle:
    """Uninstall, cleanup, enable, and disable helpers."""

    def __init__(
        self,
        *,
        context: ExtensionCommandContext,
        core: ExtensionCommandCore,
        install: ExtensionCommandInstall,
    ) -> None:
        self._context = context
        self._core = core
        self._install = install
        self._recovery = ExtensionLifecycleRecoveryOperations(context=context, install=install)
        self._activation = ExtensionLifecycleActivationOperations(
            context=context,
            core=core,
            install=install,
            recovery=self._recovery,
        )
        self._dependencies = ExtensionLifecycleDependencyOperations(install=install)

    def create_generated_extension_job(
        self,
        inp: ExtensionGeneratedJobCreateInput,
        *,
        scope: ScopeContext | None = None,
    ) -> ExtensionGeneratedJobResultView:
        command = ExtensionGeneratedJobCreateInput.model_validate(inp)
        scope = _service_scope(scope)
        account_id = scope.account_id or "acc_default"
        visibility = _normalize_text(command.visibility).lower() or "private"
        if visibility != "private":
            raise ServiceValidationError("Generated extension requests must be private")
        runtime_store = self._context.runtime_store()
        record = runtime_store.create_generated_extension_job(
            GeneratedExtensionJobRecord(
                account_id=account_id,
                requester_user=scope.actor_id or "api",
                source_job_id=str(command.source_job_id or "").strip() or None,
                visibility="private",
                request_payload=dict(command.request_payload or {}),
            )
        )
        return ExtensionGeneratedJobResultView(
            status="created",
            generated_job_id=record.generated_job_id,
            account_id=record.account_id,
            source_job_id=record.source_job_id,
            visibility=record.visibility,
        )

    def uninstall_extension(
        self,
        extension_id: str,
        *,
        scope: ScopeContext | None = None,
    ) -> ExtensionUninstallResultView:
        scope = _service_scope(scope)
        account_id = scope.account_id or "acc_default"

        store = self._context.install_store()
        existing = (
            store.get(extension_id=extension_id, account_id=account_id)
            if hasattr(store, "get")
            else None
        )
        self._reject_required_dependents_for_lifecycle(
            extension_id=extension_id,
            account_id=account_id,
            action="uninstall",
            install_store=store,
            runtime_store=self._context.runtime_store(),
        )
        # Before the install is gone, while its release can still be read: an
        # uninstalled extension whose provider row stays enabled is a route the
        # operator can still save and that can never deliver — the same half-state
        # disable guards against, and the one uninstall used to leave behind.
        if existing is not None:
            self._sync_provider_registration(existing, enabled=False)
        removed = store.uninstall(extension_id=extension_id, account_id=account_id)
        if not removed:
            raise ServiceNotFoundError(f"Extension '{extension_id}' not found for account")
        if existing is not None:
            runtime_store = self._context.runtime_store()
            if hasattr(runtime_store, "delete_projections_for_install"):
                runtime_store.delete_projections_for_install(existing.install_id)
            if hasattr(runtime_store, "delete_entitlement"):
                runtime_store.delete_entitlement(existing.install_id)
            remaining = (
                store.get(extension_id=extension_id, account_id=account_id)
                if hasattr(store, "get")
                else None
            )
            if remaining is None and hasattr(runtime_store, "list_releases"):
                for release in runtime_store.list_releases(
                    extension_id=extension_id, account_id=account_id
                ):
                    self._cleanup_release_artifacts(release.release_id)
        return ExtensionUninstallResultView(extension_id=extension_id)

    @staticmethod
    def install_record_payload(existing: Any | None) -> dict[str, Any] | None:
        return ExtensionCommandLifecycle._install_record_payload(existing)

    @staticmethod
    def _install_record_payload(existing: Any | None) -> dict[str, Any] | None:
        if existing is None:
            return None
        return {
            "extension_id": existing.extension_id,
            "version": existing.version,
            "account_id": existing.account_id,
            "visibility": getattr(existing.visibility, "value", existing.visibility),
            "installed_by": existing.installed_by,
            "config": dict(existing.config or {}),
            "install_id": existing.install_id,
            "release_id": existing.release_id,
            "descriptor_schema_version": existing.descriptor_schema_version,
            "descriptor_origin": existing.descriptor_origin,
            "descriptor_source": existing.descriptor_source,
            "provenance": dict(existing.provenance or {}),
            "runtime_mode": existing.runtime_mode,
            "remote_base_url": existing.remote_base_url,
            "handshake_status": existing.handshake_status,
            "health_status": existing.health_status,
            "runtime_status": existing.runtime_status,
            "last_error": existing.last_error,
            "state": getattr(existing.state, "value", existing.state),
        }

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
        self._rollback_install_release_failure(
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

    def _rollback_install_release_failure(
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
        self._recovery.rollback_install_release_failure(
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

    def _remove_obsolete_install_projections(
        self,
        *,
        runtime_store: Any,
        account_id: str,
        install_id: str,
        release_id: str,
    ) -> None:
        self._recovery.remove_obsolete_install_projections(
            runtime_store=runtime_store,
            account_id=account_id,
            install_id=install_id,
            release_id=release_id,
        )

    def remove_obsolete_install_projections(
        self,
        *,
        runtime_store: Any,
        account_id: str,
        install_id: str,
        release_id: str,
    ) -> None:
        self._remove_obsolete_install_projections(
            runtime_store=runtime_store,
            account_id=account_id,
            install_id=install_id,
            release_id=release_id,
        )

    def cleanup_release_if_unreferenced(
        self,
        *,
        install_store: Any,
        release_id: str,
        extension_id: str,
    ) -> None:
        self._cleanup_release_if_unreferenced(
            install_store=install_store,
            release_id=release_id,
            extension_id=extension_id,
        )

    def _cleanup_release_if_unreferenced(
        self,
        *,
        install_store: Any,
        release_id: str,
        extension_id: str,
    ) -> None:
        self._recovery.cleanup_release_if_unreferenced(
            install_store=install_store,
            release_id=release_id,
            extension_id=extension_id,
        )

    def cleanup_release_artifacts(self, release_id: str) -> None:
        self._cleanup_release_artifacts(release_id)

    def _cleanup_release_artifacts(self, release_id: str) -> None:
        self._recovery.cleanup_release_artifacts(release_id)

    def enable_extension(
        self,
        extension_id: str,
        *,
        scope: ScopeContext | None = None,
        approved_browser_authority_impact_hash: str = "",
    ) -> ExtensionToggleResultView:
        return self._activation.enable_extension(
            extension_id,
            scope=scope,
            approved_browser_authority_impact_hash=approved_browser_authority_impact_hash,
        )

    def authorize_enable_extension(
        self,
        extension_id: str,
        *,
        scope: ScopeContext | None = None,
        approved_browser_authority_impact_hash: str = "",
    ) -> None:
        self._activation.authorize_enable_extension(
            extension_id,
            scope=scope,
            approved_browser_authority_impact_hash=approved_browser_authority_impact_hash,
        )

    def _sync_legacy_enabled_projection(self, extension_id: str, *, enabled: bool) -> bool:
        from .command_lifecycle_activation import sync_legacy_enabled_projection

        return sync_legacy_enabled_projection(self._context.logger(), extension_id, enabled=enabled)

    def disable_extension(
        self,
        extension_id: str,
        *,
        scope: ScopeContext | None = None,
    ) -> ExtensionToggleResultView:
        scope = _service_scope(scope)
        _require_role(scope, "admin")
        account_id = scope.account_id or "acc_default"
        store = self._context.install_store()
        self._reject_required_dependents_for_lifecycle(
            extension_id=extension_id,
            account_id=account_id,
            action="disable",
            install_store=store,
            runtime_store=self._context.runtime_store(),
        )
        existing = (
            store.get(extension_id=extension_id, account_id=account_id)
            if hasattr(store, "get")
            else None
        )
        if not existing:
            raise ServiceNotFoundError(f"Extension '{extension_id}' not found")
        # The provider goes first. The dangerous half-applied state is "extension
        # disabled, provider still enabled" — a route the operator can still save
        # and that can never deliver. The other order leaves "extension enabled,
        # provider disabled", which refuses the save visibly and is undone below.
        descriptor, previous_provider = self._provider_rollback_state(existing)
        self._sync_provider_registration(existing, enabled=False)
        try:
            state_synced = self._sync_legacy_enabled_projection(extension_id, enabled=False)
            record = (
                store.get(extension_id=extension_id, account_id=account_id)
                if state_synced and hasattr(store, "get")
                else store.disable(extension_id=extension_id, account_id=account_id)
            )
            if not record:
                raise ServiceNotFoundError(f"Extension '{extension_id}' not found")
            runtime_store = self._context.runtime_store()
            if hasattr(runtime_store, "set_projection_active_for_install"):
                runtime_store.set_projection_active_for_install(record.install_id, is_active=False)
            self._install.deactivate_projected_skills(
                install_id=record.install_id, account_id=account_id
            )
        except BaseException:
            self._restore_provider_state(descriptor, previous_provider)
            raise
        return ExtensionToggleResultView(extension_id=extension_id, state="disabled")

    def _provider_rollback_state(self, record: Any) -> tuple[dict[str, Any], Any]:
        from .provider_registration import provider_state_snapshot

        descriptor = self._descriptor_for_record(record)
        return descriptor, provider_state_snapshot(self._context.logger(), descriptor)

    def _restore_provider_state(self, descriptor: dict[str, Any], snapshot: Any) -> None:
        from .provider_registration import restore_provider_state

        restore_provider_state(
            self._context.logger(),
            descriptor,
            snapshot,
            # The disable failure is what the caller sees; a restore that also
            # failed leaves the catalog disagreeing with the extension.
            on_error=lambda message: _logger.error("extension disable: %s", message),
        )

    def _descriptor_for_record(self, record: Any) -> dict[str, Any]:
        from .command_lifecycle_activation import ExtensionLifecycleActivationOperations
        from .provider_registration import provider_descriptor_for_extension

        release = ExtensionLifecycleActivationOperations._release_for_record(
            self._context.runtime_store(), record
        )
        return provider_descriptor_for_extension(
            self._context.logger(),
            extension_id=_normalize_text(getattr(record, "extension_id", "")),
            release_descriptor=getattr(release, "descriptor", None),
        )

    def _sync_provider_registration(self, record: Any, *, enabled: bool) -> None:
        from .provider_registration import set_provider_enabled_for_extension

        set_provider_enabled_for_extension(
            self._context.logger(), self._descriptor_for_record(record), enabled=enabled
        )

    def _reject_required_dependents_for_lifecycle(
        self,
        *,
        extension_id: str,
        account_id: str,
        action: str,
        install_store: Any,
        runtime_store: Any,
    ) -> None:
        self._dependencies.reject_required_dependents(
            extension_id=extension_id,
            account_id=account_id,
            action=action,
            install_store=install_store,
            runtime_store=runtime_store,
        )

    def _required_dependents_for_lifecycle(
        self,
        *,
        extension_id: str,
        account_id: str,
        install_store: Any,
        runtime_store: Any,
    ) -> list[dict[str, Any]]:
        return self._dependencies.required_dependents(
            extension_id=extension_id,
            account_id=account_id,
            install_store=install_store,
            runtime_store=runtime_store,
        )

    def _preserve_required_dependents_for_uninstall(
        self,
        *,
        extension_id: str,
        account_id: str,
        install_store: Any,
        runtime_store: Any,
    ) -> None:
        self._dependencies.preserve_required_dependents(
            extension_id=extension_id,
            account_id=account_id,
            install_store=install_store,
            runtime_store=runtime_store,
        )


__all__ = ["ExtensionCommandLifecycle"]
