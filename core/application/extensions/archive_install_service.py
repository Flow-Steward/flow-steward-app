from __future__ import annotations

import contextlib
import os
import stat
import zipfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

from core.application.management.command_models_registry_tools import (
    ExtensionArchiveInstallResultView,
)
from core.application.management.errors import (
    ServiceUnavailableError,
    ServiceValidationError,
)
from core.application.management.scope import ScopeContext

from .archive_extraction_service import (
    ExtensionArchiveExtractionService,
    safe_extension_archive_file_mode,
)
from .archive_new_install_service import ExtensionArchiveNewInstallService
from .archive_update_service import ExtensionArchiveUpdateService
from .archive_validation_service import (
    ExtensionArchiveValidationService,
    ValidatedExtensionBundle,
)
from .binding_constants import (
    MAX_EXTENSION_ARCHIVE_BYTES,
    MAX_EXTENSION_ARCHIVE_UNCOMPRESSED_BYTES,
)
from .binding_context import ExtensionLifecycleContext
from .binding_projection_service import ExtensionBindingProjectionService
from .browser_authority_approval import (
    PreparedBrowserAuthorityApproval,
    browser_authority_approval_provenance,
    browser_authority_impact,
    browser_authority_manifest_sha256,
    candidate_browser_tree_sha256,
    require_browser_authority_approval,
)
from .extension_state_service import ExtensionStateService
from .lifecycle_ports import (
    ERR_DATABASE_NOT_CONFIGURED,
    require_global_extension_lifecycle_scope,
    service_scope,
)
from .managed_release_service import ManagedReleaseService

_MAX_EXTENSION_ARCHIVE_BYTES = MAX_EXTENSION_ARCHIVE_BYTES
_MAX_EXTENSION_ARCHIVE_UNCOMPRESSED_BYTES = MAX_EXTENSION_ARCHIVE_UNCOMPRESSED_BYTES


def _archive_too_large_message() -> str:
    return f"Extension archive is too large — maximum {_MAX_EXTENSION_ARCHIVE_BYTES} bytes"


class ExtensionArchiveInstallService:
    def __init__(
        self,
        *,
        context: ExtensionLifecycleContext,
        projection: ExtensionBindingProjectionService,
        managed_release: ManagedReleaseService,
        state: ExtensionStateService,
        new_install: ExtensionArchiveNewInstallService,
        update: ExtensionArchiveUpdateService,
        get_logger: Callable[[], Any],
        validation: ExtensionArchiveValidationService | None = None,
        extraction: ExtensionArchiveExtractionService | None = None,
    ) -> None:
        self._context = context
        self._projection = projection
        self._managed_release = managed_release
        self._state = state
        self._new_install = new_install
        self._update = update
        self._get_logger = get_logger
        self._validation = validation or ExtensionArchiveValidationService(
            context=context,
            projection=projection,
        )
        self._extraction = extraction or ExtensionArchiveExtractionService(
            file_mode=safe_extension_archive_file_mode
        )

    @staticmethod
    def _safe_extension_archive_file_mode(member: zipfile.ZipInfo) -> int:
        return safe_extension_archive_file_mode(member)

    def install_extension_archive_path(
        self,
        *,
        archive_path: Path,
        scope: ScopeContext | None = None,
        approved_browser_authority_impact_hash: str = "",
    ) -> ExtensionArchiveInstallResultView:
        resolved_scope = service_scope(scope)
        require_global_extension_lifecycle_scope(resolved_scope)
        path = Path(archive_path)
        open_flags = os.O_RDONLY
        for flag_name in ("O_NOFOLLOW", "O_NONBLOCK", "O_CLOEXEC"):
            open_flags |= getattr(os, flag_name, 0)

        try:
            archive_fd = os.open(path, open_flags)
        except OSError as exc:
            raise ServiceValidationError(
                f"Extension archive is not a readable regular file: {path}"
            ) from exc

        try:
            descriptor_stat = os.fstat(archive_fd)
            if not stat.S_ISREG(descriptor_stat.st_mode):
                raise ServiceValidationError(f"Extension archive must be a regular file: {path}")
            if descriptor_stat.st_size > _MAX_EXTENSION_ARCHIVE_BYTES:
                raise ServiceValidationError(_archive_too_large_message())

            try:
                source = os.fdopen(archive_fd, "rb", closefd=True)
            except OSError as exc:
                raise ServiceValidationError(
                    f"Extension archive is not a readable regular file: {path}"
                ) from exc
            archive_fd = -1
            try:
                with source:
                    content = source.read(_MAX_EXTENSION_ARCHIVE_BYTES + 1)
            except OSError as exc:
                raise ServiceValidationError(
                    f"Extension archive is not a readable regular file: {path}"
                ) from exc
        finally:
            if archive_fd >= 0:
                with contextlib.suppress(OSError):
                    os.close(archive_fd)

        if len(content) > _MAX_EXTENSION_ARCHIVE_BYTES:
            raise ServiceValidationError(_archive_too_large_message())
        return self.install_extension_archive(
            filename=path.name,
            content=content,
            scope=resolved_scope,
            approved_browser_authority_impact_hash=approved_browser_authority_impact_hash,
        )

    def install_extension_archive(
        self,
        *,
        filename: str,
        content: bytes,
        scope: ScopeContext | None = None,
        approved_browser_authority_impact_hash: str = "",
    ) -> ExtensionArchiveInstallResultView:
        resolved_scope = service_scope(scope)
        require_global_extension_lifecycle_scope(resolved_scope)
        bl = self._get_logger()
        if not bl:
            raise ServiceUnavailableError(ERR_DATABASE_NOT_CONFIGURED)
        upload = self._validation.validate_upload(
            filename=filename,
            content=content,
            max_archive_bytes=_MAX_EXTENSION_ARCHIVE_BYTES,
        )
        self._context.archive_transaction_is_caller_owned(bl)
        from core.infrastructure.extension_runtime.extension_runtime import default_extensions_root
        from core.infrastructure.extension_runtime.extension_runtime_common import (
            EXTENSION_INSTALL_METADATA_FILENAME,
        )

        extensions_root = Path(default_extensions_root()).resolve()
        extensions_root.mkdir(parents=True, exist_ok=True)
        with self._extraction.extract(
            upload=upload,
            max_uncompressed_bytes=_MAX_EXTENSION_ARCHIVE_UNCOMPRESSED_BYTES,
        ) as extracted_root:
            bundle = self._validation.validate_extracted_bundle(
                extracted_root=extracted_root,
                bl=bl,
                scope=scope,
            )
            return self._install_validated_bundle(
                bl=bl,
                bundle=bundle,
                extensions_root=extensions_root,
                metadata_filename=EXTENSION_INSTALL_METADATA_FILENAME,
                resolved_scope=resolved_scope,
                scope=scope,
                approved_browser_authority_impact_hash=(approved_browser_authority_impact_hash),
            )

    def _install_validated_bundle(
        self,
        *,
        bl: Any,
        bundle: ValidatedExtensionBundle,
        extensions_root: Path,
        metadata_filename: str,
        resolved_scope: ScopeContext,
        scope: ScopeContext | None,
        approved_browser_authority_impact_hash: str = "",
    ) -> ExtensionArchiveInstallResultView:
        install_store = self._context.install_store(bl)
        runtime_store = self._context.runtime_store(bl)
        with self._context.update_lock(
            bl=bl,
            extension_id=bundle.extension_id,
            validate_stores=True,
            install_store=install_store,
            runtime_store=runtime_store,
        ):
            self._context.archive_transaction_is_caller_owned(bl)
            self._managed_release.reconcile_managed_extension_staging(
                extensions_root=extensions_root
            )
            current_install = self._state.current_extension_install_record(
                bl,
                extension_id=bundle.extension_id,
                scope=resolved_scope,
                extensions_root=extensions_root,
                install_store=install_store,
            )
            handoff = self._prepare_browser_authority_handoff(
                bundle=bundle,
                current_install=current_install,
                resolved_scope=resolved_scope,
                approved_impact_hash=approved_browser_authority_impact_hash,
            )
            descriptor_name = bundle.descriptor_path.name if bundle.descriptor_path else ""
            if current_install is not None:
                self._update.validate_update_version_transition(
                    extension_id=bundle.extension_id,
                    current_version=str(getattr(current_install, "version", "") or ""),
                    candidate_version=bundle.version,
                )
                return self._update.update_simple_extension_bundle(
                    bl=bl,
                    install_store=install_store,
                    runtime_store=runtime_store,
                    bundle_dir=bundle.bundle_dir,
                    compiled=bundle.compiled,
                    extensions_root=extensions_root,
                    scope=scope,
                    current_install=current_install,
                    descriptor_name=descriptor_name,
                    browser_authority_handoff=handoff,
                    approved_browser_authority_impact_hash=(approved_browser_authority_impact_hash),
                )
            target_dir = self._managed_release.managed_simple_extension_root(bundle.extension_id)
            if target_dir.exists():
                raise ServiceValidationError(
                    f"Extension '{bundle.extension_id}' is already present; "
                    "uninstall it before reinstalling"
                )
            return self._new_install.install_simple_extension_bundle(
                bl=bl,
                install_store=install_store,
                runtime_store=runtime_store,
                bundle_dir=bundle.bundle_dir,
                compiled=bundle.compiled,
                extension_id=bundle.extension_id,
                extensions_root=extensions_root,
                metadata_filename=metadata_filename,
                scope=scope,
                descriptor_name=descriptor_name,
                browser_authority_handoff=handoff,
                approved_browser_authority_impact_hash=(approved_browser_authority_impact_hash),
            )

    @staticmethod
    def _prepare_browser_authority_handoff(
        *,
        bundle: ValidatedExtensionBundle,
        current_install: Any | None,
        resolved_scope: ScopeContext,
        approved_impact_hash: str,
    ) -> PreparedBrowserAuthorityApproval | None:
        descriptor_sha256 = browser_authority_manifest_sha256(bundle.compiled)
        impact = browser_authority_impact(
            account_id=resolved_scope.account_id or "acc_default",
            project_id=str(resolved_scope.project_id or "").strip(),
            extension_id=bundle.extension_id,
            install_id=str(getattr(current_install, "install_id", "") or "").strip(),
            old_release_id=str(getattr(current_install, "release_id", "") or "").strip(),
            new_release_id=descriptor_sha256,
            descriptor_sha256=descriptor_sha256,
            candidate_tree_sha256=candidate_browser_tree_sha256(bundle.compiled),
            manifest=bundle.compiled,
        )
        require_browser_authority_approval(
            impact,
            approved_impact_hash=approved_impact_hash,
        )
        approval = browser_authority_approval_provenance(
            impact,
            actor_id=resolved_scope.actor_id or "api",
        )
        if not approval:
            return None
        approval["extension_id"] = bundle.extension_id
        return PreparedBrowserAuthorityApproval(
            impact=impact,
            approval=approval,
            candidate_version=bundle.version,
        )


__all__ = ["ExtensionArchiveInstallService"]
