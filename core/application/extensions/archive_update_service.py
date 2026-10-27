from __future__ import annotations

import contextlib
import shutil
import tempfile
import uuid
from pathlib import Path
from typing import Any

from core.application.management.command_models import (
    ExtensionArchiveInstallResultView,
)
from core.application.management.errors import ServiceUnavailableError, ServiceValidationError
from core.application.management.scope import ScopeContext

from .archive_update_models import (
    PreparedUpdate,
    UpdateRequest,
    UpdateRollbackState,
    account_id,
    validate_update_version_transition,
)
from .binding_cleanup_service import ExtensionCleanupService
from .binding_context import (
    ExtensionInstallStoreType,
    ExtensionLifecycleContext,
    ExtensionRuntimeStoreType,
)
from .binding_projection_service import ExtensionBindingProjectionService
from .extension_state_service import ExtensionStateService
from .extension_update_persistence import persist_candidate_update
from .managed_release_service import ManagedReleaseService
from .update_failure_reporting import raise_update_failure

UNMANAGED_LAYOUT_ADOPTION_FAILED_MESSAGE = (
    "This extension's files could not be moved into the release layout updates use. "
    "Nothing was changed; the installed version keeps running."
)


class ExtensionArchiveUpdateService:
    def __init__(
        self,
        *,
        context: ExtensionLifecycleContext,
        projection: ExtensionBindingProjectionService,
        managed_release: ManagedReleaseService,
        cleanup: ExtensionCleanupService,
        state: ExtensionStateService,
    ) -> None:
        self._context = context
        self._projection = projection
        self._managed_release = managed_release
        self._cleanup = cleanup
        self._state = state

    def validate_update_version_transition(
        self,
        *,
        extension_id: str,
        current_version: str,
        candidate_version: str,
    ) -> str:
        return validate_update_version_transition(
            extension_id=extension_id,
            current_version=current_version,
            candidate_version=candidate_version,
        )

    def _prepare_update(self, request: UpdateRequest) -> PreparedUpdate:
        extension_id = str(request.compiled.get("extension_key") or "").strip()
        candidate_version = self._projection.extension_version_from_payload(request.compiled)
        target_dir = self._managed_release.managed_simple_extension_root(extension_id)
        previous_active_release_dir = (
            self._managed_release.managed_simple_extension_active_release_root(extension_id)
        )
        staged_dir = (
            request.extensions_root / f".{target_dir.name}.update-staged-{uuid.uuid4().hex[:8]}"
        )
        previous_extension = (
            request.bl.get_extension(extension_id) if hasattr(request.bl, "get_extension") else None
        )
        current = request.current_install
        previous_release_id = str(getattr(current, "release_id", "") or "").strip()
        lifecycle_command = self._context.command_service(
            request.bl,
            install_store=request.install_store,
            runtime_store=request.runtime_store,
        )
        previous_install_record = (
            request.install_store.get(
                extension_id=extension_id,
                account_id=account_id(request.scope),
            )
            if hasattr(request.install_store, "get")
            else current
        )
        install_id = str(getattr(current, "install_id", "") or "")
        previous_projections = (
            [
                row
                for row in request.runtime_store.list_projections(
                    account_id=account_id(request.scope), only_active=False
                )
                if str(getattr(row, "install_id", "") or "").strip() == install_id.strip()
            ]
            if hasattr(request.runtime_store, "list_projections")
            else []
        )
        candidate_manifest = dict(request.compiled.get("compiled_manifest") or {})
        candidate_lifecycle = dict(candidate_manifest.get("lifecycle") or {})
        previous_config = dict(getattr(current, "config", {}) or {})
        return PreparedUpdate(
            request=request,
            extension_id=extension_id,
            candidate_version=candidate_version,
            target_dir=target_dir,
            previous_active_release_dir=previous_active_release_dir,
            staged_dir=staged_dir,
            previous_callback_ids=self._projection.callback_ids_from_payload(previous_extension),
            previous_version=str(getattr(current, "version", "") or "").strip(),
            previous_enabled=(
                str(getattr(current, "state", "") or "active").strip().lower() == "active"
            ),
            previous_config=previous_config,
            source_data_schema_version=int(getattr(current, "applied_data_schema_version", 1) or 1),
            target_data_schema_version=int(candidate_lifecycle.get("data_schema_version") or 1),
            candidate_manifest=candidate_manifest,
            candidate_lifecycle=candidate_lifecycle,
            previous_release_id=previous_release_id,
            lifecycle_command=lifecycle_command,
            previous_install_payload=lifecycle_command.install_record_payload(
                previous_install_record
            ),
            previous_release=(
                request.runtime_store.get_release(previous_release_id)
                if previous_release_id and hasattr(request.runtime_store, "get_release")
                else None
            ),
            previous_entitlement=(
                request.runtime_store.get_entitlement(install_id)
                if hasattr(request.runtime_store, "get_entitlement")
                else None
            ),
            previous_projections=previous_projections,
            migrated_install_config=previous_config,
        )

    def _run_candidate_migrations(self, prepared: PreparedUpdate) -> None:
        if prepared.target_data_schema_version == prepared.source_data_schema_version:
            return
        request = prepared.request
        from core.application.extensions.data_migrations import (
            run_candidate_data_migrations,
            snapshot_extension_data,
            validate_candidate_migration_results,
        )

        if not callable(getattr(request.bl, "cursor", None)):
            raise ServiceUnavailableError("Extension data migration requires lifecycle storage")
        prepared.migration_snapshot = snapshot_extension_data(
            request.bl,
            account_id=account_id(request.scope),
            extension_id=prepared.extension_id,
            install_id=str(getattr(request.current_install, "install_id", "") or ""),
        )
        prepared.migration_container_dir = Path(
            tempfile.mkdtemp(prefix=f"extension-migration-{prepared.target_dir.name}-")
        )
        prepared.migration_staged_dir = prepared.migration_container_dir / prepared.target_dir.name
        try:
            shutil.copytree(prepared.staged_dir, prepared.migration_staged_dir)
            candidate_runtime = {
                **request.compiled,
                "bundle_root": str(prepared.migration_staged_dir),
                "compiled_manifest": {
                    **prepared.candidate_manifest,
                    "bundle_root": str(prepared.migration_staged_dir),
                },
            }
            prepared.migrated_rows = run_candidate_data_migrations(
                candidate_extension=candidate_runtime,
                lifecycle=prepared.candidate_lifecycle,
                records=list(prepared.migration_snapshot.get("records") or []),
                source_version=prepared.source_data_schema_version,
                target_version=prepared.target_data_schema_version,
            )
        finally:
            with contextlib.suppress(OSError):
                self._managed_release.remove_managed_extension_staging_path(
                    prepared.migration_container_dir
                )
            prepared.migration_staged_dir = None
            prepared.migration_container_dir = None
        validate_candidate_migration_results(
            candidate_manifest=prepared.candidate_manifest,
            migrated=prepared.migrated_rows,
            snapshot=prepared.migration_snapshot,
        )
        prepared.migrated_install_config = next(
            (
                dict(row.get("value") or {})
                for row in prepared.migrated_rows
                if row.get("target_type") == "install_config"
            ),
            prepared.previous_config,
        )

    def _adopt_unmanaged_layout(self, prepared: PreparedUpdate) -> None:
        """Move a plain-folder install into the managed-release layout.

        An extension an author placed in ``extensions/<folder>`` by hand, or one
        an earlier build copied there, has its files straight in that folder.
        Updates swap a release link, so that folder is
        first renamed, unchanged, to ``.releases/<folder>/<version>-<fingerprint>``
        and a link to it takes its place. The extension keeps running the same
        files throughout: the rename and the link replacement are each atomic on
        one filesystem, and nothing is copied or rewritten.

        This runs before the update's rollback state is armed. A later failure
        rolls the link back to this adopted release — the same files the
        extension ran before — so the install is never left without a version.
        If adoption itself fails, the folder is put back where it was.
        """
        target_dir = prepared.target_dir
        if not target_dir.exists() or target_dir.is_symlink() or not target_dir.is_dir():
            return
        from core.infrastructure.extension_runtime.atomic_publish import rename_no_replace

        try:
            # The fingerprint only names the release. A hand-placed folder may hold
            # links or other non-regular files a release fingerprint refuses; such a
            # folder is still adopted as it is, under a unique name.
            try:
                fingerprint = self._managed_release.managed_release_tree_fingerprint(target_dir)
            except ServiceValidationError:
                fingerprint = uuid.uuid4().hex
            release_root = self._managed_release.managed_simple_extension_release_root(
                extension_id=prepared.extension_id,
                version=prepared.previous_version or "legacy",
                checksum=fingerprint,
            )
            release_root.parent.mkdir(parents=True, exist_ok=True)
            rename_no_replace(target_dir, release_root)
        except (OSError, ServiceValidationError) as exc:
            raise ServiceValidationError(UNMANAGED_LAYOUT_ADOPTION_FAILED_MESSAGE) from exc
        try:
            self._managed_release.activate_managed_simple_extension_release(
                active_root=target_dir,
                release_root=release_root,
            )
        except OSError as exc:
            with contextlib.suppress(OSError):
                rename_no_replace(release_root, target_dir)
            raise ServiceValidationError(UNMANAGED_LAYOUT_ADOPTION_FAILED_MESSAGE) from exc
        prepared.previous_active_release_dir = (
            self._managed_release.managed_simple_extension_active_release_root(
                prepared.extension_id
            )
        )

    def _stage_candidate_release(
        self,
        prepared: PreparedUpdate,
        rollback: UpdateRollbackState,
    ) -> None:
        request = prepared.request
        from core.application.extensions.archive_validation_service import (
            materialized_python_requirements,
        )
        from core.infrastructure.extension_runtime.extension_runtime_common import (
            EXTENSION_INSTALL_METADATA_FILENAME,
        )

        source_fingerprint = self._managed_release.managed_release_tree_fingerprint(
            request.bundle_dir,
            source_identity=True,
        )
        shutil.copytree(request.bundle_dir, prepared.staged_dir)
        # Restore setgid before metadata creation, not just after it: otherwise
        # the new file inherits the installer's primary group.
        self._managed_release.normalize_managed_release_tree_modes(prepared.staged_dir)
        python_deps = materialized_python_requirements(
            bundle_root=prepared.staged_dir,
            compiled_manifest=request.compiled,
        )
        import yaml

        (prepared.staged_dir / EXTENSION_INSTALL_METADATA_FILENAME).write_text(
            yaml.safe_dump(
                {
                    "python_dependencies": (
                        self._managed_release.python_dependency_install_metadata(
                            python_deps,
                            bundle_root=prepared.target_dir,
                            compiled=request.compiled,
                        )
                    ),
                },
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        self._managed_release.normalize_managed_release_tree_modes(prepared.staged_dir)
        self._run_candidate_migrations(prepared)
        staged_fingerprint = self._managed_release.managed_release_tree_fingerprint(
            prepared.staged_dir
        )
        prepared.candidate_release_dir = (
            self._managed_release.managed_simple_extension_release_root(
                extension_id=prepared.extension_id,
                version=prepared.candidate_version,
                checksum=source_fingerprint,
            )
        )
        prepared.candidate_release_dir.parent.mkdir(parents=True, exist_ok=True)
        try:
            rollback.created_release_this_attempt = (
                self._managed_release.publish_or_adopt_managed_release(
                    bl=request.bl,
                    runtime_store=request.runtime_store,
                    extension_id=prepared.extension_id,
                    staged_root=prepared.staged_dir,
                    release_root=prepared.candidate_release_dir,
                    staged_fingerprint=staged_fingerprint,
                )
            )
        except Exception:
            rollback.release_arbitration_rejected = True
            raise

    def _persist_candidate_update(
        self,
        prepared: PreparedUpdate,
        rollback: UpdateRollbackState,
    ) -> tuple[dict[str, Any], bool]:
        return persist_candidate_update(
            context=self._context,
            projection=self._projection,
            managed_release=self._managed_release,
            state=self._state,
            prepared=prepared,
            rollback=rollback,
        )

    def _cleanup_previous_release(self, prepared: PreparedUpdate) -> None:
        """Retain retired files while online readers may still hold their paths.

        Release garbage collection belongs to a quiescent maintenance operation,
        where references are revalidated immediately before deletion.
        """
        del prepared

    def _publish_candidate_update(
        self,
        prepared: PreparedUpdate,
        rollback: UpdateRollbackState,
        *,
        caller_owns_transaction: bool,
    ) -> None:
        request = prepared.request
        if prepared.candidate_release_dir is None:
            raise RuntimeError("Candidate release must be staged before publication")
        if caller_owns_transaction:
            self._context.register_archive_after_commit(
                request.bl,
                lambda: self._context.publish_committed_managed_extension_authority(
                    bl=request.bl,
                    extension_id=prepared.extension_id,
                    install_store=request.install_store,
                    runtime_store=request.runtime_store,
                ),
            )
            return
        self._managed_release.activate_managed_simple_extension_release(
            active_root=prepared.target_dir,
            release_root=prepared.candidate_release_dir,
        )
        rollback.published_target = True

    def update_simple_extension_bundle(
        self,
        *,
        bl: Any,
        install_store: ExtensionInstallStoreType,
        runtime_store: ExtensionRuntimeStoreType,
        bundle_dir: Path,
        compiled: dict[str, Any],
        extensions_root: Path,
        scope: ScopeContext | None,
        current_install: Any,
        descriptor_name: str = "",
        browser_authority_handoff: Any | None = None,
        approved_browser_authority_impact_hash: str = "",
    ) -> ExtensionArchiveInstallResultView:
        if not bl:
            raise ServiceUnavailableError("Database not configured")
        request = UpdateRequest(
            bl=bl,
            install_store=install_store,
            runtime_store=runtime_store,
            bundle_dir=bundle_dir,
            compiled=compiled,
            extensions_root=extensions_root,
            scope=scope,
            current_install=current_install,
            descriptor_name=descriptor_name,
            browser_authority_handoff=browser_authority_handoff,
            approved_browser_authority_impact_hash=(approved_browser_authority_impact_hash),
        )
        prepared = self._prepare_update(request)
        self._adopt_unmanaged_layout(prepared)
        rollback = UpdateRollbackState(
            prepared=prepared,
            context=self._context,
            managed_release=self._managed_release,
            cleanup=self._cleanup,
            state=self._state,
        )
        try:
            self._stage_candidate_release(prepared, rollback)
            extension, caller_owns_transaction = self._persist_candidate_update(prepared, rollback)
            self._publish_candidate_update(
                prepared,
                rollback,
                caller_owns_transaction=caller_owns_transaction,
            )
            return ExtensionArchiveInstallResultView(
                extension={
                    **extension,
                    "lifecycle_operation": "updated",
                    "from_version": prepared.previous_version,
                    "to_version": prepared.candidate_version,
                },
                installed_path=str(prepared.target_dir),
            )
        except BaseException as exc:
            if rollback.lifecycle_commit_attempted:
                raise
            raise_update_failure(exc, rollback.rollback_update())
        finally:
            rollback.cleanup_prepublication_paths()

    _validate_update_version_transition = validate_update_version_transition
    _update_simple_extension_bundle = update_simple_extension_bundle


__all__ = ["ExtensionArchiveUpdateService"]
