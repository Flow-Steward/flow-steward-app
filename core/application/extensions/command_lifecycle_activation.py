from __future__ import annotations

import copy
import logging
from pathlib import Path
from typing import Any

from core.application.auth.config import DEFAULT_ACCOUNT_ID
from core.application.extensions.provider_registration import (
    provider_descriptor_for_extension,
    provider_state_snapshot,
    register_provider_from_extension,
    restore_provider_state,
)
from core.application.management.command_models import (
    ExtensionDescriptorImportFileInput,
    ExtensionToggleResultView,
)
from core.application.management.errors import ServiceNotFoundError, ServiceValidationError
from core.application.management.scope import ScopeContext

from .command_context import ExtensionCommandContext
from .command_core import ExtensionCommandCore
from .command_install import ExtensionCommandInstall
from .command_lifecycle_recovery import ExtensionLifecycleRecoveryOperations
from .enable_browser_authority import authorize_extension_enable_browser_authority
from .extension_activation_validation import (
    validate_extension_activation,
    validate_managed_release_artifact,
)
from .health_gate import require_extension_health_ready
from .lifecycle_ports import require_global_extension_lifecycle_scope
from .service_support import _normalize_text, _service_scope


def sync_legacy_enabled_projection(bl: Any, extension_id: str, *, enabled: bool) -> bool:
    """Keep the legacy enabled_extensions projection aligned with install state.

    Runtime invocation still gates on the legacy enabled flag, so lifecycle
    transitions must write both stores until that reader is migrated.
    """
    if bl is None or not (
        hasattr(bl, "sync_enabled_extension_record")
        and hasattr(bl, "extension_lifecycle_transaction")
    ):
        return False
    from .extension_scan_lifecycle import _apply_llm_lifecycle_changes

    try:
        with bl.extension_lifecycle_transaction() as transaction_conn:
            result = bl.sync_enabled_extension_record(
                transaction_conn,
                extension_id,
                enabled=enabled,
                preserve_existing_install=True,
                lifecycle_state_authoritative=True,
            )
            if isinstance(result, tuple) and len(result) == 2:
                _apply_llm_lifecycle_changes(transaction_conn, result[1])
                normalized_extension_id = _normalize_text(result[0]) or extension_id
                if not enabled:
                    from .oauth_lifecycle import (
                        invalidate_install_oauth_on_disable,
                        resolve_install_id_for_disable,
                    )

                    install_id = resolve_install_id_for_disable(
                        transaction_conn,
                        extension_id=normalized_extension_id,
                        account_id=DEFAULT_ACCOUNT_ID,
                    )

                    if install_id:
                        invalidate_install_oauth_on_disable(
                            transaction_conn,
                            install_id=install_id,
                            extension_id=normalized_extension_id,
                            account_id=DEFAULT_ACCOUNT_ID,
                        )
                return True
    except ValueError as exc:
        if str(exc).lower().startswith("unknown extension"):
            return False
        raise ServiceValidationError(str(exc)) from exc
    return False


_logger = logging.getLogger(__name__)


class ExtensionLifecycleActivationOperations:
    """Enable an extension and restore its prior projections on failure."""

    def __init__(
        self,
        *,
        context: ExtensionCommandContext,
        core: ExtensionCommandCore,
        install: ExtensionCommandInstall,
        recovery: ExtensionLifecycleRecoveryOperations,
    ) -> None:
        self._context = context
        self._core = core
        self._install = install
        self._recovery = recovery

    def enable_extension(
        self,
        extension_id: str,
        *,
        scope: ScopeContext | None = None,
        approved_browser_authority_impact_hash: str = "",
    ) -> ExtensionToggleResultView:
        scope, account_id, store, runtime_store, record, release = self._prepare_enable_extension(
            extension_id,
            scope=scope,
            approved_browser_authority_impact_hash=approved_browser_authority_impact_hash,
        )
        previous_state = _normalize_text(getattr(record, "state", "")) or "active"
        previous_projections = self._install_projections(
            runtime_store=runtime_store,
            account_id=account_id,
            install_id=record.install_id,
        )
        descriptor = self._provider_descriptor_for_activation(
            release=release,
            extension_id=extension_id,
        )
        previous_provider = provider_state_snapshot(self._context.logger(), descriptor)
        self._activate_with_rollback(
            extension_id=extension_id,
            account_id=account_id,
            store=store,
            runtime_store=runtime_store,
            record=record,
            release=release,
            descriptor=descriptor,
            previous_state=previous_state,
            previous_projections=previous_projections,
            previous_provider=previous_provider,
        )
        return ExtensionToggleResultView(extension_id=extension_id, state="active")

    def _provider_descriptor_for_activation(
        self,
        *,
        release: Any | None,
        extension_id: str,
    ) -> dict[str, Any]:
        return provider_descriptor_for_extension(
            self._context.logger(),
            extension_id=extension_id,
            release_descriptor=getattr(release, "descriptor", None),
        )

    def _activate_with_rollback(
        self,
        *,
        extension_id: str,
        account_id: str,
        store: Any,
        runtime_store: Any,
        record: Any,
        release: Any | None,
        descriptor: dict[str, Any],
        previous_state: str,
        previous_projections: list[Any],
        previous_provider: dict[str, Any] | None,
    ) -> None:
        try:
            self._project_release_for_activation(
                runtime_store=runtime_store,
                record=record,
                release=release,
                account_id=account_id,
            )
            state_synced = sync_legacy_enabled_projection(
                self._context.logger(), extension_id, enabled=True
            )
            if state_synced and hasattr(store, "get"):
                updated = store.get(extension_id=extension_id, account_id=account_id)
                if updated is not None:
                    record = updated
            elif hasattr(store, "set_state"):
                updated = store.set_state(
                    extension_id=extension_id, account_id=account_id, state="active"
                )
                if updated is not None:
                    record = updated
            else:
                record.state = "active"
            if hasattr(runtime_store, "set_projection_active_for_install"):
                runtime_store.set_projection_active_for_install(record.install_id, is_active=True)
            # Last, so nothing that can fail runs after it: an extension that
            # declares itself a provider becomes one from its own typed manifest
            # fields. A failure raises and the whole enable unwinds, because
            # without the row a notification route cannot be saved or delivered —
            # an "enabled" provider missing from the catalog looks available and
            # silently does nothing.
            register_provider_from_extension(self._context.logger(), descriptor)
        except BaseException:
            self._restore_activation_state(
                store=store,
                runtime_store=runtime_store,
                record=record,
                release=release,
                account_id=account_id,
                previous_state=previous_state,
                previous_projections=previous_projections,
            )
            restore_provider_state(
                self._context.logger(),
                descriptor,
                previous_provider,
                # The enable failure is what the caller will see; a restore that
                # also failed leaves the catalog disagreeing with the extension and
                # has to be findable rather than swallowed.
                on_error=lambda message: _logger.error("extension %s: %s", extension_id, message),
            )
            raise

    def authorize_enable_extension(
        self,
        extension_id: str,
        *,
        scope: ScopeContext | None = None,
        approved_browser_authority_impact_hash: str = "",
    ) -> None:
        self._prepare_enable_extension(
            extension_id,
            scope=scope,
            approved_browser_authority_impact_hash=approved_browser_authority_impact_hash,
            validate_activation=False,
        )

    def _prepare_enable_extension(
        self,
        extension_id: str,
        *,
        scope: ScopeContext | None,
        approved_browser_authority_impact_hash: str,
        validate_activation: bool = True,
    ) -> tuple[ScopeContext, str, Any, Any, Any, Any]:
        scope = _service_scope(scope)
        require_global_extension_lifecycle_scope(scope)
        account_id = scope.account_id or "acc_default"
        store = self._context.install_store()
        record = store.get(extension_id=extension_id, account_id=account_id)
        if not record:
            raise ServiceNotFoundError(f"Extension '{extension_id}' not found")
        runtime_store = self._context.runtime_store()
        release = self._release_for_record(runtime_store, record)
        record, release = self._enrich_scanned_bundle_release(
            record=record,
            release=release,
            scope=scope,
            account_id=account_id,
            install_store=store,
            approved_browser_authority_impact_hash=(approved_browser_authority_impact_hash),
        )
        record = authorize_extension_enable_browser_authority(
            extension_id=extension_id,
            scope=scope,
            account_id=account_id,
            store=store,
            record=record,
            release=release,
            approved_impact_hash=approved_browser_authority_impact_hash,
        )
        if not validate_activation:
            return scope, account_id, store, runtime_store, record, release
        validate_extension_activation(record, release)
        if release is not None:
            self._install.validate_release_dependencies_for_install(
                release=release,
                account_id=account_id,
                runtime_store=runtime_store,
                install_store=store,
            )
            require_extension_health_ready(dict(release.descriptor or {}))
        return scope, account_id, store, runtime_store, record, release

    def _project_release_for_activation(
        self,
        *,
        runtime_store: Any,
        record: Any,
        release: Any | None,
        account_id: str,
    ) -> None:
        if release is None:
            return
        self._install.project_release_for_install(
            install_record=record,
            release=release,
            account_id=account_id,
        )
        self._recovery.remove_obsolete_install_projections(
            runtime_store=runtime_store,
            account_id=account_id,
            install_id=record.install_id,
            release_id=release.release_id,
        )
        self._install.materialize_dataset_execution_bridges(
            install_record=record,
            release=release,
            account_id=account_id,
        )

    @staticmethod
    def _release_for_record(runtime_store: Any, record: Any) -> Any | None:
        release_id = _normalize_text(getattr(record, "release_id", ""))
        if not release_id or not hasattr(runtime_store, "get_release"):
            return None
        return runtime_store.get_release(release_id)

    def _enrich_scanned_bundle_release(
        self,
        *,
        record: Any,
        release: Any | None,
        scope: ScopeContext,
        account_id: str,
        install_store: Any,
        approved_browser_authority_impact_hash: str,
    ) -> tuple[Any, Any | None]:
        runtime_store = self._context.runtime_store()
        if (
            release is None
            or _normalize_text(getattr(release, "source_kind", "")) != "extension_scan"
        ):
            return record, release
        if not hasattr(runtime_store, "list_artifact_snapshots"):
            return record, release
        if runtime_store.list_artifact_snapshots(release.release_id):
            return record, release
        bundle_root = _normalize_text(dict(release.descriptor or {}).get("bundle_root"))
        descriptor_path = Path(bundle_root) / "extension-descriptor.yaml"
        if not bundle_root or not descriptor_path.is_file():
            return record, release

        self._core.import_descriptor_file(
            ExtensionDescriptorImportFileInput(
                descriptor_path=str(descriptor_path),
                source_ref=record.extension_id,
                install_after_import=True,
                approved_browser_authority_impact_hash=(approved_browser_authority_impact_hash),
            ),
            scope=scope,
            source_kind_override="extension_scan",
            allow_same_version_enrichment=True,
        )
        refreshed_record = install_store.get(
            extension_id=record.extension_id,
            account_id=account_id,
        )
        if refreshed_record is None:
            raise ServiceNotFoundError(
                f"Extension '{record.extension_id}' not found after enrichment"
            )
        refreshed_release = runtime_store.get_release(
            _normalize_text(getattr(refreshed_record, "release_id", ""))
        )
        return refreshed_record, refreshed_release

    @staticmethod
    def _install_projections(*, runtime_store: Any, account_id: str, install_id: str) -> list[Any]:
        if not hasattr(runtime_store, "list_projections"):
            return []
        return [
            copy.deepcopy(row)
            for row in runtime_store.list_projections(account_id=account_id, only_active=False)
            if _normalize_text(getattr(row, "install_id", "")) == _normalize_text(install_id)
        ]

    def _restore_activation_state(
        self,
        *,
        store: Any,
        runtime_store: Any,
        record: Any,
        release: Any | None,
        account_id: str,
        previous_state: str,
        previous_projections: list[Any],
    ) -> None:
        install_id = _normalize_text(getattr(record, "install_id", ""))
        self._install.remove_dataset_execution_bridges(install_id=install_id)
        if hasattr(runtime_store, "delete_projections_for_install"):
            runtime_store.delete_projections_for_install(install_id)
        if hasattr(runtime_store, "upsert_projection"):
            for projection in previous_projections:
                runtime_store.upsert_projection(projection)
        if release is not None and previous_projections:
            self._install.materialize_dataset_execution_bridges(
                install_record=record,
                release=release,
                account_id=account_id,
            )
        if hasattr(store, "set_state"):
            store.set_state(
                extension_id=record.extension_id,
                account_id=account_id,
                state=previous_state,
            )
        else:
            record.state = previous_state
        sync_legacy_enabled_projection(
            self._context.logger(),
            record.extension_id,
            enabled=previous_state == "active",
        )


__all__ = [
    "ExtensionLifecycleActivationOperations",
    "sync_legacy_enabled_projection",
    "validate_extension_activation",
    "validate_managed_release_artifact",
]
