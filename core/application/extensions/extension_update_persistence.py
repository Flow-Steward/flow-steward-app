from __future__ import annotations

from typing import Any

from core.application.management.command_models import (
    ExtensionDescriptorImportFileInput,
    ExtensionReleaseInstallInput,
)
from core.application.secrets.repository import install_release

from .archive_update_models import PreparedUpdate, UpdateRollbackState, account_id
from .binding_context import ExtensionLifecycleContext
from .binding_projection_service import ExtensionBindingProjectionService
from .extension_state_service import ExtensionStateService
from .install_browser_authority import persist_scanned_archive_browser_authority
from .managed_release_service import ManagedReleaseService


def _import_candidate_descriptor(prepared: PreparedUpdate) -> None:
    request = prepared.request
    if not request.descriptor_name or prepared.candidate_release_dir is None:
        return
    descriptor_import = prepared.lifecycle_command.import_descriptor_file(
        ExtensionDescriptorImportFileInput(
            descriptor_path=str(prepared.candidate_release_dir / request.descriptor_name),
            source_ref=prepared.extension_id,
            install_after_import=False,
            approved_browser_authority_impact_hash=(request.approved_browser_authority_impact_hash),
        ),
        scope=request.scope,
        source_kind_override="extension_scan",
        allow_same_version_enrichment=True,
        _browser_authority_handoff=request.browser_authority_handoff,
    )
    prepared.candidate_release_id = str(descriptor_import.release_id or "").strip()


def _apply_candidate_migrations(prepared: PreparedUpdate, transaction_conn: Any) -> None:
    if prepared.migration_snapshot is None:
        return
    from core.application.extensions.data_migrations import apply_extension_data_migrations

    request = prepared.request
    with transaction_conn.cursor() as cur:
        apply_extension_data_migrations(
            cur,
            account_id=account_id(request.scope),
            install_id=str(getattr(request.current_install, "install_id", "") or ""),
            snapshot=prepared.migration_snapshot,
            migrated=prepared.migrated_rows,
            target_version=prepared.target_data_schema_version,
        )


def _install_candidate_descriptor_release(
    *, prepared: PreparedUpdate, managed_release: ManagedReleaseService
) -> None:
    request = prepared.request
    if prepared.candidate_release_id:
        candidate_release_dir = prepared.candidate_release_dir
        assert candidate_release_dir is not None
        from core.infrastructure.extension_runtime.extension_runtime_common import (
            EXTENSION_INSTALL_METADATA_FILENAME,
        )

        install_result = prepared.lifecycle_command.install_release(
            ExtensionReleaseInstallInput(
                release_id=prepared.candidate_release_id,
                approved_browser_authority_impact_hash=(
                    request.approved_browser_authority_impact_hash
                ),
            ),
            scope=request.scope,
            allow_same_version_enrichment=True,
            _browser_authority_handoff=request.browser_authority_handoff,
        )
        managed_release.record_install_id_in_bundle_metadata(
            bundle_root=candidate_release_dir,
            metadata_filename=EXTENSION_INSTALL_METADATA_FILENAME,
            install_id=str(install_result.install_id or ""),
        )


def _reconcile_candidate_oauth_release(prepared: PreparedUpdate, transaction_conn: Any) -> None:
    """Move OAuth configurations with the install when the update did not change sign-in.

    The release install path does this on activation; an archive update reaches the
    new release here and must do the same, or every OAuth connection of the install
    stays blocked (oauth_runtime_blocked) until someone opens the setup page.
    """
    request = prepared.request
    current = request.install_store.get(
        extension_id=prepared.extension_id,
        account_id=account_id(request.scope),
    )
    if current is None:
        return
    release_id = str(getattr(current, "release_id", "") or "").strip()
    if not release_id or release_id == str(prepared.previous_release_id or "").strip():
        return
    get_release = getattr(request.runtime_store, "get_release", None)
    release = get_release(release_id) if callable(get_release) else None
    descriptor = dict(getattr(release, "descriptor", None) or {})
    if not descriptor:
        return
    from .oauth_release_activation import reconcile_install_oauth_release

    reconcile_install_oauth_release(
        transaction_conn,
        account_id=str(getattr(current, "account_id", "") or account_id(request.scope)),
        install_id=str(getattr(current, "install_id", "") or ""),
        extension_id=prepared.extension_id,
        release_id=release_id,
        descriptor=descriptor,
    )


def persist_candidate_update(
    *,
    context: ExtensionLifecycleContext,
    projection: ExtensionBindingProjectionService,
    managed_release: ManagedReleaseService,
    state: ExtensionStateService,
    prepared: PreparedUpdate,
    rollback: UpdateRollbackState,
) -> tuple[dict[str, Any], bool]:
    """Persist an already-staged extension update in its lifecycle transaction."""
    request = prepared.request
    if prepared.candidate_release_dir is None:
        raise RuntimeError("Candidate release must be staged before persistence")
    caller_owns_transaction = context.archive_transaction_is_caller_owned(request.bl)
    with context.archive_lifecycle_transaction(request.bl) as transaction_conn:
        secret_update = request.catalog_secret_update
        previous_secret_release = None
        if secret_update is not None:
            with transaction_conn.cursor() as cur:
                previous_secret_release = install_release(cur, install_id=secret_update.install_id)
        installed = state.sync_staged_extension_release(
            bl=request.bl,
            release_root=prepared.candidate_release_dir,
            extension_id=prepared.extension_id,
        )
        _import_candidate_descriptor(prepared)
        _apply_candidate_migrations(prepared, transaction_conn)
        _install_candidate_descriptor_release(
            prepared=prepared,
            managed_release=managed_release,
        )
        _reconcile_candidate_oauth_release(prepared, transaction_conn)
        enabled = state.set_enabled_state_locked(
            prepared.extension_id,
            bl=request.bl,
            version=prepared.candidate_version,
            enabled=prepared.previous_enabled,
            config=prepared.migrated_install_config,
            scope=request.scope,
            _skip_rescan=True,
            _skip_browser_authority_gate=True,
            _install_store=request.install_store,
            _runtime_store=request.runtime_store,
        )
        if not prepared.candidate_release_id:
            persist_scanned_archive_browser_authority(
                handoff=request.browser_authority_handoff,
                install_store=request.install_store,
                runtime_store=request.runtime_store,
                release_root=prepared.candidate_release_dir,
            )
        projection.reconcile_callback_registrations(
            bl=request.bl,
            extension_id=prepared.extension_id,
            previous_callback_ids=prepared.previous_callback_ids,
            current_callback_ids=projection.callback_ids_from_payload(
                enabled.extension or installed
            ),
        )
        if secret_update is not None and previous_secret_release is not None:
            secret_update.persist(
                transaction_conn,
                scope=request.scope,
                previous_install=request.current_install,
                previous_release=previous_secret_release,
                install_store=request.install_store,
            )
        rollback.lifecycle_commit_attempted = True
    return dict(enabled.extension or installed), caller_owns_transaction


__all__ = ["persist_candidate_update"]
