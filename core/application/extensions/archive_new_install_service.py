from __future__ import annotations

import contextlib
import shutil
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.application.management.command_models_extensions import (
    ExtensionDescriptorImportFileInput,
    ExtensionDescriptorValidateInput,
)
from core.application.management.command_models_registry_tools import (
    ExtensionArchiveInstallResultView,
)
from core.application.management.errors import (
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
from .browser_authority_approval import PreparedBrowserAuthorityApproval
from .extension_state_service import ExtensionStateService
from .lifecycle_ports import ERR_DATABASE_NOT_CONFIGURED, service_scope
from .managed_release_service import ManagedReleaseService

_MAX_EXTENSION_ARCHIVE_BYTES = MAX_EXTENSION_ARCHIVE_BYTES
_MAX_EXTENSION_ARCHIVE_UNCOMPRESSED_BYTES = MAX_EXTENSION_ARCHIVE_UNCOMPRESSED_BYTES
_archive_too_large_message = archive_too_large_message


@dataclass(frozen=True, slots=True)
class _NewInstallRequest:
    bl: Any
    install_store: Any
    runtime_store: Any
    bundle_dir: Path
    compiled: dict[str, Any]
    extension_id: str
    extensions_root: Path
    metadata_filename: str
    scope: ScopeContext | None
    descriptor_name: str
    browser_authority_handoff: PreparedBrowserAuthorityApproval | None
    approved_browser_authority_impact_hash: str


@dataclass(slots=True)
class _NewInstallRollbackState:
    target_dir: Path
    staged_dir: Path
    release_dir: Path | None = None
    created_release_this_attempt: bool = False
    release_arbitration_rejected: bool = False
    published_target: bool = False
    lifecycle_commit_attempted: bool = False


@dataclass(frozen=True, slots=True)
class _CommittedNewInstall:
    installed: dict[str, Any]
    enabled: Any
    caller_owns_transaction: bool


class ExtensionArchiveNewInstallService:
    def __init__(
        self,
        *,
        context: ExtensionLifecycleContext,
        projection: ExtensionBindingProjectionService,
        managed_release: ManagedReleaseService,
        cleanup: ExtensionCleanupService,
        state: ExtensionStateService,
        get_logger: Callable[[], Any],
    ) -> None:
        self._context = context
        self._projection = projection
        self._managed_release = managed_release
        self._cleanup = cleanup
        self._state = state
        self._get_logger = get_logger

    def install_simple_extension_bundle(
        self,
        *,
        bl: Any,
        install_store: Any,
        runtime_store: Any,
        bundle_dir: Path,
        compiled: dict[str, Any],
        extension_id: str,
        extensions_root: Path,
        metadata_filename: str,
        scope: ScopeContext | None = None,
        descriptor_name: str = "",
        browser_authority_handoff: PreparedBrowserAuthorityApproval | None = None,
        approved_browser_authority_impact_hash: str = "",
    ) -> ExtensionArchiveInstallResultView:
        return self._install_simple_extension_bundle(
            bl=bl,
            install_store=install_store,
            runtime_store=runtime_store,
            bundle_dir=bundle_dir,
            compiled=compiled,
            extension_id=extension_id,
            extensions_root=extensions_root,
            metadata_filename=metadata_filename,
            scope=scope,
            descriptor_name=descriptor_name,
            browser_authority_handoff=browser_authority_handoff,
            approved_browser_authority_impact_hash=(approved_browser_authority_impact_hash),
        )

    def install_extension_descriptor_archive(
        self,
        *,
        descriptor_path: Path,
        bl: Any | None = None,
        install_store: Any | None = None,
        runtime_store: Any | None = None,
        scope: ScopeContext | None = None,
        previous_install: Any | None = None,
        browser_authority_handoff: PreparedBrowserAuthorityApproval | None = None,
        approved_browser_authority_impact_hash: str = "",
    ) -> ExtensionArchiveInstallResultView:
        return self._install_extension_descriptor_archive(
            bl=bl,
            install_store=install_store,
            runtime_store=runtime_store,
            descriptor_path=descriptor_path,
            scope=scope,
            previous_install=previous_install,
            browser_authority_handoff=browser_authority_handoff,
            approved_browser_authority_impact_hash=(approved_browser_authority_impact_hash),
        )

    def _install_simple_extension_bundle(
        self,
        *,
        bl: Any,
        install_store: Any,
        runtime_store: Any,
        bundle_dir: Path,
        compiled: dict[str, Any],
        extension_id: str,
        extensions_root: Path,
        metadata_filename: str,
        scope: ScopeContext | None = None,
        descriptor_name: str = "",
        browser_authority_handoff: PreparedBrowserAuthorityApproval | None = None,
        approved_browser_authority_impact_hash: str = "",
    ) -> ExtensionArchiveInstallResultView:
        if not bl:
            raise ServiceUnavailableError(ERR_DATABASE_NOT_CONFIGURED)
        request = _NewInstallRequest(
            bl=bl,
            install_store=install_store,
            runtime_store=runtime_store,
            bundle_dir=bundle_dir,
            compiled=compiled,
            extension_id=extension_id,
            extensions_root=extensions_root,
            metadata_filename=metadata_filename,
            scope=scope,
            descriptor_name=descriptor_name,
            browser_authority_handoff=browser_authority_handoff,
            approved_browser_authority_impact_hash=(approved_browser_authority_impact_hash),
        )
        target_dir = self._managed_release.managed_simple_extension_root(request.extension_id)
        if target_dir.exists():
            raise ServiceValidationError(
                f"Extension '{request.extension_id}' is already present; "
                "uninstall it before reinstalling"
            )
        state = _NewInstallRollbackState(
            target_dir=target_dir,
            staged_dir=request.extensions_root
            / f".{target_dir.name}.install-staged-{uuid.uuid4().hex[:8]}",
        )
        try:
            self._prepare_managed_release(request=request, state=state)
            committed = self._commit_new_install(request=request, state=state)
            self._publish_new_install(request=request, state=state, committed=committed)
            return self._new_install_result(state=state, committed=committed)
        except Exception:
            if not state.lifecycle_commit_attempted:
                self._rollback_new_install(request=request, state=state)
            raise
        finally:
            with contextlib.suppress(OSError):
                self._managed_release.remove_managed_extension_staging_path(state.staged_dir)

    def _prepare_managed_release(
        self,
        *,
        request: _NewInstallRequest,
        state: _NewInstallRollbackState,
    ) -> None:
        source_fingerprint = self._managed_release.managed_release_tree_fingerprint(
            request.bundle_dir,
            source_identity=True,
        )
        shutil.copytree(request.bundle_dir, state.staged_dir)
        # copytree copies the upload's mode, clearing inherited setgid. Restore
        # the shared writer policy before creating installer-owned metadata.
        self._managed_release.normalize_managed_release_tree_modes(state.staged_dir)
        self._write_staged_metadata(request=request, state=state)
        self._managed_release.normalize_managed_release_tree_modes(state.staged_dir)
        staged_fingerprint = self._managed_release.managed_release_tree_fingerprint(
            state.staged_dir
        )
        state.release_dir = self._managed_release.managed_simple_extension_release_root(
            extension_id=request.extension_id,
            version=self._projection.extension_version_from_payload(request.compiled),
            checksum=source_fingerprint,
        )
        state.release_dir.parent.mkdir(parents=True, exist_ok=True)
        try:
            state.created_release_this_attempt = (
                self._managed_release.publish_or_adopt_managed_release(
                    bl=request.bl,
                    runtime_store=request.runtime_store,
                    extension_id=request.extension_id,
                    staged_root=state.staged_dir,
                    release_root=state.release_dir,
                    staged_fingerprint=staged_fingerprint,
                )
            )
        except Exception:
            state.release_arbitration_rejected = True
            raise

    def _write_staged_metadata(
        self,
        *,
        request: _NewInstallRequest,
        state: _NewInstallRollbackState,
    ) -> None:
        import yaml

        from core.application.extensions.archive_validation_service import (
            materialized_python_requirements,
        )

        python_deps = materialized_python_requirements(
            bundle_root=state.staged_dir,
            compiled_manifest=request.compiled,
        )
        metadata = {
            "python_dependencies": self._managed_release.python_dependency_install_metadata(
                python_deps,
                bundle_root=state.target_dir,
                compiled=request.compiled,
            ),
        }
        (state.staged_dir / request.metadata_filename).write_text(
            yaml.safe_dump(metadata, sort_keys=True),
            encoding="utf-8",
        )

    def _commit_new_install(
        self,
        *,
        request: _NewInstallRequest,
        state: _NewInstallRollbackState,
    ) -> _CommittedNewInstall:
        assert state.release_dir is not None
        caller_owns_transaction = self._context.archive_transaction_is_caller_owned(request.bl)
        with self._context.archive_lifecycle_transaction(request.bl):
            installed = self._state.sync_staged_extension_release(
                bl=request.bl,
                release_root=state.release_dir,
                extension_id=request.extension_id,
            )
            if request.descriptor_name:
                self._install_release_descriptor(request=request, state=state)
            elif request.browser_authority_handoff is not None:
                current = request.install_store.get(
                    extension_id=request.extension_id,
                    account_id=service_scope(request.scope).account_id or "acc_default",
                )
                if current is not None:
                    from .browser_authority_approval import with_browser_authority_approval

                    request.install_store.set_provenance(
                        extension_id=request.extension_id,
                        account_id=current.account_id,
                        provenance=with_browser_authority_approval(
                            current.provenance,
                            approval=request.browser_authority_handoff.approval,
                        ),
                    )
            enabled = self._state.set_enabled_state_locked(
                request.extension_id,
                bl=request.bl,
                version=None,
                enabled=True,
                config={},
                scope=request.scope,
                _skip_rescan=True,
                _skip_browser_authority_gate=True,
                _install_store=request.install_store,
                _runtime_store=request.runtime_store,
            )
            state.lifecycle_commit_attempted = True
        return _CommittedNewInstall(
            installed=installed,
            enabled=enabled,
            caller_owns_transaction=caller_owns_transaction,
        )

    def _install_release_descriptor(
        self,
        *,
        request: _NewInstallRequest,
        state: _NewInstallRollbackState,
    ) -> None:
        assert state.release_dir is not None
        previous_install = self._state.current_extension_install_record(
            request.bl,
            extension_id=request.extension_id,
            scope=service_scope(request.scope),
            extensions_root=request.extensions_root,
            install_store=request.install_store,
        )
        descriptor_result = self._install_extension_descriptor_archive(
            bl=request.bl,
            install_store=request.install_store,
            runtime_store=request.runtime_store,
            descriptor_path=state.release_dir / request.descriptor_name,
            scope=request.scope,
            previous_install=previous_install,
            browser_authority_handoff=request.browser_authority_handoff,
            approved_browser_authority_impact_hash=(request.approved_browser_authority_impact_hash),
        )
        descriptor_install = dict((descriptor_result.extension or {}).get("install") or {})
        self._managed_release.record_install_id_in_bundle_metadata(
            bundle_root=state.release_dir,
            metadata_filename=request.metadata_filename,
            install_id=str(descriptor_install.get("install_id") or ""),
        )

    def _publish_new_install(
        self,
        *,
        request: _NewInstallRequest,
        state: _NewInstallRollbackState,
        committed: _CommittedNewInstall,
    ) -> None:
        assert state.release_dir is not None
        if committed.caller_owns_transaction:
            self._context.register_archive_after_commit(
                request.bl,
                lambda: self._context.publish_committed_managed_extension_authority(
                    bl=request.bl,
                    extension_id=request.extension_id,
                    install_store=request.install_store,
                    runtime_store=request.runtime_store,
                ),
            )
            return
        self._managed_release.activate_managed_simple_extension_release(
            active_root=state.target_dir,
            release_root=state.release_dir,
        )
        state.published_target = True

    @staticmethod
    def _new_install_result(
        *,
        state: _NewInstallRollbackState,
        committed: _CommittedNewInstall,
    ) -> ExtensionArchiveInstallResultView:
        return ExtensionArchiveInstallResultView(
            extension={
                **dict(committed.enabled.extension or committed.installed),
                "lifecycle_operation": "installed",
            },
            installed_path=str(state.target_dir),
        )

    def _rollback_new_install(
        self,
        *,
        request: _NewInstallRequest,
        state: _NewInstallRollbackState,
    ) -> None:
        if state.published_target:
            self._managed_release.remove_managed_simple_extension_active_root(state.target_dir)
        if state.created_release_this_attempt and state.release_dir is not None:
            shutil.rmtree(state.release_dir, ignore_errors=True)
        if state.release_arbitration_rejected:
            return
        with contextlib.suppress(Exception):
            self._cleanup.purge_simple_extension_scan_state(
                bl=request.bl,
                extension_id=request.extension_id,
            )
        with contextlib.suppress(Exception):
            self._cleanup.rollback_failed_new_extension_lifecycle(
                bl=request.bl,
                extension_id=request.extension_id,
                scope=request.scope,
                install_store=request.install_store,
                runtime_store=request.runtime_store,
            )

    def _install_extension_descriptor_archive(
        self,
        *,
        bl: Any | None = None,
        install_store: Any | None = None,
        runtime_store: Any | None = None,
        descriptor_path: Path,
        scope: ScopeContext | None = None,
        previous_install: Any | None = None,
        browser_authority_handoff: PreparedBrowserAuthorityApproval | None = None,
        approved_browser_authority_impact_hash: str = "",
    ) -> ExtensionArchiveInstallResultView:
        pinned_bl = bl if bl is not None else self._get_logger()
        # Extension compatibility is declared against the extension host contract
        # version, not the application build version. See _runtime_platform_version.
        from core.application.extensions.service_support import _runtime_platform_version

        lifecycle_command = self._context.command_service(
            pinned_bl,
            install_store=install_store,
            runtime_store=runtime_store,
        )
        validation = lifecycle_command.validate_descriptor(
            ExtensionDescriptorValidateInput(
                source_type="file",
                descriptor_path=str(descriptor_path),
                platform_version=_runtime_platform_version(),
                allow_insecure_remote=False,
            ),
            scope=scope,
        )
        normalized = lifecycle_command.descriptor_validation_or_error(validation)
        source_extension_id = str(normalized.get("extension_id") or "").strip()
        extension_import_result = lifecycle_command.import_descriptor_file(
            ExtensionDescriptorImportFileInput(
                descriptor_path=str(descriptor_path),
                source_ref=source_extension_id,
                install_after_import=True,
                approved_browser_authority_impact_hash=(approved_browser_authority_impact_hash),
            ),
            scope=scope,
            source_kind_override="extension_scan",
            allow_same_version_enrichment=True,
            _browser_authority_handoff=browser_authority_handoff,
        )
        install_payload = extension_import_result.dump()
        extension_detail = self._context.query_service(
            pinned_bl,
            install_store=install_store,
            runtime_store=runtime_store,
        ).get_extension(extension_import_result.extension_id, scope=scope)
        return ExtensionArchiveInstallResultView(
            extension={
                **self._projection.runtime_extension_payload(extension_detail),
                "imported_artifacts": list(install_payload.get("imported_artifacts") or []),
                "install": dict(install_payload.get("install") or {}),
                "lifecycle_operation": "updated" if previous_install is not None else "installed",
                "from_version": str(getattr(previous_install, "version", "") or "").strip(),
                "to_version": str(install_payload.get("release_version") or "").strip(),
            },
            installed_path=str(descriptor_path.parent),
        )


__all__ = ["ExtensionArchiveNewInstallService"]
