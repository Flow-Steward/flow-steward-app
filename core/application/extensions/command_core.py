from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol

from core.application.management.command_models import (
    ExtensionDescriptorImportFileInput,
    ExtensionDescriptorImportUrlInput,
    ExtensionDescriptorValidateInput,
    ExtensionDescriptorValidateResultView,
    ExtensionInstallResultView,
    ExtensionReleaseImportResultView,
    ExtensionReleaseInstallInput,
)
from core.application.management.errors import ServiceValidationError
from core.application.management.scope import ScopeContext

from .browser_authority_approval import PreparedBrowserAuthorityApproval
from .command_context import ExtensionCommandContext
from .command_core_persistence import (
    DescriptorReleasePersistenceRequest,
    ReleasePersistenceStore,
    persist_descriptor_release,
)
from .descriptor_bundle_runtime import descriptor_with_bundle_runtime
from .extension_runtime import (
    ExtensionArtifactSnapshotRecord,
    ExtensionReleaseRecord,
)
from .lifecycle_ports import require_role as _require_role
from .service_support import (
    _descriptor_validation_status_code,
    _normalize_text,
    _runtime_platform_version,
    _service_scope,
)

_IMPORTED_ORIGIN = "imported"


class InstallReleaseCallback(Protocol):
    def __call__(
        self,
        inp: ExtensionReleaseInstallInput,
        *,
        scope: ScopeContext | None = None,
        allow_same_version_enrichment: bool = False,
        _browser_authority_handoff: PreparedBrowserAuthorityApproval | None = None,
    ) -> ExtensionInstallResultView: ...


class ExtensionCommandCore:
    """Command service construction, descriptor validation, and import helpers."""

    def __init__(
        self,
        *,
        context: ExtensionCommandContext,
        install_release: InstallReleaseCallback,
        cleanup_release_artifacts: Callable[[str], None],
    ) -> None:
        self._context = context
        self._install_release = install_release
        self._cleanup_release_artifacts = cleanup_release_artifacts

    def _logger(self) -> Any:
        return self._context.logger()

    @staticmethod
    def _logger_connection(bl: Any) -> Any:
        return ExtensionCommandContext.logger_connection(bl)

    def _extension_persistence_connection(self) -> Any:
        return self._context.persistence_connection()

    def _install_store(self) -> Any:
        return self._context.install_store()

    def _runtime_store(self) -> Any:
        return self._context.runtime_store()

    def validate_descriptor(
        self,
        inp: ExtensionDescriptorValidateInput,
        *,
        scope: ScopeContext | None = None,
    ) -> ExtensionDescriptorValidateResultView:
        _ = _service_scope(scope)
        command = ExtensionDescriptorValidateInput.model_validate(inp)
        source_type = _normalize_text(command.source_type).lower() or "file"

        from core.application.extensions.extension_descriptor import validate_extension_descriptor
        from core.application.extensions.lifecycle import (
            validate_extension_descriptor_builtin,
            validate_extension_descriptor_file,
            validate_extension_descriptor_url,
        )

        if source_type == "file":
            result = validate_extension_descriptor_file(
                command.descriptor_path,
                platform_version=command.platform_version,
                allow_insecure_remote=command.allow_insecure_remote,
            )
        elif source_type == "url":
            result = validate_extension_descriptor_url(
                command.descriptor_url,
                platform_version=command.platform_version,
                allow_insecure_remote=command.allow_insecure_remote,
            )
        elif source_type == "builtin":
            source: dict[str, Any] | str = command.descriptor_payload or command.descriptor_path
            result = validate_extension_descriptor_builtin(
                source,
                source_root=command.source_root or None,
                platform_version=command.platform_version,
                allow_insecure_remote=command.allow_insecure_remote,
            )
        elif source_type == "payload":
            result = validate_extension_descriptor(
                command.descriptor_payload,
                platform_version=command.platform_version,
                allow_insecure_remote=command.allow_insecure_remote,
            )
        else:
            raise ServiceValidationError(
                f"Unsupported descriptor source_type '{source_type}'",
                payload={"supported": ["file", "url", "builtin", "payload"]},
            )

        normalized: dict[str, Any] = (
            result.get("normalized") if isinstance(result.get("normalized"), dict) else {}
        )
        execution_modes = []
        distribution: dict[str, Any] = (
            normalized.get("distribution")
            if isinstance(normalized.get("distribution"), dict)
            else {}
        )
        raw_modes = distribution.get("execution_modes") if isinstance(distribution, dict) else []
        if isinstance(raw_modes, list):
            execution_modes = [
                _normalize_text(mode).lower() for mode in raw_modes if _normalize_text(mode)
            ]
        return ExtensionDescriptorValidateResultView(
            valid=bool(result.get("valid")),
            extension_id=_normalize_text(normalized.get("extension_id")),
            release_version=_normalize_text(normalized.get("release_version")),
            execution_modes=execution_modes,
            normalized=normalized,
            errors=result.get("errors", []),
            warnings=result.get("warnings", []),
        )

    def descriptor_validation_or_error(
        self, result: ExtensionDescriptorValidateResultView
    ) -> dict[str, Any]:
        return self._descriptor_validation_or_error(result)

    def _descriptor_validation_or_error(
        self, result: ExtensionDescriptorValidateResultView
    ) -> dict[str, Any]:
        if result.valid:
            return dict(result.normalized)
        raise ServiceValidationError(
            "Descriptor validation failed",
            status_code=_descriptor_validation_status_code(result),
            payload={"errors": result.errors, "warnings": result.warnings},
        )

    def persist_descriptor_release(
        self,
        *,
        normalized_descriptor: dict[str, Any],
        account_id: str,
        actor_id: str,
        source_kind: str,
        source_ref: str,
        origin_override: str = "",
    ) -> tuple[ExtensionReleaseRecord, list[ExtensionArtifactSnapshotRecord]]:
        runtime_store: ReleasePersistenceStore = self._runtime_store()
        return persist_descriptor_release(
            runtime_store=runtime_store,
            request=DescriptorReleasePersistenceRequest(
                normalized_descriptor=normalized_descriptor,
                account_id=account_id,
                actor_id=actor_id,
                source_kind=source_kind,
                source_ref=source_ref,
                origin_override=origin_override,
            ),
        )

    @staticmethod
    def _prepare_file_import_source(
        command: ExtensionDescriptorImportFileInput,
        *,
        normalized: dict[str, Any],
        source_kind_override: str,
    ) -> tuple[dict[str, Any], str, str]:
        source_kind = _normalize_text(source_kind_override) or "file"
        if source_kind not in {"file", "extension_scan"}:
            raise ServiceValidationError("Unsupported descriptor source kind override")
        # A provenance origin read from a descriptor is never trusted: whatever it
        # claims and whatever its id, a file import records what it is.
        if source_kind != "extension_scan":
            return normalized, source_kind, _IMPORTED_ORIGIN
        bundle_descriptor = {
            **normalized,
            "bundle_root": str(Path(command.descriptor_path).resolve().parent),
        }
        bundle_descriptor, runtime_errors = descriptor_with_bundle_runtime(
            descriptor_path=Path(command.descriptor_path),
            normalized_descriptor=bundle_descriptor,
        )
        if runtime_errors:
            raise ServiceValidationError(
                "Extension runtime bundle validation failed",
                payload={"errors": runtime_errors},
            )
        return bundle_descriptor, source_kind, _IMPORTED_ORIGIN

    def import_descriptor_file(
        self,
        inp: ExtensionDescriptorImportFileInput,
        *,
        scope: ScopeContext | None = None,
        source_kind_override: str = "",
        allow_same_version_enrichment: bool = False,
        _browser_authority_handoff: PreparedBrowserAuthorityApproval | None = None,
    ) -> ExtensionReleaseImportResultView:
        scope = _service_scope(scope)
        # FS-DEEP-CAND-042: descriptor import persists releases (and may install
        # them) and must require lifecycle (admin) authorization, not plain
        # operator write access. Releases remain account-scoped to the caller.
        _require_role(scope, "admin")
        command = ExtensionDescriptorImportFileInput.model_validate(inp)
        platform_version = _runtime_platform_version()
        validation = self.validate_descriptor(
            ExtensionDescriptorValidateInput(
                source_type="file",
                descriptor_path=command.descriptor_path,
                platform_version=platform_version,
                allow_insecure_remote=False,
            ),
            scope=scope,
        )
        normalized = self._descriptor_validation_or_error(validation)
        account_id = scope.account_id or "acc_default"
        actor_id = scope.actor_id or "api"
        normalized, source_kind, import_origin = self._prepare_file_import_source(
            command,
            normalized=normalized,
            source_kind_override=source_kind_override,
        )
        release, snapshots = self.persist_descriptor_release(
            normalized_descriptor=normalized,
            account_id=account_id,
            actor_id=actor_id,
            source_kind=source_kind,
            source_ref=_normalize_text(command.source_ref) or command.descriptor_path,
            origin_override=import_origin,
        )
        install_payload: dict[str, Any] | None = None
        if command.install_after_import:
            try:
                install_result = self._install_release(
                    ExtensionReleaseInstallInput(
                        release_id=release.release_id,
                        runtime_mode=command.runtime_mode,
                        remote_base_url=command.remote_base_url,
                        approved_browser_authority_impact_hash=(
                            command.approved_browser_authority_impact_hash
                        ),
                    ),
                    scope=scope,
                    allow_same_version_enrichment=allow_same_version_enrichment,
                    _browser_authority_handoff=_browser_authority_handoff,
                )
                install_payload = install_result.dump()
            except Exception:
                self._cleanup_release_artifacts(release.release_id)
                raise
        return ExtensionReleaseImportResultView(
            status="imported",
            release_id=release.release_id,
            extension_id=release.extension_id,
            release_version=release.release_version,
            imported_artifacts=[
                {
                    "artifact_kind": row.artifact_kind,
                    "artifact_id": row.artifact_id,
                    "publish": row.publish,
                }
                for row in snapshots
            ],
            install=install_payload,
            errors=[],
        )

    def import_descriptor_url(
        self,
        inp: ExtensionDescriptorImportUrlInput,
        *,
        scope: ScopeContext | None = None,
    ) -> ExtensionReleaseImportResultView:
        scope = _service_scope(scope)
        # FS-DEEP-CAND-042: descriptor import persists releases (and may install
        # them) and must require lifecycle (admin) authorization, not plain
        # operator write access. Releases remain account-scoped to the caller.
        _require_role(scope, "admin")
        command = ExtensionDescriptorImportUrlInput.model_validate(inp)
        platform_version = _runtime_platform_version()
        validation = self.validate_descriptor(
            ExtensionDescriptorValidateInput(
                source_type="url",
                descriptor_url=command.descriptor_url,
                platform_version=platform_version,
                allow_insecure_remote=False,
            ),
            scope=scope,
        )
        normalized = self._descriptor_validation_or_error(validation)
        account_id = scope.account_id or "acc_default"
        actor_id = scope.actor_id or "api"
        release, snapshots = self.persist_descriptor_release(
            normalized_descriptor=normalized,
            account_id=account_id,
            actor_id=actor_id,
            source_kind="url",
            source_ref=command.descriptor_url,
        )
        install_payload: dict[str, Any] | None = None
        if command.install_after_import:
            try:
                install_result = self._install_release(
                    ExtensionReleaseInstallInput(
                        release_id=release.release_id,
                        runtime_mode=command.runtime_mode,
                        remote_base_url=command.remote_base_url,
                    ),
                    scope=scope,
                )
                install_payload = install_result.dump()
            except Exception:
                self._cleanup_release_artifacts(release.release_id)
                raise
        return ExtensionReleaseImportResultView(
            status="imported",
            release_id=release.release_id,
            extension_id=release.extension_id,
            release_version=release.release_version,
            imported_artifacts=[
                {
                    "artifact_kind": row.artifact_kind,
                    "artifact_id": row.artifact_id,
                    "publish": row.publish,
                }
                for row in snapshots
            ],
            install=install_payload,
            errors=[],
        )


__all__ = [name for name in globals() if not (name.startswith("__") and name.endswith("__"))]
